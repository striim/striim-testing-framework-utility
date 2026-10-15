"""Oracle observer teardown runs through the real plugin, even on a failed case."""
import pytest
from .exec_harness import run_case  # noqa: F401

pytest_plugins = ["pytester"]


@pytest.mark.parametrize("assert_fail,cleanup_fail,keep", [
    (True, False, False), (True, True, False), (False, True, False), (True, False, True), (True, False, "on-error")])
def test_observer_cleanup_after_failure_and_with_resource_retention(run_case, assert_fail, cleanup_fail, keep):
    def prepare(root, name):
        case = root / "cases/legacy"
        (case / "observer.sql").write_text("CREATE TRIGGER ${TID_ORACLE}LOGON AFTER LOGON ON DATABASE BEGIN NULL; END;\n/\n")
        (case / "view.sql").write_text("CREATE VIEW ${TID_ORACLE}SESS AS SELECT 1 id FROM dual;")
        with (root / "conftest.py").open("a") as f:
            f.write(r'''
_resolve = I.resolve_service
def resolve_oracle(*a, **kw):
    result = _resolve(*a, **kw)
    if result.name == 'oracle': result.base.update(admin_user='system', admin_password='striim', service='PDB')
    return result
I.resolve_service = resolve_oracle
class FakeOracle:
    def __init__(self, dsn, prefix=None):
        self.dsn, self.prefix, self.touched = dsn, prefix, False
    def run_sql(self, sql):
        self.touched = True
        log('oracle_sql', user=self.dsn['source_user'], sql=sql)
    def drop_observer_objects(self, prefix):
        log('oracle_observer_drop', user=self.dsn['source_user'], prefix=prefix)
        if SC.get('observer_drop_fail') and self.dsn['source_user'] == 'system':
            raise RuntimeError('observer drop rejected')
    def drop_test_tables(self, prefix):
        log('oracle_tables_drop', user=self.dsn['source_user'], prefix=prefix)
P.OraAdmin = FakeOracle
_original_run_cleanup = P._slt_ownership.run_cleanup
def observed_run_cleanup(*args, **kwargs):
    log('run_cleanup')
    return _original_run_cleanup(*args, **kwargs)
P._slt_ownership.run_cleanup = observed_run_cleanup
if SC.get('observer_assert_fail'):
    def smoke(*a, **k): raise AssertionError('primary assertion failed')
    P.assert_smoke = smoke
''')
    run = run_case("legacy", {"observer_assert_fail": assert_fail, "observer_drop_fail": cleanup_fail},
        prepare=prepare, env={("SLT_KEEP_RESOURCES_ON_ERROR" if keep == "on-error" else "SLT_KEEP_RESOURCES"): "1"} if keep else None,
        edit=lambda y: y.replace("requires: [postgres]", "requires: [postgres, oracle]") + """
ddl:
- file: observer.sql
  db: oracle-admin
seed:
- file: view.sql
  db: oracle-target
""")
    assert run.ret == 1, run.text
    events = run.events()
    drops = [e for e in events if e['event'] == 'oracle_observer_drop']
    assert [e['user'] for e in drops] == ['system', 'qatarget'], run.text
    assert all(e['prefix'] and e['prefix'].endswith('_') for e in drops)
    if not keep and not cleanup_fail:
        assert run.names().index('oracle_observer_drop') < run.names().index('oracle_tables_drop')
    if cleanup_fail:
        assert 'oracle_tables_drop' not in run.names(), 'unsafe table drop after observer failure'
    assert run.names().index('oracle_observer_drop') < run.names().index('run_cleanup')
    if assert_fail:
        assert 'primary assertion failed' in run.junit()[2]
    from livetest import evidence
    env = run.envelope(); evidence.validate_case(env, secrets=())
    if cleanup_fail:
        assert env['lifecycle']['cleanup']['status'] == 'failed'
        assert env['resources']['cleanupVerified'] is False
        assert any('oracle-observer' in str(g) for g in env['resources']['verificationGaps'])
        if not keep:
            assert any('engine-tables oracle-admin' in g and 'observer' in g
                       for g in env['resources']['verificationGaps'])
