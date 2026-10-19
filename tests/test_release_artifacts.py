"""Release-specific file identities shared by both artifact engines."""
import importlib
import os
from pathlib import Path
from types import SimpleNamespace
import xml.etree.ElementTree as ET
import zipfile

import pytest



@pytest.mark.parametrize('versions,version,expected', [
    (['5.4.2', '5.2.0.8'], '5.4.2', '5.4'),
    (['5.4.2', '5.4.4.0'], '5.4.2', '5.4.2'),
    (['5.4.2', '5.4.4.0'], '5.4.4.0', '5.4.4'),
    (['5.4.4.0', '5.4.4.1'], '5.4.4.0', '5.4.4.0'),
    (['5.4.4.0', '5.4.4.1'], '5.4.4.1', '5.4.4.1'),
    (None, '5.4.2', '5.4'),
    ([], '5.4.2', '5.4'),
    (['5.4.0.6', '5.4.0.6C'], '5.4.0.6C', '5.4.0.6C'),
])
def test_shortest_unique_suffix(versions, version, expected):
    from inttest import opjarrelease
    assert opjarrelease.jar_suffix({'STRIIM_VERSION': version, 'STRIIM_SERIES': '5.4'}, versions) == expected


def test_discover_existing_matrix_and_install_siblings(tmp_path):
    from inttest import opjarrelease
    matrix = tmp_path / 'matrix.env'
    matrix.write_text('export STRIIM_BUILD_5_4_2="install one"\n'
                      'STRIIM_BUILD_5_4_4_0=install-two\n'
                      'STRIIM_BUILD_5_4_4_0_JAVA=17\nSTRIIM_BUILD_5_4_9_JAVA=17\n')
    lib = tmp_path / 'installs' / 'third' / 'lib'
    lib.mkdir(parents=True)
    (lib / 'Platform-5.4.4.1.jar').touch()
    (lib / 'Platform-5.4.4.1-sources.jar').touch()
    env = {'FTC_PUBLISHER_RELEASES_ENV': str(matrix),
           'STRIIM_HOME': str(tmp_path / 'installs' / 'selected')}
    assert opjarrelease.configured_versions(tmp_path, env) == {'5.4.2', '5.4.4.0', '5.4.4.1'}
    assert opjarrelease.configured_versions(tmp_path, {}) == set()


def _jar(path, version):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('META-INF/MANIFEST.MF', 'Striim-Build-Version: ' + version +
                   '\nStriim-Build-Series: ' + '.'.join(version.split('.')[:2]) +
                   '\nStriim-Build-Java: ' + ('11' if version.startswith('5.2.') else '17') + '\n')


@pytest.fixture(params=['livetest', 'inttest'])
def engine(request, tmp_path, monkeypatch):
    mod = importlib.import_module(request.param + '.opartifacts')
    monkeypatch.setattr(mod, '_root', lambda: tmp_path)
    monkeypatch.setattr(mod, 'resolve_build_java_home', lambda *a: None)
    monkeypatch.setattr(mod, '_verify_striim_home', lambda *a: None)
    for key in list(os.environ):
        if key.startswith(('SLT_PREBUILT_', 'STRIIM_BUILD_', 'FTC_')) or key == 'STRIIM_HOME':
            monkeypatch.delenv(key)
    (tmp_path / '.env').write_text('STRIIM_BUILD_5_4_2=one\nSTRIIM_BUILD_5_4_4_0=two\n')
    return mod


def test_other_release_jar_does_not_skip_or_change_operator(engine, tmp_path):
    module = tmp_path / 'ExampleOp'
    module.mkdir()
    (module / 'pom.xml').write_text('<project xmlns="http://maven.apache.org/POM/4.0.0"><build>'
                                  '<finalName>ExampleOpV1-${STRIIM_SERIES}</finalName></build></project>')
    other = module / 'target' / 'ExampleOpV1-5.4.jar'
    _jar(other, '5.4.4.0')
    before = other.read_bytes()
    calls = []

    def run(argv, cwd, env):
        assert env['STRIIM_SERIES'] == '5.4'
        pom = Path(argv[argv.index('-f') + 1])
        assert '<project xmlns=' in pom.read_text()  # Maven rejects prefixed project tags.
        tree = ET.parse(pom)
        output = Path(tree.findtext('{*}build/{*}directory'))
        assert output != module / 'target'
        assert output.is_relative_to(module / 'target')
        calls.append((output, pom))
        _jar(output / 'ExampleOpV1-5.4.jar', env['STRIIM_VERSION'])
        return SimpleNamespace(returncode=0)

    artifacts = []
    for version, suffix in [('5.4.2', '5.4.2'), ('5.4.4.0', '5.4.4')]:
        release = {'STRIIM_VERSION': version, 'STRIIM_SERIES': '5.4', 'JAVA_RELEASE': '17'}
        built = engine.build_jar('ExampleOp', release, run=run)
        assert built.path == module / 'target' / f'ExampleOpV1-{suffix}.jar'
        assert built.op_name == engine.op_name_for(other.name, '5.4')
        assert engine._read_manifest_fingerprint(built.path)['STRIIM_VERSION'] == version
        artifacts.append((built.path, built.path.read_bytes()))
        assert engine.build_jar('ExampleOp', release, run=run) == built
    assert len(calls) == 2
    assert calls[0][0] != calls[1][0]
    assert all(not pom.exists() for _, pom in calls)
    assert other.read_bytes() == before
    assert all(p.read_bytes() == data for p, data in artifacts)


def test_prebuilt_directory_selects_release_suffix(engine, tmp_path, monkeypatch):
    folder = tmp_path / 'prebuilt'
    for suffix, version in [('5.4.2', '5.4.2'), ('5.4.4', '5.4.4.0')]:
        _jar(folder / f'ExampleOpV1-{suffix}.jar', version)
    monkeypatch.setenv('SLT_PREBUILT_DIR', str(folder))
    built = engine.build_jar('ExampleOp', {'STRIIM_VERSION': '5.4.2', 'STRIIM_SERIES': '5.4'},
                             run=lambda *a: pytest.fail('prebuilt must not build'))
    assert built.name == 'ExampleOpV1-5.4.2.jar'
    assert built.op_name == 'ExampleOpV1'


@pytest.mark.parametrize('matrix', ['', 'STRIIM_BUILD_5_4_2=one\nSTRIIM_BUILD_5_2_0_8=two\n'])
def test_unique_or_unknown_set_reuses_series_name(engine, tmp_path, matrix):
    (tmp_path / '.env').write_text(matrix)
    jar = tmp_path / 'ExampleOp/target/ExampleOpV1-5.4.jar'
    _jar(jar, '5.4.2')
    built = engine.build_jar('ExampleOp', {'STRIIM_VERSION': '5.4.2', 'STRIIM_SERIES': '5.4',
                                         'JAVA_RELEASE': '17'},
                             run=lambda *a: pytest.fail('series jar should be reused'))
    assert built.path == jar
    assert built.op_name == 'ExampleOpV1'


def test_parallel_release_builds_have_separate_clean_outputs(engine, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import shutil
    import threading
    module = tmp_path / 'ExampleOp'
    module.mkdir()
    (module / 'pom.xml').write_text('<project><build/></project>')
    # Existing shared classes force clean; cleaning must be confined to each release.
    (module / 'target/classes').mkdir(parents=True)
    with (tmp_path / '.env').open('a') as matrix:
        matrix.write('STRIIM_BUILD_5_2_0_8=three\n')
    barrier = threading.Barrier(3)

    def run(argv, cwd, env):
        assert 'clean' in argv
        pom = Path(argv[argv.index('-f') + 1])
        output = Path(ET.parse(pom).findtext('build/directory'))
        if output.exists():
            shutil.rmtree(output)
        barrier.wait(timeout=10)
        _jar(output / f"ExampleOpV1-{env['STRIIM_SERIES']}.jar", env['STRIIM_VERSION'])
        return SimpleNamespace(returncode=0)

    def build(version):
        return engine.build_jar('ExampleOp', {'STRIIM_VERSION': version,
                                              'STRIIM_SERIES': '.'.join(version.split('.')[:2]),
                                              'JAVA_RELEASE': '11' if version.startswith('5.2.') else '17'}, run=run)

    with ThreadPoolExecutor(max_workers=3) as pool:
        built = list(pool.map(build, ['5.4.2', '5.4.4.0', '5.2.0.8']))
    assert {b.name for b in built} == {'ExampleOpV1-5.4.2.jar', 'ExampleOpV1-5.4.4.jar', 'ExampleOpV1-5.2.jar'}
    assert [engine._read_manifest_fingerprint(b.path)['STRIIM_VERSION'] for b in built] == ['5.4.2', '5.4.4.0', '5.2.0.8']
    assert (module / 'target/classes').exists()
    assert not list(module.glob('.release-build-*.xml'))


def test_matrix_label_is_resolved_to_install_version(tmp_path):
    from inttest import opjarrelease
    lib = tmp_path / 'install/lib'
    lib.mkdir(parents=True)
    (lib / 'Platform-5.4.4.0.jar').touch()
    (tmp_path / '.env').write_text('STRIIM_BUILD_5_4_4=install\n')
    versions = opjarrelease.configured_versions(tmp_path, {})
    assert versions == {'5.4.4.0'}
    release = {'STRIIM_SERIES': '5.4', 'STRIIM_VERSION': '5.4.4.0'}
    assert opjarrelease.jar_suffix(release, versions) == '5.4'
    versions.add('5.4.2')
    assert opjarrelease.jar_suffix(release, versions) == '5.4.4'


@pytest.mark.parametrize('package', ['livetest', 'inttest'])
def test_artifacts_import_with_only_their_engine_on_path(tmp_path, package):
    import subprocess
    import sys
    tier = 'live' if package == 'livetest' else 'integration'
    engine_path = Path(__file__).resolve().parents[1] / 'scripts' / tier
    env = {**os.environ, 'PYTHONPATH': str(engine_path), 'SLT_PROJECT_ROOT': str(tmp_path)}
    result = subprocess.run([sys.executable, '-c',
                             f'from {package} import opartifacts; '
                             'print(opartifacts.op_name_for("ExampleOpV1-5.4.jar", "5.4"))'],
                            cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'ExampleOpV1'


def test_release_helpers_are_identical_between_engines():
    scripts = Path(__file__).resolve().parents[1] / 'scripts'
    assert (scripts / 'live/livetest/opjarrelease.py').read_bytes() == (
        scripts / 'integration/inttest/opjarrelease.py').read_bytes()
