import pytest
from livetest.pgclient import PgAdmin
from livetest.registry import load_service
from livetest.services import resolve
from livetest.plugin import build_service_tokens


# ---- service definition + token rendering ----------------------------------

def test_postgres_service_def_loads():
    defn = load_service("postgres")
    assert defn.isolation == "none"
    assert defn.container == "slt-postgres"
    assert defn.live_override_env == "SLT_PG_HOST"
    for key in ("PG_HOST", "PG_PORT", "PG_DB", "PG_SOURCE_USER",
                "PG_TARGET_USER", "PG_SLOT", "PG_URL"):
        assert key in defn.provides


def test_postgres_resolve_docker_defaults():
    started = set()
    r = resolve("postgres", env={}, started=started, compose_up=lambda defn: None, post_up=lambda defn: None)
    assert r.mode == "docker" and r.started is True
    assert r.base["host"] == "localhost"
    assert r.base["port"] == 5432
    assert r.base["dbname"] == "sltdb"
    assert "slt-postgres" in started


def test_postgres_resolve_live_override():
    env = {
        "SLT_PG_HOST": "pghost",
        "SLT_PG_PORT": "5433",
        "SLT_PG_DB": "custom_db",
        "SLT_PG_SOURCE_USER": "src",
        "SLT_PG_SOURCE_PASSWORD": "srcpw",
        "SLT_PG_TARGET_USER": "tgt",
        "SLT_PG_TARGET_PASSWORD": "tgtpw",
    }
    r = resolve("postgres", env=env, started=set(),
                compose_up=lambda defn: (_ for _ in ()).throw(AssertionError("should not compose up")))
    assert r.mode == "live" and r.started is False
    assert r.base["host"] == "pghost"
    assert r.base["port"] == "5433"
    assert r.base["dbname"] == "custom_db"


def test_postgres_view_host_for_docker_striim():
    r = resolve("postgres", env={"SLT_STRIIM_VIEW_HOST": "host.docker.internal"},
                started=set(), compose_up=lambda defn: None, post_up=lambda defn: None)
    assert r.base["view_host"] == "host.docker.internal"


def test_postgres_url_token_renders_with_view_host():
    defn = load_service("postgres")
    r = resolve("postgres", env={"SLT_STRIIM_VIEW_HOST": "host.docker.internal"},
                started=set(), compose_up=lambda defn: None, post_up=lambda defn: None)
    tokens = build_service_tokens(defn, r, schema="")
    assert tokens["PG_HOST"] == "host.docker.internal"
    assert tokens["PG_PORT"] == "5432"
    assert tokens["PG_SOURCE_USER"] == "qasource"
    assert tokens["PG_TARGET_USER"] == "qatarget"
    assert tokens["PG_DB"] == "sltdb"
    assert "PG_SLOT" in tokens
    assert "jdbc:postgresql://host.docker.internal:5432/sltdb" in tokens["PG_URL"]


# ---- FakeCursor and test utilities ------------------------------------------

class FakeCursor:
    # description/fetchall: DDL reads its own backend pid, and the idle-blocker watcher (design §4.4; only while
    # a DDL is still waiting after the first poll) reads pg_stat_activity through _query; an
    # empty result means "no pid to watch" / "nobody to terminate".
    description = None
    def __init__(self, log): self.log = log
    def execute(self, sql, params=None): self.log.append(sql if params is None else (sql, params))
    def fetchall(self): return []
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass

class FakeConn:
    def __init__(self, log):
        self.log = log; self.autocommit = False; self.closed = False
    def cursor(self): return FakeCursor(self.log)
    def close(self): self.closed = True

def _admin(log):
    return PgAdmin({"host": "h", "port": 5432, "dbname": "d", "source_user": "u", "source_password": "p"},
                   connect=lambda **kw: FakeConn(log))

def test_create_schema_quotes_identifier():
    log = []
    _admin(log).create_schema("slt_pg_smoke")
    assert any('CREATE SCHEMA IF NOT EXISTS "slt_pg_smoke"' in s for s in log)

def _stmts(log):
    return [s if isinstance(s, str) else s[0] for s in log]

def test_drop_schema_cascades():
    log = []
    _admin(log).drop_schema("slt_pg_smoke")
    assert any('DROP SCHEMA IF EXISTS "slt_pg_smoke" CASCADE' in s for s in _stmts(log))

def test_ddl_sets_a_lock_timeout_before_dropping():
    # Design §4.4: a blocked DROP must fail loudly, not leave the harness silent at its last line.
    log = []
    _admin(log).drop_schema("slt_pg_smoke")
    stmts = _stmts(log)
    i_set = next(i for i, s in enumerate(stmts) if s.startswith("SET lock_timeout = '60000ms'"))
    i_drop = next(i for i, s in enumerate(stmts) if s.startswith("DROP SCHEMA"))
    assert i_set < i_drop

def test_run_sql_sets_search_path_to_role_schema_first():
    log = []
    # role defaults to "source" -> the qasource schema; run_sql takes only the SQL now
    # (the schema is fixed per role, no per-call schema argument).
    _admin(log).run_sql("CREATE TABLE hello (id varchar);")
    assert 'search_path' in log[0].lower() and "qasource" in log[0]
    assert any("CREATE TABLE hello" in s for s in log)

_FULL_DSN = {"host": "h", "port": 5432, "dbname": "d",
             "admin_user": "postgres", "admin_password": "striim",
             "source_user": "qasource", "source_password": "striim", "source_schema": "qasource",
             "target_user": "qatarget", "target_password": "striim", "target_schema": "qatarget"}

def test_ensure_setup_creates_both_roles_and_schemas():
    log = []
    PgAdmin(dict(_FULL_DSN), connect=lambda **kw: FakeConn(log)).ensure_setup()
    joined = " ".join(log)
    assert "CREATE ROLE qasource LOGIN REPLICATION PASSWORD 'striim'" in joined
    assert "CREATE ROLE qatarget LOGIN REPLICATION PASSWORD 'striim'" in joined
    assert 'CREATE SCHEMA IF NOT EXISTS "qasource" AUTHORIZATION qasource' in joined
    assert 'CREATE SCHEMA IF NOT EXISTS "qatarget" AUTHORIZATION qatarget' in joined

def test_ensure_setup_precreates_the_default_checkpoint_table():
    # Parallel workers share qatarget, and DatabaseWriter auto-creates the default `chkpoint` there
    # on START. Two apps starting together both try, and the loser halts (pg_type duplicate key).
    log = []
    PgAdmin(dict(_FULL_DSN), connect=lambda **kw: FakeConn(log)).ensure_setup()
    stmts = _stmts(log)
    joined = " ".join(stmts)
    create = next(i for i, s in enumerate(stmts) if s.startswith('CREATE TABLE IF NOT EXISTS "qatarget"."chkpoint"'))
    assert stmts[create] == ('CREATE TABLE IF NOT EXISTS "qatarget"."chkpoint" (id varchar(100) PRIMARY KEY, '
                             'sourceposition bytea, pendingddl numeric(1), ddl text)')
    assert joined.index('CREATE SCHEMA IF NOT EXISTS "qatarget"') < joined.index('"qatarget"."chkpoint"')
    # ALTER ... OWNER takes ACCESS EXCLUSIVE even when the owner is already right, and a previous
    # run's writer idle in a transaction on chkpoint would hang it: owner change only when wrong,
    # and both statements under the DDL lock timeout + idle-blocker watcher (design §4.4).
    owner = next(s for s in stmts if "OWNER TO qatarget" in s)
    assert "tablename = 'chkpoint') <> 'qatarget' THEN" in owner and owner.startswith("DO $$")
    i_set = max(i for i, s in enumerate(stmts[:create]) if s.startswith("SET lock_timeout"))
    assert i_set < create

def test_reset_schemas_drops_then_recreates_both():
    log = []
    PgAdmin(dict(_FULL_DSN), connect=lambda **kw: FakeConn(log)).reset_schemas()
    joined = " ".join(_stmts(log))
    assert 'DROP SCHEMA IF EXISTS "qasource" CASCADE' in joined
    assert 'DROP SCHEMA IF EXISTS "qatarget" CASCADE' in joined
    assert 'CREATE SCHEMA "qasource" AUTHORIZATION qasource' in joined
    assert 'CREATE SCHEMA "qatarget" AUTHORIZATION qatarget' in joined


def test_ensure_setup_role_create_is_concurrency_safe():
    # The role create must be atomic against a concurrent worker: unconditional CREATE ROLE
    # wrapped in EXCEPTION WHEN duplicate_object, NOT an IF NOT EXISTS(...) TOCTOU guard
    # (which two workers both pass, then both CREATE -> "role already exists", the gate bug).
    log = []
    PgAdmin(dict(_FULL_DSN), connect=lambda **kw: FakeConn(log)).ensure_setup()
    joined = " ".join(log)
    assert "EXCEPTION WHEN duplicate_object" in joined
    assert "IF NOT EXISTS (SELECT FROM pg_roles" not in joined


class _FakeCursorSel:
    def __init__(self, log, rows):
        self.log = log; self._rows = rows; self.description = [("tablename",)]; self._out = []
    def execute(self, sql, params=None):
        self.log.append((sql, params))
        # Only the pg_tables read answers rows; the backend-pid read and the idle-blocker watcher
        # (pg_stat_activity + pg_blocking_pids) answer none here.
        self._out = self._rows if "pg_tables" in sql else []
    def fetchall(self): return self._out
    def close(self): pass

class _FakeConnSel:
    def __init__(self, log, rows): self.log = log; self._rows = rows; self.autocommit = False
    def cursor(self): return _FakeCursorSel(self.log, self._rows)
    def close(self): pass


def test_reset_test_objects_drops_only_this_tests_prefixed_tables():
    # Parallel-safe per-test reset: SELECT this test's ${TID} tables (the tid VALUE carries its
    # own trailing '_' separator, so it IS the full prefix; underscores escaped so they aren't
    # LIKE wildcards), then DROP each — never a whole-schema DROP that would clobber a sibling.
    log = []
    # SELECT returns one table per schema query; assert the targeted DROP + no schema drop.
    PgAdmin(dict(_FULL_DSN), connect=lambda **kw: _FakeConnSel(log, [("postgres_diff_src",)])
            ).reset_test_objects("postgres_diff_")
    stmts = [s for s, _ in log]
    params = [p for _, p in log if p is not None]
    # parameterized SELECT against pg_tables with the escaped prefix
    assert any("pg_tables" in s and "LIKE %s ESCAPE" in s for s in stmts)
    assert any(p[1] == "postgres\\_diff\\_%" for p in params)          # underscores escaped
    assert ("qasource", "postgres\\_diff\\_%") in [tuple(p) for p in params]
    # targeted DROP of the returned table, in BOTH schemas; NEVER a whole-schema drop
    assert any('DROP TABLE IF EXISTS "qasource"."postgres_diff_src" CASCADE' in s for s in stmts)
    assert not any("DROP SCHEMA" in s for s in stmts)

def test_ensure_setup_rejects_unsafe_role_password():
    dsn = dict(_FULL_DSN, source_password="bad'; DROP--")
    with pytest.raises(ValueError):
        PgAdmin(dsn, connect=lambda **kw: FakeConn([])).ensure_setup()

def test_target_role_run_sql_uses_qatarget_schema():
    log = []
    PgAdmin(dict(_FULL_DSN), connect=lambda **kw: FakeConn(log), role="target").run_sql("CREATE TABLE t (id int);")
    assert "qatarget" in log[0] and 'search_path' in log[0].lower()

def test_bad_schema_name_rejected():
    with pytest.raises(ValueError, match="schema"):
        _admin([]).create_schema("bad-name; DROP")

class FakeCursorRes:
    def __init__(self, rows, description):
        self._rows = rows; self.description = description; self.executed = []
    def execute(self, sql): self.executed.append(sql)
    def fetchone(self): return self._rows[0]
    def fetchall(self): return self._rows
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass

class FakeConnRes:
    def __init__(self, rows, description):
        self._rows = rows; self._desc = description; self.autocommit = False
    def cursor(self): return FakeCursorRes(self._rows, self._desc)
    def close(self): pass

def test_count_rows_returns_int():
    from livetest.pgclient import PgAdmin
    admin = PgAdmin({"host":"h","port":1,"dbname":"d","source_user":"u","source_password":"p"},
                    connect=lambda **kw: FakeConnRes([(3,)], [("count",)]))
    assert admin.count_rows("slt_x.hello") == 3

def test_select_rows_returns_dicts():
    from livetest.pgclient import PgAdmin
    admin = PgAdmin({"host":"h","port":1,"dbname":"d","source_user":"u","source_password":"p"},
                    connect=lambda **kw: FakeConnRes([("1","hello")], [("id",), ("msg",)]))
    rows = admin.select_rows("slt_x.hello")
    assert rows == [{"id": "1", "msg": "hello"}]

def test_read_rejects_bad_identifier():
    from livetest.pgclient import PgAdmin
    admin = PgAdmin({"host":"h","port":1,"dbname":"d","source_user":"u","source_password":"p"},
                    connect=lambda **kw: FakeConnRes([(0,)], [("count",)]))
    with pytest.raises(ValueError):
        admin.count_rows("bad-schema.tbl; DROP")

# ---- connect retry (#9) -----------------------------------------------------

def test_open_retries_through_cold_start_then_succeeds():
    n = {"tries": 0}; log = []
    def connect(**kw):
        n["tries"] += 1
        if n["tries"] < 3:
            raise OSError("connection refused")   # cold-start transient
        return FakeConn(log)
    conn = _admin_c(connect)._open(attempts=5, delay=0)
    assert n["tries"] == 3
    assert conn.autocommit is True                # sets autocommit on the returned conn

def test_open_gives_up_after_attempts_and_reraises_last():
    def connect(**kw):
        raise OSError("still refused")
    with pytest.raises(OSError, match="still refused"):
        _admin_c(connect)._open(attempts=3, delay=0)

# ---- replication-slot drop (#8) --------------------------------------------

def test_drop_replication_slot_issues_guarded_sql():
    log = []
    _admin(log).drop_replication_slot("SLT_mytest")
    assert len(log) == 1
    assert "pg_drop_replication_slot('SLT_mytest')" in log[0]
    assert "slot_name = 'SLT_mytest'" in log[0]    # guarded: no-op when the slot is absent

def test_drop_replication_slot_rejects_unsafe_name():
    with pytest.raises(ValueError):
        _admin([]).drop_replication_slot("bad'; DROP--")


def test_drop_replication_slot_waits_for_stopped_reader_to_disconnect(monkeypatch):
    from livetest import pgclient

    class InUse(Exception):
        pgcode = "55006"

    log, tries = [], []
    admin = _admin(log)
    execute = admin._exec

    def release(statements, **kw):
        tries.append(1)
        if len(tries) < 3:
            raise InUse("slot is active")
        execute(statements, **kw)

    monkeypatch.setattr(admin, "_exec", release)
    monkeypatch.setattr(pgclient.time, "sleep", lambda delay: None)
    admin.drop_replication_slot("slt_t123456789")
    assert len(tries) == 3 and len(log) == 1


@pytest.mark.parametrize("active", [False, True])
def test_drop_replication_slot_requests_stop_only_after_in_use(monkeypatch, active):
    from livetest import pgclient

    class InUse(Exception):
        pgcode = "55006"

    calls = []
    admin = _admin([])

    def execute(*a, **kw):
        calls.append("drop")
        if active and "stop" not in calls:
            raise InUse("slot is active")

    def stop(remaining_s):
        assert 0 < remaining_s <= 10.0
        calls.append("stop")

    monkeypatch.setattr(admin, "_exec", execute)
    monkeypatch.setattr(pgclient.time, "sleep", lambda delay: None)
    admin.drop_replication_slot("slt_t123456789", on_active=stop)
    assert calls == (["drop", "stop", "drop"] if active else ["drop"])


def test_drop_replication_slot_stop_uses_existing_retry_window(monkeypatch):
    from livetest import pgclient

    class InUse(Exception):
        pgcode = "55006"

    now, calls = [0.0], []
    admin = _admin([])

    def execute(*a, **kw):
        calls.append("drop")
        raise InUse("slot is active")

    def stop(remaining_s):
        calls.append("stop")
        now[0] += remaining_s + 1

    monkeypatch.setattr(admin, "_exec", execute)
    monkeypatch.setattr(pgclient.time, "monotonic", lambda: now[0])
    with pytest.raises(InUse):
        admin.drop_replication_slot("slt_t123456789", on_active=stop)
    assert calls == ["drop", "stop"]


@pytest.mark.parametrize("code", ["55006", "42501"])
def test_drop_replication_slot_reports_timeout_or_permission_error(monkeypatch, code):
    from livetest import pgclient

    class Failure(Exception):
        pgcode = code

    clock = iter([0.0, 11.0])
    admin = _admin([])
    monkeypatch.setattr(pgclient.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(admin, "_exec", lambda *a, **kw: (_ for _ in ()).throw(Failure("refused")))
    with pytest.raises(Failure, match="refused"):
        admin.drop_replication_slot("slt_t123456789")


@pytest.mark.parametrize("code", ["55006", "42704"])
def test_stale_slot_sweep_continues_if_candidate_became_active_or_disappeared(monkeypatch, code):
    class Changed(Exception):
        pgcode = code

    admin = _admin([])
    queries, logged = [], []

    def query(sql, params, which):
        assert which == "admin" and "active = false" in sql
        queries.append(params)
        if len(queries) == 1:
            return [], [("slt_t123456789",), ("slt_tabcdef012",)]
        if params[0] == "slt_t123456789":
            raise Changed("candidate changed")
        return [], [(params[0], None)]

    monkeypatch.setattr(admin, "_query", query)
    admin.sweep_stale_replication_slots(logged.append)
    assert len(queries) == 3
    assert logged == ["postgres: dropped stale replication slot slt_tabcdef012"]

# ---- jsonb column canonicalization (json target, no TO_STRING) --------------

def test_select_rows_canonicalizes_jsonb_dict_and_array():
    # psycopg2 parses a jsonb column to a dict/list; select_rows renders it as canonical
    # JSON text (sorted keys, compact) so a jsonb target compares key-order-independently.
    a = PgAdmin({"host":"h","port":1,"dbname":"d","source_user":"u","source_password":"p"},
                connect=lambda **kw: FakeConnRes([({"user_name":"C","id":"1"}, [3,1,2])],
                                                 [("result",), ("arr",)]))
    rows = a.select_rows("slt_x.out")
    assert rows == [{"result": '{"id":"1","user_name":"C"}', "arr": "[3,1,2]"}]

def _admin_c(connect):
    return PgAdmin({"host": "h", "port": 5432, "dbname": "d", "source_user": "u", "source_password": "p"},
                   connect=connect)


# --- wal2json must be an ALLOWED output plugin --------------------------------------------
# Added 2026-08-19. output_plugin_libraries is an allow-list added as a security hardening in a
# recent postgres minor; it defaults to "pgoutput, test_decoding". `FROM postgres:16` is
# unpinned, so a base-image pull moved that default under us and PostgreSQLReader's slot
# creation started failing with `library "wal2json" may not be used as an output plugin` --
# with the .so installed and loadable the whole time. Nothing in the repo had changed.

def test_compose_allows_wal2json_as_an_output_plugin():
    from pathlib import Path
    compose = (Path(__file__).resolve().parents[1] / "services" / "postgres" / "compose.yaml").read_text()
    setting = next((line for line in compose.splitlines()
                    if "output_plugin_libraries=" in line), None)
    assert setting, ("services/postgres/compose.yaml must set output_plugin_libraries: its "
                     "default excludes wal2json, so PostgreSQLReader cannot create its slot")
    value = setting.split("output_plugin_libraries=", 1)[1].strip().strip('"],\' ')
    plugins = {p.strip() for p in value.split(",")}
    assert "wal2json" in plugins, f"wal2json missing from output_plugin_libraries: {value!r}"
    # The defaults must survive: replacing the list would disallow pgoutput, which every
    # non-wal2json logical slot uses.
    assert {"pgoutput", "test_decoding"} <= plugins, (
        f"output_plugin_libraries must keep the built-in defaults, got {sorted(plugins)}")


# ---- Design §4.4: idle-in-transaction blockers are terminated while DDL waits; active ones fail loudly ----

class _FakeCursorF17:
    """A DDL that BLOCKS until the planted blocker is terminated (or raises a lock timeout
    when `block_forever`); the watcher's pg_blocking_pids query answers `blockers`."""
    def __init__(self, state):
        self.state = state; self.description = None; self._out = []
    def execute(self, sql, params=None):
        self.state["log"].append((sql, params))
        self._out = []
        if sql == "SELECT pg_backend_pid()":
            self._out = [(999,)]
        elif "pg_blocking_pids" in sql:
            assert params == (999,)
            self._out = list(self.state["blockers"])
        elif "pg_terminate_backend" in sql:
            self.state["terminated"].append(params[0])
            self.state["blockers"] = [r for r in self.state["blockers"] if r[0] != params[0]]
        elif "FROM pg_stat_activity WHERE datname" in sql:
            self._out = [(4242, "qatarget", "PostgreSQL JDBC Driver", "active", 77, "INSERT INTO ...")]
        elif sql.startswith("DROP"):
            import time
            deadline = time.monotonic() + 3.0
            while self.state["blockers"] and time.monotonic() < deadline:
                time.sleep(0.05)               # "waiting on the lock"
            if self.state["blockers"] or self.state.get("block_forever"):
                class LockNotAvailable(Exception):
                    pgcode = "55P03"
                raise LockNotAvailable("canceling statement due to lock timeout")
    def fetchall(self): return self._out
    def close(self): pass

class _FakeConnF17:
    def __init__(self, state): self.state = state; self.autocommit = False
    def cursor(self): return _FakeCursorF17(self.state)
    def close(self): pass

def _f17_admin(state):
    a = PgAdmin(dict(_FULL_DSN), connect=lambda **kw: _FakeConnF17(state))
    a._BLOCKER_POLL_S = 0.05
    return a

def test_blocked_reset_terminates_its_idle_in_transaction_blocker_and_reports_it():
    state = {"log": [], "terminated": [],
             "blockers": [(31, "qatarget", "PostgreSQL JDBC Driver", "idle in transaction", 1247, 1250,
                           "INSERT INTO qatarget.copy4 ...")]}
    admin = _f17_admin(state)
    terminated = admin.reset_schemas()
    assert state["terminated"] == [31]
    assert [r["pid"] for r in terminated] == [31]
    assert terminated[0]["idle_s"] == 1247 and terminated[0]["usename"] == "qatarget"
    assert admin.terminated == terminated
    stmts = [s for s, _ in state["log"]]
    assert stmts[0].startswith("SET lock_timeout = '60000ms'")
    # the DROP was issued (and waited) BEFORE the terminate: the watcher acts on a real block
    i_drop = next(i for i, s in enumerate(stmts) if s.startswith("DROP SCHEMA"))
    i_kill = next(i for i, s in enumerate(stmts) if "pg_terminate_backend" in s)
    assert i_drop < i_kill
    # ... and the CREATE that follows ran normally
    assert any(s.startswith('CREATE SCHEMA "qasource"') for s in stmts)

def test_watcher_query_only_ever_names_idle_in_transaction_blockers_of_this_ddl():
    state = {"log": [], "terminated": [], "blockers": []}
    _f17_admin(state).drop_schema("slt_x")
    # No block -> the DROP returned inside the first poll and the watcher never ran ...
    assert not any("pg_blocking_pids" in s for s, _ in state["log"])
    # ... but when it does run, the guard is in the SQL, not in Python.
    state = {"log": [], "terminated": [],
             "blockers": [(7, "qatarget", "x", "idle in transaction", 1, 1, "q")]}
    _f17_admin(state).drop_schema("slt_x")
    sql = next(s for s, _ in state["log"] if "pg_blocking_pids" in s)
    assert "a.pid = ANY(pg_blocking_pids(%s))" in sql
    assert "a.state LIKE 'idle in transaction%%'" in sql

def test_an_active_blocker_is_left_alone_and_the_ddl_fails_naming_it():
    state = {"log": [], "terminated": [], "blockers": [], "block_forever": True}
    with pytest.raises(RuntimeError) as ei:
        _f17_admin(state).drop_schema("slt_x")
    msg = str(ei.value)
    assert "waited 60s for a lock" in msg and "DROP SCHEMA" in msg
    assert "pid 4242 qatarget" in msg and "active" in msg and "§4.4" in msg
    # (The "left alone" guard is the watcher's SQL, asserted above; this test covers the
    # timeout-message path.)

def test_reset_test_objects_reports_what_its_drops_terminated():
    state = {"log": [], "terminated": [],
             "blockers": [(5, "qatarget", "x", "idle in transaction", 9, 9, "q")]}
    class _Cur(_FakeCursorF17):
        def execute(self, sql, params=None):
            super().execute(sql, params)
            if "pg_tables" in sql:
                self._out = [("mytest_copy1",)]
    class _Conn(_FakeConnF17):
        def cursor(self): return _Cur(self.state)
    a = PgAdmin(dict(_FULL_DSN), connect=lambda **kw: _Conn(state)); a._BLOCKER_POLL_S = 0.05
    terminated = a.reset_test_objects("mytest_")
    assert [r["pid"] for r in terminated] == [5]
    assert any(s.startswith('DROP TABLE IF EXISTS "qasource"."mytest_copy1"') for s, _ in state["log"])
