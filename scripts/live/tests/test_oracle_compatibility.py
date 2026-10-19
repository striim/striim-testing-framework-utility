"""Observer isolation preserves ordinary Oracle routes and records trigger ownership."""
import json
import re
from types import SimpleNamespace
import pytest
from livetest import ownership, runident
from livetest.plugin import _oracle_admins, teardown_oracle_observers

BASE = dict(host='db.example', port=1521, service='PDB', source_user='qasource',
            source_password='striim', target_user='qatarget', target_password='striim',
            admin_user='SYSTEM', admin_password='striim')


def connect_fake(admin, state=None):
    state = state if state is not None else set()
    admin.fake_pdb_identity = {'dbid': '456', 'guid': 'A' * 32}
    class Cursor:
        warning = None
        def execute(self, sql):
            create = re.match(r'CREATE (?:OR REPLACE )?TRIGGER ([A-Z0-9_]+)', sql, re.I)
            drop = re.match(r'DROP TRIGGER "([A-Z0-9_]+)"', sql, re.I)
            if create: state.add((create[1], 'TRIGGER'))
            if drop: state.discard((drop[1], 'TRIGGER'))
        def close(self): pass
    admin._open = lambda: SimpleNamespace(cursor=Cursor, close=lambda:None)
    def query(sql):
        if 'V_$DATABASE' in sql:
            if 'c.DBID' in sql and 'RAWTOHEX' in sql:
                assert 'SYS.V_$CONTAINERS' in sql and 'CON_ID' in sql
                return ['DBID', 'PDB', 'PDB_DBID', 'GUID'], [
                    ('123', 'PDB', admin.fake_pdb_identity['dbid'], admin.fake_pdb_identity['guid'])]
            return ['DBID', 'PDB'], [('123', 'PDB')]
        if 'user_objects' in sql.lower(): return ['OBJECT_NAME','OBJECT_TYPE'], list(state)
        raise AssertionError(sql)
    admin._query = query
    return state


@pytest.mark.parametrize('route', ['oracle-source', 'oracle-target'])
@pytest.mark.parametrize('sql', [
    'DROP VIEW IF EXISTS {user}.AUDIT_SUMMARY_VIEW;',
    'CREATE VIEW AUDIT_SUMMARY_VIEW AS SELECT 1 id FROM dual;',
    'CREATE TABLE ORDERS(id NUMBER); INSERT INTO ORDERS VALUES(1);',
])
def test_ordinary_sql_through_route_setup_is_not_observer_scoped(route, sql):
    admin = _oracle_admins(BASE, 'TCASE_')[route]['admin']
    connect_fake(admin)
    admin.run_sql(sql.format(user=admin.dsn['source_user']))
    assert admin.touched is False


def test_no_observer_case_does_not_read_or_fail_observer_catalog():
    admins = _oracle_admins(BASE, 'TCASE_')
    admin = admins['oracle-source']['admin']; connect_fake(admin)
    admin.run_sql('CREATE TABLE ORDERS(id NUMBER); INSERT INTO ORDERS VALUES(1);')
    def unreadable(sql): raise RuntimeError('observer catalog unavailable')
    admin._query = unreadable
    assert teardown_oracle_observers(admins, {'TID_ORACLE':'TCASE_'}) == {'attempted':[], 'failed':{}}


def test_admin_drop_view_if_exists_checks_the_actual_object_name():
    admin = _oracle_admins(BASE, 'TCASE_')['oracle-admin']['admin']; connect_fake(admin)
    admin.run_sql('DROP VIEW IF EXISTS SYSTEM.TCASE_V;')
    with pytest.raises(ValueError, match='oracle-observer-ddl-scope'):
        admin.run_sql('DROP VIEW IF EXISTS SYSTEM.OTHER_V;')


def test_admin_materialized_view_is_explicitly_refused():
    admin = _oracle_admins(BASE, 'TCASE_')['oracle-admin']['admin']
    admin._open = lambda: pytest.fail('unsupported observer must not connect')
    with pytest.raises(ValueError, match='oracle-observer-ddl-unsupported'):
        admin.run_sql('CREATE MATERIALIZED VIEW TCASE_MV AS SELECT 1 id FROM dual;')


def test_created_trigger_is_persisted_and_exactly_replayable(tmp_path):
    ident = runident.derive('oracle-trigger-ledger', {'SLT_RUN_EPOCH':'observer-ledger-test'})
    ledger = ownership.Ledger.open(ident, state_dir=tmp_path, env={})
    admins = _oracle_admins(BASE, ident.tid_oracle, ledger=ledger)
    admin = admins['oracle-admin']['admin']; state = connect_fake(admin)
    ctx = SimpleNamespace(mode='native', url=None, user=None)
    ledger.bind(admins, ctx)
    name = ident.tid_oracle+'LOGON'
    admin.run_sql(f'CREATE TRIGGER {name} AFTER LOGON ON DATABASE BEGIN NULL; END;\n/')
    entries = json.loads(ledger.path.read_text())['entries']
    trigger = next(e for e in entries if e['kind']=='oracle-trigger')
    assert trigger['name'] == name and trigger['state']=='confirmed'
    assert trigger['oracleTarget']['pdbDbid'] == '456'
    assert trigger['oracleTarget']['pdbGuid'] == 'A' * 32
    assert 'password' not in json.dumps(trigger).lower()
    state.add(('OTHER_LOGON','TRIGGER'))
    result = ownership.replay(ledger.path, admins=admins, ctx=ctx, client=None, env={})
    assert result.status == 'ok' and result.verified
    assert state == {('OTHER_LOGON','TRIGGER')}


def trigger_case(tmp_path):
    ident = runident.derive('oracle-trigger-ledger', {'SLT_RUN_EPOCH':'observer-ledger-test'})
    ledger = ownership.Ledger.open(ident, state_dir=tmp_path, env={})
    admins = _oracle_admins(BASE, ident.tid_oracle, ledger=ledger)
    admin = admins['oracle-admin']['admin']; state = connect_fake(admin)
    ctx = SimpleNamespace(mode='native', url=None, user=None)
    ledger.bind(admins, ctx)
    return ident, ledger, admins, admin, state, ctx


def test_trigger_replay_refuses_a_different_database(tmp_path):
    ident, ledger, admins, admin, state, ctx = trigger_case(tmp_path)
    name=ident.tid_oracle+'LOGON'
    admin.run_sql(f'CREATE TRIGGER {name} AFTER LOGON ON DATABASE BEGIN NULL; END;\n/')
    query=admin._query
    def changed_database(sql):
        cols, rows = query(sql)
        return (cols, [('999', *rows[0][1:])]) if 'V_$DATABASE' in sql else (cols, rows)
    admin._query = changed_database
    result=ownership.replay(ledger.path, admins=admins, ctx=ctx, client=None, env={})
    assert result.status=='failed' and not result.verified
    assert (name,'TRIGGER') in state


def test_observer_teardown_settles_ledger_even_with_resources_retained(tmp_path):
    ident, ledger, admins, admin, state, ctx = trigger_case(tmp_path)
    name=ident.tid_oracle+'LOGON'
    admin.run_sql(f'CREATE TRIGGER {name} AFTER LOGON ON DATABASE BEGIN NULL; END;\n/')
    assert not teardown_oracle_observers(admins, {'TID_ORACLE':ident.tid_oracle})['failed']
    trigger=next(e for e in ledger.entries if e['kind']=='oracle-trigger')
    assert trigger['state']=='verified-absent' and not state
    result=ledger.cleanup(client=None, admins=admins, ctx=ctx, tokens={}, keep_reason='keep tables')
    assert result.status=='skipped' and trigger['state']=='verified-absent'


def test_trigger_collision_is_not_replaced(tmp_path):
    ident, ledger, admins, admin, state, ctx = trigger_case(tmp_path)
    name=ident.tid_oracle+'LOGON';state.add((name,'TRIGGER'))
    with pytest.raises(RuntimeError,match='oracle-observer-not-owned'):
        admin.run_sql(f'CREATE OR REPLACE TRIGGER {name} AFTER LOGON ON DATABASE BEGIN NULL; END;\n/')
    assert state=={(name,'TRIGGER')}
    assert not any(e['state']=='confirmed' for e in ledger.entries)
    outcome=teardown_oracle_observers(admins, {'TID_ORACLE':ident.tid_oracle})
    assert outcome['failed'] and state=={(name,'TRIGGER')}



def test_teardown_does_not_mark_a_prior_database_entry_absent(tmp_path):
    ident, ledger, admins, admin, state, ctx = trigger_case(tmp_path)
    name=ident.tid_oracle+'LOGON'
    admin.run_sql(f'CREATE TRIGGER {name} AFTER LOGON ON DATABASE BEGIN NULL; END;\n/')
    prior={**ledger.entries[0], 'attempt':'older', 'name':ident.tid_oracle+'OLD',
           'oracleTarget':{**ledger.entries[0]['oracleTarget'],'dbid':'999'}}
    ledger.entries.append(prior);ledger.persist()
    assert not teardown_oracle_observers(admins, {'TID_ORACLE':ident.tid_oracle})['failed']
    assert prior['state']=='confirmed'


def test_replay_reconstructs_oracle_route_without_recording_passwords(tmp_path, monkeypatch):
    from livetest import oraadmin
    ident, ledger, admins, admin, state, ctx = trigger_case(tmp_path)
    name=ident.tid_oracle+'LOGON'
    admin.run_sql(f'CREATE TRIGGER {name} AFTER LOGON ON DATABASE BEGIN NULL; END;\n/')
    original=oraadmin.OraAdmin;opened=[]
    def rebuild(dsn, **kwargs):
        opened.append(dsn)
        result=original(dsn, **kwargs);connect_fake(result,state);return result
    monkeypatch.setattr(oraadmin,'OraAdmin',rebuild)
    result=ownership.replay(ledger.path,ctx=ctx,client=None,env={'SLT_ORA_ADMIN_PASSWORD':'proof-secret'})
    assert result.status=='ok' and result.verified and not state
    assert opened[0]['source_password']=='proof-secret'
    assert 'proof-secret' not in ledger.path.read_text()


def test_compilation_warning_keeps_a_ledger_record_until_verified_drop(tmp_path):
    ident, ledger, admins, admin, state, ctx = trigger_case(tmp_path)
    cursor_type=type(admin._open().cursor())
    class WarningCursor(cursor_type):
        def execute(self,sql):
            super().execute(sql)
            self.warning=SimpleNamespace(full_code='DPY-7000') if sql.startswith('CREATE') else None
    admin._open=lambda:SimpleNamespace(cursor=WarningCursor,close=lambda:None)
    name=ident.tid_oracle+'LOGON'
    with pytest.raises(RuntimeError,match='oracle-ddl-compilation-error'):
        admin.run_sql(f'CREATE TRIGGER {name} AFTER LOGON ON DATABASE BEGIN NULL; END;\n/')
    assert not state and ledger.entries[0]['state']=='confirmed'
    assert not teardown_oracle_observers(admins,{'TID_ORACLE':ident.tid_oracle})['failed']
    assert ledger.entries[0]['state']=='verified-absent'


@pytest.mark.parametrize('sql', [
    'DROP TABLE QASOURCE.OTHER_CASE_TABLE;',
    'DROP USER QATARGET CASCADE;',
    'ALTER SYSTEM SET open_cursors=10;',
    'CREATE TABLE TCASE_LOG(id NUMBER);',
    'GRANT SELECT ON TCASE_V TO PUBLIC;',
    'ALTER TRIGGER TCASE_TR RENAME TO OTHER_TR;',
    'CREATE VIEW TCASE_V AS SELECT 1 id FROM dual; DROP USER QATARGET CASCADE;',
])
def test_admin_refuses_non_observer_sql_before_connect(sql):
    admin = _oracle_admins(BASE, 'TCASE_')['oracle-admin']['admin']
    admin._open = lambda: pytest.fail('refused script must not connect')
    with pytest.raises(ValueError, match='oracle-observer-ddl-unsupported'):
        admin.run_sql(sql)


@pytest.mark.parametrize('route', ['oracle-source', 'oracle-target'])
@pytest.mark.parametrize('name', ['REPORT_V', 'TCASE_REPORT_V'])
def test_ordinary_force_view_warning_keeps_the_invalid_view(route, name):
    admin = _oracle_admins(BASE, 'TCASE_')[route]['admin']
    sent = []
    class Cursor:
        warning = SimpleNamespace(full_code='DPY-7000')
        def execute(self, sql): sent.append(sql)
        def close(self): pass
    admin._open = lambda: SimpleNamespace(cursor=Cursor, close=lambda: None)
    sql = f'CREATE OR REPLACE FORCE VIEW {name} AS SELECT * FROM NOT_YET_CREATED'
    admin.run_sql(sql + ';')
    assert sent == [sql]


def test_admin_force_view_warning_drops_the_invalid_observer():
    admin = _oracle_admins(BASE, 'TCASE_')['oracle-admin']['admin']
    sent = []
    class Cursor:
        warning = SimpleNamespace(full_code='DPY-7000')
        def execute(self, sql): sent.append(sql)
        def close(self): pass
    admin._open = lambda: SimpleNamespace(cursor=Cursor, close=lambda: None)
    with pytest.raises(RuntimeError, match='oracle-ddl-compilation-error'):
        admin.run_sql('CREATE OR REPLACE FORCE VIEW TCASE_REPORT_V AS SELECT * FROM NOT_YET_CREATED;')
    assert sent[-1] == 'DROP VIEW "TCASE_REPORT_V"'


def create_trigger_then_disconnect(admin, name):
    cursor_type = type(admin._open().cursor())
    class DisconnectCursor(cursor_type):
        def execute(self, sql):
            super().execute(sql)
            if sql.startswith('CREATE'):
                raise RuntimeError('DPY-4011: connection closed after DDL commit')
    admin._open = lambda: SimpleNamespace(cursor=DisconnectCursor, close=lambda: None)
    with pytest.raises(RuntimeError, match='DPY-4011'):
        admin.run_sql(f'CREATE TRIGGER {name} AFTER LOGON ON DATABASE BEGIN NULL; END;\n/')


@pytest.mark.parametrize('cleanup', ['teardown', 'cleanup', 'replay', 'replay-unresolved'])
def test_trigger_committed_before_execute_raised_is_removed(tmp_path, cleanup):
    ident, ledger, admins, admin, state, ctx = trigger_case(tmp_path)
    name = ident.tid_oracle + 'LOGON'
    create_trigger_then_disconnect(admin, name)
    assert (name, 'TRIGGER') in state
    assert ledger.entries[0]['state'] == 'intended'
    if cleanup == 'teardown':
        assert not teardown_oracle_observers(admins, {'TID_ORACLE': ident.tid_oracle})['failed']
    elif cleanup == 'cleanup':
        result = ledger.cleanup(client=None, admins=admins, ctx=ctx, tokens={})
        assert result.status == 'ok' and result.verified
    else:
        if cleanup == 'replay-unresolved':
            ledger._set(ledger.entries[0], 'unresolved-intent')
        result = ownership.replay(ledger.path, admins=admins, ctx=ctx, client=None, env={})
        assert result.status == 'ok' and result.verified
    assert not state
    assert json.loads(ledger.path.read_text())['entries'][0]['state'] == 'verified-absent'


@pytest.mark.parametrize('field,value', [('dbid', '999'), ('pdb', 'OTHER'),
                                       ('user', 'OTHER'), ('host', 'other.example')])
@pytest.mark.parametrize('cleanup', ['teardown', 'replay'])
def test_intended_trigger_on_changed_target_is_preserved(tmp_path, field, value, cleanup):
    ident, ledger, admins, admin, state, ctx = trigger_case(tmp_path)
    name = ident.tid_oracle + 'LOGON'
    create_trigger_then_disconnect(admin, name)
    ledger.entries[0]['oracleTarget'][field] = value
    ledger.persist()
    if cleanup == 'teardown':
        assert teardown_oracle_observers(admins, {'TID_ORACLE': ident.tid_oracle})['failed']
    else:
        result = ownership.replay(ledger.path, admins=admins, ctx=ctx, client=None, env={})
        assert not result.verified
    assert state == {(name, 'TRIGGER')}


def test_unowned_trigger_does_not_prevent_view_cleanup(tmp_path):
    ident, ledger, admins, admin, state, ctx = trigger_case(tmp_path)
    trigger, view = ident.tid_oracle + 'FOREIGN', ident.tid_oracle + 'V'
    state.update({(trigger, 'TRIGGER'), (view, 'VIEW')})
    cursor_type = type(admin._open().cursor())
    class ViewCursor(cursor_type):
        def execute(self, sql):
            super().execute(sql)
            if sql == f'DROP VIEW "{view}"':
                state.discard((view, 'VIEW'))
    admin._open = lambda: SimpleNamespace(cursor=ViewCursor, close=lambda: None)
    admin.touched = True
    outcome = teardown_oracle_observers(admins, {'TID_ORACLE': ident.tid_oracle})
    assert outcome['failed']
    assert 'oracle-observer-not-owned' in str(outcome)
    assert state == {(trigger, 'TRIGGER')}


def test_intended_trigger_is_reported_as_replayable_leftover(tmp_path):
    ident, ledger, admins, admin, state, ctx = trigger_case(tmp_path)
    create_trigger_then_disconnect(admin, ident.tid_oracle + 'LOGON')
    leftovers = ownership.leftover_ledgers(tmp_path, pid_alive=lambda pid: False)
    assert len(leftovers) == 1 and leftovers[0][0] == ledger.path


def test_intended_trigger_reset_checks_recorded_target_and_removes_exact_name(tmp_path):
    ident, ledger, admins, admin, state, ctx = trigger_case(tmp_path)
    name = ident.tid_oracle + 'LOGON'
    create_trigger_then_disconnect(admin, name)
    retry_ident = runident.derive(ident.case, {'SLT_RUN_EPOCH': ident.run_id})
    retry = ownership.Ledger.open(retry_ident, state_dir=tmp_path, env={})
    state.add(('OTHER_LOGON', 'TRIGGER'))
    retry.reset(client=None, admins=admins, ctx=ctx, tokens={})
    assert state == {('OTHER_LOGON', 'TRIGGER')}
    assert retry.entries[0]['state'] == 'deleted'


def assert_identity_cleanup_refused(ident, ledger, admins, ctx, tmp_path, cleanup, code):
    if cleanup == 'teardown':
        outcome = teardown_oracle_observers(admins, {'TID_ORACLE': ident.tid_oracle})
        assert outcome['failed'] and code in str(outcome['failed'])
    elif cleanup == 'reset':
        retry_ident = runident.derive(ident.case, {'SLT_RUN_EPOCH': ident.run_id})
        retry = ownership.Ledger.open(retry_ident, state_dir=tmp_path, env={})
        retry.reset(client=None, admins=admins, ctx=ctx, tokens={})
        assert code in str(retry.gaps)
    else:
        if cleanup == 'replay':
            result = ownership.replay(ledger.path, admins=admins, ctx=ctx, client=None, env={})
        else:
            result = ledger.cleanup(client=None, admins=admins, ctx=ctx, tokens={})
        assert not result.verified and code in str(result.gaps)
    assert json.loads(ledger.path.read_text())['entries'][0]['state'] != 'verified-absent'


@pytest.mark.parametrize('cleanup', ['teardown', 'cleanup', 'replay', 'reset'])
@pytest.mark.parametrize('changed', ['dbid', 'guid'])
def test_replacement_pdb_preserves_same_name_trigger(tmp_path, cleanup, changed):
    ident, ledger, admins, admin, state, ctx = trigger_case(tmp_path)
    name = ident.tid_oracle + 'LOGON'
    create_trigger_then_disconnect(admin, name)
    # Only the PDB incarnation changes; the CDB, name, endpoint and user stay fixed.
    admin.fake_pdb_identity[changed] = '789' if changed == 'dbid' else 'B' * 32
    state.clear()
    state.add((name, 'TRIGGER'))  # same name in the replacement PDB's catalog
    assert_identity_cleanup_refused(ident, ledger, admins, ctx, tmp_path, cleanup,
                                    'oracle-observer-target-changed')
    assert state == {(name, 'TRIGGER')}


@pytest.mark.parametrize('cleanup', ['teardown', 'cleanup', 'replay', 'reset'])
@pytest.mark.parametrize('missing', [('pdbDbid',), ('pdbGuid',), ('pdbDbid', 'pdbGuid')])
def test_missing_pdb_identity_refuses_legacy_entry(tmp_path, cleanup, missing):
    ident, ledger, admins, admin, state, ctx = trigger_case(tmp_path)
    name = ident.tid_oracle + 'LOGON'
    create_trigger_then_disconnect(admin, name)
    for field in missing:
        ledger.entries[0]['oracleTarget'].pop(field, None)
    ledger.persist()
    assert_identity_cleanup_refused(ident, ledger, admins, ctx, tmp_path, cleanup,
                                    'oracle-observer-pdb-identity-missing')
    assert state == {(name, 'TRIGGER')}
    persisted = json.loads(ledger.path.read_text())['entries'][0]
    assert all(field not in persisted['oracleTarget'] for field in missing)
    # Reported diagnostics must give an explicit manual recovery path.
    diagnostic = str(teardown_oracle_observers(admins, {'TID_ORACLE': ident.tid_oracle}))
    assert 'by hand' in diagnostic and f'DROP TRIGGER "SYSTEM"."{name}"' in diagnostic


@pytest.mark.parametrize('identity', ['replacement', 'missing'])
def test_absence_verification_requires_recorded_pdb_identity(tmp_path, identity):
    from livetest.oracle_ownership import absent, settle
    ident, ledger, admins, admin, state, ctx = trigger_case(tmp_path)
    create_trigger_then_disconnect(admin, ident.tid_oracle + 'LOGON')
    state.clear()
    if identity == 'replacement':
        admin.fake_pdb_identity['guid'] = 'B' * 32
        code = 'oracle-observer-target-changed'
    else:
        ledger.entries[0]['oracleTarget'].pop('pdbGuid', None)
        code = 'oracle-observer-pdb-identity-missing'
    with pytest.raises(RuntimeError, match=code):
        absent(admin, ledger.entries[0])
    with pytest.raises(RuntimeError, match=code):
        settle(admin)
    assert ledger.entries[0]['state'] == 'intended'


@pytest.mark.parametrize('cleanup', ['teardown', 'cleanup', 'replay', 'reset'])
@pytest.mark.parametrize('identity', ['replacement', 'missing'])
def test_empty_catalog_does_not_verify_a_different_or_legacy_pdb(tmp_path, cleanup, identity):
    ident, ledger, admins, admin, state, ctx = trigger_case(tmp_path)
    create_trigger_then_disconnect(admin, ident.tid_oracle + 'LOGON')
    state.clear()
    if identity == 'replacement':
        admin.fake_pdb_identity['dbid'] = '789'
        code = 'oracle-observer-target-changed'
    else:
        ledger.entries[0]['oracleTarget'].pop('pdbDbid', None)
        ledger.persist()
        code = 'oracle-observer-pdb-identity-missing'
    assert_identity_cleanup_refused(ident, ledger, admins, ctx, tmp_path, cleanup, code)
