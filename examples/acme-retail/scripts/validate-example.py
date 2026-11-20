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


def routes(case):
    """Every database route the case's files and assertions name."""
    named = [route for route, _file in case.ddl_files] + [entry[0] for entry in case.seed_files]
    for spec in case.assert_.get('data', []):
        named.append(spec.get('target_db') or spec.get('db'))
    for spec in case.assert_.get('diff', []):
        named += [spec.get('source_db'), spec.get('target_db')]
    return {route for route in named if route}


def source_table(case):
    """The one table the case's source DDL creates; the golden is checked against its rows."""
    tables = []
    for route, relative in case.ddl_files:
        if route.endswith('source'):
            text = (case.source_dir / relative).read_text()
            tables += [name.split('.')[-1].replace('${TID}', '') for name in re.findall(r'CREATE TABLE (\S+)', text)]
    check(len(tables) == 1, (case.name, 'the source DDL must create one table', tables))
    return tables[0]


def validate_case(path, root):
    case = manifest.load_manifest(path)
    # The app is a folder of apps/, and the services the case requires are exactly the ones
    # its routes use (postgres-source -> postgres, mysql-target -> mysql, kafka -> kafka).
    check(case.source_dir.parent == root / 'apps' and (case.source_dir / case.tql).is_file(),
          (case.name, 'example: must name an app folder under apps/', case.source_dir))
    services = {route.split('-')[0] for route in routes(case)}
    check(set(case.requires) == services, (case.name, 'requires', sorted(case.requires), 'routes use', sorted(services)))
    table = source_table(case)
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
        # table is a name the case's own DDL created in this database.
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
    check(files, 'no cases under tests/live')
    names = [validate_case(path, root) for path in files]
    check(len(set(names)) == len(names), ('two cases share a name', names))
    print(f'PASS: {len(names)} manifests and all offline contract checks')


if __name__ == '__main__':
    main()
