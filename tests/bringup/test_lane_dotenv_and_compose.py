"""Hermetic checks of the complete stock lane configuration and mutable recipe tags."""
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from livetest import paths as live
from inttest import paths as integration

ROOT = Path(__file__).resolve().parents[2]


def published_keys():
    return {key for tier in ('live', 'integration')
            for file in (ROOT / 'scripts' / tier / 'services').rglob('compose*.yaml')
            for key in re.findall(r'\$\{((?:SLT|INT)_\w+(?:HOST_PORT|CLIENT_PORT))', file.read_text())}


@pytest.mark.parametrize('value,expected', [(None, '4g'), ('', '4g'), ('6g', '6g')])
def test_live_postgres_memory_default_and_checkout_override(value, expected, tmp_path, monkeypatch):
    from livetest import service_env as resolver
    from striim_test.dispatch import service_env

    key = 'SLT_POSTGRES_MEM_LIMIT'
    file = ROOT / 'scripts/live/services/postgres/compose.yaml'
    expression = yaml.safe_load(file.read_text())['services']['slt-postgres']['mem_limit']
    monkeypatch.setattr(live, 'dotenv_values', lambda env=None: resolver.values(live, env))
    (tmp_path / '.env').write_text('' if value is None else f'{key}={value}\n')
    env = {'SLT_PROJECT_ROOT': str(tmp_path), 'SLT_MACHINE_ENV': str(tmp_path / 'machine.env')}
    assert resolver.declarations(env)[key] == 'string'
    forwarded = service_env(env)
    rendered = subprocess.check_output(['bash', '-c', 'printf "%s" "' + expression + '"'],
                                       env=forwarded, text=True)
    assert rendered == expected
    if value:
        assert forwarded[key] == value
        shell = {**env, key: '8g'}
        child = {**shell, **service_env(shell)}
        assert child[key] == '8g'
        assert live.effective_env(shell)[key] == '8g'


@pytest.mark.parametrize('paths', [live, integration])
def test_all_stock_ports_and_prefixes_survive_dotenv(paths, tmp_path):
    keys = published_keys() | {'SLT_STACK_PREFIX', 'INT_STACK_PREFIX'}
    values = {key: str(30000 + i) for i, key in enumerate(sorted(keys))}
    file = tmp_path / '.env'
    file.write_text(''.join(f'{k}={v}\n' for k, v in values.items()))
    assert keys <= set(paths.SERVICE_KEYS)
    dot = paths.read_dotenv(file)
    assert dot == values
    assert paths.effective_env({}, dot) == values
    assert paths.effective_env({k: 'shell' for k in keys}, dot) == {k: 'shell' for k in keys}


@pytest.mark.parametrize('rel,tag,key', [
    ('live/services/postgres', 'slt-postgres:16', 'SLT_STACK_PREFIX'),
    ('live/services/mssql', 'slt-mssql:2022', 'SLT_STACK_PREFIX'),
    ('live/services/oracle', 'slt-oracle:23.26.2', 'SLT_STACK_PREFIX'),
    ('live/services/vertica', 'slt-vertica:25.3.0-8', 'SLT_STACK_PREFIX'),
    ('integration/services/sqlserver', 'int-mssql:2022', 'INT_STACK_PREFIX'),
    ('integration/services/vertica', 'int-vertica:25.3.0-8', 'INT_STACK_PREFIX'),
])
def test_mutable_recipe_tag_is_scoped_with_identical_unprefixed_default(rel, tag, key):
    services = yaml.safe_load((ROOT / 'scripts' / rel / 'compose.yaml').read_text())['services']
    image = next(s['image'] for s in services.values() if 'build' in s)
    # These compose alternative-value expressions are also valid bash parameter expansions.
    def render(value):
        return subprocess.check_output(['bash', '-c', 'printf "%s" "' + image + '"'],
                                       env={key: value}, text=True)
    assert render('') == tag
    assert [render(lane) for lane in ('one', 'two', 'three')] == [f'{lane}-{tag}' for lane in ('one', 'two', 'three')]


@pytest.mark.parametrize('paths', [live, integration])
def test_consumer_declared_checkout_settings_reach_every_boundary(paths, tmp_path, monkeypatch):
    from livetest import layout, prestart, services, service_env as resolver
    for mod in (live, integration):
        monkeypatch.setattr(mod, "dotenv_values", lambda env=None, mod=mod: resolver.values(mod, env))
    from inttest import services as int_services
    from striim_test.dispatch import service_env
    svc = tmp_path / 'services' / 'widget'
    svc.mkdir(parents=True)
    (tmp_path / 'cases').mkdir()
    manifest = tmp_path / 'gold-targets.yaml'
    manifest.write_text('schemaVersion: 1\nsuites: {live: cases}\nservicesRoots: [services]\n')
    (svc / 'service.yaml').write_text('''name: widget
isolation: none
compose: compose.yaml
docker_env: {port: CUSTOM_HOST_PORT}
live_env: {host: CUSTOM_HOST}
pre_up: check.sh
pre_up_env:
  - {name: ARTIFACT_PATH, type: path}
  - {name: FETCH_URL, type: string}
''')
    (svc / 'compose.yaml').write_text('services: {widget: {image: "${CUSTOM_IMAGE:-example}"}}\n')
    (tmp_path / '.env').write_text('CUSTOM_HOST_PORT=32451\nCUSTOM_HOST=remote\nCUSTOM_IMAGE=private\nARTIFACT_PATH=inputs/tiny.zip\nFETCH_URL=https://fixture.invalid/a\nUNRELATED_SECRET=never-forward\n')
    env = {'SLT_MACHINE_ENV': str(tmp_path / 'machine.env'), 'SLT_PROJECT_ROOT': str(tmp_path), 'GOLD_TARGETS': str(manifest), 'SLT_PRE_UP': '1'}
    got = paths.effective_env(env)
    forwarded = service_env(env)
    for key in ('CUSTOM_HOST_PORT', 'CUSTOM_HOST', 'CUSTOM_IMAGE', 'ARTIFACT_PATH', 'FETCH_URL'):
        assert got[key] == forwarded[key]
        assert key not in paths.SERVICE_KEYS
    assert got['ARTIFACT_PATH'] == str(tmp_path / 'inputs/tiny.zip')
    assert 'UNRELATED_SECRET' not in got
    assert paths.effective_env({**env, 'CUSTOM_IMAGE': 'shell'})['CUSTOM_IMAGE'] == 'shell'
    monkeypatch.setattr(prestart, 'enabled', lambda *a: True)
    monkeypatch.setattr(prestart, 'lock_path', lambda *a: tmp_path / 'hook.lock')
    (svc / 'check.sh').write_text('exit 0\n')
    captured = {}
    monkeypatch.setattr(prestart, '_execute', lambda path, cwd, child, timeout: (captured.update(child) or (0, '')))
    assert prestart.run_hook('widget', svc, 'check.sh', env=got)
    assert captured['ARTIFACT_PATH'] == got['ARTIFACT_PATH']
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert services._compose_env()['FETCH_URL'] == got['FETCH_URL']
    assert int_services._compose_up_env('widget')['CUSTOM_HOST_PORT'] == '32451'
    layout._reset()


@pytest.mark.parametrize('paths', [live, integration])
def test_consumer_machine_filtering_paths_and_clone_fallback(paths, tmp_path, monkeypatch):
    from livetest import service_env as resolver
    from striim_test.dispatch import child_env
    from types import SimpleNamespace
    clone, checkout = tmp_path / 'clone', tmp_path / 'checkout'
    clone.mkdir(); checkout.mkdir()
    svc = checkout / 'services/widget'
    svc.mkdir(parents=True)
    (checkout / 'cases').mkdir()
    manifest = checkout / 'gold-targets.yaml'
    manifest.write_text('schemaVersion: 1\nsuites: {live: cases}\nservicesRoots: [services]\n')
    (svc / 'service.yaml').write_text('''name: widget
isolation: none
docker_env: {port: CUSTOM_HOST_PORT}
pre_up_env:
  - {name: ARTIFACT_PATH, type: path}
  - {name: FETCH_URL, type: string}
''')
    machine = tmp_path / 'machine.env'
    machine.write_text('CUSTOM_HOST_PORT=9999\nFETCH_URL=machine\nARTIFACT_PATH=/machine/file\nUNRELATED_SECRET=private\n')
    machine.chmod(0o600)
    (clone / '.env').write_text('FETCH_URL=clone\n')
    monkeypatch.setattr(live, '_default_project_root', lambda: clone)
    monkeypatch.setattr(paths, '_default_project_root', lambda: clone)
    monkeypatch.setattr(paths, 'dotenv_values', lambda env=None: resolver.values(paths, env))
    env = {'SLT_PROJECT_ROOT': str(checkout), 'GOLD_TARGETS': str(manifest), 'SLT_MACHINE_ENV': str(machine)}
    resolved = paths.effective_env(env)
    assert resolved['FETCH_URL'] == 'clone'
    assert resolved['ARTIFACT_PATH'] == '/machine/file'
    assert 'CUSTOM_HOST_PORT' not in resolved and 'UNRELATED_SECRET' not in resolved
    child = child_env(SimpleNamespace(mode='clone', home=ROOT), base={}, location=env)
    assert child['FETCH_URL'] == 'clone' and child['ARTIFACT_PATH'] == '/machine/file'
    machine.write_text('ARTIFACT_PATH=relative/file\n')
    with pytest.raises(paths.PathConfigError, match='absolute path'):
        paths.effective_env(env)


@pytest.mark.parametrize('entry', ['a', '{name: FOO, type: number}', '{name: foo, type: string}'])
def test_pre_up_env_rejects_untyped_or_invalid_declarations(entry):
    from livetest.service_env import pre_up_env, RegistryError
    with pytest.raises(RegistryError, match='pre_up_env'):
        pre_up_env(yaml.safe_load(f'[{entry}]'), Path('service.yaml'))
