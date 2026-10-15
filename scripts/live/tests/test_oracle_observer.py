"""Oracle observer routes, connection identity and failure-safe object cleanup."""
from pathlib import Path
from types import SimpleNamespace
import re
import pytest
from livetest import oraadmin

ROOT = Path(__file__).resolve().parents[3]
BASE = {'host':'h','port':1521,'service':'PDB',
        'source_user':'source','source_password':'source-secret',
        'target_user':'target','target_password':'target-secret',
        'admin_user':'SYSTEM','admin_password':'admin-secret'}

def test_probe_program_is_supplied_during_login(monkeypatch):
    import oracledb
    calls = []
    monkeypatch.setattr(oracledb,'connect',lambda **kwargs: calls.append(kwargs) or object())
    oraadmin._default_connect(host='h',port=1521,service='PDB',user='target',password='secret')
    assert calls[0]['program'] == 'striim-test-observer'

def test_oracle_admin_route_has_independent_credentials_and_pdb():
    from livetest.plugin import _oracle_admins
    admins = _oracle_admins(BASE,'TCASE_')
    assert set(admins) == {'oracle-source','oracle-target','oracle-admin'}
    assert admins['oracle-admin']['admin'].dsn['source_user'] == 'SYSTEM'
    assert admins['oracle-admin']['admin'].dsn['source_password'] == 'admin-secret'
    assert admins['oracle-admin']['admin'].dsn['service'] == 'PDB'
    assert admins['oracle-target']['admin'].dsn['source_user'] == 'target'

def test_image_grants_target_observer_privileges_directly_in_pdb():
    sql = (ROOT/'scripts/live/services/oracle/images/oracle/bake.sql').read_text()
    pdb = sql.split('ALTER SESSION SET CONTAINER=FREEPDB1;',1)[1]
    assert re.search(r'GRANT\s+CREATE VIEW\s+TO\s+qatarget\s*;',pdb,re.I)
    assert re.search(r'GRANT\s+SELECT\s+ON\s+SYS\.V_\$SESSION\s+TO\s+qatarget\s*;',pdb,re.I)
    assert re.search(r'GRANT\s+SELECT\s+ON\s+SYS\.V_\$DATABASE\s+TO\s+qatarget\s*;',pdb,re.I)

def test_trigger_script_preserves_inner_semicolons_and_next_sql():
    sql = "CREATE VIEW TCASE_V AS SELECT 1 id FROM dual;\nCREATE OR REPLACE TRIGGER TCASE_TR AFTER LOGON ON DATABASE\nBEGIN\n IF ora_login_user = 'TARGET' THEN\n  INSERT INTO target.TCASE_LOG VALUES ('a;b');\n END IF;\nEND;\n/\nINSERT INTO target.TCASE_LOG VALUES ('after');"
    statements = oraadmin._split_statements(sql)
    assert len(statements) == 3
    assert statements[1].startswith('CREATE OR REPLACE TRIGGER')
    assert "VALUES ('a;b');" in statements[1] and 'END IF;' in statements[1]
    assert statements[1].rstrip().endswith('END;') and not statements[1].rstrip().endswith('/')
    assert statements[2].startswith('INSERT')

def test_cleanup_views_and_triggers_is_scoped_verified_and_leaves_tables():
    state = {'objects':{('TCASE_TR','TRIGGER'),('TCASE_V','VIEW'),('OTHER_TR','TRIGGER'),('TCASE_T','TABLE')},'calls':[]}
    class Cursor:
        def execute(self,sql):
            state['calls'].append(sql)
            match = re.fullmatch(r'DROP (VIEW|TRIGGER) "([^"]+)"',sql)
            assert match
            state['objects'].discard((match[2],match[1]))
        def close(self): pass
    class Connection:
        def cursor(self): return Cursor()
        def close(self): pass
    admin = oraadmin.OraAdmin(BASE)
    admin._open = lambda: Connection()
    admin._query = lambda sql: (['OBJECT_NAME','OBJECT_TYPE'],list(state['objects']))
    admin.drop_observer_objects('TCASE_')
    assert state['objects'] == {('OTHER_TR','TRIGGER'),('TCASE_T','TABLE')}
    assert state['calls'][0] == 'DROP TRIGGER "TCASE_TR"'

def test_observer_cleanup_does_not_swallow_leftover_objects():
    admin = oraadmin.OraAdmin(BASE)
    admin._query = lambda sql: (['OBJECT_NAME','OBJECT_TYPE'],[('TCASE_V','VIEW')])
    class Cursor:
        def execute(self,sql): raise RuntimeError('drop rejected')
        def close(self): pass
    admin._open = lambda: SimpleNamespace(cursor=lambda:Cursor(),close=lambda:None)
    with pytest.raises(Exception,match='oracle-observer-cleanup-failed'):
        admin.drop_observer_objects('TCASE_')

def test_observer_cleanup_refuses_empty_scope_before_connection():
    admin = oraadmin.OraAdmin(BASE)
    admin._open = lambda: pytest.fail('must not connect')
    with pytest.raises(ValueError,match='oracle-observer-cleanup-scope'):
        admin.drop_observer_objects('')

def test_seed_only_admin_observer_cleanup_runs_first_even_after_a_failure():
    from livetest.plugin import teardown_oracle_observers
    order = []
    class Admin:
        def __init__(self,name,fail=False): self.name,self.fail,self.touched = name,fail,True
        def drop_observer_objects(self,prefix):
            order.append((self.name,prefix))
            if self.fail: raise RuntimeError('rejected')
    admins = {'oracle-target':{'admin':Admin('target')},'oracle-admin':{'admin':Admin('admin',True)}}
    result = teardown_oracle_observers(admins,{'TID_ORACLE':'TCASE_'})
    assert order == [('admin','TCASE_'),('target','TCASE_')]
    assert result['failed'] and 'oracle-admin' in next(iter(result['failed']))

def test_unused_admin_observer_cleanup_never_opens_admin_connection():
    from livetest.plugin import teardown_oracle_observers
    class Admin:
        touched = False
        def drop_observer_objects(self,prefix): pytest.fail('unused admin must not connect')
    assert not teardown_oracle_observers({'oracle-admin':{'admin':Admin()}},{'TID_ORACLE':'TCASE_'})['attempted']


@pytest.mark.parametrize("sql", [
    "CREATE VIEW OTHER_V AS SELECT 1 id FROM dual;",
    "CREATE OR REPLACE TRIGGER foreign.TCASE_TR AFTER LOGON ON DATABASE BEGIN NULL; END;\n/",
    "CREATE VIEW TARGET.OTHER_V AS SELECT 1 id FROM dual;",
    'CREATE VIEW "TCASE_bad name" AS SELECT 1 id FROM dual;',
    'CREATE VIEW "TCASE_""OTHER" AS SELECT 1 id FROM dual;',
    'CREATE VIEW "TCASE_""" AS SELECT 1 id FROM dual;',
    'CREATE VIEW "SYSTEM.TCASE_V" AS SELECT 1 id FROM dual;',
    "CREATE FORCE VIEW OTHER_V AS SELECT 1 id FROM dual;",
    "CREATE NOFORCE VIEW OTHER_V AS SELECT 1 id FROM dual;",
    'CREATE VIEW "TCASE_bad\nname" AS SELECT 1 id FROM dual;',
])
def test_foreign_observer_ddl_is_refused_before_connect(sql):
    admin = oraadmin.OraAdmin(BASE, prefix="TCASE_")
    admin._open = lambda: pytest.fail("foreign observer DDL must not connect")
    with pytest.raises(ValueError, match="oracle-observer-ddl-scope"):
        admin.run_sql(sql)


def test_failed_observer_ddl_is_still_marked_for_teardown():
    admin = oraadmin.OraAdmin(BASE, prefix="TCASE_")
    admin._open = lambda: (_ for _ in ()).throw(RuntimeError("open failed"))
    with pytest.raises(RuntimeError, match="open failed"):
        admin.run_sql("CREATE VIEW TCASE_V AS SELECT 1 id FROM dual;")
    assert admin.touched is True


def test_trigger_without_slash_terminator_fails_before_execution():
    with pytest.raises(ValueError, match="oracle-plsql-terminator"):
        oraadmin._split_statements("CREATE TRIGGER TCASE_TR AFTER LOGON ON DATABASE BEGIN NULL; END;")


def test_program_parameter_minimum_is_declared():
    for name in ("pyproject.toml", "scripts/live/pyproject.toml", "scripts/live/requirements.txt", "requirements-test.in"):
        assert "oracledb>=2.5" in (ROOT / name).read_text()


def test_direct_grants_cover_both_data_users_and_the_trigger_owner():
    sql = (ROOT / 'scripts/live/services/oracle/images/oracle/bake.sql').read_text().split(
        'ALTER SESSION SET CONTAINER=FREEPDB1;', 1)[1]
    for user in ('qasource', 'qatarget'):
        assert re.search(r'GRANT CREATE VIEW TO ' + user + r'\s*;', sql, re.I)
        for view in ('SESSION', 'DATABASE'):
            assert re.search(r'GRANT SELECT ON SYS\.V_\$' + view + r' TO ' + user + r'\s*;', sql, re.I)
    assert re.search(r'GRANT CREATE TRIGGER, ADMINISTER DATABASE TRIGGER TO SYSTEM\s*;', sql, re.I)
    assert re.search(r'GRANT SELECT ON SYS\.V_\$SESSION TO SYSTEM\s*;', sql, re.I)


def test_documented_observer_scripts_keep_the_trigger_body_and_case_scope():
    text = (ROOT / 'docs/SERVICES.md').read_text().split('### Oracle observers', 1)[1].split('## SQL Server', 1)[0]
    blocks = re.findall(r'```sql\n(.*?)```', text, re.S)
    assert len(blocks) == 2
    replacements = {'TID_ORACLE': 'TCASE_', 'ORACLE_TARGET_USER': 'qatarget',
                    'ORACLE_TARGET_SCHEMA': 'QATARGET', 'ORACLE_ADMIN_USER': 'SYSTEM'}
    for block, user, expected in zip(blocks, ('qatarget', 'SYSTEM'), (3, 1)):
        for token, value in replacements.items(): block = block.replace('${' + token + '}', value)
        sent = []
        class Cursor:
            def execute(self, sql): sent.append(sql)
            def close(self): pass
        admin = oraadmin.OraAdmin({**BASE, 'source_user': user}, prefix='TCASE_')
        admin._open = lambda: SimpleNamespace(cursor=Cursor, close=lambda: None)
        admin.run_sql(block)
        assert len(sent) == expected
        assert admin.touched
    assert 'END IF;' in sent[0] and sent[0].endswith('END;')


def test_observer_regression_exercises_routes_and_filtered_writer_logons():
    from livetest.manifest import load_manifest
    root = ROOT / 'scripts/live/regression/services/oracle/oracle-observer'
    manifest = load_manifest(root / 'test.yaml')
    assert manifest.requires == ['oracle']
    assert [db for db, _ in manifest.ddl_files] == ['oracle-source', 'oracle-target', 'oracle-admin']
    checks = manifest.assert_['data']
    assert any(x['target'].endswith('LOGONS') and x['min_rows'] == 1 for x in checks)
    assert any(x['target'].endswith('PROBES') and x['rows'] == 0 for x in checks)
    target = (root / 'ddl_target.sql').read_text()
    trigger = (root / 'observer_trigger.sql').read_text()
    assert 'SYS.V_$SESSION' in target
    assert 'GRANT INSERT' in target and '${ORACLE_ADMIN_USER}' in target
    assert "<> 'striim-test-observer'" in trigger and trigger.rstrip().endswith('/')


@pytest.mark.parametrize("modifiers", [
    "OR REPLACE FORCE EDITIONABLE", "EDITIONING", "FORCE EDITIONING",
    "NOFORCE EDITIONABLE EDITIONING", "NONEDITIONABLE", "FORCE NONEDITIONABLE",
])
@pytest.mark.parametrize("name", ["OTHER_V", "foreign.TCASE_V"])
def test_all_view_grammar_is_scoped_before_connection(modifiers, name):
    admin = oraadmin.OraAdmin(BASE, prefix="TCASE_")
    admin._open = lambda: pytest.fail("foreign view must not connect")
    with pytest.raises(ValueError, match="oracle-observer-ddl-scope"):
        admin.run_sql(f"CREATE {modifiers} VIEW {name} AS SELECT 1 id FROM dual;")


@pytest.mark.parametrize("kind,body", [
    ("TRIGGER", "AFTER LOGON ON DATABASE BEGIN NULL; END;\n/"),
    ("VIEW", "AS SELECT missing_column FROM dual;"),
])
def test_compilation_warning_drops_just_created_object_and_raises(kind, body):
    calls, closed = [], []
    class Cursor:
        warning = None
        def execute(self, sql):
            calls.append(sql)
            self.warning = SimpleNamespace(full_code="DPY-7000", message="compilation errors") if sql.startswith("CREATE") else None
        def close(self): closed.append("cursor")
    admin = oraadmin.OraAdmin(BASE, prefix="TCASE_")
    admin._open = lambda: SimpleNamespace(cursor=Cursor, close=lambda: closed.append("connection"))
    with pytest.raises(RuntimeError, match="oracle-ddl-compilation-error"):
        admin.run_sql(f"CREATE {kind} TCASE_INVALID {body}")
    assert calls[-1] == f'DROP {kind} "TCASE_INVALID"'
    assert admin.touched and closed == ["cursor", "connection"]
