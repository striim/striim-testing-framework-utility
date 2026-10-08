import re
from pathlib import Path

import pytest
import yaml

from livetest.registry import load_service
from livetest.services import resolve
from livetest.plugin import build_service_tokens
from livetest.mssqladmin import MssqlAdmin, _split_statements, _check_table

SERVICE_DIR = Path(__file__).resolve().parents[1] / "services" / "mssql"

# ---- service definition + token rendering ----------------------------------

def test_mssql_service_def_loads():
    defn = load_service("mssql")
    assert defn.isolation == "none"
    assert defn.container == "slt-mssql"
    assert defn.live_override_env == "SLT_MSSQL_HOST"
    for key in ("MSSQL_URL", "MSSQL_HOSTPORT", "MSSQL_DB", "MSSQL_SOURCE_PASSWORD", "MSSQL_TARGET_PASSWORD"):
        assert key in defn.provides

def test_mssql_resolve_docker_defaults():
    r = resolve("mssql", env={}, started=set(),
                compose_up=lambda defn: None, post_up=lambda defn: None)
    assert r.mode == "docker" and r.started is True
    assert r.base["port"] == 1433
    assert r.base["user"] == "sa"
    assert r.base["database"] == "qauser"

def test_mssql_url_token_renders_with_view_host():
    defn = load_service("mssql")
    r = resolve("mssql", env={"SLT_STRIIM_VIEW_HOST": "host.docker.internal"},
                started=set(), compose_up=lambda defn: None, post_up=lambda defn: None)
    tokens = build_service_tokens(defn, r, schema="")
    assert tokens["MSSQL_HOSTPORT"] == "host.docker.internal:1433"
    assert tokens["MSSQL_URL"] == (
        "jdbc:sqlserver://host.docker.internal:1433;databaseName=qauser;"
        "encrypt=true;trustServerCertificate=true")

# ---- statement splitting ----------------------------------------------------

def test_split_drops_comments_go_and_terminators():
    sql = """
    -- a comment
    CREATE TABLE dbo.SRC (ID VARCHAR(20) PRIMARY KEY);
    GO
    INSERT INTO dbo.SRC VALUES ('1');
    """
    assert _split_statements(sql) == [
        "CREATE TABLE dbo.SRC (ID VARCHAR(20) PRIMARY KEY)",
        "INSERT INTO dbo.SRC VALUES ('1')",
    ]

# ---- table-name guard -------------------------------------------------------

def test_check_table_defaults_to_dbo_and_brackets():
    assert _check_table("SRC") == "[dbo].[SRC]"
    assert _check_table("dbo.TGT") == "[dbo].[TGT]"

def test_check_table_rejects_injection():
    with pytest.raises(ValueError):
        _check_table("dbo.tbl; DROP TABLE x")

# ---- reads ------------------------------------------------------------------

class FakeCursor:
    def __init__(self, rows, description):
        self._rows = rows; self.description = description
    def execute(self, sql): pass
    def fetchall(self): return self._rows
    def fetchone(self): return self._rows[0]

class FakeConn:
    def __init__(self, rows, description):
        self._rows = rows; self._desc = description
    def cursor(self): return FakeCursor(self._rows, self._desc)
    def close(self): pass

def _admin(rows, desc):
    dsn = {"host": "h", "port": 1433, "user": "sa", "password": "p", "database": "qauser"}
    return MssqlAdmin(dsn, connect=lambda **kw: FakeConn(rows, desc))

def test_select_rows_str_coerces_and_keeps_none():
    admin = _admin([(1, "alpha", None)], [("ID",), ("MSG",), ("EXTRA",)])
    assert admin.select_rows("dbo.SRC") == [{"ID": "1", "MSG": "alpha", "EXTRA": None}]

def test_count_rows_returns_int():
    admin = _admin([(7,)], [("",)])
    assert admin.count_rows("dbo.TGT") == 7

# ---- _open connection lifecycle ---------------------------------------------

class FakeCursorProbe:
    def __init__(self, fail):
        self._fail = fail
    def execute(self, sql):
        if self._fail:
            raise Exception("database is not currently available")
    def fetchall(self):
        return [(1,)]

class FakeConnProbe:
    def __init__(self, fail, tracker):
        self._fail = fail
        self.closed = False
        tracker.append(self)
    def cursor(self):
        return FakeCursorProbe(self._fail)
    def close(self):
        self.closed = True

def test_open_raises_immediately_on_permanent_cannot_open_database():
    # "Cannot open database ..." is a genuine PERMANENT error (bad DB name / no rights).
    # It must NOT be classified transient (the old bare "database" substring retried it
    # for ~60s); _open must re-raise on the first attempt.
    calls = {"n": 0}
    def fake_connect(**kw):
        calls["n"] += 1
        raise Exception('Cannot open database "X" requested by the login')
    dsn = {"host": "h", "port": 1433, "user": "sa", "password": "p", "database": "qauser"}
    admin = MssqlAdmin(dsn, connect=fake_connect)
    with pytest.raises(Exception, match="Cannot open database"):
        admin._open(delay=0)
    assert calls["n"] == 1                             # raised immediately, no retry loop


def test_open_closes_connection_on_transient_probe_failure():
    created = []
    calls = {"n": 0}
    def fake_connect(**kw):
        calls["n"] += 1
        fail = calls["n"] <= 2                    # first 2 attempts: probe fails transiently
        return FakeConnProbe(fail, created)
    dsn = {"host": "h", "port": 1433, "user": "sa", "password": "p", "database": "qauser"}
    admin = MssqlAdmin(dsn, connect=fake_connect)
    conn = admin._open(delay=0)
    assert len(created) == 3
    assert conn is created[-1]
    assert conn.closed is False                   # the returned connection stays open
    for c in created[:-1]:
        assert c.closed is True                   # every failed-probe connection was closed (no leak)

# ---- entrypoint/healthcheck marker consistency ------------------------------
#
# compose.yaml's healthcheck and entrypoint.sh each hardcode the SAME marker path with no
# shared constant between them (a shell healthcheck can't import Python, and vice versa).
# A typo in either would silently produce "never healthy" or "always healthy" with no
# other signal — these tests are the tripwire. Also guards the specific bug this marker
# scheme was built to avoid recurring: a stale marker (surviving `stop`/`start`, since
# /var/opt/mssql has no volume) must be cleared at the top of every boot, or the
# healthcheck can go green before THIS boot's sa-password migration has actually run.

def _compose_healthcheck_test() -> str:
    doc = yaml.safe_load((SERVICE_DIR / "compose.yaml").read_text())
    return doc["services"]["slt-mssql"]["healthcheck"]["test"][1]

def _entrypoint_text() -> str:
    return (SERVICE_DIR / "images" / "mssql" / "entrypoint.sh").read_text()

def _entrypoint_done_marker() -> str:
    m = re.search(r'^DONE_MARKER=(\S+)$', _entrypoint_text(), re.MULTILINE)
    assert m, "entrypoint.sh must define DONE_MARKER=<path>"
    return m.group(1)

def test_healthcheck_marker_path_matches_entrypoint():
    marker = _entrypoint_done_marker()
    assert marker in _compose_healthcheck_test()

def test_entrypoint_clears_marker_before_writing_it():
    text = _entrypoint_text()
    clear_idx = text.find('rm -f "$DONE_MARKER"')
    write_idx = text.find('touch "$DONE_MARKER"')
    assert clear_idx != -1, (
        "entrypoint.sh must clear DONE_MARKER at boot -- /var/opt/mssql has no volume, so "
        "the marker survives docker stop/start (only `down -v` wipes it); leaving a stale "
        "marker in place lets the healthcheck go green before THIS boot's sa-password "
        "migration has run, reintroducing the very race this marker exists to prevent."
    )
    assert write_idx != -1, "entrypoint.sh must touch DONE_MARKER only after init.sql succeeds"
    assert clear_idx < write_idx, "the clear must happen before the write, not after"

def test_healthcheck_probes_a_login_that_cannot_race_the_sa_migration():
    # Defense in depth: the healthcheck should also verify a real login works, using an
    # account init.sql creates once under IF NOT EXISTS and never re-ALTERs on a later
    # boot (qasource/qatarget) -- unlike sa, that login can't be caught mid-ALTER LOGIN,
    # so this check stays a real liveness signal even if the marker itself is ever wrong.
    test_cmd = _compose_healthcheck_test()
    assert "-U qasource" in test_cmd or "-U qatarget" in test_cmd
    assert "-U sa" not in test_cmd
