"""Exact data assertions through ``slt-canon/1`` (contract C8.1, C8.3).

``load_manifest`` validates the top-level ``exact`` block and every exact spec (``parse_manifest_block``,
the C8.1 rules in ``livetest.canon``). At assertion time the plugin splits each assertion list
(``partition``): exact specs come here, everything else keeps its legacy path unchanged.

Reads are bounded and owned:
- Postgres: a fresh ``lifecycle.PgProbe`` per read (``lifecycle.exact_read``: the rows are measured server-side,
  then fetched through a server-side cursor within the row and byte budget) with the spec deadline and the
  probe's cancel protocol; psycopg2's typed values with json/jsonb as raw text (r1 F1), never ``coerce_cell``.
- Files (docker mode): listed per node through ``ownership.docker_ls``; each file read with
  ``docker exec <node> head -c <max_bytes + 1>`` under a subprocess timeout; JSON numbers as Decimal.
- In a lifecycle case every target (a diff's source included, a file's path) must be a confirmed
  resource of this attempt, checked before any read (``exact-target-not-owned``); one read per side
  after completion, no polling. A legacy case records ``owned: false``, polls bounded reads until
  equal or timeout, and never qualifies.

Goldens are parsed from the input snapshot bytes (``livetest.inputs``), never re-read. Every spec yields
a v1 record (types data/diff/file, both hashes in the detail) and a comparison record appended to
``collector``. Nothing here writes a file.
"""
from __future__ import annotations

import json
import posixpath
import subprocess
from decimal import Decimal
from pathlib import Path

from livetest import canon
from livetest import lifecycle as _lifecycle
from livetest import ownership as _ownership
from livetest.assertions import AssertionFailed
from livetest.assertions import file as _file
from livetest.resultschema import build_assertion_result
from livetest.striimfile import _output_names
from livetest.substitute import render

DETAIL_CAP = 2000
NO_DATA_REASON = "no exact data assertion"
SAMPLE_DETAIL_CAP = 600
TRANSFER_FACTOR = 2     # r1 F7: Postgres row-text bytes an exact read may transfer, per canonical byte of max_bytes
_PROBE_TIMEOUTS = (_lifecycle.ProbeCancelled, _lifecycle.ProbeCancelFailed, _lifecycle.ProbeSpent, _lifecycle.ProbeHung)


class ExactSpecError(ValueError):
    """An invalid exact block or spec (surfaced by load_manifest as ManifestError)."""


class ExactReadError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def parse_manifest_block(raw: dict, path, lifecycle=None) -> dict | None:
    """C8.1 at load. ``lifecycle`` is accepted for the load order only; ownership is a run-time check."""
    try:
        return canon.check_manifest(raw, case_dir=Path(path).parent)
    except canon.CanonError as e:
        raise ExactSpecError(f"{path}: exact: {e}") from None


def partition(raw, m, kind: str) -> tuple[list, list]:
    """``(legacy specs, [(index, spec, Declaration)])`` for one assertion list, by the load-time resolution."""
    exact = getattr(m, "exact", None)
    if not exact or not isinstance(raw, list):
        return raw, []
    picked = {i: d for k, i, d in exact["specs"] if k == kind}
    return [s for i, s in enumerate(raw) if i not in picked], [(i, raw[i], picked[i]) for i in sorted(picked)]


# ---------------------------------------------------------------- readers

def read_pg(admin, table_sql: str, order_by, max_rows: int, max_bytes: int, remaining: float) -> list[dict]:
    """One bounded read (``lifecycle.exact_read``): typed rows as dicts keyed by ``cursor.description`` names.

    r1 F7: the row and byte limits hold before the rows are transferred. The server measures the rows' text
    first; a target over ``max_rows`` or over ``TRANSFER_FACTOR * max_bytes`` bytes of row text is
    ``canonical-limit-exceeded`` with nothing fetched, and the fetch itself stops at that budget. The factor
    allows for row text longer than its canonical bytes (jsonb prints a space after each separator);
    ``canon.compare`` still enforces ``max_bytes`` on the canonical bytes exactly."""
    if order_by:
        for col in order_by:
            if not _lifecycle._TABLE.match(f"x.{col}"):
                raise canon.CanonError("invalid-declaration", f"order_by column {col!r} is not a plain identifier")
    try:
        names, rows = _lifecycle.PgProbe(admin).read_exact(table_sql, order_by, max_rows,
                                                           TRANSFER_FACTOR * max_bytes, remaining)
    except _lifecycle.ReadLimitExceeded as e:
        raise canon.CanonError("canonical-limit-exceeded", str(e)) from None
    if len(set(names)) != len(names):
        raise canon.CanonError("duplicate-column", f"{table_sql} returns duplicate column names {names}")
    return [dict(zip(names, r)) for r in rows]


def _run(argv, timeout: float):
    return subprocess.run(argv, capture_output=True, timeout=timeout)


def head_file(node: str, path: str, max_bytes: int, remaining: float, run=None) -> bytes:
    argv = ["docker", "exec", node, "head", "-c", str(max_bytes + 1), "--", path]
    try:
        r = (run or _run)(argv, max(0.05, remaining))
    except subprocess.TimeoutExpired:
        raise ExactReadError("exact-read-timeout", f"reading {node}:{path} did not finish within {remaining:.1f}s; "
                                                   f"the reader was killed") from None
    if getattr(r, "returncode", 1) != 0:
        err = getattr(r, "stderr", b"") or b""
        raise ExactReadError("exact-read-error", f"{node}:{path}: rc {r.returncode}: "
                                                 f"{err.decode('utf-8', 'replace') if isinstance(err, bytes) else err}"[:500])
    out = r.stdout
    return out if isinstance(out, bytes) else str(out).encode("utf-8")


def parse_events(text: str, where: str) -> list[dict]:
    """``assertions.file.parse_json_events`` semantics with JSON numbers as Decimal (no float enters)."""
    dec = json.JSONDecoder(parse_float=Decimal)
    out, i, n = [], 0, len(text)
    while i < n:
        while i < n and text[i] in " \t\r\n,[]":
            i += 1
        if i >= n:
            break
        try:
            obj, end = dec.raw_decode(text, i)
        except json.JSONDecodeError as e:
            if text[i:].strip(" \t\r\n,[]") and _file._has_later_value(text, i + 1, n):
                raise canon.CanonError(f"invalid-value:actual:{where}", f"corrupt JSON event stream at {i}: {e}") from None
            break
        if isinstance(obj, dict):
            out.append(obj)
        i = end
    return out


# ---------------------------------------------------------------- records

def _redact(text: str) -> str:
    """Known secrets never reach a v1 record, a junit failure or a console line (C4 1.9.0 redaction)."""
    from livetest import evidence
    return evidence.redact_text(text, evidence.known_secrets())


def legacy_diff(assert_diff, admin_map: dict, specs: list, *, collector=None, **kwargs) -> list[dict]:
    """Run the legacy ``assert_diff`` unchanged and record each ``exact: true`` spec as a ``legacy-text/1``
    comparison (never qualifying: no canonical hashes, ``owned: false``). Pass/fail and messages are the
    diff's own."""
    def note(records):
        if collector is None:
            return
        for spec, rec in zip(specs, records or []):
            if spec.get("exact") is True:
                collector.append({"index": None, "type": "diff", "target": rec.get("target"),
                                  "route": spec.get("target_db", spec.get("db", "postgres-source")),
                                  "profile": canon.LEGACY_PROFILE, "equal": rec.get("status") == "passed",
                                  "actual": {"owned": False}})
    try:
        records = assert_diff(admin_map, specs, **kwargs)
    except AssertionFailed as e:
        note(e.records)
        raise
    note(records)
    return records


def data_section(comparisons: list) -> dict:
    """``data`` of the case envelope (C4 1.9.0): comparisons renumbered in collection order; the aggregates
    are ``canon.aggregate`` over the comparisons that carry canonical hashes."""
    if not comparisons:
        return {"reason": NO_DATA_REASON}
    comps = [dict(c, index=i) for i, c in enumerate(comparisons)]
    return {"profile": canon.PROFILE if any(c.get("profile") == canon.PROFILE for c in comps) else canon.LEGACY_PROFILE,
            **aggregates(comps),
            "sampleTruncated": any(bool(c.get("sampleTruncated")) for c in comps),
            "normalization": [{"index": c["index"], "declarationSha256": c["declarationSha256"]}
                              for c in comps if "declarationSha256" in c],
            "comparisons": comps}


def aggregates(comps: list) -> dict:
    hashed = [c for c in comps if isinstance((c.get("actual") or {}).get("sha256"), str)]
    return {"canonicalSha256": canon.aggregate([c["actual"]["sha256"] for c in hashed]),
            "expectedSha256": canon.aggregate([c["expected"]["sha256"] for c in hashed]),
            "rowCount": sum(int(c["actual"].get("rowCount", 0)) for c in hashed)}


def _bindings(tokens: dict) -> dict:
    return {k: v for k, v in (tokens or {}).items() if k in canon.BINDING_TOKENS or k.startswith("SENTINEL_")}


def _sample_text(samples: dict) -> str:
    text = json.dumps({k: v for k, v in samples.items() if v}, ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= SAMPLE_DETAIL_CAP else text[:SAMPLE_DETAIL_CAP] + "..."


class _Prepared:
    def __init__(self, kind, index, spec, decl, target, route, owned, evaluate, source=None, template_sha=None):
        self.kind, self.index, self.spec, self.decl = kind, index, spec, decl
        self.target, self.route, self.owned, self.evaluate = target, route, owned, evaluate
        self.source, self.template_sha = source, template_sha
        self.result = None

    def done(self, comp: dict):
        entry = dict(comp)
        entry.update(index=self.index, type=self.kind, target=self.target, route=self.route)
        entry["expected"] = {**comp["expected"], "source": self.source, "templateSha256": self.template_sha}
        entry["actual"] = {**comp["actual"], "owned": self.owned}
        e, a = comp["expected"], comp["actual"]
        if comp["equal"]:
            detail = f"{canon.PROFILE} {comp['order']}: equal, {a['rowCount']} rows, {a['sha256']}"
        else:
            detail = (f"{canon.PROFILE} {comp['order']}: {self.target} expected {e['sha256']} ({e['rowCount']} rows) "
                      f"!= actual {a['sha256']} ({a['rowCount']} rows); samples {_sample_text(comp['samples'])}")
        self.result = (comp["equal"], detail, entry,
                       {"kind": "count", "count": e["rowCount"]}, {"kind": "count", "count": a["rowCount"]})

    def error(self, code: str, message: str):
        entry = {"index": self.index, "type": self.kind, "target": self.target, "route": self.route,
                 "profile": canon.PROFILE, "order": self.decl.order, "declaration": self.decl.to_dict(),
                 "declarationSha256": self.decl.sha256, "equal": False, "error": code,
                 "expected": {"source": self.source, "templateSha256": self.template_sha},
                 "actual": {"owned": self.owned}}
        self.result = (False, message if message.startswith(code) else f"{code}: {message}", entry, None, None)

    def record(self):
        ok, detail, _entry, exp, act = self.result
        return build_assertion_result(type=self.kind, status="passed" if ok else "failed", spec=self.spec,
                                      target=self.target, db=self.route, detail=_redact(detail)[:DETAIL_CAP],
                                      expected=exp, actual=act)


def _drive(kind: str, prepared: list, m, *, lifecycle: bool, collector, status_probe, poll: float, clock) -> list[dict]:
    """Lifecycle: one attempt per spec within the spec deadline. Legacy: bounded attempts until every
    spec is equal, refused or the deadline passes. Raises AssertionFailed with every record on failure."""
    clock = clock or _lifecycle.Clock
    deadline = clock.monotonic() + float(m.timeout)
    pending = [p for p in prepared if p.result is None]
    while pending:
        if status_probe:
            status_probe()
        for p in list(pending):
            remaining = deadline - clock.monotonic()
            try:
                p.done(p.evaluate(max(0.05, remaining)))
                if p.result[0] or lifecycle:
                    pending.remove(p)
            except (canon.CanonError, ExactReadError) as e:
                p.error(e.code, str(e))
                pending.remove(p)
            except _PROBE_TIMEOUTS as e:
                p.error("exact-read-timeout", f"{p.target}: {e}")
                pending.remove(p)
            except Exception as e:      # noqa: BLE001 - a read failure is this spec's failed record
                p.error("exact-read-error", f"{p.target}: {type(e).__name__}: {e}")
                pending.remove(p)
        if not pending or clock.monotonic() >= deadline:
            break
        clock.sleep(max(0.01, min(poll, deadline - clock.monotonic())))
    for p in pending:                   # legacy: still unequal at the deadline, the last attempt stands
        p.result = (False, f"not equal within {m.timeout}s: " + p.result[1], *p.result[2:])
    prepared.sort(key=lambda p: p.index)
    if collector is not None:
        collector.extend(p.result[2] for p in prepared)
    records = [p.record() for p in prepared]
    failed = [p for p in prepared if not p.result[0]]
    if failed:
        raise AssertionFailed(_redact(f"exact {kind} assertion failed: " + "; ".join(p.result[1] for p in failed))[:DETAIL_CAP],
                              records)
    return records


def _golden(p_kwargs: dict, spec: dict, m, inputs, tokens, db_route: bool):
    path = Path(m.dir) / str(spec["match"])
    data = inputs.golden(path)
    p_kwargs.update(source=str(spec["match"]), template_sha=inputs.sha256(path))
    return canon.parse_golden(data, db_route=db_route, tokens=tokens)


def _pg_ref(tokens, table: str):
    rendered = render(table, tokens)
    match = _lifecycle._TABLE.match(rendered)
    if not match:
        raise ExactReadError("exact-target-unresolvable", f"{rendered!r} does not render to schema.table")
    return rendered, _lifecycle._table(rendered), f"{match.group(1)}.{match.group(2)}".lower()


def _admin(admins: dict, route: str):
    entry = admins.get(route)
    if entry is None:
        raise ExactReadError("exact-route-unrequired", f"route {route!r} has no admin (add its service to requires)")
    return entry["admin"]


def _prepare(kind, index, spec, decl, target, route, owned_flag, build):
    """Resolve what can be resolved before any read; a refusal here is the spec's failed record."""
    kw = {"source": None, "template_sha": None, "spec_source": None}
    p = _Prepared(kind, index, spec, decl, target, route, owned_flag, evaluate=None)
    try:
        p.evaluate = build(kw)
    except (canon.CanonError, ExactReadError) as e:
        p.error(e.code, str(e))
    except Exception as e:              # noqa: BLE001 - e.g. a golden missing from the snapshot
        p.error("exact-input-error", f"{type(e).__name__}: {e}")
    p.source, p.template_sha = kw["source"], kw["template_sha"]
    if kw["spec_source"] is not None:
        # The record carries the source table this assertion rendered and read, so
        # evidence binds the comparison's expected.source to it exactly (tokens outside inputs.bindings included).
        p.spec = {**spec, "source": kw["spec_source"]}
    return p


def assert_exact_data(admins: dict, exact_specs: list, m, *, tokens: dict, owned, lifecycle: bool, inputs,
                      collector=None, status_probe=None, poll: float = 2.0, clock=None) -> list[dict]:
    max_rows, max_bytes = canon.limits(m.exact["block"])
    prepared = []
    for index, spec, decl in exact_specs:
        route = spec.get("target_db", spec.get("db", "postgres-source"))
        target = render(str(spec["target"]), tokens)

        def build(kw, spec=spec, decl=decl, route=route):
            golden = _golden(kw, spec, m, inputs, tokens, db_route=True)
            _rendered, table_sql, name = _pg_ref(tokens, str(spec["target"]))
            admin = _admin(admins, route)

            def evaluate(remaining):
                if lifecycle and not owned("pg-table", route, name):
                    raise ExactReadError("exact-target-not-owned", f"{route}:{name} is not a confirmed table of this "
                                                                   f"attempt; nothing was read")
                actual = read_pg(admin, table_sql, decl.order_by, max_rows, max_bytes, remaining)
                return canon.compare(golden, actual, decl, max_rows=max_rows, max_bytes=max_bytes,
                                     bindings=_bindings(tokens))
            return evaluate
        prepared.append(_prepare("data", index, spec, decl, target, route, bool(lifecycle), build))
    return _drive("data", prepared, m, lifecycle=lifecycle, collector=collector, status_probe=status_probe,
                  poll=poll, clock=clock)


def assert_exact_diff(admins: dict, exact_specs: list, m, *, tokens: dict, owned, lifecycle: bool, inputs=None,
                      collector=None, status_probe=None, poll: float = 2.0, clock=None) -> list[dict]:
    max_rows, max_bytes = canon.limits(m.exact["block"])
    prepared = []
    for index, spec, decl in exact_specs:
        src_route = spec.get("source_db", spec.get("db", "postgres-source"))
        tgt_route = spec.get("target_db", spec.get("db", "postgres-source"))
        target = render(str(spec["target"]), tokens)

        def build(kw, spec=spec, decl=decl, src_route=src_route, tgt_route=tgt_route):
            src_render, src_sql, src_name = _pg_ref(tokens, str(spec["source"]))
            _t, tgt_sql, tgt_name = _pg_ref(tokens, str(spec["target"]))
            kw["source"], kw["spec_source"] = f"{src_route}:{src_render}", src_render
            src_admin, tgt_admin = _admin(admins, src_route), _admin(admins, tgt_route)
            clock_ = clock or _lifecycle.Clock

            def evaluate(remaining):
                if lifecycle:
                    for route, name, what in ((src_route, src_name, "source"), (tgt_route, tgt_name, "target")):
                        if not owned("pg-table", route, name):
                            raise ExactReadError("exact-target-not-owned",
                                                 f"diff {what} {route}:{name} is not a confirmed table of this "
                                                 f"attempt; nothing was read")
                started = clock_.monotonic()
                source = read_pg(src_admin, src_sql, decl.order_by, max_rows, max_bytes, remaining)
                if not source:
                    raise ExactReadError("exact-source-empty", f"source {src_render} is empty; nothing to propagate")
                actual = read_pg(tgt_admin, tgt_sql, decl.order_by, max_rows, max_bytes,
                                 max(0.05, remaining - (clock_.monotonic() - started)))
                return canon.compare(source, actual, decl, max_rows=max_rows, max_bytes=max_bytes,
                                     bindings=_bindings(tokens), expected_observed=True)
            return evaluate
        prepared.append(_prepare("diff", index, spec, decl, target, tgt_route, bool(lifecycle), build))
    return _drive("diff", prepared, m, lifecycle=lifecycle, collector=collector, status_probe=status_probe,
                  poll=poll, clock=clock)


def assert_exact_file(exact_specs: list, m, *, tokens: dict, owned, lifecycle: bool, inputs, mode: str,
                      collector=None, nodes=None, run=None, status_probe=None, poll: float = 2.0,
                      clock=None) -> list[dict]:
    max_rows, max_bytes = canon.limits(m.exact["block"])
    prepared = []
    for index, spec, decl in exact_specs:
        path = render(str(spec["path"]), tokens)

        def build(kw, spec=spec, decl=decl, path=path):
            golden = _golden(kw, spec, m, inputs, tokens, db_route=False)
            if mode != "docker":
                raise ExactReadError("exact-route-unsupported", "native-mode file reads are not exact routes (F6)")
            clock_ = clock or _lifecycle.Clock

            def evaluate(remaining):
                if lifecycle and (posixpath.normpath(path) != path or not owned("owned-file", None, path)):
                    raise ExactReadError("exact-target-not-owned", f"{path!r} is not inside a confirmed owned "
                                                                   f"directory of this attempt; nothing was read")
                started = clock_.monotonic()
                files = []
                parent, base = posixpath.split(path)
                parent = parent or "."
                for node in (nodes if nodes is not None else _ownership._nodes()):
                    try:
                        listed = _ownership.docker_ls(posixpath.join(parent, "*"), nodes=[node])
                    except _ownership.InventoryError as e:
                        raise ExactReadError("exact-read-error", str(e)) from None
                    by_name = {
                        posixpath.basename(found): found
                        for found in listed
                        if posixpath.dirname(found) == parent
                    }
                    files += [(node, by_name[name]) for name in _output_names(base, by_name)]
                if decl.order == "sequence" and len(files) != 1:
                    raise ExactReadError("order-source-ambiguous",
                                         f"order: sequence needs exactly one file on one node for {path}*, found "
                                         f"{[f'{n}:{f}' for n, f in files]}")
                events, total = [], 0
                for node, f in files:
                    data = head_file(node, f, max_bytes, remaining - (clock_.monotonic() - started), run=run)
                    total += len(data)
                    if total > max_bytes:
                        raise canon.CanonError("canonical-limit-exceeded", f"{path}* exceeds {max_bytes} bytes")
                    try:
                        text = data.decode("utf-8")
                    except UnicodeDecodeError as e:
                        raise canon.CanonError(f"invalid-value:actual:{f}", str(e)) from None
                    events += parse_events(text, f)
                rows = [_file._project(e, "data", spec.get("metadata") or [], None) for e in events]
                return canon.compare(golden, rows, decl, max_rows=max_rows, max_bytes=max_bytes,
                                     bindings=_bindings(tokens))
            return evaluate
        prepared.append(_prepare("file", index, spec, decl, path, None, bool(lifecycle), build))
    return _drive("file", prepared, m, lifecycle=lifecycle, collector=collector, status_probe=status_probe,
                  poll=poll, clock=clock)
