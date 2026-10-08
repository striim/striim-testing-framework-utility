"""Exact assertions (C8.3) through ``livetest.exactdata`` with the real manifest loader,
the real input snapshot, the real bounded ``lifecycle.PgProbe`` and ``canon``; fakes only at the psycopg2
connection, the docker listing and the ``docker exec head`` process edges."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import pytest

from livetest import canon, exactdata, inputs, lifecycle, ownership, resultschema
from livetest.assertions import AssertionFailed
from livetest.assertions import diff as diff_assertion
from livetest.manifest import load_manifest

CASES = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "evidence" / "cases"
TOKENS = {"PG_SOURCE_SCHEMA": "qasource", "PG_TARGET_SCHEMA": "qatarget", "TID": "ab12cd34_",
          "OWNED_DIR": "/opt/striim/slt-runs/ns_ab12cd34"}
SRC, TGT = "qasource.ab12cd34_src", "qatarget.ab12cd34_tgt"
_STAR = re.compile(r'SELECT \* FROM "([^"]+)"\."([^"]+)"(?: ORDER BY (.+))? LIMIT %s$')       # the pre-r1 read
_ROW_TEXT = r"octet_length\(ROW\(_slt_r\.\*\)::text\)"
_MEASURE = re.compile(rf'SELECT count\(\*\), coalesce\(sum\({_ROW_TEXT}\), 0\), coalesce\(max\({_ROW_TEXT}\), 0\) '
                      r'FROM \(SELECT \* FROM "([^"]+)"\."([^"]+)" LIMIT %s\) _slt_r$')
_FETCH = re.compile(rf'SELECT {_ROW_TEXT}, _slt_r\.\* FROM "([^"]+)"\."([^"]+)" _slt_r(?: ORDER BY (.+))? LIMIT %s$')
JSON_OID, JSONB_OID = 114, 3802


class Raw:
    """A json/jsonb cell as the server sends it: the type oid and its text. The fake cursor decodes it the way
    the driver does: with the connection's registered typecaster, else psycopg2's default ``json.loads``."""

    def __init__(self, oid, text):
        self.oid, self.text = oid, text


def _text_len(values) -> int:
    """The server's ``octet_length(ROW(r.*)::text)`` for the fake rows (unquoted, close enough for accounting)."""
    return len(("(" + ",".join("" if v is None else v.text if isinstance(v, Raw) else str(v) for v in values)
                + ")").encode("utf-8"))


class PgWorld:
    """In-memory tables behind a psycopg2-shaped connection: ``{name: [row dicts]}`` or a callable per read.

    ``sql`` lists the row-returning reads, ``statements`` every statement. ``pulled`` is the text size of every
    row handed to the client and ``batches`` the bytes of each fetch (r1 F7). A callable table is read once per
    connection (the read's snapshot)."""

    def __init__(self, tables=None):
        self.tables, self.sql, self.statements = dict(tables or {}), [], []
        self.hang, self.cancelled = False, threading.Event()
        self.pulled, self.batches = [], []

    def connect(self, **kw):
        world, snapshot = self, {}

        def rows_of(name):
            if name not in snapshot:
                rows = world.tables[name]
                snapshot[name] = rows() if callable(rows) else rows
            return snapshot[name]

        class Cur:
            def __init__(self, conn, name):
                self.conn, self.name, self.description, self.rows = conn, name, None, []

            def execute(self, sql, params=None):
                world.statements.append((sql, params))
                if world.hang:
                    world.cancelled.wait(30)
                    raise RuntimeError("canceling statement due to user request")
                if sql.startswith("SET TRANSACTION"):
                    return
                m = _MEASURE.match(sql)
                if m:
                    assert self.name is None, "the measurement returns one row on an ordinary cursor"
                    rows = rows_of(f"{m.group(1)}.{m.group(2)}")[:params[0]]
                    sizes = [_text_len(r.values()) for r in rows]
                    self.description = [("count",), ("coalesce",), ("coalesce",)]
                    self.rows = [(len(sizes), sum(sizes), max(sizes, default=0))]
                    return
                m = _FETCH.match(sql) or _STAR.match(sql)
                assert m, sql
                world.sql.append((sql, params))
                rows = rows_of(f"{m.group(1)}.{m.group(2)}")
                if m.group(3):
                    keys = [c.strip().split(".")[-1].strip('"') for c in m.group(3).split(",")]
                    rows = sorted(rows, key=lambda r: tuple(r[k] for k in keys))
                cols = list(rows[0]) if rows else ["id"]
                values = [tuple(r.get(c) for c in cols) for r in rows][:params[0]]
                if m.re is _FETCH:
                    assert self.name, "the exact read fetches its rows from a server-side cursor"
                    self.description = [("octet_length",)] + [(c,) for c in cols]
                    self.rows = [(_text_len(v),) + v for v in values]
                else:
                    self.description, self.rows = [(c,) for c in cols], values

            def _decode(self, value):
                if not isinstance(value, Raw):
                    return value
                caster = self.conn.casters.get(value.oid)
                return caster.cast(value.text, self) if caster else json.loads(value.text)

            def _hand(self, rows):
                sizes = [_text_len(r[1:] if self.name else r) for r in rows]
                world.pulled += sizes
                world.batches.append(sum(sizes))
                return [tuple(self._decode(v) for v in r) for r in rows]

            def fetchone(self):
                return self.rows[0] if self.rows else None

            def fetchmany(self, size):
                batch, self.rows = self.rows[:size], self.rows[size:]
                return self._hand(batch)

            def fetchall(self):
                batch, self.rows = self.rows, []
                return self._hand(batch)

            def close(self):
                pass

        class Conn:
            autocommit = False

            def __init__(self):
                self.casters = {}

            def cursor(self, name=None):
                return Cur(self, name)

            def cancel(self):
                world.cancelled.set()

            def close(self):
                pass
        return Conn()


@pytest.fixture
def world(monkeypatch):
    w = PgWorld()
    monkeypatch.setattr(lifecycle, "_pg_connect", w.connect)
    return w


def admins():
    dsn = {"host": "localhost", "port": 5432, "dbname": "sltdb", "source_user": "qasource", "source_password": "x",
           "target_user": "qatarget", "target_password": "x"}
    return {"postgres-source": {"admin": SimpleNamespace(dsn=dsn, role="source")},
            "postgres-target": {"admin": SimpleNamespace(dsn=dsn, role="target")}}


def case(tmp_path, name, *, golden=None, edit=None, timeout=None):
    dest = tmp_path / name
    shutil.copytree(CASES / name, dest)
    if golden is not None:
        (dest / "expected" / golden[0]).write_text(golden[1])
    if edit:
        (dest / "test.yaml").write_text(edit((dest / "test.yaml").read_text()))
    m = load_manifest(dest / "test.yaml")
    if timeout is not None:
        m.timeout = timeout
    return m, inputs.snapshot(m, dest / "test.yaml")


def owns(*allowed):
    return lambda kind, db, name: (kind, db, name) in set(allowed)


def ids(*values):
    return [{"id": v} for v in values]


def run_data(m, snap, collector=None, owned=None, lifecycle_case=True, **kw):
    specs = exactdata.partition(m.assert_["data"], m, "data")[1]
    return exactdata.assert_exact_data(admins(), specs, m, tokens=TOKENS, inputs=snap, lifecycle=lifecycle_case,
                                       owned=owned or owns(("pg-table", "postgres-target", TGT)),
                                       collector=collector if collector is not None else [], **kw)


def failed(fn):
    with pytest.raises(AssertionFailed) as ei:
        fn()
    return ei.value


# ---------------------------------------------------------------- wrong golden and row differences

def test_wrong_golden_fails_hashes_differ_sample_names_row_golden_sha_and_mtime_unchanged(tmp_path, world):
    m, snap = case(tmp_path, "exact-wrong-golden")
    golden = tmp_path / "exact-wrong-golden" / "expected" / "tgt.csv"
    before = (hashlib.sha256(golden.read_bytes()).hexdigest(), golden.stat().st_mtime_ns)
    world.tables[TGT] = ids(1, 2, 3)
    collector = []
    err = failed(lambda: run_data(m, snap, collector))
    [entry] = collector
    assert entry["expected"]["sha256"] != entry["actual"]["sha256"] and entry["equal"] is False
    assert entry["samples"]["missing"] == [{"row": [["id", "integer", "4"]], "count": 1}]
    assert entry["samples"]["extra"] == [{"row": [["id", "integer", "3"]], "count": 1}]
    assert entry["expected"]["sha256"] in err.records[0]["detail"] and entry["actual"]["sha256"] in err.records[0]["detail"]
    assert (hashlib.sha256(golden.read_bytes()).hexdigest(), golden.stat().st_mtime_ns) == before
    assert entry["expected"]["templateSha256"] == "sha256:" + before[0]


def test_injected_duplicate_fails(tmp_path, world):
    m, snap = case(tmp_path, "exact-db-any")
    world.tables[TGT] = ids(1, 2, 3, 3)
    collector = []
    failed(lambda: run_data(m, snap, collector))
    assert collector[0]["samples"] == {"missing": [], "extra": [{"row": [["id", "integer", "3"]], "count": 1}]}


def test_missing_row_fails(tmp_path, world):
    m, snap = case(tmp_path, "exact-db-any")
    world.tables[TGT] = ids(1, 2)
    collector = []
    failed(lambda: run_data(m, snap, collector))
    assert collector[0]["samples"] == {"missing": [{"row": [["id", "integer", "3"]], "count": 1}], "extra": []}


def test_extra_row_fails(tmp_path, world):
    m, snap = case(tmp_path, "exact-db-any")
    world.tables[TGT] = ids(1, 2, 3, 4)
    collector = []
    failed(lambda: run_data(m, snap, collector))
    assert collector[0]["samples"] == {"missing": [], "extra": [{"row": [["id", "integer", "4"]], "count": 1}]}
    assert collector[0]["actual"]["rowCount"] == 4 and collector[0]["expected"]["rowCount"] == 3


def test_row_permutation_passes_any_fails_sequence(tmp_path, world):
    world.tables[TGT] = ids(3, 2, 1)
    m, snap = case(tmp_path, "exact-db-any")                              # golden 3,1,2
    collector = []
    records = run_data(m, snap, collector)
    assert records[0]["status"] == "passed" and collector[0]["order"] == "any"
    m, snap = case(tmp_path / "s", "exact-db-sequence", edit=lambda y: y.replace("expected/tgt.csv", "expected/permuted.csv"))
    collector = []
    failed(lambda: run_data(m, snap, collector))
    assert world.sql[-1][0].endswith('ORDER BY _slt_r."id" LIMIT %s')
    assert collector[0]["samples"]["firstMismatch"]["index"] == 0


# ---------------------------------------------------------------- markers and types

@pytest.mark.parametrize("marker", ["null", "empty", "absent"])
def test_null_vs_empty_vs_absent_file_events(tmp_path, monkeypatch, marker):
    cell = {"null": "<null>", "empty": "", "absent": "<absent>"}[marker]
    events = {"null": {"id": 1, "name": None}, "empty": {"id": 1, "name": ""}, "absent": {"id": 1}}
    m, snap = case(tmp_path, "exact-file-sequence", golden=("events.csv", f"id,name\n1,{cell}\n"),
                   edit=lambda y: y.replace('{order: sequence, columns: {price: "decimal:2"}}', "true"))
    path = TOKENS["OWNED_DIR"] + "/out.json"
    monkeypatch.setattr(ownership, "docker_ls", lambda pattern, nodes=None: [path])
    specs = exactdata.partition(m.assert_["file"], m, "file")[1]
    for variant, event in events.items():
        body = json.dumps([{"data": event}]).encode()
        call = lambda: exactdata.assert_exact_file(   # noqa: E731
            specs, m, tokens=TOKENS, owned=owns(("owned-file", None, path)), lifecycle=True, inputs=snap, mode="docker",
            nodes=["slt-striim"], run=lambda argv, timeout, body=body: SimpleNamespace(returncode=0, stdout=body, stderr=b""))
        if variant == marker:
            assert call()[0]["status"] == "passed"
        else:
            failed(call)


def test_typed_postgres_values_decimal_timestamptz_bytea_jsonb(tmp_path, world):
    m, snap = case(tmp_path, "exact-typed")
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    world.tables[TGT] = [
        {"id": 1, "amount": D("10.50"), "created": dt.datetime(2026, 3, 1, 13, 30, tzinfo=ist),
         "img": memoryview(bytes.fromhex("deadbeef")), "payload": {"a": [1, 2], "b": 1}, "note": ""},
        {"id": 2, "amount": None, "created": dt.datetime(2026, 3, 1, 8, tzinfo=dt.timezone.utc),
         "img": memoryview(b"\x00\xff"), "payload": {}, "note": None}]
    collector = []
    assert run_data(m, snap, collector)[0]["status"] == "passed"
    assert world.sql == [('SELECT octet_length(ROW(_slt_r.*)::text), _slt_r.* FROM "qatarget"."ab12cd34_tgt" _slt_r LIMIT %s',
                          (canon.DEFAULT_MAX_ROWS + 1,))]
    # the same rows stringified the way pgclient.select_rows / coerce_cell would hand them over are refused
    from livetest.sqlutil import coerce_cell
    world.tables[TGT] = [{k: coerce_cell(v) for k, v in r.items()} for r in world.tables[TGT]]
    err = failed(lambda: run_data(m, snap, []))
    assert "invalid-value:actual:" in err.records[0]["detail"]


# ---------------------------------------------------------------- ownership and bounds

def test_target_not_owned_fails_before_any_query(tmp_path, world):
    m, snap = case(tmp_path, "exact-db-any")
    world.tables[TGT] = ids(1, 2, 3)
    collector = []
    err = failed(lambda: run_data(m, snap, collector, owned=owns()))
    assert world.sql == []
    assert err.records[0]["detail"].startswith("exact-target-not-owned") and collector[0]["error"] == "exact-target-not-owned"


def test_diff_source_must_be_owned(tmp_path, world):
    m, snap = case(tmp_path, "exact-diff-declared")
    world.tables.update({SRC: ids(1, 2, 3), TGT: ids(1, 2, 3)})
    specs = exactdata.partition(m.assert_["diff"], m, "diff")[1]

    def go(owned):
        return exactdata.assert_exact_diff(admins(), specs, m, tokens=TOKENS, owned=owned, lifecycle=True, inputs=snap,
                                           collector=[])
    err = failed(lambda: go(owns(("pg-table", "postgres-target", TGT))))
    assert "diff source postgres-source:" + SRC in err.records[0]["detail"] and world.sql == []
    records = go(owns(("pg-table", "postgres-target", TGT), ("pg-table", "postgres-source", SRC)))
    assert records[0]["type"] == "diff" and records[0]["status"] == "passed" and len(world.sql) == 2


def test_limit_exceeded_is_failure_not_truncation(tmp_path, world):
    m, snap = case(tmp_path, "exact-db-any", golden=("tgt.csv", "id\n1\n2\n"),
                   edit=lambda y: y.replace("max_rows: 100", "max_rows: 2"))
    world.tables[TGT] = ids(1, 2, 3)
    collector = []
    err = failed(lambda: run_data(m, snap, collector))
    assert world.statements[1][1] == (3,) and world.sql == [] and world.pulled == []    # refused before any row is sent
    assert collector[0]["error"] == "canonical-limit-exceeded" and "more than 2 rows" in err.records[0]["detail"]


def test_hanging_read_cancelled_within_bound_no_late_read(tmp_path, world):
    m, snap = case(tmp_path, "exact-db-any", timeout=1)
    world.tables[TGT] = ids(1, 2, 3)
    world.hang = True
    started = time.monotonic()
    collector = []
    err = failed(lambda: run_data(m, snap, collector))
    assert time.monotonic() - started < 1 + 3 * lifecycle.CANCEL_BOUND_S + 2
    assert world.cancelled.is_set() and len(world.statements) == 1
    assert collector[0]["error"] == "exact-read-timeout" and "exact-read-timeout" in err.records[0]["detail"]
    time.sleep(0.2)
    assert len(world.statements) == 1


# ---------------------------------------------------------------- r1 F7: the byte budget before materialization

def _budget_case(tmp_path):
    return case(tmp_path, "exact-db-any", golden=("tgt.csv", "id,blob\n1,x\n"),
                edit=lambda y: y.replace("max_rows: 100", "max_rows: 100, max_bytes: 4096"))


@pytest.mark.parametrize("shape", ["many-rows", "one-oversized-cell"])
def test_byte_budget_refused_before_any_row_is_transferred(tmp_path, world, shape):
    """Rows within max_rows but over the byte budget, or a single cell over it: canonical-limit-exceeded from
    the server's measurement, with no row handed to the client."""
    m, snap = _budget_case(tmp_path)
    world.tables[TGT] = ([{"id": i, "blob": "x" * 400} for i in range(40)] if shape == "many-rows"
                         else [{"id": 1, "blob": "x" * 1_000_000}])
    collector = []
    err = failed(lambda: run_data(m, snap, collector))
    assert world.pulled == [] and world.sql == [], (len(world.pulled), sum(world.pulled))
    assert collector[0]["error"] == "canonical-limit-exceeded" and "no row was transferred" in err.records[0]["detail"]


def test_byte_budget_fetch_is_batched_within_the_read_budget(tmp_path, world):
    """Row text within the read budget (2 x max_bytes) but canonical bytes over max_bytes: the rows come in
    batches sized from the widest row, no batch and no total above the budget, and canon refuses the side."""
    m, snap = _budget_case(tmp_path)
    world.tables[TGT] = [{"id": i, "blob": "y" * 100} for i in range(30)] + [{"id": 99, "blob": "z" * 3000}]
    collector = []
    err = failed(lambda: run_data(m, snap, collector))
    budget = exactdata.TRANSFER_FACTOR * 4096
    assert len(world.batches) > 1 and max(world.batches) <= budget and sum(world.pulled) <= budget, world.batches
    assert len(world.pulled) == 31
    assert collector[0]["error"] == "canonical-limit-exceeded" and "canonical bytes" in err.records[0]["detail"]


# ---------------------------------------------------------------- r1 F1: lossless JSON at the read boundary

@pytest.fixture
def driver_json(monkeypatch):
    """psycopg2's own registration path (``extras.register_default_json[b]`` -> ``_create_json_typecasters`` ->
    its ``typecast_json`` callback) with only the C edges captured: ``new_type`` keeps the Python callback and
    ``register_type`` installs it on the scope it is given. The world's connections count as driver connections."""
    import psycopg2._json as pgjson
    scopes = []
    monkeypatch.setattr(pgjson, "new_type", lambda oids, name, cast: SimpleNamespace(oids=oids, name=name, cast=cast))
    monkeypatch.setattr(pgjson, "new_array_type", lambda oids, name, base: SimpleNamespace(oids=oids, name=name))

    def register_type(caster, scope):
        scopes.append((caster.name, scope))
        if scope is not None and hasattr(caster, "cast"):
            scope.casters.update({oid: caster for oid in caster.oids})
    monkeypatch.setattr(pgjson, "register_type", register_type)
    monkeypatch.setattr(lifecycle, "_driver_connection", lambda conn: True, raising=False)
    return scopes


def test_exact_read_jsonb_numbers_keep_precision_diff_unequal(tmp_path, world, driver_json):
    """The review's F1 control: source and target jsonb differ only past float precision; the diff is unequal."""
    m, snap = case(tmp_path, "exact-diff-declared",
                   edit=lambda y: y.replace("exact: {columns: {id: integer}}", "exact: {columns: {id: integer, payload: json}}"))
    specs = exactdata.partition(m.assert_["diff"], m, "diff")[1]
    owned = owns(("pg-table", "postgres-target", TGT), ("pg-table", "postgres-source", SRC))

    def diff(src, tgt):
        world.tables.update({SRC: [{"id": 1, "payload": Raw(JSONB_OID, src)}],
                             TGT: [{"id": 1, "payload": Raw(JSONB_OID, tgt)}]})
        collector = []
        try:
            exactdata.assert_exact_diff(admins(), specs, m, tokens=TOKENS, owned=owned, lifecycle=True, inputs=snap,
                                        collector=collector)
        except AssertionFailed:
            pass
        return collector[0]
    unequal = diff('{"n": 1.00000000000000001}', '{"n": 1.00000000000000002}')
    assert unequal["equal"] is False and unequal["expected"]["sha256"] != unequal["actual"]["sha256"], unequal
    assert diff('{"n": 1.00000000000000001}', '{"n": 1.00000000000000001}')["equal"] is True
    assert driver_json and all(scope is not None for _name, scope in driver_json)          # never registered globally
    assert {name for name, _scope in driver_json} == {"JSON", "JSONARRAY", "JSONB", "JSONBARRAY"}


def test_exact_read_json_scale_duplicate_keys_non_finite_per_connection_decoder(tmp_path, world, driver_json):
    import psycopg2.extensions
    import psycopg2._json as pgjson
    global_jsonb = psycopg2.extensions.string_types[JSONB_OID]
    m, snap = case(tmp_path, "exact-db-any", golden=("tgt.csv", 'id,payload\n1,"{""n"":1.00}"\n'),
                   edit=lambda y: y.replace("exact: {columns: {id: integer}}", "exact: {columns: {id: integer, payload: json}}"))

    def read(raw):
        world.tables[TGT] = [{"id": 1, "payload": raw}]
        collector = []
        try:
            status = run_data(m, snap, collector)[0]["status"]
        except AssertionFailed:
            status = "failed"
        return status, collector[0].get("error")
    assert read(Raw(JSONB_OID, '{"n": 1.00}')) == ("passed", None)                     # the scale is kept ...
    assert read(Raw(JSONB_OID, '{"n": 1.0}')) == ("failed", None)                       # ... so 1.0 != 1.00
    assert read(Raw(JSON_OID, '{"n": 1.00, "n": 2}')) == ("failed", "invalid-value:actual:payload")
    assert read(Raw(JSON_OID, '{"n": NaN}')) == ("failed", "invalid-value:actual:payload")
    # nothing leaks: the global typecaster is untouched and a default decoder still rounds (the driver default)
    assert psycopg2.extensions.string_types[JSONB_OID] is global_jsonb
    default, _ = pgjson._create_json_typecasters(JSONB_OID, None, name="JSONB")
    assert default.cast('{"n": 1.00}', None) == {"n": 1.0} and type(default.cast('{"n": 1.00}', None)["n"]) is float


def _file_case(tmp_path, monkeypatch, files):
    m, snap = case(tmp_path, "exact-file-sequence", timeout=1)
    monkeypatch.setattr(ownership, "docker_ls", lambda pattern, nodes=None: list(files))
    return m, snap, exactdata.partition(m.assert_["file"], m, "file")[1], TOKENS["OWNED_DIR"] + "/out.json"


def test_file_read_killed_at_deadline(tmp_path, monkeypatch):
    m, snap, specs, path = _file_case(tmp_path, monkeypatch, [TOKENS["OWNED_DIR"] + "/out.json"])
    procs = []

    def slow(argv, timeout):
        assert argv[:4] == ["docker", "exec", "slt-striim", "head"] and argv[5] == str(canon.DEFAULT_MAX_BYTES + 1)
        procs.append(timeout)
        return subprocess.run([sys.executable, "-c", "import time; time.sleep(30)"], capture_output=True, timeout=timeout)
    started = time.monotonic()
    err = failed(lambda: exactdata.assert_exact_file(specs, m, tokens=TOKENS, owned=owns(("owned-file", None, path)),
                                                     lifecycle=True, inputs=snap, mode="docker", nodes=["slt-striim"],
                                                     run=slow, collector=[]))
    assert time.monotonic() - started < 5 and procs and procs[0] <= 1.0
    assert "exact-read-timeout" in err.records[0]["detail"] and "killed" in err.records[0]["detail"]


def test_file_sequence_with_two_files_is_order_source_ambiguous(tmp_path, monkeypatch):
    base = TOKENS["OWNED_DIR"] + "/out.json"
    m, snap, specs, path = _file_case(tmp_path, monkeypatch, [base, base + ".1"])
    reads = []
    err = failed(lambda: exactdata.assert_exact_file(
        specs, m, tokens=TOKENS, owned=owns(("owned-file", None, path)), lifecycle=True, inputs=snap, mode="docker",
        nodes=["slt-striim"], run=lambda argv, timeout: reads.append(argv), collector=[]))
    assert err.records[0]["detail"].startswith("order-source-ambiguous") and reads == []


def test_file_rollover_name_is_the_output(tmp_path, monkeypatch):
    """FileWriter names its first part out.00.json for a declared out.json; other names in the directory are not output."""
    d = TOKENS["OWNED_DIR"]
    m, snap, specs, path = _file_case(tmp_path, monkeypatch, [d + "/out.00.json", d + "/out.json.bak", d + "/other.json"])
    body = json.dumps([{"data": {"id": 1, "name": "a", "price": 1.50}},
                       {"data": {"id": 2, "name": None, "price": 2.25, "note": ""}}]).encode()
    reads = []

    def run(argv, timeout):
        reads.append(argv[-1])
        return SimpleNamespace(returncode=0, stdout=body, stderr=b"")
    [record] = exactdata.assert_exact_file(specs, m, tokens=TOKENS, owned=owns(("owned-file", None, path)),
                                           lifecycle=True, inputs=snap, mode="docker", nodes=["slt-striim"],
                                           run=run, collector=[])
    assert record["status"] == "passed" and reads == [d + "/out.00.json"]


# ---------------------------------------------------------------- timing

def test_lifecycle_case_reads_once_after_completion(tmp_path, world):
    m, snap = case(tmp_path, "exact-db-any")
    served = iter([ids(1, 2), ids(1, 2, 3)])
    world.tables[TGT] = lambda: next(served)
    failed(lambda: run_data(m, snap, poll=0.01))
    assert len(world.sql) == 1                                              # a late row is never waited for


def test_legacy_case_polls_until_equal(tmp_path, world):
    m, snap = case(tmp_path, "exact-db-any")
    served = iter([ids(1), ids(1, 2), ids(1, 2, 3)])
    world.tables[TGT] = lambda: next(served)
    collector = []
    records = run_data(m, snap, collector, owned=owns(), lifecycle_case=False, poll=0.01)
    assert records[0]["status"] == "passed" and len(world.sql) == 3
    assert collector[0]["actual"]["owned"] is False


# ---------------------------------------------------------------- golden snapshot, legacy diff, v1

def test_golden_parsed_from_snapshot_not_reread(tmp_path, world):
    m, snap = case(tmp_path, "exact-db-any")
    golden = tmp_path / "exact-db-any" / "expected" / "tgt.csv"
    template = "sha256:" + hashlib.sha256(golden.read_bytes()).hexdigest()
    golden.write_text("id\n9\n")                                            # changed after the snapshot
    world.tables[TGT] = ids(1, 2, 3)
    collector = []
    assert run_data(m, snap, collector)[0]["status"] == "passed"
    golden.unlink()
    assert run_data(m, snap, [])[0]["status"] == "passed"
    assert collector[0]["expected"]["templateSha256"] == template and collector[0]["expected"]["source"] == "expected/tgt.csv"
    assert snap.verify_goldens()[str(golden)]["unchanged"] is False


def test_legacy_exact_diff_routes_through_canon(monkeypatch):
    calls = []
    real = canon.legacy_text_multiset
    monkeypatch.setattr(canon, "legacy_text_multiset", lambda rows: calls.append(len(rows)) or real(rows))

    class FakePg:
        def __init__(self, tables):
            self.tables = tables

        def select_rows(self, name):
            return self.tables[name]
    pg = FakePg({"s.src": [{"id": "1"}, {"id": "2"}], "s.tgt": [{"id": "1"}, {"id": "1"}, {"id": "2"}]})
    with pytest.raises(AssertionError, match=r"s\.tgt does not hold exactly s\.src: \|src\|=2 \|tgt\|=3, 0 missing, 1 extra-or-duplicated"):
        diff_assertion.assert_diff({"postgres-source": pg}, [{"source": "s.src", "target": "s.tgt", "exact": True}],
                                   timeout=0, poll=0)
    assert calls == [2, 3]


def test_v1_record_types_stay_in_the_v1_enum_and_validate(tmp_path, world, monkeypatch):
    world.tables.update({SRC: ids(1, 2, 3), TGT: ids(1, 2, 3)})
    m, snap = case(tmp_path, "exact-db-any")
    records = run_data(m, snap)
    m, snap = case(tmp_path / "d", "exact-diff-declared")
    records += exactdata.assert_exact_diff(admins(), exactdata.partition(m.assert_["diff"], m, "diff")[1], m,
                                           tokens=TOKENS, inputs=snap, lifecycle=True, collector=[],
                                           owned=owns(("pg-table", "postgres-target", TGT), ("pg-table", "postgres-source", SRC)))
    m, snap = case(tmp_path / "f", "exact-file-sequence")
    path = TOKENS["OWNED_DIR"] + "/out.json"
    monkeypatch.setattr(ownership, "docker_ls", lambda pattern, nodes=None: [path])
    body = json.dumps([{"data": {"id": 1, "name": "a", "price": 1.50}},
                       {"data": {"id": 2, "name": None, "price": 2.25, "note": ""}}]).encode()
    records += exactdata.assert_exact_file(exactdata.partition(m.assert_["file"], m, "file")[1], m, tokens=TOKENS,
                                           owned=owns(("owned-file", None, path)), lifecycle=True, inputs=snap,
                                           mode="docker", nodes=["slt-striim"], collector=[],
                                           run=lambda argv, timeout: SimpleNamespace(returncode=0, stdout=body, stderr=b""))
    assert [r["type"] for r in records] == ["data", "diff", "file"]
    assert all(r["type"] in resultschema.VALID_ASSERTION_TYPES and r["status"] == "passed" for r in records)
    resultschema.validate({"schema_version": 1, "tests": [
        {"name": "x", "nodeid": "x", "status": "passed", "topology": "single", "services": ["postgres"],
         "duration": 0.1, "skip_reason": None, "assertions": records}]})
