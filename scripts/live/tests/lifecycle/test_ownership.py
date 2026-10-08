"""The ownership ledger (C7.6).

Objects are acquired per object (a catalog pre-existence check before every CREATE), only confirmed
ledger entries are deleted, always by exact name, and every foreign lookalike -- another run's prefix,
a legacy name, a rolled output part, a fallback checkpoint -- is reported and preserved. In-memory
Postgres catalog, docker file store and Striim namespaces only.
"""
from __future__ import annotations

import fnmatch
import json
import re
import threading
import time
from types import SimpleNamespace

import pytest

from livetest import evidence, lifecycle, ownership, runident

RUN = "20260914T230000Z-abcd1234"


class DependentObjectsStillExist(Exception):
    pgcode = "2BP01"                                # psycopg2's error for a DROP without CASCADE


class Pg:
    """pg_tables + pg_views/pg_matviews + pg_replication_slots, and the SQL the ledger sent. ``deps``
    maps a relation to the relations that depend on it (a view reading it, a table whose foreign key
    references it): as in Postgres, a DROP without CASCADE is refused while one of them exists."""

    def __init__(self, tables=(), slots=(), drop_error=None, drop_noop=False):
        self.tables, self.slots, self.sql = set(tables), set(slots), []
        self.views, self.matviews, self.deps = set(), set(), {}
        self.drop_error, self.drop_noop = drop_error, drop_noop
        self.sysid = "7000000000000000001"          # pg_control_system().system_identifier

    def probe(self):
        pg = self

        class Probe:
            def query(self, sql, params=None, remaining=10.0):
                pg.sql.append(sql)
                if "pg_views" in sql:
                    return ([tuple(n.split(".", 1)) + (False,) for n in sorted(pg.views) if n in params[0]]
                            + [tuple(n.split(".", 1)) + (True,) for n in sorted(pg.matviews) if n in params[1]])
                if "= ANY(%s)" in sql:
                    return [tuple(n.split(".", 1)) for n in sorted(pg.tables) if n in params[0]]
                if "LIKE %s" in sql:
                    rx = re.compile("".join(".*" if c == "%" else re.escape(c) for c in params[1].replace("\\_", "_")))
                    return [tuple(n.split(".", 1)) for n in sorted(pg.tables)
                            if n.split(".", 1)[0] == params[0] and rx.fullmatch(n.split(".", 1)[1])]
                if "pg_control_system" in sql:
                    return [(pg.sysid,)]
                if "pg_replication_slots" in sql:
                    return [(1 if params[0] in pg.slots else 0,)]
                raise AssertionError(f"unexpected catalog read {sql}")

            def run(self, sql, remaining=10.0):
                pg.sql.append(sql)
                if pg.drop_error:
                    raise pg.drop_error
                m = re.fullmatch(r'DROP (TABLE|VIEW|MATERIALIZED VIEW) IF EXISTS "(\w+)"\."(\w+)"', sql)
                assert m, sql
                name = f"{m.group(2)}.{m.group(3)}"
                live = pg.tables | pg.views | pg.matviews
                blocking = sorted(d for d in pg.deps.get(name, ()) if d in live and d != name)
                if name in live and blocking:
                    raise DependentObjectsStillExist(f"cannot drop {name} because other objects depend on it\n"
                                                     f"DETAIL:  {', '.join(blocking)} depends on {name}")
                if not pg.drop_noop:
                    {"TABLE": pg.tables, "VIEW": pg.views, "MATERIALIZED VIEW": pg.matviews}[m.group(1)].discard(name)
        return Probe()


class Docker:
    def __init__(self, files=(), dirs=()):
        self.files, self.dirs, self.calls = set(files), set(dirs), []

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[1] == "inspect":
            return SimpleNamespace(returncode=0, stdout=f"id-{argv[-1]}\n", stderr="")
        cmd = argv[3:]
        ok = SimpleNamespace(returncode=0, stdout="", stderr="")

        def hit(path):
            return path in self.files or path in self.dirs or any(f.startswith(path + "/") for f in self.files)
        if cmd[0] == "test":
            return SimpleNamespace(returncode=0 if hit(cmd[2]) else 1, stdout="", stderr="")
        if cmd[0] == "sh" and len(cmd) == 5:
            return SimpleNamespace(returncode=0, stdout="slt-present\n" if hit(cmd[4]) else "slt-absent\n", stderr="")
        if cmd[0] == "mkdir":
            if "-p" not in cmd and hit(cmd[-1]):
                return SimpleNamespace(returncode=1, stdout="", stderr=f"mkdir: cannot create directory '{cmd[-1]}': File exists")
            self.dirs.add(cmd[-1])
            return ok
        if cmd[0] == "rm":
            path = cmd[-1]
            if cmd[1] == "-rf":
                self.dirs.discard(path)
                self.files = {f for f in self.files if not f.startswith(path + "/")}
            else:
                self.files.discard(path)
            return ok
        if cmd[0] == "sh":
            return _ls(cmd[2], self.files | self.dirs)
        raise AssertionError(argv)


def _ls(script, paths):
    """``sh -c 'ls -1d <patterns> 2>/dev/null[; echo slt-ls-done]'`` over a set of paths."""
    body, done = script, ""
    if body.endswith("; echo slt-ls-done"):
        body, done = body[:-len("; echo slt-ls-done")], "slt-ls-done\n"
    pats = body[len("ls -1d "):-len(" 2>/dev/null")].split()
    out = sorted({f for f in paths for p in pats if fnmatch.fnmatch(f, p)})
    return SimpleNamespace(returncode=0, stdout="".join(f"{f}\n" for f in out) + done, stderr="")


class Nodes:
    """``docker`` against several app-node containers, each with its own filesystem (``/opt/striim`` is not
    a shared volume). ``broken[node] = n`` makes that node's next n ``docker exec`` calls fail as the
    docker CLI does (exit 1, daemon error); ``mkdir_fail`` nodes refuse to create the owned directory."""

    def __init__(self, **fs):
        self.fs = {n: set(paths) for n, paths in fs.items()}
        self.ids = {n: f"id-{n}" for n in fs}
        self.broken, self.mkdir_fail, self.calls = {}, set(), []

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[1] == "inspect":
            n = argv[-1]
            if n not in self.ids:
                return SimpleNamespace(returncode=1, stdout="", stderr=f"Error: No such object: {n}")
            return SimpleNamespace(returncode=0, stdout=self.ids[n] + "\n", stderr="")
        node, cmd = argv[2], argv[3:]
        if node not in self.fs:
            return SimpleNamespace(returncode=1, stdout="", stderr=f"Error: No such container: {node}")
        if self.broken.get(node, 0) > 0:
            self.broken[node] -= 1
            return SimpleNamespace(returncode=1, stdout="", stderr="Error response from daemon: container is restarting")
        fs = self.fs[node]
        ok = SimpleNamespace(returncode=0, stdout="", stderr="")

        def hit(path):
            return path in fs or any(x.startswith(path + "/") for x in fs)
        if cmd[0] == "test":
            return SimpleNamespace(returncode=0 if hit(cmd[2]) else 1, stdout="", stderr="")
        if cmd[0] == "sh" and len(cmd) == 5:
            return SimpleNamespace(returncode=0, stdout="slt-present\n" if hit(cmd[4]) else "slt-absent\n", stderr="")
        if cmd[0] == "sh":
            return _ls(cmd[2], fs)
        if cmd[0] == "mkdir":
            path = cmd[-1]
            if node in self.mkdir_fail and path != ownership.OWNED_ROOT.rstrip("/"):
                return SimpleNamespace(returncode=1, stdout="", stderr=f"mkdir: cannot create directory '{path}': Permission denied")
            if "-p" not in cmd and hit(path):
                return SimpleNamespace(returncode=1, stdout="", stderr=f"mkdir: cannot create directory '{path}': File exists")
            fs.add(path)
            return ok
        if cmd[0] == "rm":
            path = cmd[-1]
            for x in [x for x in fs if x == path or (cmd[1] == "-rf" and x.startswith(path + "/"))]:
                fs.discard(x)
            return ok
        raise AssertionError(argv)


class Client:
    def __init__(self, namespaces=(), unreadable=False):
        self.namespaces, self.dropped, self.unreadable = set(namespaces), [], unreadable
        client = self

        class Api:
            def post_tungsten_line(self, line, timeout=None):
                assert line == "LIST NAMESPACES;"
                if client.unreadable:
                    return {"unexpected": "shape"}
                return [{"command": line, "executionStatus": "Success",
                         "output": [{f"namespace{i}": {"name": n}} for i, n in enumerate(sorted(client.namespaces))]}]
        self.api = Api()

    def teardown_namespace(self, ns):
        self.dropped.append(ns)
        self.namespaces.discard(ns)


@pytest.fixture
def world(monkeypatch):
    pg, docker = Pg(), Docker()
    monkeypatch.setattr(ownership, "make_catalog", lambda admin: admin.pg.probe())
    monkeypatch.setattr(ownership, "_docker", docker)
    monkeypatch.setattr(ownership, "_nodes", lambda: ("slt-striim",))
    return SimpleNamespace(pg=pg, docker=docker)


def _admins(pg, **extra):
    base = {"host": "localhost", "port": 5432, "dbname": "sltdb", "admin_user": "postgres", "admin_password": "striim",
            "source_user": "qasource", "source_password": "striim", "source_schema": "qasource",
            "target_user": "qatarget", "target_password": "striim", "target_schema": "qatarget"}
    admins = {route: {"admin": SimpleNamespace(dsn=base, role=role, pg=pg,
                                              drop_replication_slot=lambda name, **kw: pg.slots.discard(name))}
              for route, role in (("postgres-source", "source"), ("postgres-target", "target"))}
    admins.update(extra)
    return admins


DOCKER = SimpleNamespace(mode="docker", url="http://localhost:9080", user="admin")


def _ident(attempt="a1a1a1a1", run=RUN):
    return runident.derive("lc", {"SLT_RUN_EPOCH": run}, attempt=attempt)


def _ledger(tmp_path, attempt="a1a1a1a1", env=None):
    return ownership.Ledger.open(_ident(attempt), state_dir=tmp_path, env=env or {})


def _create_runner(pg):
    """run_one: execute the CREATE TABLE / [MATERIALIZED] VIEW statements of the rendered file into the
    fake catalog, recording what each one reads (FROM/JOIN) or references (a foreign key)."""
    def run_one(db, fname, sql_by_file):
        schema = "qasource" if db == "postgres-source" else "qatarget"
        for stmt in sql_by_file[fname].split(";"):
            m = re.search(r"CREATE (?:OR REPLACE )?(TABLE|VIEW|MATERIALIZED VIEW) (\w+)", stmt)
            if not m:
                continue
            name = f"{schema}.{m.group(2)}"
            {"TABLE": pg.tables, "VIEW": pg.views, "MATERIALIZED VIEW": pg.matviews}[m.group(1)].add(name)
            for used in re.findall(r"\b(?:FROM|JOIN|REFERENCES) (\w+)", stmt):
                pg.deps.setdefault(f"{schema}.{used}", set()).add(name)
        for slot in re.findall(r"pg_create_logical_replication_slot\('(\w+)'", sql_by_file[fname]):
            pg.slots.add(slot)
    return run_one


def _ddl(ledger, world, files, sqls, fail_on=None):
    run = _create_runner(world.pg)

    def run_one(db, fname):
        if fail_on and fname in fail_on:
            fail_on[fname](db, fname)
        run(db, fname, sqls)
    ledger.run_ddl_files(files, admins=_admins(world.pg), tokens={"TID": ledger.ident.tid},
                         render_file=sqls.__getitem__, run_one=run_one)


def _cleanup(ledger, world, client=None, **kw):
    return ledger.cleanup(client=client or Client(), admins=kw.pop("admins", _admins(world.pg)), ctx=kw.pop("ctx", DOCKER),
                          tokens={"TID": ledger.ident.tid, "TID_ORACLE": ledger.ident.tid_oracle}, **kw)


def test_ledger_opened_and_persisted_before_first_side_effect(tmp_path, world):
    led = _ledger(tmp_path)
    doc = json.loads(led.path.read_text())
    assert led.path == tmp_path / "lifecycle" / "ledgers" / f"{led.ident.per_test}.json"
    assert doc["identity"]["runId"] == RUN and doc["attempts"] == ["a1a1a1a1"] and doc["entries"] == []
    assert world.docker.calls == [] and world.pg.sql == []                       # nothing touched yet
    led.add("pg-table", "qasource.x", "postgres-source", "confirmed")
    again = _ledger(tmp_path, attempt="b2b2b2b2")
    assert again.doc["attempts"] == ["a1a1a1a1", "b2b2b2b2"] and again.entries[0]["name"] == "qasource.x"
    led.path.write_text("{not json")
    with pytest.raises(ownership.OwnershipError, match="unreadable"):
        _ledger(tmp_path)


def test_ddl_file_by_file_records_table_after_each_file_and_keeps_original_exception(tmp_path, world):
    led = _ledger(tmp_path)
    t = led.ident.tid
    sqls = {"src.sql": f"CREATE TABLE {t}src (id int);", "tgt.sql": f"CREATE TABLE {t}tgt (id int);"}
    seen_on_disk = {}

    def boom(db, fname):
        seen_on_disk["entries"] = json.loads(led.path.read_text())["entries"]
        raise ValueError("tgt.sql: syntax error")

    with pytest.raises(ValueError, match="syntax error"):
        _ddl(led, world, [("postgres-source", "src.sql"), ("postgres-target", "tgt.sql")], sqls, fail_on={"tgt.sql": boom})
    assert [(e["name"], e["state"]) for e in seen_on_disk["entries"]] == [
        (f"qasource.{t}src", "confirmed"), (f"qatarget.{t}tgt", "intended")]
    assert [(e["name"], e["state"]) for e in led.entries] == [(f"qasource.{t}src", "confirmed")]


def test_publication_in_ddl_refused_before_execution(tmp_path, world):
    led = _ledger(tmp_path)
    for sql in ("CREATE PUBLICATION p FOR TABLE x;", "create schema lc_extra;"):
        ran = []
        with pytest.raises(ownership.OwnershipError, match="unsupported-resource: p.sql runs CREATE (PUBLICATION|SCHEMA)"):
            led.run_ddl_files([("postgres-source", "p.sql")], admins=_admins(world.pg), tokens={},
                              render_file=lambda f: sql, run_one=lambda db, f: ran.append(f))
        assert ran == [] and led.entries == []


def test_reset_consumes_prior_attempt_ledger_exact_names_only(tmp_path, world):
    first = _ledger(tmp_path)
    t = first.ident.tid
    first.reset(client=Client(), admins=_admins(world.pg), ctx=DOCKER)          # the first attempt records its targets
    world.pg.tables |= {f"qasource.{t}src", "qasource.t9f8e7d6c_src", "qasource.src"}
    first.add("pg-table", f"qasource.{t}src", "postgres-source", "confirmed")
    retry = _ledger(tmp_path, attempt="b2b2b2b2")
    retry.reset(client=Client(), admins=_admins(world.pg), ctx=DOCKER)
    assert world.pg.tables == {"qasource.t9f8e7d6c_src", "qasource.src"}
    assert [s for s in world.pg.sql if s.startswith("DROP")] == [f'DROP TABLE IF EXISTS "qasource"."{t}src"']
    assert retry.entries[0]["state"] == "deleted"


def test_prefix_listed_object_absent_from_ledger_is_foreign_not_deleted(tmp_path, world):
    led = _ledger(tmp_path)
    t = led.ident.tid
    _ddl(led, world, [("postgres-source", "src.sql")], {"src.sql": f"CREATE TABLE {t}src (id int);"})
    world.pg.tables.add(f"qasource.{t}made_by_the_app")                        # our prefix, never ledgered
    result = _cleanup(led, world)
    assert f"qasource.{t}made_by_the_app" in world.pg.tables
    assert {"kind": "pg-table", "name": f"qasource.{t}made_by_the_app",
            "note": "carries this run's prefix but is not in its ledger (never deleted on a prefix alone); preserved"} in result.foreign


def test_cleanup_drops_only_ledgered_exact_names(tmp_path, world):
    led = _ledger(tmp_path)
    t = led.ident.tid
    _ddl(led, world, [("postgres-source", "a.sql"), ("postgres-target", "b.sql")],
         {"a.sql": f"CREATE TABLE {t}src (id int);", "b.sql": f"CREATE TABLE {t}tgt (id int);"})
    world.pg.tables |= {f"qasource.{t}other", "qatarget.shared"}
    result = _cleanup(led, world)
    drops = [s for s in world.pg.sql if not s.startswith("SELECT")]
    assert drops == [f'DROP TABLE IF EXISTS "qatarget"."{t}tgt"', f'DROP TABLE IF EXISTS "qasource"."{t}src"']
    assert not any("LIKE" in s or "SCHEMA" in s.upper() for s in drops)
    assert world.pg.tables == {f"qasource.{t}other", "qatarget.shared"} and result.status == "ok"


def test_sibling_run_and_legacy_lookalike_tables_preserved_and_reported(tmp_path, world):
    led = _ledger(tmp_path)
    t = led.ident.tid
    world.pg.tables |= {"qasource.t9f8e7d6c_users", "qasource.users"}
    _ddl(led, world, [("postgres-source", "u.sql")], {"u.sql": f"CREATE TABLE {t}users (id int);"})
    result = _cleanup(led, world)
    assert world.pg.tables == {"qasource.t9f8e7d6c_users", "qasource.users"}
    assert {f["name"] for f in result.foreign} == {"qasource.t9f8e7d6c_users", "qasource.users"}
    assert all(f["note"].endswith("preserved") for f in result.foreign)


def test_namespace_cleanup_only_own_namespace(tmp_path, world):
    led = _ledger(tmp_path)
    client = Client(namespaces={"SLT_lc", "SLT_lc_t0badc0de"})
    led.acquire_namespace(client)
    client.namespaces.add(led.ident.ns)                                          # the deploy creates it
    result = _cleanup(led, world, client=client)
    assert client.dropped == [led.ident.ns] and client.namespaces == {"SLT_lc", "SLT_lc_t0badc0de"}
    assert result.status == "ok" and result.verified and result.owned[0]["state"] == "verified-absent"


def test_namespace_acquire_refuses_existing_and_unparseable(tmp_path, world):
    led = _ledger(tmp_path)
    with pytest.raises(ownership.OwnershipError, match=f"collision:namespace:{led.ident.ns}"):
        led.acquire_namespace(Client(namespaces={led.ident.ns}))
    with pytest.raises(ownership.OwnershipError, match="namespace-catalog-unparseable"):
        led.acquire_namespace(Client(unreadable=True))
    assert led.entries == []


def test_server_file_removal_exact_paths_never_parent_wipe(tmp_path, world):
    led = _ledger(tmp_path)
    mine, other = f"/tmp/{led.ident.ns}/in/in.csv", f"/tmp/{led.ident.ns}/in/other.csv"
    world.docker.files.add(other)
    assert led.claim_exact_file(DOCKER, mine, "server-file")["state"] == "confirmed"
    world.docker.files.add(mine)                                                 # placed by the plugin
    result = _cleanup(led, world)
    assert world.docker.files == {other} and result.status == "ok"
    rms = [c for c in world.docker.calls if c[3] == "rm"]
    assert rms == [["docker", "exec", "slt-striim", "rm", "-f", "--", mine]]
    assert not any("*" in " ".join(c) for c in rms)


def test_slot_dropped_and_verified(tmp_path, world):
    led = _ledger(tmp_path)
    slot = led.ident.pg_slot
    _ddl(led, world, [("postgres-source", "slot.sql")],
         {"slot.sql": f"SELECT 'init' FROM pg_create_logical_replication_slot('{slot}', 'wal2json');"})
    assert [(e["kind"], e["state"]) for e in led.entries] == [("pg-slot", "confirmed")]
    world.pg.slots.add("slt_t0000000aa")                                          # a sibling's slot
    result = _cleanup(led, world)
    assert world.pg.slots == {"slt_t0000000aa"} and result.owned == [
        {"kind": "pg-slot", "name": slot, "db": "postgres-source", "state": "verified-absent"}]


def test_cleanup_verified_true_only_when_every_entry_absent(tmp_path, world):
    led = _ledger(tmp_path)
    t = led.ident.tid
    _ddl(led, world, [("postgres-source", "a.sql")], {"a.sql": f"CREATE TABLE {t}a (id int);"})
    world.pg.drop_noop = True                                                    # the DROP "succeeds" but nothing changes
    result = _cleanup(led, world)
    assert result.status == "failed" and result.verified is False and "still present after delete" in result.detail
    led2 = _ledger(tmp_path / "b")
    world.pg.drop_noop = False
    _ddl(led2, world, [("postgres-source", "a.sql")], {"a.sql": f"CREATE TABLE {t}b (id int);"})
    assert _cleanup(led2, world).verified is True


def test_verification_gap_recorded_for_unsupported_engine(tmp_path, world):
    led = _ledger(tmp_path)
    dropped, ran = [], []
    ora = {"admin": SimpleNamespace(drop_test_tables=dropped.append)}
    led.run_ddl_files([("oracle-source", "ora.sql")], admins=_admins(world.pg, **{"oracle-source": ora}),
                      tokens={"TID_ORACLE": led.ident.tid_oracle}, render_file=lambda f: "CREATE TABLE ${TID_ORACLE}T (ID INT)",
                      run_one=lambda db, f: ran.append(f))
    result = _cleanup(led, world, admins=_admins(world.pg, **{"oracle-source": ora}))
    assert ran == ["ora.sql"] and dropped == [] and result.status == "ok" and result.verified is False
    assert any(g.startswith("engine-tables oracle-source: the tables of ora.sql are not acquired") for g in result.gaps)
    mysql = {"admin": SimpleNamespace()}
    led2 = _ledger(tmp_path / "m")
    led2.run_ddl_files([("mysql-source", "my.sql")], admins={"mysql-source": mysql}, tokens={"TID": led2.ident.tid},
                       render_file=lambda f: "CREATE TABLE x (id int)", run_one=lambda db, f: None)
    r2 = _cleanup(led2, world, admins={"mysql-source": mysql})
    assert r2.status == "ok" and r2.verified is False and any("never deleted" in g for g in r2.gaps)


def test_keep_resources_status_skipped_with_reason(tmp_path, world):
    led = _ledger(tmp_path)
    t = led.ident.tid
    _ddl(led, world, [("postgres-source", "a.sql")], {"a.sql": f"CREATE TABLE {t}a (id int);"})
    result = _cleanup(led, world, keep_reason="SLT_KEEP_RESOURCES is set")
    assert result.status == "skipped" and "SLT_KEEP_RESOURCES is set" in result.detail
    assert f"python -m livetest.ownership replay {led.path}" in result.detail
    assert f"qasource.{t}a" in world.pg.tables and result.verified is False
    assert json.loads(led.path.read_text())["keep"]["reason"] == "SLT_KEEP_RESOURCES is set"


@pytest.mark.parametrize("active", [False, True])
def test_failed_test_keeps_apps_and_tables_but_drops_slots(tmp_path, world, active):
    from livetest.plugin import should_keep_resources

    led = _ledger(tmp_path)
    client = Client()
    stopped = []
    client.stop_app = stopped.append
    attempts = []
    api = client.api.post_tungsten_line

    def listing(line, timeout=None):
        if line == "LIST APPLICATIONS;":
            return [{"output": [led.ident.app, led.ident.app + "_reader", "User.App"]}]
        return api(line)

    client.api.post_tungsten_line = listing
    led.acquire_namespace(client)
    client.namespaces.add(led.ident.ns)
    slot = led.ident.pg_slot
    _ddl(led, world, [("postgres-source", "a.sql")],
         {"a.sql": f"CREATE TABLE {led.ident.tid}a (id int);"
                   f"SELECT pg_create_logical_replication_slot('{slot}', 'wal2json');"
                   f"SELECT pg_create_logical_replication_slot('{slot}_extra', 'wal2json');"})
    world.pg.slots.add("user_slot")
    assert should_keep_resources({"SLT_KEEP_RESOURCES_ON_ERROR": "1"}, deploy_attempted=True, succeeded=False)
    admins = _admins(world.pg)

    def drop(name, on_active=None):
        attempts.append((name, list(stopped)))
        if active and not stopped:
            assert on_active is not None
            on_active(10.0)
        world.pg.slots.discard(name)

    admins["postgres-source"]["admin"].drop_replication_slot = drop
    result = _cleanup(led, world, client=client, admins=admins,
                      keep_reason="failed: SLT_KEEP_RESOURCES_ON_ERROR")
    assert world.pg.slots == {"user_slot"}
    assert attempts[0][1] == []
    assert stopped == ([led.ident.app, led.ident.app + "_reader"] if active else [])
    assert client.namespaces == {led.ident.ns} and client.dropped == []
    assert world.pg.tables == {f"qasource.{led.ident.tid}a"}
    assert result.status == "skipped"
    assert all(e["state"] == "verified-absent" for e in result.owned if e["kind"] == "pg-slot")


def test_kept_slot_cleanup_failure_is_reported_and_other_resources_survive(tmp_path, world):
    led = _ledger(tmp_path)
    slot = led.ident.pg_slot
    _ddl(led, world, [("postgres-source", "a.sql")],
         {"a.sql": f"CREATE TABLE {led.ident.tid}a (id int);"
                   f"SELECT pg_create_logical_replication_slot('{slot}', 'wal2json');"})
    admins = _admins(world.pg)
    admins["postgres-source"]["admin"].drop_replication_slot = lambda name, **kw: None
    result = _cleanup(led, world, admins=admins, keep_reason="keep failed test")
    assert result.status == "failed" and "still present after delete" in result.detail
    assert slot in world.pg.slots and f"qasource.{led.ident.tid}a" in world.pg.tables


def test_cleanup_failure_status_failed_with_detail(tmp_path, world):
    led = _ledger(tmp_path)
    t = led.ident.tid
    _ddl(led, world, [("postgres-source", "a.sql")], {"a.sql": f"CREATE TABLE {t}a (id int);"})
    world.pg.drop_error = RuntimeError("permission denied for table")
    result = _cleanup(led, world)
    assert result.status == "failed" and f"pg-table qasource.{t}a: permission denied" in result.detail
    assert led.entries[0]["state"] == "delete-failed" and result.record() == {"status": "failed", "detail": result.detail}


def _drops(pg):
    return [s for s in pg.sql if s.startswith("DROP")]


def test_cleanup_drops_owned_views_before_the_tables_they_read(tmp_path, world):
    # lookup-gated-declared: views on gline blocked DROP TABLE gline
    led = _ledger(tmp_path)
    t = led.ident.tid
    ddl = (f"CREATE TABLE {t}gorder (order_id bigint);"
           f"CREATE TABLE {t}gline (order_id bigint);"
           f"CREATE VIEW {t}gviolations AS SELECT 1 FROM {t}gline l LEFT JOIN {t}gorder o ON true;"
           f"CREATE OR REPLACE VIEW {t}gline_v AS SELECT order_id FROM {t}gline;"
           f"CREATE VIEW {t}gline_ab AS SELECT order_id FROM {t}gline_v;"
           f"CREATE MATERIALIZED VIEW {t}gorder_m AS SELECT order_id FROM {t}gorder;")
    _ddl(led, world, [("postgres-target", "t.sql")], {"t.sql": ddl})
    assert [(e["kind"], e["name"].split(".", 1)[1], e["state"]) for e in led.entries] == [
        ("pg-table", f"{t}gorder", "confirmed"), ("pg-table", f"{t}gline", "confirmed"),
        ("pg-view", f"{t}gviolations", "confirmed"), ("pg-view", f"{t}gline_v", "confirmed"),
        ("pg-view", f"{t}gline_ab", "confirmed"), ("pg-view", f"{t}gorder_m", "confirmed")]
    result = _cleanup(led, world)
    assert result.status == "ok" and result.verified, (result.detail, result.gaps)
    assert _drops(world.pg) == [f'DROP MATERIALIZED VIEW IF EXISTS "qatarget"."{t}gorder_m"',
                                f'DROP VIEW IF EXISTS "qatarget"."{t}gline_ab"',
                                f'DROP VIEW IF EXISTS "qatarget"."{t}gline_v"',
                                f'DROP VIEW IF EXISTS "qatarget"."{t}gviolations"',
                                f'DROP TABLE IF EXISTS "qatarget"."{t}gline"',
                                f'DROP TABLE IF EXISTS "qatarget"."{t}gorder"']
    assert world.pg.tables == world.pg.views == world.pg.matviews == set()
    assert {e["state"] for e in led.entries} == {"verified-absent"}


def test_cleanup_drops_the_foreign_key_child_before_its_owned_parent(tmp_path, world):
    # jdbcsink/postgres-basic: orders' FK blocked DROP TABLE customers
    led = _ledger(tmp_path)
    t = led.ident.tid
    ddl = (f"CREATE TABLE {t}customers (customer_id int PRIMARY KEY);"
           f"CREATE TABLE {t}orders (customer_id int, CONSTRAINT {t}orders_customer_fk "
           f"FOREIGN KEY (customer_id) REFERENCES {t}customers (customer_id));"
           f"CREATE TABLE {t}chkpoint (id varchar(100) PRIMARY KEY);")
    _ddl(led, world, [("postgres-source", "s.sql"), ("postgres-target", "t.sql")],
         {"s.sql": ddl, "t.sql": ddl})
    result = _cleanup(led, world)
    assert result.status == "ok" and result.verified, (result.detail, result.gaps)
    assert _drops(world.pg) == [f'DROP TABLE IF EXISTS "qatarget"."{t}chkpoint"',
                                f'DROP TABLE IF EXISTS "qatarget"."{t}orders"',
                                f'DROP TABLE IF EXISTS "qatarget"."{t}customers"',
                                f'DROP TABLE IF EXISTS "qasource"."{t}chkpoint"',
                                f'DROP TABLE IF EXISTS "qasource"."{t}orders"',
                                f'DROP TABLE IF EXISTS "qasource"."{t}customers"']
    assert world.pg.tables == set()


def test_cleanup_never_drops_a_dependent_it_does_not_own(tmp_path, world):
    led = _ledger(tmp_path)
    t = led.ident.tid
    _ddl(led, world, [("postgres-target", "t.sql")],
         {"t.sql": f"CREATE TABLE {t}gline (id int);CREATE VIEW {t}gline_v AS SELECT id FROM {t}gline;"})
    # another run's view and foreign key on this run's table: never in this ledger
    world.pg.views.add("qatarget.t9f8e7d6c_spy")
    world.pg.tables.add("qatarget.t9f8e7d6c_child")
    world.pg.deps[f"qatarget.{t}gline"] |= {"qatarget.t9f8e7d6c_spy", "qatarget.t9f8e7d6c_child"}
    result = _cleanup(led, world)
    assert result.status == "failed" and result.verified is False
    assert f"pg-table qatarget.{t}gline: not-owned-dependent:" in result.detail
    assert "qatarget.t9f8e7d6c_spy" in result.detail and "not in this run's ledger" in result.detail
    assert {"qatarget.t9f8e7d6c_spy"} == world.pg.views
    assert {f"qatarget.{t}gline", "qatarget.t9f8e7d6c_child"} == world.pg.tables
    assert not any("CASCADE" in s.upper() for s in world.pg.sql)
    assert [(e["kind"], e["state"]) for e in led.entries] == [("pg-table", "delete-failed"),
                                                              ("pg-view", "verified-absent")]


def test_view_acquired_only_when_absent_before_the_file_runs(tmp_path, world):
    led = _ledger(tmp_path)
    t = led.ident.tid
    world.pg.views.add(f"qatarget.{t}gline_v")                                  # not this run's
    ran = []
    with pytest.raises(ownership.OwnershipError, match=f"collision:pg-view:qatarget.{t}gline_v"):
        led.run_ddl_files([("postgres-target", "t.sql")], admins=_admins(world.pg), tokens={},
                          render_file=lambda f: f"CREATE OR REPLACE VIEW {t}gline_v AS SELECT 1;",
                          run_one=lambda db, f: ran.append(f))
    assert ran == [] and led.entries == [] and world.pg.views == {f"qatarget.{t}gline_v"}


def test_replay_from_ledger_file(tmp_path, world):
    led = _ledger(tmp_path)
    t = led.ident.tid
    _ddl(led, world, [("postgres-source", "a.sql")], {"a.sql": f"CREATE TABLE {t}a (id int);"})
    _cleanup(led, world, keep_reason="SLT_KEEP_RESOURCES is set")
    client = Client()
    result = ownership.replay(led.path, admins=_admins(world.pg), client=client, ctx=DOCKER, env={})
    assert result.status == "ok" and f"qasource.{t}a" not in world.pg.tables
    doc = json.loads(led.path.read_text())
    assert [(e["name"], e["state"]) for e in doc["entries"]] == [(f"qasource.{t}a", "verified-absent")]


def test_replay_takes_postgres_passwords_as_a_run_gets_them(tmp_path, world, monkeypatch):
    # Shell > project .env > clone .env, as striim-test hands them to the run
    from livetest import paths, pgclient
    led = _ledger(tmp_path)
    led.reset(client=Client(), admins=_admins(world.pg), ctx=DOCKER)             # records the route binding
    clone, project = tmp_path / "clone", tmp_path / "project"
    clone.mkdir(); project.mkdir()
    monkeypatch.setattr(paths, "_default_project_root", lambda: clone)
    monkeypatch.setattr(paths, "dotenv_values", lambda env=None: paths.read_dotenv(paths.dotenv_path(env)))
    (clone / ".env").write_text("SLT_PG_ADMIN_PASSWORD=clone-admin\nSLT_PG_SOURCE_PASSWORD=clone-src\n"
                                "SLT_PG_TARGET_PASSWORD=clone-tgt\n")
    (project / ".env").write_text("SLT_PG_SOURCE_PASSWORD=project-src\nSLT_PG_TARGET_PASSWORD=project-tgt\n")
    seen = []

    class Stop(Exception):
        pass

    def capture(dsn, role="source"):
        seen.append(dict(dsn))
        raise Stop

    monkeypatch.setattr(pgclient, "PgAdmin", capture)
    with pytest.raises(Stop):
        ownership.replay(led.path, client=Client(), ctx=DOCKER,
                         env={"SLT_PROJECT_ROOT": str(project), "SLT_PG_TARGET_PASSWORD": "shell-tgt"})
    assert {k: seen[0][k] for k in ("admin_password", "source_password", "target_password")} == {
        "admin_password": "clone-admin", "source_password": "project-src", "target_password": "shell-tgt"}



@pytest.mark.parametrize("shell,project_env,want", [
    ({}, "", "clone-pw"),                                          # clone .env only
    ({}, "STRIIM_PASSWORD=project-pw\n", "project-pw"),           # project .env, by the alias
    ({"STRIIM_PASSWORD": "shell-pw"}, "STRIIM_PASS=project-pw\n", "shell-pw"),   # the shell wins
], ids=["clone-env", "project-env-alias", "shell"])
def test_replay_takes_the_striim_password_as_a_run_gets_it(tmp_path, world, monkeypatch, shell, project_env, want):
    # Shell > project .env > clone .env, as striim-test hands STRIIM_* to the run
    from livetest import paths, striim
    led = _ledger(tmp_path)
    led.reset(client=Client(), admins=_admins(world.pg), ctx=DOCKER)             # records the striim binding
    clone, project = tmp_path / "clone", tmp_path / "project"
    clone.mkdir(); project.mkdir()
    monkeypatch.setattr(paths, "_default_project_root", lambda: clone)
    monkeypatch.setattr(paths, "dotenv_values", lambda env=None: paths.read_dotenv(paths.dotenv_path(env)))
    (clone / ".env").write_text("STRIIM_PASS=clone-pw\n")
    (project / ".env").write_text(project_env)
    seen = []

    class Stop(Exception):
        pass

    def capture(url, user, pw):
        seen.append((url, user, pw))
        raise Stop

    monkeypatch.setattr(striim.StriimClient, "from_url", staticmethod(capture))
    monkeypatch.setattr(ownership, "_LazyClient", lambda make: make())   # build the client now
    with pytest.raises(Stop):
        ownership.replay(led.path, admins={}, ctx=DOCKER, env={"SLT_PROJECT_ROOT": str(project), **shell})
    assert seen == [("http://localhost:9080", "admin", want)]

def test_acquire_refuses_preexisting_foreign_table_before_create(tmp_path, world):
    led = _ledger(tmp_path)
    t = led.ident.tid
    world.pg.tables.add(f"qasource.{t}src")                                       # exists; not in this ledger
    ran = []
    with pytest.raises(ownership.OwnershipError, match=f"collision:pg-table:qasource.{t}src"):
        led.run_ddl_files([("postgres-source", "a.sql")], admins=_admins(world.pg), tokens={},
                          render_file=lambda f: f"DROP TABLE IF EXISTS {t}src; CREATE TABLE {t}src (id int);",
                          run_one=lambda db, f: ran.append(f))
    assert ran == [] and f"qasource.{t}src" in world.pg.tables and led.entries == []
    assert not any(s.startswith("DROP") for s in world.pg.sql)


def test_acquire_adopts_nothing_on_failed_create(tmp_path, world):
    led = _ledger(tmp_path)

    def fail(db, fname):
        raise RuntimeError("could not connect")

    with pytest.raises(RuntimeError, match="could not connect"):
        _ddl(led, world, [("postgres-source", "a.sql")], {"a.sql": f"CREATE TABLE {led.ident.tid}a (id int);"},
             fail_on={"a.sql": fail})
    assert led.entries == [] and _cleanup(led, world).owned == []


def test_failed_second_file_confirms_first_files_objects_only(tmp_path, world):
    led = _ledger(tmp_path)
    t = led.ident.tid
    sql = f"CREATE TABLE {t}one (id int); CREATE TABLE {t}two (id int);"

    def half(db, fname):
        world.pg.tables.add(f"qasource.{t}one")
        raise RuntimeError("relation two: syntax error")

    with pytest.raises(RuntimeError, match="syntax error"):
        led.run_ddl_files([("postgres-source", "ab.sql")], admins=_admins(world.pg), tokens={},
                          render_file=lambda f: sql, run_one=half)
    assert [(e["name"], e["state"]) for e in led.entries] == [(f"qasource.{t}one", "confirmed")]
    _cleanup(led, world)
    assert world.pg.tables == set()


def test_owned_dir_allocated_refused_if_exists_and_removed_as_unit(tmp_path, world):
    led = _ledger(tmp_path)
    d = led.ident.owned_dir
    led.allocate_owned_dir(DOCKER)
    assert d in world.docker.dirs
    world.docker.files |= {f"{d}/out.csv", f"{d}/in/in.csv"}
    result = _cleanup(led, world)
    assert world.docker.files == set() and d not in world.docker.dirs and result.verified
    assert ["docker", "exec", "slt-striim", "rm", "-rf", "--", d] in world.docker.calls
    world.docker.dirs.add(d)
    with pytest.raises(ownership.OwnershipError, match=f"collision:owned-dir:{d}"):
        _ledger(tmp_path / "again").allocate_owned_dir(DOCKER)
    with pytest.raises(ownership.OwnershipError, match="owned-dir-unsupported-native"):
        _ledger(tmp_path / "native").allocate_owned_dir(SimpleNamespace(mode="native"))


def test_unledgered_file_matching_output_glob_outside_owned_dir_survives(tmp_path, world):
    led = _ledger(tmp_path)
    d = led.ident.owned_dir
    keep = f"{d}_result.keep"                                                   # matches <owned dir>* but is not inside it
    world.docker.files.add(keep)
    led.allocate_owned_dir(DOCKER)
    world.docker.files.add(f"{d}/out.csv")
    _cleanup(led, world)
    assert world.docker.files == {keep}


def test_legacy_output_exact_path_only_rolled_parts_reported(tmp_path, world):
    led = _ledger(tmp_path)
    out = f"/tmp/{led.ident.ns}-out"
    world.docker.files.add(out + ".00")                                         # a part left by something else
    led.claim_exact_file(DOCKER, out, "file-output")
    world.docker.files |= {out, out + ".01"}                                    # the writer's output and a rolled part
    result = _cleanup(led, world, file_globs=[out])
    assert world.docker.files == {out + ".00", out + ".01"}
    assert {f["name"] for f in result.foreign} == {out + ".00", out + ".01"}


def test_checkpoint_listed_names_deleted_only_with_ns_as_dot_component(tmp_path, world):
    led = _ledger(tmp_path)
    ns = led.ident.ns
    own = {f"/opt/striim/ExampleReaderOp_ab12_{ns}.lcApp__src.position.json", f"/opt/striim/{ns}.lcApp.cursors.json"}
    survivors = {f"/opt/striim/foo{ns}.position.json", f"/opt/striim/x.{ns}_extra.position.json",
                 f"/opt/striim/x_{ns}9.lcApp.position.json"}
    world.docker.files |= own | survivors
    deleted = ownership.clear_own_checkpoints(DOCKER, ns, led)
    assert set(deleted) == own and world.docker.files == survivors
    rms = [c[-1] for c in world.docker.calls if c[3] == "rm"]
    assert set(rms) == own and not any("*" in r for r in rms)


def test_fallback_checkpoint_files_reported_not_deleted(tmp_path, world):
    led = _ledger(tmp_path)
    fallbacks = {"/opt/striim/ExampleReaderOp.position.json", "/opt/striim/OtherReaderV2.cursors.json"}
    other_ns = "/opt/striim/ExampleReaderOp_ab12_SLT_other.lcApp__src.position.json"
    world.docker.files |= fallbacks | {other_ns}
    assert ownership.clear_own_checkpoints(DOCKER, led.ident.ns, led) == []
    assert fallbacks | {other_ns} <= world.docker.files
    assert {f["name"] for f in led.foreign} == fallbacks     # another namespace's file is not reported
    assert {f["kind"] for f in led.foreign} == {"op-checkpoint"}
    assert ownership.clear_own_checkpoints(SimpleNamespace(mode="native"), led.ident.ns, led) == []


def test_fault_injection_fails_cleanup_and_marks_envelope(tmp_path, world):
    led = _ledger(tmp_path, env={"SLT_LIFECYCLE_FAULT": "cleanup:table"})
    t = led.ident.tid
    _ddl(led, world, [("postgres-source", "a.sql"), ("postgres-target", "b.sql")],
         {"a.sql": f"CREATE TABLE {t}a (id int);", "b.sql": f"CREATE TABLE {t}b (id int);"})
    result = _cleanup(led, world)
    assert result.status == "failed" and "injected-fault: SLT_LIFECYCLE_FAULT=cleanup:table" in result.detail
    # tables drop in reverse creation order, so the first delete is b's
    assert result.fault["kind"] == "pg-table" and result.fault["name"] == f"qatarget.{t}b"
    assert f"qatarget.{t}b" in world.pg.tables and f"qasource.{t}a" not in world.pg.tables   # only the first delete failed
    state = lifecycle.State("cdc")
    ok = {"kind": "k", "condition": "c", "witness": "w", "at": "t", "startedAt": "t", "endedAt": "t",
          "deadlineS": 1.0, "observations": [], "reason": "satisfied"}
    state.ready, state.completion = dict(ok), dict(ok)
    (tmp_path / "case").mkdir()
    (tmp_path / "case" / "test.yaml").write_text("name: lc\n")
    item = SimpleNamespace(config=SimpleNamespace(option=SimpleNamespace(xmlpath=str(tmp_path / "junit.xml")),
                                                  rootpath=tmp_path, _slt_infra=None, _slt_striim=None),
                           name="lc", path=tmp_path / "case" / "test.yaml", manifest_path=tmp_path / "case" / "test.yaml",
                           _slt_records=[], user_properties=[], _slt_lc=state, _slt_ident=led.ident,
                           _slt_cleanup=result.record(), _slt_resources=result.resources())
    env = evidence.case_envelope(item, SimpleNamespace(when="call", outcome="failed", longrepr="x",
                                                       longreprtext="E   CleanupError"), "failed")
    assert env["lifecycle"]["faultInjected"]["kind"] == "pg-table" and env["run"]["qualifies"] is False
    assert env["lifecycle"]["cleanup"]["status"] == "failed"
    replayed = ownership.replay(led.path, admins=_admins(world.pg), client=Client(), ctx=DOCKER, env={})
    assert replayed.status == "ok" and f"qatarget.{t}b" not in world.pg.tables


def test_cleanup_with_ledger_none_is_skipped_with_reason():
    result = ownership.run_cleanup(None)
    assert result.status == "skipped" and "before its ownership ledger was opened" in result.detail
    assert result.verified is False and result.gaps and result.record()["status"] == "skipped"


# ---------------------------------------------------------------- code review r1

class _EngineSchema:
    """One Oracle/MSSQL schema with the real prefix semantics of ``OraAdmin/MssqlAdmin.drop_test_tables``."""

    def __init__(self, tables=()):
        self.tables, self.drops = set(tables), []

    def drop_test_tables(self, prefix=""):
        for t in sorted(self.tables):
            if t.upper().startswith(prefix.upper()):
                self.tables.discard(t)
                self.drops.append(t)


def test_r1_same_prefix_foreign_engine_table_is_never_dropped(tmp_path, world):
    led = _ledger(tmp_path)
    p = led.ident.tid_oracle
    schema = _EngineSchema({f"{p}SRC_KEEP"})                                    # an unledgered table, same prefix
    admins = _admins(world.pg, **{"oracle-source": {"admin": schema}})
    led.run_ddl_files([("oracle-source", "ora.sql")], admins=admins, tokens={"TID_ORACLE": p},
                      render_file=lambda f: f"CREATE TABLE {p}SRC (ID INT)", run_one=lambda db, f: schema.tables.add(f"{p}SRC"))
    result = _cleanup(led, world, admins=admins)
    assert f"{p}SRC_KEEP" in schema.tables and schema.drops == []
    assert result.verified is False and any(g.startswith("engine-tables oracle-source") for g in result.gaps)


def test_r1_derived_topic_bucket_upload_never_deleted_even_after_failure_before_allocation(tmp_path, world, monkeypatch):
    from livetest import opartifacts
    led = _ledger(tmp_path)
    deleted = []
    monkeypatch.setattr(opartifacts, "delete_artifacts", lambda ctx, names: deleted.append(("upload", tuple(names))))
    kafka = SimpleNamespace(delete_topic=lambda n: deleted.append(("topic", n)))
    gcs = SimpleNamespace(delete_bucket=lambda n: deleted.append(("bucket", n)))
    # the case failed before creating anything; resources of these exact names already exist elsewhere
    result = _cleanup(led, world, kafka_cleanup=[(kafka, f"{led.ident.per_test}_src")], gcs_cleanup=[(gcs, "slt-bucket")],
                      upload_names=[f"{led.ident.tid}cfg.json"])
    assert deleted == []
    assert result.status == "ok" and result.verified is False
    assert {g.split(" ", 1)[0] for g in result.gaps} >= {"topic", "bucket", "upload"}
    assert not [e for e in led.entries if e["kind"] in ("topic", "bucket", "upload")]


def _admins_at(pg, host):
    admins = _admins(pg)
    for e in admins.values():
        e["admin"] = SimpleNamespace(**{**vars(e["admin"]), "dsn": {**e["admin"].dsn, "host": host}})
    return admins


@pytest.mark.parametrize("change", ["system-identifier", "host"])
def test_r2_reset_never_deletes_through_a_different_backend(tmp_path, world, change):
    first = _ledger(tmp_path)
    t = first.ident.tid
    first.reset(client=Client(), admins=_admins(world.pg), ctx=DOCKER)          # attempt 1 on backend A
    first.add("pg-table", f"qasource.{t}src", "postgres-source", "confirmed")
    first.add("pg-view", f"qasource.{t}src_v", "postgres-source", "confirmed")
    world.pg.tables.add(f"qasource.{t}src")                                      # backend B: an unrelated table, same name
    world.pg.views.add(f"qasource.{t}src_v")                                     # and an unrelated view
    if change == "system-identifier":
        world.pg.sysid = "7999999999999999999"                                   # recreated behind the same host:port
        admins_b = _admins(world.pg)
    else:
        admins_b = _admins_at(world.pg, "pg-b.internal")
    retry = _ledger(tmp_path, attempt="b2b2b2b2")
    retry.reset(client=Client(), admins=admins_b, ctx=DOCKER)
    assert f"qasource.{t}src" in world.pg.tables and not any(s.startswith("DROP") for s in world.pg.sql)
    assert f"qasource.{t}src_v" in world.pg.views
    assert {g.split(":", 1)[0] for g in retry.gaps if "not deleted" in g} == {
        f"pg-table qasource.{t}src", f"pg-view qasource.{t}src_v"}
    doc = json.loads(retry.path.read_text())
    assert doc["bindings"]["a1a1a1a1"]["routes"]["postgres-source"]["systemIdentifier"] == "7000000000000000001"
    assert doc["bindings"]["a1a1a1a1"]["routes"]["postgres-source"]["host"] == "localhost"


def test_r2_replay_deletes_only_on_the_recorded_stack_nodes(tmp_path, world, monkeypatch):
    nodes = Nodes(**{"xabc-slt-striim": set(), "slt-striim": set()})
    monkeypatch.setattr(ownership, "_docker", nodes)
    monkeypatch.setattr(ownership, "_nodes", lambda: ("xabc-slt-striim",))   # the kept run's stack
    led = _ledger(tmp_path)
    d = led.ident.owned_dir
    led.reset(client=Client(), admins=_admins(world.pg), ctx=DOCKER)
    led.allocate_owned_dir(DOCKER)
    _cleanup(led, world, keep_reason="SLT_KEEP_RESOURCES is set")
    nodes.fs["slt-striim"] |= {d, f"{d}/theirs.csv"}                           # the default stack: someone else's dir
    monkeypatch.setattr(ownership, "_nodes", lambda: ("slt-striim",))        # the replay shell has no prefix
    result = ownership.replay(led.path, admins=_admins(world.pg), client=Client(), ctx=DOCKER, env={})
    assert f"{d}/theirs.csv" in nodes.fs["slt-striim"] and d not in nodes.fs["xabc-slt-striim"]
    assert result.status == "ok"
    assert not any(c[2] == "slt-striim" and c[3] == "rm" for c in nodes.calls if c[1] == "exec")


def test_r2_replay_refuses_a_recreated_node(tmp_path, world, monkeypatch):
    nodes = Nodes(**{"slt-striim": set()})
    monkeypatch.setattr(ownership, "_docker", nodes)
    led = _ledger(tmp_path)
    d = led.ident.owned_dir
    led.reset(client=Client(), admins=_admins(world.pg), ctx=DOCKER)
    led.allocate_owned_dir(DOCKER)
    _cleanup(led, world, keep_reason="SLT_KEEP_RESOURCES is set")
    nodes.ids["slt-striim"] = "id-recreated"                                   # same name, another container
    nodes.fs["slt-striim"] = {d, f"{d}/theirs.csv"}
    result = ownership.replay(led.path, admins=_admins(world.pg), client=Client(), ctx=DOCKER, env={})
    assert f"{d}/theirs.csv" in nodes.fs["slt-striim"] and result.verified is False
    assert any("changed or cannot be inspected" in g for g in result.gaps)


def test_r2_ledger_of_another_full_identity_or_version_is_refused(tmp_path, world):
    led = _ledger(tmp_path)
    led.add("pg-table", "qasource.x", "postgres-source", "confirmed")
    doc = json.loads(led.path.read_text())
    foreign = {**doc, "identity": {**doc["identity"], "runId": "another-run", "case": "another-case"}}
    led.path.write_text(json.dumps(foreign))
    with pytest.raises(ownership.OwnershipError, match="ledger-identity-mismatch"):
        _ledger(tmp_path, attempt="b2b2b2b2")
    led.path.write_text(json.dumps({**doc, "ledgerVersion": 1}))
    with pytest.raises(ownership.OwnershipError, match="ledger-version-mismatch"):
        _ledger(tmp_path, attempt="c3c3c3c3")


def test_r2_unresolved_intent_is_never_deleted_by_reset_or_replay(tmp_path, world):
    first = _ledger(tmp_path)
    t = first.ident.tid
    first.reset(client=Client(), admins=_admins(world.pg), ctx=DOCKER)
    first.add("pg-table", f"qasource.{t}src", "postgres-source")                 # intended; the process stopped here
    world.pg.tables.add(f"qasource.{t}src")                                      # created by someone else meanwhile
    retry = _ledger(tmp_path, attempt="b2b2b2b2")
    retry.reset(client=Client(), admins=_admins(world.pg), ctx=DOCKER)
    replayed = ownership.replay(retry.path, admins=_admins(world.pg), client=Client(), ctx=DOCKER, env={})
    assert f"qasource.{t}src" in world.pg.tables and not any(s.startswith("DROP") for s in world.pg.sql)
    assert any("unresolved intent" in g for g in retry.gaps) and replayed.verified is False


def test_r8_failed_inventory_then_recovery_never_adopts_a_foreign_owned_dir(tmp_path, world, monkeypatch):
    led = _ledger(tmp_path)
    d = led.ident.owned_dir
    nodes = Nodes(**{"slt-striim": {d, f"{d}/theirs.csv"}, "slt-striim-node": set()})
    nodes.broken["slt-striim"] = 1                                               # one transient docker failure
    monkeypatch.setattr(ownership, "_docker", nodes)
    monkeypatch.setattr(ownership, "_nodes", lambda: ("slt-striim", "slt-striim-node"))
    with pytest.raises(ownership.OwnershipError, match="owned-dir-inventory-unreadable"):
        led.allocate_owned_dir(DOCKER)
    assert led.entries == []
    _cleanup(led, world)
    assert f"{d}/theirs.csv" in nodes.fs["slt-striim"]


def test_r8_second_node_mkdir_failure_keeps_the_first_nodes_allocation(tmp_path, world, monkeypatch):
    led = _ledger(tmp_path)
    d = led.ident.owned_dir
    nodes = Nodes(**{"slt-striim": set(), "slt-striim-node": set()})
    nodes.mkdir_fail.add("slt-striim-node")
    monkeypatch.setattr(ownership, "_docker", nodes)
    monkeypatch.setattr(ownership, "_nodes", lambda: ("slt-striim", "slt-striim-node"))
    with pytest.raises(ownership.OwnershipError, match="mkdir"):
        led.allocate_owned_dir(DOCKER)
    assert d in nodes.fs["slt-striim"]
    entries = json.loads(led.path.read_text())["entries"]
    assert [(e["kind"], e["state"], e.get("nodes")) for e in entries] == [("owned-dir", "confirmed", ["slt-striim"])]
    result = _cleanup(led, world)
    assert d not in nodes.fs["slt-striim"] and result.status == "ok"
    assert not any(c[1] == "exec" and c[2] == "slt-striim-node" and c[3] == "rm" for c in nodes.calls)


def test_r8_unreadable_listing_refuses_claim_and_unreadable_verification_is_not_absence(tmp_path, world, monkeypatch):
    led = _ledger(tmp_path)
    out = f"/tmp/{led.ident.ns}-out"
    nodes = Nodes(**{"slt-striim": {out}})                                       # a pre-existing file of that exact name
    nodes.broken["slt-striim"] = 1
    monkeypatch.setattr(ownership, "_docker", nodes)
    with pytest.raises(ownership.OwnershipError, match="file-inventory-unreadable"):
        led.claim_exact_file(DOCKER, out, "file-output")
    assert led.entries == [] and out in nodes.fs["slt-striim"]

    led2 = _ledger(tmp_path / "v")
    nodes2 = Nodes(**{"slt-striim": set()})
    monkeypatch.setattr(ownership, "_docker", nodes2)
    led2.allocate_owned_dir(DOCKER)
    real = nodes2.__call__

    def breaks_after_rm(argv):
        r = real(argv)
        if argv[1] == "exec" and argv[3] == "rm":
            nodes2.broken["slt-striim"] = 5                                      # the node cannot be read afterwards
        return r
    monkeypatch.setattr(ownership, "_docker", breaks_after_rm)
    result = _cleanup(led2, world)
    assert [e["state"] for e in result.owned] == ["deleted"] and result.verified is False
    assert any("verification read failed" in g for g in result.gaps)


def test_r9_persistence_failure_during_cleanup_is_a_failed_result(tmp_path, world, monkeypatch):
    led = _ledger(tmp_path)
    _ddl(led, world, [("postgres-source", "a.sql")], {"a.sql": f"CREATE TABLE {led.ident.tid}a (id int);"})

    def disk_full(self):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(ownership.Ledger, "persist", disk_full)
    result = ownership.run_cleanup(led, client=Client(), admins=_admins(world.pg), ctx=DOCKER, tokens={})
    assert result.status == "failed" and "No space left on device" in result.detail
    assert result.verified is False and result.record()["status"] == "failed"


def test_r4_hanging_cleanup_delete_is_bounded(tmp_path, world, monkeypatch):
    monkeypatch.setattr(ownership, "CLEANUP_OP_S", 0.3, raising=False)
    led = _ledger(tmp_path)
    gate = threading.Event()
    client = Client()
    led.acquire_namespace(client)
    client.teardown_namespace = lambda ns: gate.wait(60)                        # a DROP NAMESPACE that never returns
    box = {}
    started = time.monotonic()
    worker = threading.Thread(target=lambda: box.update(result=_cleanup(led, world, client=client)), daemon=True)
    worker.start()
    worker.join(15)
    alive = worker.is_alive()
    gate.set()
    assert not alive, "cleanup was not bounded"
    assert time.monotonic() - started < 15
    assert box["result"].status == "failed" and "did not return" in box["result"].detail


def _witness_spec(**over):
    block = {"version": 1, "mode": "initial-load", "sink": "db",
             "readiness": {"kind": "baseline-landed", "source": {"db": "postgres-source", "table": "qasource.${TID}src"},
                           "target": {"db": "postgres-target", "table": "qatarget.${TID}tgt"}},
             "completion": {"kind": "source-count", "source": {"db": "postgres-source", "table": "qasource.${TID}src"},
                            "target": {"db": "postgres-target", "table": "qatarget.${TID}tgt"}}}
    block.update(over)
    return lifecycle.parse_spec(block, "p")


def _owned_tables(led, world):
    t = led.ident.tid
    _ddl(led, world, [("postgres-source", "s.sql"), ("postgres-target", "t.sql")],
         {"s.sql": f"CREATE TABLE {t}src (id int);", "t.sql": f"CREATE TABLE {t}tgt (id int);"})
    return {"TID": t, "OWNED_DIR": led.ident.owned_dir}


def test_r5_witness_references_must_be_run_owned_on_their_route(tmp_path, world):
    led = _ledger(tmp_path)
    tokens = _owned_tables(led, world)
    assert lifecycle.check_witness_refs(_witness_spec(), tokens, led.owns) is None
    foreign = {"kind": "source-count", "source": {"db": "postgres-source", "table": "qasource.${TID}src"},
               "target": {"db": "postgres-target", "table": "qatarget.previous_result"}}
    world.pg.tables.add("qatarget.previous_result")
    assert lifecycle.check_witness_refs(_witness_spec(completion=foreign), tokens, led.owns).startswith(
        "witness-not-owned: completion.target postgres-target:qatarget.previous_result")
    wrong_route = {**foreign, "target": {"db": "postgres-source", "table": "qatarget.${TID}tgt"}}
    assert "witness-not-owned" in lifecycle.check_witness_refs(_witness_spec(completion=wrong_route), tokens, led.owns)
    as_source = {**foreign, "target": {"db": "postgres-source", "table": "qasource.${TID}src"}}
    assert lifecycle.check_witness_refs(_witness_spec(completion=as_source), tokens, led.owns).startswith(
        "witness-self-observation: completion.target")


def test_r5_witness_file_paths_must_stay_inside_this_attempts_owned_dir(tmp_path, world):
    led = _ledger(tmp_path)
    led.allocate_owned_dir(DOCKER)
    d = led.ident.owned_dir

    def spec(path):
        return lifecycle.parse_spec({"version": 1, "mode": "cdc", "sink": "file",
                                     "readiness": {"kind": "source-progress", "db": "postgres-source"},
                                     "completion": {"kind": "file-lines", "path": path, "lines": 1}}, "p")
    tokens = {"OWNED_DIR": d, "UP": ".."}
    assert lifecycle.check_witness_refs(spec("${OWNED_DIR}/out.csv"), tokens, led.owns) is None
    for bad in ("${OWNED_DIR}/${UP}/foreign/out.csv", "/opt/striim/slt-runs/SLT_other_t000000000/out.csv", "${OWNED_DIR}"):
        assert lifecycle.check_witness_refs(spec(bad), tokens, led.owns).startswith("witness-not-owned: completion.path")


def test_r5_witness_on_a_foreign_target_is_refused_before_any_poll(tmp_path, world, monkeypatch):
    led = _ledger(tmp_path)
    tokens = _owned_tables(led, world)
    polled = []
    monkeypatch.setattr(lifecycle, "make_probe", lambda admin: pytest.fail("a witness polled a foreign table"))
    state = lifecycle.State("initial-load")
    foreign = {"kind": "baseline-landed", "source": {"db": "postgres-source", "table": "qasource.${TID}src"},
               "target": {"db": "postgres-target", "table": "qatarget.previous_result"}}
    with pytest.raises(lifecycle.LifecycleError, match="witness-not-owned"):
        lifecycle.record_baseline(state, _witness_spec(readiness=foreign), _admins(world.pg), tokens, owned=led.owns)
    assert state.ready["reason"] == "witness-not-owned" and polled == []
