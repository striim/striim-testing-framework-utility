"""Exact trigger ownership for observer teardown and interrupted-run replay."""
from __future__ import annotations
import re
from livetest.oraadmin import _IDENT, _SQL_IDENT


def target(admin):
    _, rows = admin._query("SELECT DBID, SYS_CONTEXT('USERENV', 'CON_NAME') FROM SYS.V_$DATABASE")
    if len(rows) != 1 or len(rows[0]) != 2 or any(value is None for value in rows[0]):
        raise RuntimeError('oracle-observer-identity-unreadable: database identity is required')
    dsn = admin.dsn
    return dict(host=dsn['host'], port=int(dsn['port']), service=dsn['service'],
                user=dsn['source_user'], dbid=str(rows[0][0]), pdb=str(rows[0][1]))


def objects(admin):
    _, rows = admin._query("SELECT object_name, object_type FROM user_objects WHERE object_type = 'TRIGGER'")
    return {(name, kind) for name, kind in rows}


def before_create(admin, reference):
    ledger = getattr(admin, 'observer_ledger', None)
    if ledger is None:
        return None
    parts = [part[1:-1].replace('""', '"') if part.startswith('"') else part.upper()
             for part in re.findall(_SQL_IDENT, reference)]
    name, prefix = parts[-1], admin.observer_prefix
    if (not prefix or not name.upper().startswith(prefix.upper()) or not _IDENT.fullmatch(name)
            or (len(parts) == 2 and parts[0].upper() != admin.dsn['source_user'].upper())):
        return None  # an ordinary data-user trigger is outside the observer contract
    route = admin.observer_route
    identity = target(admin)
    if (name, 'TRIGGER') in objects(admin) and not any(
            e.get('oracleTarget') == identity and e['state'] in ('confirmed', 'delete-failed')
            for e in ledger.find('oracle-trigger', name, route)):
        raise RuntimeError('oracle-observer-not-owned: existing trigger is not acquired by this ledger')
    return ledger.add('oracle-trigger', name, route, 'intended', prefix=prefix, oracleTarget=identity)


def settle(admin):
    # Called only after drop_observer_objects readback proved all owned hooks absent.
    ledger = getattr(admin, 'observer_ledger', None)
    if ledger is not None:
        for entry in ledger.entries:
            if (entry['kind'] == 'oracle-trigger' and entry.get('db') == admin.observer_route
                    and entry['attempt'] == ledger.ident.attempt):
                check_target(admin, entry)
                ledger._set(entry, 'verified-absent')


def check_target(admin, entry):
    name, prefix = entry['name'], entry.get('prefix')
    if (not prefix or not _IDENT.fullmatch(prefix) or not _IDENT.fullmatch(name)
            or not name.upper().startswith(prefix.upper())):
        raise RuntimeError('oracle-observer-cleanup-scope: invalid recorded trigger name')
    if target(admin) != entry.get('oracleTarget'):
        raise RuntimeError('oracle-observer-target-changed: recorded database or user does not match')


def drop(admin, entry):
    check_target(admin, entry)
    if (entry['name'], 'TRIGGER') not in objects(admin):
        return
    conn = admin._open()
    try:
        conn.autocommit = True
        cur = conn.cursor()
        try:
            cur.execute(f'DROP TRIGGER "{entry["name"]}"')
        finally:
            cur.close()
    finally:
        conn.close()


def absent(admin, entry):
    check_target(admin, entry)
    return (entry['name'], 'TRIGGER') not in objects(admin)


def replay_admins(doc, env):
    from livetest import ownership
    from livetest.oraadmin import OraAdmin
    from livetest.registry import load_service
    defaults = load_service('oracle').docker_defaults
    admins = {}
    for entry in doc['entries']:
        if entry['kind'] != 'oracle-trigger':
            continue
        identity = entry.get('oracleTarget') or {}
        role = entry['db'].split('-', 1)[1]
        if role not in ('source', 'target', 'admin'):
            raise RuntimeError('oracle-observer-cleanup-scope: invalid recorded route')
        dsn = {k: identity[k] for k in ('host', 'port', 'service')}
        dsn['source_user'] = identity['user']
        dsn['source_password'] = (ownership._service_setting(f'SLT_ORA_{role.upper()}_PASSWORD', env)
                                  or defaults.get(role + '_password'))
        admins[entry['db']] = {'admin': OraAdmin(dsn, prefix=entry['prefix']), 'schema': None}
    return admins



def authorise_catalog_drop(admin, found):
    ledger = getattr(admin, 'observer_ledger', None)
    if ledger is None:
        return found, []
    allowed, errors = set(), []
    for name, kind in found:
        if kind != 'TRIGGER':
            allowed.add((name, kind))
            continue
        entries = ledger.find('oracle-trigger', name, admin.observer_route,
                              states=('intended', 'unresolved-intent', 'confirmed', 'delete-failed'))
        if not entries:
            errors.append(f'oracle-observer-not-owned: {name} has no ledger entry')
            continue
        try:
            # An intent follows the absence check in before_create. Oracle can
            # commit CREATE and then lose its response; confirmation is not needed
            # to remove that exact object on the unchanged recorded target.
            identity = target(admin)
            entry = next((e for e in entries if e.get('oracleTarget') == identity), entries[-1])
            check_target(admin, entry)
        except Exception as exc:
            errors.append(f'{name}: {exc}')
        else:
            allowed.add((name, kind))
    return allowed, errors
