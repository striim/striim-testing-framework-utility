"""Disposable Postgres role policy, reconciled even when compose reuses its volume."""
import re


def policy_sql(env, setting, database):
    value = env.get(setting) or '600000'
    if not re.fullmatch(r'[1-9][0-9]{0,9}', value) or int(value) > 2147483647:
        raise ValueError(f'invalid-postgres-idle-transaction-budget: {setting}: '
                         'expected positive integer milliseconds, at most 2147483647')
    return '; '.join(
        f'ALTER ROLE {role} IN DATABASE {database} SET idle_in_transaction_session_timeout = \'{value}ms\''
        for role in ('qasource', 'qatarget'))


def apply_policy(compose, env, tier, sql, run):
    service, database = ('slt-postgres', 'sltdb') if tier == 'live' else ('int-postgres', 'intdb')
    run(['docker', 'compose', '-f', str(compose), 'exec', '-T', service,
         'psql', '-X', '-v', 'ON_ERROR_STOP=1', '-U', 'postgres', '-d', database, '-c', sql],
        check=True, capture_output=True, text=True, env=env)
