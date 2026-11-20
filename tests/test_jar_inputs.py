"""Both engines rebuild an OP jar when anything Maven compiles into it changed: one input set.

The input set is the module's src/main and pom.xml, every root its pom add-sources (a shared
library such as OpenProcessorCommon or FieldCommon), and the parent poms it inherits from.
"""
import importlib
import os
import types
import zipfile
from pathlib import Path

import pytest

ENGINES = ['livetest', 'inttest']
SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'

POM = """<project>
  <!-- <parent><relativePath>../../nowhere/pom.xml</relativePath></parent> -->
  <parent><groupId>g</groupId><artifactId>op-parent</artifactId><version>1</version></parent>
  <build><plugins><plugin><executions><execution><configuration><sources>
    <source>${project.basedir}/../OpenProcessorCommon/src/main/java</source>
    <source>${project.basedir}/../../FieldCommon/src/main/java</source>
  </sources></configuration></execution></executions></plugin></plugins></build>
</project>
"""
OLD, JAR_TIME, EDIT_TIME = 1_700_000_000, 1_700_000_100, 1_700_000_500


def _engine(name):
    return importlib.import_module(name + '.opartifacts')


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / 'proj'
    mod = root / 'java/OpenProcessors/MyOp'
    files = {
        'own': mod / 'src/main/java/m/MyOp.java',
        'common': root / 'java/OpenProcessors/OpenProcessorCommon/src/main/java/c/Common.java',
        'field': root / 'java/FieldCommon/src/main/java/f/Shared.java',
        'field2': root / 'java/FieldCommon/src/main/java/f/Other.java',
        'parent': root / 'java/OpenProcessors/pom.xml',
        'aggregator': root / 'java/pom.xml',
    }
    for f in files.values():
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text('// v1\n')
    files['parent'].write_text('<project><modules><module>MyOp</module></modules></project>')
    (mod / 'pom.xml').write_text(POM)
    jar = mod / 'target/MyOp-5.4.jar'
    jar.parent.mkdir()
    with zipfile.ZipFile(jar, 'w') as z:
        z.writestr('META-INF/MANIFEST.MF', 'Manifest-Version: 1.0\n')
    for name in ENGINES:
        monkeypatch.setattr(_engine(name), '_root', lambda: root)
    for key in list(os.environ):
        if key.startswith(('SLT_PREBUILT_', 'STRIIM_BUILD_', 'FTC_')) or key == 'STRIIM_HOME':
            monkeypatch.delenv(key)
    for f in [*files.values(), mod / 'pom.xml']:
        os.utime(f, (OLD, OLD))
    return types.SimpleNamespace(root=root, mod=mod, jar=jar, **files)


def _built(engine, p):
    """The state build_jar leaves: the jar and its source record, newer than every source."""
    engine._write_source_record(p.jar, p.mod)
    os.utime(p.jar, (JAR_TIME, JAR_TIME))
    assert engine._stale_trigger(p.jar, p.mod) is None


def _edit(path):
    path.write_text('// v2: behaviour changed\n')
    os.utime(path, (EDIT_TIME, EDIT_TIME))


def _same(a, b):
    return a is not None and Path(os.path.normpath(a)).resolve() == b.resolve()


@pytest.mark.parametrize('name', ENGINES)
@pytest.mark.parametrize('edited', ['own', 'common', 'field', 'parent'])
def test_an_edit_to_any_input_is_a_rebuild_trigger(project, name, edited):
    engine = _engine(name)
    _built(engine, project)
    _edit(getattr(project, edited))
    assert _same(engine._stale_trigger(project.jar, project.mod), getattr(project, edited))


@pytest.mark.parametrize('edited', ['own', 'common', 'field', 'parent'])
def test_both_engines_name_the_same_trigger(project, edited):
    live, integration = _engine('livetest'), _engine('inttest')
    _built(integration, project)
    _edit(getattr(project, edited))
    assert _same(live._stale_trigger(project.jar, project.mod), getattr(project, edited))
    assert _same(integration._stale_trigger(project.jar, project.mod), getattr(project, edited))


@pytest.mark.parametrize('name', ENGINES)
@pytest.mark.parametrize('deleted', ['own', 'field'])
def test_a_deleted_input_is_a_rebuild_trigger(project, name, deleted):
    engine = _engine(name)
    _built(engine, project)
    getattr(project, deleted).unlink()
    trigger = engine._stale_trigger(project.jar, project.mod)
    assert _same(trigger, getattr(project, deleted)) and not trigger.exists()


@pytest.mark.parametrize('name', ENGINES)
def test_an_aggregator_pom_the_module_does_not_inherit_from_is_not_an_input(project, name):
    engine = _engine(name)
    _built(engine, project)
    _edit(project.aggregator)        # java/pom.xml lists modules; nothing declares it a parent
    assert engine._stale_trigger(project.jar, project.mod) is None


@pytest.mark.parametrize('name', ENGINES)
def test_a_jar_without_a_source_record_is_a_rebuild_trigger(project, name):
    engine = _engine(name)
    os.utime(project.jar, (JAR_TIME, JAR_TIME))
    assert engine._stale_trigger(project.jar, project.mod) == engine._source_record(project.jar)


@pytest.mark.parametrize('pom,expected', [
    ('<project/>', []),                                                         # no parent
    ('<project><parent><artifactId>p</artifactId></parent></project>', ['java/OpenProcessors/pom.xml']),
    ('<project><parent><relativePath/></parent></project>', []),                # repository only
    ('<project><parent><relativePath></relativePath></parent></project>', []),
    ('<project><parent><relativePath>../..</relativePath></parent></project>', ['java/pom.xml']),
    ('<project><parent><relativePath>../../pom.xml</relativePath></parent></project>', ['java/pom.xml']),
    ('<project><parent><relativePath>../missing.xml</relativePath></parent></project>', []),
])
def test_parent_poms_follow_maven_relative_path(project, pom, expected):
    (project.mod / 'pom.xml').write_text(pom)
    for name in ENGINES:
        jarinputs = _engine(name).jarinputs
        assert jarinputs.parent_poms(project.mod) == [(project.root / e).resolve() for e in expected]


def test_parent_poms_follow_the_chain_and_stop_on_a_cycle(project):
    project.parent.write_text('<project><parent><relativePath>../pom.xml</relativePath></parent></project>')
    project.aggregator.write_text('<project><parent><relativePath>OpenProcessors</relativePath></parent></project>')
    from livetest import jarinputs
    assert jarinputs.parent_poms(project.mod) == [project.parent.resolve(), project.aggregator.resolve()]


# The three spellings of an add-source root Maven accepts. A plain relative path is relative to
# the module dir, never to the directory the run was started from.
SOURCE_STYLES = {
    'basedir': lambda p: ('${project.basedir}/../OpenProcessorCommon/src/main/java',
                          '${project.basedir}/../../FieldCommon/src/main/java'),
    'relative': lambda p: ('../OpenProcessorCommon/src/main/java', '../../FieldCommon/src/main/java'),
    'absolute': lambda p: (str(p.common.parents[2]), str(p.field.parents[2])),
}


def _source_style(p, style, monkeypatch):
    """Rewrite the module pom's add-source roots in `style` and run from outside the module."""
    common, field = SOURCE_STYLES[style](p)
    (p.mod / 'pom.xml').write_text(POM.replace(SOURCE_STYLES['basedir'](p)[0], common)
                                   .replace(SOURCE_STYLES['basedir'](p)[1], field))
    os.utime(p.mod / 'pom.xml', (OLD, OLD))
    monkeypatch.chdir(p.root.parent)


@pytest.mark.parametrize('style', SOURCE_STYLES)
@pytest.mark.parametrize('edited', ['common', 'field'])
def test_integration_build_jar_rebuilds_after_a_shared_root_edit(project, monkeypatch, style, edited):
    engine = _engine('inttest')
    _source_style(project, style, monkeypatch)
    monkeypatch.setattr(engine.opjarrelease, 'configured_versions', lambda *a, **k: set())
    monkeypatch.setattr(engine, 'resolve_build_java_home', lambda *a, **k: None)
    monkeypatch.setattr(engine, '_verify_striim_home', lambda *a, **k: None)
    monkeypatch.setattr(engine, '_assert_jar_java_version', lambda *a, **k: None)
    _built(engine, project)
    _edit(getattr(project, edited))
    ran = []

    def mvn(argv, cwd, env):
        ran.append(argv)
        os.utime(project.jar, (EDIT_TIME + 1, EDIT_TIME + 1))
        return types.SimpleNamespace(returncode=0, stdout='', stderr='')

    release = {'STRIIM_SERIES': '5.4', 'STRIIM_VERSION': '5.4.2', 'JAVA_RELEASE': '17'}
    built = engine.build_jar('java/OpenProcessors/MyOp', release, run=mvn)
    assert built.origin == 'built' and ran and ran[0][0] == 'mvn' and 'clean' in ran[0]


@pytest.mark.parametrize('style', SOURCE_STYLES)
def test_live_build_jar_clean_rebuilds_after_a_source_is_deleted(project, monkeypatch, style):
    engine = _engine('livetest')
    _source_style(project, style, monkeypatch)
    monkeypatch.setattr(engine.opjarrelease, 'configured_versions', lambda *a, **k: set())
    monkeypatch.setattr(engine, 'resolve_build_java_home', lambda *a, **k: None)
    monkeypatch.setattr(engine, '_verify_striim_home', lambda *a, **k: None)
    monkeypatch.setattr(engine, '_require_platform_home', lambda *a, **k: None)
    monkeypatch.setattr(engine, '_assert_jar_java_version', lambda *a, **k: None)
    _built(engine, project)
    project.field2.unlink()
    ran, reasons = [], []

    def mvn(argv, cwd, env):
        ran.append(argv)
        os.utime(project.jar, (EDIT_TIME + 1, EDIT_TIME + 1))
        return types.SimpleNamespace(returncode=0, stdout='', stderr='')

    release = {'STRIIM_SERIES': '5.4', 'STRIIM_VERSION': '5.4.2', 'JAVA_RELEASE': '17'}
    built = engine.build_jar('java/OpenProcessors/MyOp', release, run=mvn, report=reasons.append)
    assert built.origin == 'built' and 'clean' in ran[0]
    assert 'source removed' in reasons[0]
    assert engine._stale_trigger(project.jar, project.mod) is None     # the new record holds


def test_both_engines_use_the_one_input_set_helper():
    from livetest import jarinputs
    assert _engine('inttest').jarinputs is jarinputs and _engine('livetest').jarinputs is jarinputs
    assert not (SCRIPTS / 'integration/inttest/jarinputs.py').exists()


def _hermetic_build_jar(engine, project, monkeypatch, package=None):
    """build_jar with the environment probes stubbed out; returns (built, mvn argvs, reasons).
    `package`, if given, is called with each Maven argv to model what that build writes."""
    for probe in ('_verify_striim_home', '_require_platform_home', '_assert_jar_java_version',
                  'resolve_build_java_home'):
        if hasattr(engine, probe):
            monkeypatch.setattr(engine, probe, lambda *a, **k: None)
    monkeypatch.setattr(engine.opjarrelease, 'configured_versions', lambda *a, **k: set())
    ran, reasons = [], []

    def mvn(argv, cwd, env):
        ran.append(argv)
        if package:
            package(argv)
        os.utime(project.jar, (EDIT_TIME + 1, EDIT_TIME + 1))
        return types.SimpleNamespace(returncode=0, stdout='', stderr='')

    release = {'STRIIM_SERIES': '5.4', 'STRIIM_VERSION': '5.4.2', 'JAVA_RELEASE': '17'}
    built = engine.build_jar('java/OpenProcessors/MyOp', release, run=mvn, report=reasons.append)
    return built, ran, reasons


@pytest.mark.parametrize('name', ENGINES)
@pytest.mark.parametrize('edited', ['own', 'field'])
@pytest.mark.parametrize('mtime', [OLD, OLD - 100], ids=['same-mtime', 'older-mtime'])
def test_changed_bytes_restored_with_an_old_mtime_clean_rebuild(project, monkeypatch, name, edited, mtime):
    # A checkout, archive extract or copy that preserves timestamps can change an input's bytes
    # without making it newer than the jar. Maven's compiler decides by mtime too, so only a
    # clean build recompiles it.
    engine = _engine(name)
    _built(engine, project)
    path = getattr(project, edited)
    path.write_text('// v2: behaviour changed\n')
    os.utime(path, (mtime, mtime))
    assert _same(engine._stale_trigger(project.jar, project.mod), path)
    built, ran, reasons = _hermetic_build_jar(engine, project, monkeypatch)
    assert built.origin == 'built' and 'clean' in ran[0]
    assert 'clean rebuild' in reasons[0]
    assert engine._stale_trigger(project.jar, project.mod) is None     # the new record holds


MIXED_EDITS = {
    # (changed behind its old mtime, changed normally so it is newer than the jar)
    'java-and-resource': ('src/main/java/m/MyOp.java', 'src/main/resources/settings.properties'),
    'resource-and-resource': ('src/main/resources/a.txt', 'src/main/resources/b.txt'),
}


@pytest.mark.parametrize('name', ENGINES)
@pytest.mark.parametrize('mixed', MIXED_EDITS)
def test_a_newer_input_does_not_mask_a_change_behind_an_old_mtime(project, monkeypatch, name, mixed):
    # Maven recompiles and recopies only what is newer than its previous output, so a build
    # without `clean` keeps the old output of the input whose bytes changed behind an old mtime,
    # even though the other, newer input makes the jar stale. The record of that
    # build would then vouch for a jar holding the old bytes.
    engine = _engine(name)
    hidden, newer = (project.mod / rel for rel in MIXED_EDITS[mixed])
    for f in (hidden, newer):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text('v1\n')
        os.utime(f, (OLD, OLD))
    _built(engine, project)
    output = {hidden: 'v1\n', newer: 'v1\n'}     # what target/classes and the jar hold

    def package(argv):
        for f in output:
            if 'clean' in argv or f.stat().st_mtime > JAR_TIME:
                output[f] = f.read_text()

    hidden.write_text('v2\n')
    os.utime(hidden, (OLD, OLD))
    _edit(newer)
    built, ran, reasons = _hermetic_build_jar(engine, project, monkeypatch, package)
    assert built.origin == 'built' and len(ran) == 1 and 'clean' in ran[0]
    assert 'clean rebuild' in reasons[0]
    assert output == {hidden: 'v2\n', newer: newer.read_text()}, 'the jar kept stale bytes'
    assert engine._stale_trigger(project.jar, project.mod) is None     # and the record says so


@pytest.mark.parametrize('name', ENGINES)
def test_a_record_of_names_only_is_a_rebuild_trigger(project, name):
    # A record written before it carried content digests cannot vouch for the jar's bytes.
    engine = _engine(name)
    jarinputs = _engine(name).jarinputs
    srcs = jarinputs.inputs(project.mod)
    engine._source_record(project.jar).write_text(
        ''.join(n + '\n' for n in sorted({os.path.relpath(p, project.mod) for p in srcs})))
    os.utime(project.jar, (JAR_TIME, JAR_TIME))
    assert engine._stale_trigger(project.jar, project.mod) == engine._source_record(project.jar)


@pytest.mark.parametrize('name', ENGINES)
def test_a_source_saved_during_the_build_leaves_the_jar_stale(project, monkeypatch, name):
    # Maven has already compiled MyOp.java when the edit is saved, so the jar holds the bytes from
    # before the edit. The record must carry the digests the build started from: one taken after
    # the build would vouch for the edit, and the next run would reuse the jar over it.
    engine = _engine(name)
    _built(engine, project)
    _edit(project.own)

    def package(argv):
        project.own.write_text('// v3: saved while Maven was packaging\n')
        os.utime(project.own, (EDIT_TIME, EDIT_TIME))     # older than the jar the build writes

    built, ran, reasons = _hermetic_build_jar(engine, project, monkeypatch, package)
    assert built.origin == 'built' and len(ran) == 1
    assert _same(engine._stale_trigger(project.jar, project.mod), project.own)


@pytest.mark.parametrize('name', ENGINES)
@pytest.mark.parametrize('change', ['edited', 'deleted', 'unchanged'])
def test_a_build_walks_the_input_tree_once(project, monkeypatch, name, change):
    # Each walk is an rglob over src/main, every add-sourced root and the parent poms; the staleness
    # check, the rebuild's reason and the record all use the one list.
    engine = _engine(name)
    _built(engine, project)
    if change == 'edited':
        _edit(project.own)
    elif change == 'deleted':
        project.field2.unlink()
    walks = []
    real = engine.jarinputs.inputs
    monkeypatch.setattr(engine.jarinputs, 'inputs', lambda d: walks.append(d) or real(d))
    built, ran, reasons = _hermetic_build_jar(engine, project, monkeypatch)
    assert built.origin == ('reused' if change == 'unchanged' else 'built')
    assert len(walks) == 1


@pytest.mark.parametrize('name', ENGINES)
@pytest.mark.parametrize('change', ['edited', 'unchanged'])
def test_an_input_gone_between_the_walk_and_its_digest_is_a_change_not_a_crash(project, monkeypatch, name, change):
    # An editor's swap file under src/main is walked, then deleted before it is digested (review N5).
    engine = _engine(name)
    _built(engine, project)
    if change == 'edited':
        _edit(project.own)
    swap = project.mod / 'src/main/java/m/.MyOp.java.swp'
    real = engine.jarinputs.inputs
    monkeypatch.setattr(engine.jarinputs, 'inputs', lambda d: real(d) + [swap])
    built, ran, reasons = _hermetic_build_jar(engine, project, monkeypatch)
    assert built.origin == 'built' and len(ran) == 1
    monkeypatch.setattr(engine.jarinputs, 'inputs', real)
    assert engine._stale_trigger(project.jar, project.mod) is None     # the record holds what was read
    swap.write_text('back\n')
    assert _same(engine._stale_trigger(project.jar, project.mod), swap)  # and one that returns is a change


@pytest.mark.parametrize('name', ENGINES)
def test_build_jar_writes_its_record_through_the_helper_the_tests_use(project, monkeypatch, name):
    # The record is written once, by _write_source_record, from the digests taken before Maven ran
    # (review N7): a test that sets up a built jar with the helper sets it up as build_jar does.
    engine = _engine(name)
    _built(engine, project)
    _edit(project.own)
    before = engine.jarinputs.digests(project.mod, engine.jarinputs.inputs(project.mod))
    written = []
    real = engine._write_source_record
    monkeypatch.setattr(engine, '_write_source_record',
                        lambda jar, mod, recorded=None: written.append(recorded) or real(jar, mod, recorded))

    def package(argv):
        project.own.write_text('// v3: saved while Maven was packaging\n')

    built, ran, reasons = _hermetic_build_jar(engine, project, monkeypatch, package)
    assert built.origin == 'built'
    assert written == [before]
