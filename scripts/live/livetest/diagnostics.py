"""Bounded key samples for transaction and failed-completion evidence."""
from livetest import lifecycle

MAX_KEYS = 50
MAX_KEY_CHARS = 256


def read_keys(probe, table, key, remaining):
    if not lifecycle._IDENT.fullmatch(key):
        raise ValueError('invalid-diagnostic-key')
    sql = lifecycle._table(table)
    column = '"' + key + '"'
    rows = probe.query(f'SELECT left({column}::text, {MAX_KEY_CHARS}), '
                       f'length({column}::text) > {MAX_KEY_CHARS} FROM {sql} '
                       f'ORDER BY {column} LIMIT %s', (MAX_KEYS + 1,), remaining)
    return {'table': table, 'key': key, 'keys': [r[0] for r in rows[:MAX_KEYS]],
            'keysTruncated': len(rows) > MAX_KEYS,
            'valuesTruncated': any(r[1] for r in rows[:MAX_KEYS])}
