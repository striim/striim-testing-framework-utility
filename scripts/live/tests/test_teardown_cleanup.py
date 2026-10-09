"""Post-test resource cleanup (plugin teardown helpers): the tables a test's ddl:
created and its FileWriter output files must be removed once the test finishes
(unless the keep-flags say otherwise — that gating lives in should_keep_resources,
tested in test_plugin_routing)."""

from livetest.plugin import teardown_ddl_tables, teardown_file_outputs

class _Admin:
    def __init__(self, fail=False):
        self.dropped = []
        self.fail = fail
    def drop_test_tables(self, prefix=""):
        if self.fail:
            raise RuntimeError("boom")
        self.dropped.append(prefix)

class _NoDrop:
    pass   # e.g. PgAdmin/GcsAdmin/KafkaAdmin — cleaned elsewhere in teardown

def test_serial_drops_whole_schema_once_per_route():
    ora, ms = _Admin(), _Admin()
    admins = {"oracle-source": {"admin": ora}, "mssql-target": {"admin": ms}}
    ddl = [("oracle-source", "a.sql"), ("oracle-source", "b.sql"), ("mssql-target", "c.sql")]
    teardown_ddl_tables(admins, ddl, {"TID": "t1_", "TID_ORACLE": "T1_"}, parallel=False)
    assert ora.dropped == [""] and ms.dropped == [""]   # "" => everything; one call per route

def test_serial_drops_both_mysql_routes():
    # mysql-source and mysql-target are separate MySQLAdmin instances bound to the
    # qasource/qatarget schemas, so each route drops in its own schema.
    src, tgt = _Admin(), _Admin()
    admins = {"mysql-source": {"admin": src}, "mysql-target": {"admin": tgt}}
    ddl = [("mysql-source", "ddl_source.sql"), ("mysql-target", "ddl_target.sql")]
    teardown_ddl_tables(admins, ddl, {"TID": "t1_"}, parallel=False)
    assert src.dropped == [""] and tgt.dropped == [""]

def test_parallel_uses_each_services_per_test_prefix():
    ora, sp, ms = _Admin(), _Admin(), _Admin()
    admins = {"oracle-target": {"admin": ora}, "spanner-google": {"admin": sp},
              "mssql-source": {"admin": ms}}
    ddl = [("oracle-target", "a.sql"), ("spanner-google", "s.sql"), ("mssql-source", "m.sql")]
    tokens = {"TID": "tabc_", "TID_UPPER": "TABC_", "TID_ORACLE": "TABC_"}
    teardown_ddl_tables(admins, ddl, tokens, parallel=True)
    assert ora.dropped == ["TABC_"]   # oracle keys on ${TID_ORACLE}
    assert sp.dropped == ["tabc_"]    # spanner tables are ${TID}-prefixed
    assert ms.dropped == ["tabc_"]    # mssql tables are ${TID}-prefixed

def test_parallel_mysql_uses_the_plain_tid_prefix():
    # mysql is absent from _DDL_TEARDOWN_PREFIX_TOKENS on purpose: plain ${TID} is its
    # prefix token, so it must come through the .get() default rather than be skipped.
    src, tgt = _Admin(), _Admin()
    admins = {"mysql-source": {"admin": src}, "mysql-target": {"admin": tgt}}
    ddl = [("mysql-source", "ddl_source.sql"), ("mysql-target", "ddl_target.sql")]
    teardown_ddl_tables(admins, ddl, {"TID": "tabc_", "TID_ORACLE": "TABC_"}, parallel=True)
    assert src.dropped == ["tabc_"] and tgt.dropped == ["tabc_"]

def test_vertica_routes_drop_in_their_own_schema_with_the_plain_tid():
    # Like mysql: two VerticaAdmin routes, one per fixed schema, keyed on plain ${TID}
    # through the .get() default; the admin route is a no-op in VerticaAdmin itself.
    for parallel, want in ((False, ""), (True, "tabc_")):
        src, tgt = _Admin(), _Admin()
        admins = {"vertica-source": {"admin": src}, "vertica-target": {"admin": tgt}}
        ddl = [("vertica-source", "ddl_source.sql"), ("vertica-target", "ddl_target.sql")]
        teardown_ddl_tables(admins, ddl, {"TID": "tabc_", "TID_ORACLE": "TABC_"}, parallel=parallel)
        assert src.dropped == [want] and tgt.dropped == [want]

def test_skips_unresolved_routes_and_admins_without_drop():
    # postgres (no drop_test_tables — pg teardown resets its schemas) and a route whose
    # service never resolved must both be skipped silently.
    admins = {"postgres-source": {"admin": _NoDrop()}}
    teardown_ddl_tables(admins, [("postgres-source", "d.sql"), ("oracle-source", "o.sql")],
                        {}, parallel=False)   # must not raise

def test_a_failed_drop_warns_but_never_raises(capsys):
    admins = {"oracle-source": {"admin": _Admin(fail=True)}}
    teardown_ddl_tables(admins, [("oracle-source", "a.sql")], {}, parallel=False)
    assert "WARNING" in capsys.readouterr().out

def test_file_outputs_cleared_per_rendered_path():
    cleared = []
    specs = [{"path": "/tmp/${NS}-out", "min_events": 1},
             {"path": "/tmp/${NS}-out2", "match": "expected/e.csv"}]
    teardown_file_outputs(specs, {"NS": "SLT_x"}, cleared.append)
    assert cleared == ["/tmp/SLT_x-out", "/tmp/SLT_x-out2"]

def test_file_outputs_noop_without_spec_and_swallows_clear_failures():
    teardown_file_outputs(None, {}, lambda p: 1 / 0)   # no spec -> nothing to do
    def boom(p):
        raise RuntimeError("rm failed")
    # a clear failure must not raise (best-effort teardown)
    teardown_file_outputs([{"path": "/tmp/x", "min_events": 1}], {}, boom)

def test_malformed_file_spec_is_ignored_at_teardown():
    # parse_file_specs would raise on this; the assertion phase already failed loudly,
    # teardown must not raise again.
    teardown_file_outputs([{"min_events": 1}], {}, lambda p: None)

def test_mssql_admin_route_never_logs_in_at_teardown():
    from livetest.plugin import teardown_unowned
    from livetest.mssqladmin import MssqlAdmin
    logins = []

    def connect(**kw):
        logins.append(kw["user"])
        raise AssertionError("teardown must not log in on mssql-admin")

    dsn = {"host": "h", "port": 1433, "user": "sa", "password": "striim", "database": "qauser"}
    admins = {"mssql-admin": {"admin": MssqlAdmin(dsn, connect=connect, role="admin")}}
    ddl = [("mssql-admin", "x.sql")]
    teardown_ddl_tables(admins, ddl, {"TID": "t1_"}, parallel=False)
    teardown_ddl_tables(admins, ddl, {"TID": "t1_"}, parallel=True)
    out = teardown_unowned(admins, ddl, {"TID": "t1_"}, [], [], None, lambda p: None, [], lambda n: None)
    assert logins == [] and out["failed"] == {}

def test_teradata_admin_route_never_logs_in_at_teardown():
    from livetest.plugin import teardown_unowned
    from livetest.teradataadmin import TeradataAdmin
    logins = []

    def connect(**kw):
        logins.append(kw["user"])
        raise AssertionError("teardown must not log in on teradata-admin")

    dsn = {"host": "h", "port": 1025, "user": "dbc", "password": "dbc"}
    admins = {"teradata-admin": {"admin": TeradataAdmin(dsn, connect=connect, role="admin")}}
    ddl = [("teradata-admin", "x.sql")]
    teardown_ddl_tables(admins, ddl, {"TID": "t1_"}, parallel=False)
    teardown_ddl_tables(admins, ddl, {"TID": "t1_"}, parallel=True)
    out = teardown_unowned(admins, ddl, {"TID": "t1_"}, [], [], None, lambda p: None, [], lambda n: None)
    assert logins == [] and out["failed"] == {}
