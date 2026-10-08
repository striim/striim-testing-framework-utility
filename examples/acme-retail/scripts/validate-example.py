#!/usr/bin/env python3
"""Validate this example offline, without network, services, Docker or Striim.

SQLite checks fixture/golden consistency only; it does not prove adapter behavior.
"""
import csv
from decimal import Decimal
from pathlib import Path
import re
import sqlite3

from livetest import manifest, project, registry
from livetest.assertions.data import parse_data_specs
from livetest.assertions.diff import parse_diff_specs

# Explicit app/table names also ensure each case references the intended production app.
APPS = {
    'acme-orders-cdc': ('orders', 'orders', {'postgres', 'mysql'}),
    'acme-customers-load-cdc': ('customers', 'customers', {'postgres'}),
    'acme-events-kafka': ('events', 'retail_events', {'mysql', 'kafka'}),
}


def check(condition, message):
    if not condition:
        raise SystemExit(f"FAIL: {message}")


def canonical(row):
    """Compare SQLite values with the hand-written golden's intended types."""
    cells = []
    for key, value in row.items():
        if value is None or value == '<null>':
            value = '<null>'
        elif key in {'amount', 'credit_limit'}:
            value = str(Decimal(str(value)).quantize(Decimal('.01')))
        else:
            value = str(value)
        cells.append((key, value))
    return tuple(sorted(cells))


def validate_case(path, root):
    case = manifest.load_manifest(path)
    app, table, services = APPS[case.name]
    check(case.source_dir == root / 'apps' / app, path)
    check(set(case.requires) == services, path)
    check(case.depth in {'gate', 'regression'}, path)
    check(set(case.assert_) <= {'smoke', 'data', 'diff'}, path)
    parse_data_specs(case.assert_['data'])
    if 'diff' in case.assert_:
        parse_diff_specs(case.assert_['diff'])

    tokens = {'NS', 'APP', 'TID'}
    for service in case.requires:
        tokens.update(registry.load_service(service).provides)
    references = ([case.tql] + [entry[1] for entry in case.ddl_files]
                  + [entry[1] for entry in case.seed_files])
    for relative in references:
        file = case.source_dir / relative
        check(file.is_file(), file)
        text = file.read_text()
        used = set(re.findall(r'\$\{([A-Z][A-Z0-9_]*)\}', text))
        check(used <= tokens, (file, used - tokens))
        check(not re.search(r'CREATE\s+(SCHEMA|PUBLICATION)', text, re.I), file)
        for name in re.findall(r'CREATE TABLE (\S+)', text):
            check(name.split('.')[-1].startswith('${TID}'), (file, name))

    tql = (case.source_dir / case.tql).read_text()
    components = re.findall(r'CREATE(?: OR REPLACE)? (?:SOURCE|TARGET|CQ|STREAM) (\w+)', tql)
    check(all(len(name) <= 21 for name in components), components)
    for line in ('CREATE NAMESPACE ${NS}', 'USE ${NS}',
                 'DEPLOY APPLICATION ${APP}', 'START APPLICATION ${APP}'):
        check(line in tql, (path, line))
    for spec in case.assert_['data']:
        check((case.dir / spec['match']).is_file(), spec['match'])

    # Execute only the portable source fixture SQL. Slot creation and adapters require live QA.
    with sqlite3.connect(':memory:') as database:
        for route, relative in case.ddl_files:
            if route.endswith('source') and not relative.endswith('slot.sql'):
                sql = (case.source_dir / relative).read_text().replace('${TID}', '').replace('${MYSQL_SOURCE_SCHEMA}.', '')
                database.executescript(sql)
        for route, relative, _when, _after in case.seed_files:
            if route.endswith('source'):
                sql = (case.source_dir / relative).read_text().replace('${TID}', '').replace('${MYSQL_SOURCE_SCHEMA}.', '')
                database.executescript(sql)
        # table comes only from the fixed APPS mapping above.
        cursor = database.execute('SELECT * FROM ' + table)
        columns = [column[0] for column in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    golden = case.dir / case.assert_['data'][0]['match']
    with golden.open(newline='') as stream:
        expected = list(csv.DictReader(stream))
    check(sorted(map(canonical, rows)) == sorted(map(canonical, expected)), case.name)
    print(f'[ ok ] {case.name}: manifest, assertions, files, tokens, names and golden '
          f'({len(rows)} rows)')
    return case.name


def main():
    root = Path(__file__).resolve().parents[1]
    project.load_and_activate(root / 'gold-targets.yaml')
    files = sorted((root / 'tests/live').rglob('test.yaml'))
    check(len(files) == 3, 'The example must have three cases')
    check({validate_case(path, root) for path in files} == set(APPS), 'example contract check failed')
    print('PASS: 3 manifests and all offline contract checks')


if __name__ == '__main__':
    main()
