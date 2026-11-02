"""The postgres-admin route: only its statements run, as the superuser, and teardown resets them."""
import pytest

from livetest import pgserveradmin as psa


class FakePg:
    def __init__(self, waiters=None, standby="", written=None):
        self.ran = []
        self.waiters = list(waiters or [])
        self.standby = standby
        self.written = written

    def _exec(self, statements, which=None, **kw):
        assert which == "admin"
        assert statements[0] == "SET synchronous_commit = local", "every route commit is local"
        self.ran.extend(statements[1:])

    def _query(self, sql, params=None, which=None):
        assert which == "admin"
        self.ran.append(sql)
        if sql.startswith("SHOW"):
            return ["synchronous_standby_names"], [(self.standby,)]
        if "pg_file_settings" in sql:
            return ["setting"], [(self.written,)] if self.written else []
        if "pg_terminate_backend" in sql:
            return ["pg_terminate_backend"], [(True,)] if (self.waiters and self.waiters.pop(0)) else []
        return [], []


SET = "ALTER SYSTEM SET synchronous_standby_names = 'slt_no_standby'"
TERMINATE = ("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
             "WHERE usename = 'qatarget' AND wait_event = 'SyncRep'")


def test_accepted_statements_run_as_the_superuser():
    pg = FakePg()
    admin = psa.PgServerAdmin(pg, parallel=False)
    admin.run_sql(f"-- in-doubt\nALTER ROLE qasource SET synchronous_commit = local;\n{SET};\n"
                  "SELECT pg_reload_conf();")
    assert pg.ran == ["ALTER ROLE qasource SET synchronous_commit = local", SET, "SELECT pg_reload_conf()"]
    assert admin.touched


@pytest.mark.parametrize("sql", [
    "ALTER SYSTEM SET synchronous_standby_names = 'other'",
    "ALTER SYSTEM SET fsync = off",
    "DROP TABLE qatarget.t",
    "ALTER ROLE postgres SET synchronous_commit = local",
    "ALTER SYSTEM SET synchronous_standby_names = 'SLT_NO_STANDBY'",
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity",
    f"{SET}; DROP ROLE qatarget",
])
def test_anything_else_is_refused_before_a_connection(sql):
    pg = FakePg()
    with pytest.raises(psa.PgAdminRouteError, match="postgres-admin-unsupported"):
        psa.PgServerAdmin(pg, parallel=False).run_sql(sql)
    assert pg.ran == []


def test_refused_in_a_parallel_run():
    pg = FakePg()
    with pytest.raises(psa.PgAdminRouteError, match="postgres-admin-parallel"):
        psa.PgServerAdmin(pg, parallel=True).run_sql(SET)
    assert pg.ran == []


def test_terminate_waits_for_a_syncrep_waiter():
    pg = FakePg(waiters=[False, False, True])
    psa.PgServerAdmin(pg, parallel=False, sleep=lambda s: None).run_sql(TERMINATE)
    assert pg.ran.count(TERMINATE) == 3


def test_terminate_fails_when_no_backend_waits():
    now = [0.0]
    pg = FakePg()
    admin = psa.PgServerAdmin(pg, parallel=False, clock=lambda: now[0],
                              sleep=lambda s: now.__setitem__(0, now[0] + s))
    with pytest.raises(psa.PgAdminRouteError, match="postgres-admin-no-waiter"):
        admin.run_sql(TERMINATE, timeout=2)


def test_reset_only_after_use():
    pg = FakePg()
    admin = psa.PgServerAdmin(pg, parallel=False)
    admin.reset()
    assert pg.ran == []
    admin.run_sql(SET)
    admin.reset()
    assert pg.ran[1:] == psa.RESET and not admin.touched


def test_a_semicolon_in_a_comment_splits_nothing():
    pg = FakePg()
    psa.PgServerAdmin(pg, parallel=False).run_sql(
        "-- the source commits locally; the target waits\n"
        "ALTER ROLE qasource SET synchronous_commit = local; -- first; then the standby\n"
        f"{SET};\nSELECT pg_reload_conf();")
    assert pg.ran == ["ALTER ROLE qasource SET synchronous_commit = local", SET, "SELECT pg_reload_conf()"]


def test_reset_reloads_before_the_role_resets():
    assert psa.RESET.index("SELECT pg_reload_conf()") < psa.RESET.index("ALTER ROLE qasource RESET synchronous_commit")


def test_clear_stale_resets_only_the_sentinel():
    assert not psa.clear_stale(FakePg(standby=""))
    pg = FakePg(standby=psa.SENTINEL)
    assert psa.clear_stale(pg)
    assert pg.ran[2:] == psa.RESET
    written = FakePg(standby="", written=psa.SENTINEL)   # set, not yet reloaded
    assert psa.clear_stale(written)
