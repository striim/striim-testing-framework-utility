"""Fast (no maven, no Striim) unit tests for inttest.releases / inttest.opartifacts
(docs/INTEGRATION-TESTS.md).

Covers release detection, jar-name derivation, MANIFEST fingerprint parsing, and the
mtime staleness guard -- including the SPEC §8 "build-staleness gotcha" extension that
folds OpenProcessorCommon's source tree into every op's staleness check. Nothing here
shells out to `mvn` or touches STRIIM_HOME/java on disk beyond a fake fixture tree; the
real end-to-end build lives in test_opartifacts_build.py (gated on STRIIM_HOME).
"""
from __future__ import annotations

import os
import struct
import types
import zipfile

import pytest

from inttest import opartifacts, releases


# --------------------------------------------------------------------------------
# releases.py
# --------------------------------------------------------------------------------

def _make_platform_jar(lib_dir, version):
    lib_dir.mkdir(parents=True, exist_ok=True)
    (lib_dir / f"Platform-{version}.jar").write_bytes(b"not a real jar")


def test_detect_release_from_platform_jar(tmp_path):
    home = tmp_path / "striim_home"
    _make_platform_jar(home / "lib", "5.4.0.6C")

    rel = releases.detect_release(home)

    assert rel["STRIIM_VERSION"] == "5.4.0.6C"
    assert rel["STRIIM_RELEASE"] == "5.4.0.6C"
    assert rel["STRIIM_SERIES"] == "5.4"
    assert rel["JAVA_RELEASE"] == "17"
    assert "MSSQL_JDBC_VERSION" not in rel


def test_detect_release_picks_up_mssql_jdbc(tmp_path):
    home = tmp_path / "striim_home"
    _make_platform_jar(home / "lib", "5.0.0.3")
    (home / "lib" / "mssql-jdbc-9.4.1.jre8.jar").write_bytes(b"x")

    rel = releases.detect_release(home)

    assert rel["STRIIM_SERIES"] == "5.0"
    assert rel["JAVA_RELEASE"] == "11"
    assert rel["MSSQL_JDBC_VERSION"] == "9.4.1.jre8"


def test_detect_release_no_platform_jar_raises(tmp_path):
    home = tmp_path / "striim_home"
    (home / "lib").mkdir(parents=True)

    with pytest.raises(releases.ReleaseError):
        releases.detect_release(home)


def test_detect_release_ambiguous_platform_jars_raises(tmp_path):
    home = tmp_path / "striim_home"
    _make_platform_jar(home / "lib", "5.4.0.6C")
    (home / "lib" / "Platform-5.4.0.5.jar").write_bytes(b"x")

    with pytest.raises(releases.ReleaseError):
        releases.detect_release(home)


def test_resolve_release_with_striim_home(tmp_path):
    home = tmp_path / "striim_home"
    _make_platform_jar(home / "lib", "5.2.1.1")

    rel = releases.resolve_release({"STRIIM_HOME": str(home)})

    assert rel["STRIIM_VERSION"] == "5.2.1.1"
    assert rel["STRIIM_SERIES"] == "5.2"
    assert rel["JAVA_RELEASE"] == "11"


def test_resolve_release_falls_back_when_striim_home_unset():
    rel = releases.resolve_release({})

    assert rel["STRIIM_VERSION"] == "5.4.2"
    assert rel["STRIIM_SERIES"] == "5.4"
    assert rel["JAVA_RELEASE"] == "17"


# --------------------------------------------------------------------------------
# opartifacts.op_name_for / module_name
# --------------------------------------------------------------------------------

def test_op_name_for_strips_series_suffix():
    assert opartifacts.op_name_for("ReferenceOpV1-5.4.jar", "5.4") == "ReferenceOpV1"


def test_op_name_for_rejects_wrong_suffix():
    with pytest.raises(opartifacts.OpArtifactError):
        opartifacts.op_name_for("ReferenceOpV1-5.0.jar", "5.4")


def test_module_name_from_dir_ref():
    assert opartifacts.module_name("java/OpenProcessors/ReferenceOp") == "ReferenceOp"


def test_module_name_from_pom_ref():
    assert opartifacts.module_name("java/OpenProcessors/ReferenceOp/pom.xml") == "ReferenceOp"


# --------------------------------------------------------------------------------
# opartifacts._read_manifest_fingerprint
# --------------------------------------------------------------------------------

def _write_jar_with_manifest(path, manifest_text):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/MANIFEST.MF", manifest_text)


def test_read_manifest_fingerprint_complete_stamp(tmp_path):
    jar = tmp_path / "Thing-5.4.jar"
    _write_jar_with_manifest(jar, (
        "Manifest-Version: 1.0\n"
        "Striim-Build-Version: 5.4.0.6C\n"
        "Striim-Build-Series: 5.4\n"
        "Striim-Build-Java: 17\n"
    ))

    fp = opartifacts._read_manifest_fingerprint(jar)

    assert fp == {"STRIIM_VERSION": "5.4.0.6C", "STRIIM_SERIES": "5.4", "JAVA_RELEASE": "17"}


def test_read_manifest_fingerprint_missing_entries_returns_none(tmp_path):
    jar = tmp_path / "Thing-5.4.jar"
    _write_jar_with_manifest(jar, "Manifest-Version: 1.0\nStriim-Build-Version: 5.4.0.6C\n")

    assert opartifacts._read_manifest_fingerprint(jar) is None


def test_read_manifest_fingerprint_unresolved_property_returns_none(tmp_path):
    jar = tmp_path / "Thing-5.4.jar"
    _write_jar_with_manifest(jar, (
        "Manifest-Version: 1.0\n"
        "Striim-Build-Version: ${STRIIM_VERSION}\n"
        "Striim-Build-Series: 5.4\n"
        "Striim-Build-Java: 17\n"
    ))

    assert opartifacts._read_manifest_fingerprint(jar) is None


def test_read_manifest_fingerprint_no_manifest_returns_none(tmp_path):
    jar = tmp_path / "Thing-5.4.jar"
    with zipfile.ZipFile(jar, "w") as z:
        z.writestr("some/other/File.class", b"x")

    assert opartifacts._read_manifest_fingerprint(jar) is None


# --------------------------------------------------------------------------------
# opartifacts bytecode Java-level guard
# --------------------------------------------------------------------------------

def _write_jar_with_class(path, class_name, major):
    header = b"\xca\xfe\xba\xbe" + struct.pack(">HH", 0, major)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(class_name, header + b"\x00" * 4)


def test_jar_own_class_major_reads_own_class(tmp_path):
    jar = tmp_path / "Thing-5.4.jar"
    _write_jar_with_class(jar, "com/example/ThingV1/Processor.class", 61)  # Java 17

    assert opartifacts._jar_own_class_major(jar) == 61


def test_assert_jar_java_version_matches_ok(tmp_path):
    jar = tmp_path / "Thing-5.4.jar"
    _write_jar_with_class(jar, "com/example/ThingV1/Processor.class", 61)  # Java 17

    opartifacts._assert_jar_java_version(jar, "17", tmp_path)  # does not raise


def test_assert_jar_java_version_mismatch_raises(tmp_path):
    jar = tmp_path / "Thing-5.4.jar"
    _write_jar_with_class(jar, "com/example/ThingV1/Processor.class", 55)  # Java 11

    with pytest.raises(opartifacts.OpArtifactError):
        opartifacts._assert_jar_java_version(jar, "17", tmp_path)


# --------------------------------------------------------------------------------
# opartifacts._stale_trigger -- including the OpenProcessorCommon fold (SPEC §8)
# --------------------------------------------------------------------------------

@pytest.fixture
def op_and_common(tmp_path, monkeypatch):
    """A fake op module + a fake OpenProcessorCommon dir, wired so _stale_trigger
    consults the fake Common tree instead of the real repo's."""
    module_dir = tmp_path / "SomeOp"
    (module_dir / "src" / "main" / "java").mkdir(parents=True)
    (module_dir / "pom.xml").write_text("<project/>")
    op_src_file = module_dir / "src" / "main" / "java" / "Processor.java"
    op_src_file.write_text("class Processor {}")

    common_dir = tmp_path / "OpenProcessorCommon"
    (common_dir / "src" / "main" / "java").mkdir(parents=True)
    common_src_file = common_dir / "src" / "main" / "java" / "Logger.java"
    common_src_file.write_text("class Logger {}")

    monkeypatch.setattr(opartifacts, "_common_dir", lambda: common_dir)

    target = module_dir / "target"
    target.mkdir()
    jar = target / "SomeOp-5.4.jar"
    jar.write_bytes(b"jar")

    return module_dir, common_dir, jar, op_src_file, common_src_file


def _touch(path, when):
    os.utime(path, (when, when))


def test_stale_trigger_none_when_jar_newest(op_and_common):
    module_dir, common_dir, jar, op_src_file, common_src_file = op_and_common
    base = 1_700_000_000.0
    _touch(op_src_file, base)
    _touch(common_src_file, base)
    _touch((module_dir / "pom.xml"), base)
    _touch(jar, base + 100)

    assert opartifacts._stale_trigger(jar, module_dir) is None


def test_stale_trigger_detects_op_source_newer(op_and_common):
    module_dir, common_dir, jar, op_src_file, common_src_file = op_and_common
    base = 1_700_000_000.0
    _touch(jar, base)
    _touch((module_dir / "pom.xml"), base)
    _touch(common_src_file, base)
    _touch(op_src_file, base + 100)   # op's own source edited after build

    trigger = opartifacts._stale_trigger(jar, module_dir)

    assert trigger == op_src_file


def test_stale_trigger_detects_common_source_newer(op_and_common):
    # The SPEC §8 "build-staleness gotcha": editing a shared OpenProcessorCommon file
    # must trigger a rebuild of a dependent op even though it lives outside the op's
    # own src/ tree.
    module_dir, common_dir, jar, op_src_file, common_src_file = op_and_common
    base = 1_700_000_000.0
    _touch(jar, base)
    _touch((module_dir / "pom.xml"), base)
    _touch(op_src_file, base)
    _touch(common_src_file, base + 100)   # shared contract edited after the op's build

    trigger = opartifacts._stale_trigger(jar, module_dir)

    assert trigger == common_src_file


def test_stale_trigger_missing_jar_returns_jar_path(op_and_common):
    module_dir, common_dir, jar, op_src_file, common_src_file = op_and_common
    jar.unlink()

    assert opartifacts._stale_trigger(jar, module_dir) == jar


def test_stale_trigger_common_itself_not_double_folded(tmp_path, monkeypatch):
    # When module_dir IS OpenProcessorCommon, its own tree is already covered by the
    # "module's own src/" scan -- the fold-in must not run twice (harmless either way,
    # but keep the exemption honest: no crash, correct answer).
    common_dir = tmp_path / "OpenProcessorCommon"
    (common_dir / "src" / "main" / "java").mkdir(parents=True)
    src_file = common_dir / "src" / "main" / "java" / "Logger.java"
    src_file.write_text("class Logger {}")
    monkeypatch.setattr(opartifacts, "_common_dir", lambda: common_dir)

    jar = common_dir / "target" / "OpenProcessorCommon-5.4.jar"
    jar.parent.mkdir(parents=True)
    jar.write_bytes(b"jar")

    base = 1_700_000_000.0
    _touch(jar, base)
    _touch(src_file, base + 100)

    trigger = opartifacts._stale_trigger(jar, common_dir)

    assert trigger == src_file


# --------------------------------------------------------------------------------
# opartifacts.build_jar -- ambiguous-output and corrupt-jar clean-rebuild triggers
# (behavior ported from the live tier, commit 2f313977a + the corrupt-jar guard)
# --------------------------------------------------------------------------------

MODULE = "java/OpenProcessors/ReferenceOp"
RELEASE_54 = {"STRIIM_RELEASE": "5.4.0.6", "STRIIM_VERSION": "5.4.0.6",
              "STRIIM_SERIES": "5.4", "JAVA_RELEASE": "17"}


def _fp(release):
    return {"STRIIM_VERSION": release["STRIIM_VERSION"],
            "STRIIM_SERIES": release["STRIIM_SERIES"],
            "JAVA_RELEASE": release["JAVA_RELEASE"]}


def _jar(jar, *, fp=None, major=None):
    # A real (zip) jar, optionally carrying a Striim-Build-* MANIFEST stamp and/or a
    # com/example class of a given bytecode major version. Same helper shape as
    # scripts/live/tests/test_opartifacts.py.
    jar.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(jar, "w") as z:
        if fp is not None:
            z.writestr("META-INF/MANIFEST.MF",
                       "Manifest-Version: 1.0\r\n"
                       f"Striim-Build-Version: {fp['STRIIM_VERSION']}\r\n"
                       f"Striim-Build-Series: {fp['STRIIM_SERIES']}\r\n"
                       f"Striim-Build-Java: {fp['JAVA_RELEASE']}\r\n")
        if major is not None:
            z.writestr("com/example/X.class",
                       b"\xca\xfe\xba\xbe\x00\x00" + struct.pack(">H", major))


def _mk(path, when, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (when, when))


def _hermetic_build_env(tmp_path, monkeypatch):
    # Keep build_jar's environment probing out of these hermetic tests: JAVA_HOME
    # resolution shells out to `java`/`java_home`, the STRIIM_HOME<->STRIIM_VERSION
    # guard hits the real filesystem, and _stale_trigger's Common fold must consult a
    # fake OpenProcessorCommon under tmp_path, not the real repo's.
    monkeypatch.setattr(opartifacts, "_root", lambda: tmp_path)
    monkeypatch.setattr(opartifacts, "_common_dir", lambda: tmp_path / "java" / "OpenProcessors" / "OpenProcessorCommon")
    monkeypatch.setattr(opartifacts, "resolve_build_java_home", lambda *a, **k: None)
    monkeypatch.setattr(opartifacts, "_verify_striim_home", lambda *a, **k: None)


def test_build_jar_clean_rebuilds_on_ambiguous_output(tmp_path, monkeypatch):
    # Several *-5.4.jar in target/ is stale build output (a version bump writes the new
    # jar beside the old one), not a broken repo. It must trigger a CLEAN rebuild -- a
    # plain `package` would leave the stale jar and stay ambiguous forever. Previously
    # this raised OpArtifactError, which plugin.py turned into pytest.skip: a skip reads
    # like a pass, so the module silently stopped being tested.
    _hermetic_build_env(tmp_path, monkeypatch)
    mod = tmp_path / MODULE
    old = mod / "target" / "ReferenceOpV1B-5.4.jar"
    new = mod / "target" / "ReferenceOpV1-5.4.jar"
    _mk(old, 1000)
    _mk(new, 1000)
    goals, reasons = [], []

    def fake_run(argv, cwd, env):
        goals.extend(argv)
        old.unlink()                       # what `mvn clean` does to the stale jar
        _jar(new, fp=_fp(RELEASE_54), major=61)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    built = opartifacts.build_jar(MODULE, RELEASE_54, run=fake_run, report=reasons.append)
    assert "clean" in goals, "ambiguity must force `mvn clean package`, not a plain package"
    assert built.name == "ReferenceOpV1-5.4.jar"
    assert "clean rebuild" in reasons[0]
    assert "ReferenceOpV1B-5.4.jar" in reasons[0]   # names what made it ambiguous


def test_build_jar_still_raises_if_ambiguous_after_the_clean_rebuild(tmp_path, monkeypatch):
    # The clean rebuild is the recovery, not a licence to guess: if target/ STILL holds
    # two candidates afterwards, something is genuinely wrong and it must fail loudly.
    _hermetic_build_env(tmp_path, monkeypatch)
    mod = tmp_path / MODULE
    _mk(mod / "target" / "ReferenceOpV1-5.4.jar", 1000)
    _mk(mod / "target" / "ReferenceOpOther-5.4.jar", 1000)
    noop = lambda argv, cwd, env: types.SimpleNamespace(returncode=0, stdout="", stderr="")
    with pytest.raises(opartifacts.OpBuildFailed, match="no unambiguous"):
        opartifacts.build_jar(MODULE, RELEASE_54, run=noop)


def test_build_jar_fails_when_the_clean_rebuild_itself_fails(tmp_path, monkeypatch):
    # A version bump plus a compile error: the incremental build passes on stale classes and
    # leaves two jars, then the clean rebuild fails. That is a broken module, not an environment.
    _hermetic_build_env(tmp_path, monkeypatch)
    mod = tmp_path / MODULE
    _mk(mod / "src" / "main" / "java" / "X.java", 2000)      # newer than the jar -> rebuild
    old = mod / "target" / "ReferenceOpV1-5.4.jar"
    _jar(old, fp=_fp(RELEASE_54), major=61)
    os.utime(old, (1000, 1000))
    goals = []

    def fake_run(argv, cwd, env):
        goals.append(argv)
        if "clean" in argv:
            return types.SimpleNamespace(returncode=1, stdout="", stderr="javac error")
        _jar(mod / "target" / "ReferenceOpV1B-5.4.jar", fp=_fp(RELEASE_54), major=61)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    with pytest.raises(opartifacts.OpBuildFailed, match="mvn clean package failed"):
        opartifacts.build_jar(MODULE, RELEASE_54, run=fake_run)
    assert len(goals) == 2 and "clean" not in goals[0], "the incremental build ran first"


def test_build_jar_clean_rebuilds_a_corrupt_jar(tmp_path, monkeypatch):
    # A truncated jar (interrupted/concurrent `mvn`) is NEWER than every source, so the
    # mtime check says reuse, and the manifest/bytecode guards read None ("unknown")
    # from it, which must not block for a VALID jar. Reusing it hands the Java harness a
    # jar it dies on (`ZipException: zip END header not found` in
    # IntegrationProcessor.readServiceImplementation) -- a test FAILURE where
    # PERF_SPEC.md §2 wants a rebuild (or, if unbuildable, a skip). It must instead
    # trigger a CLEAN rebuild (the interrupted build may have left target/classes
    # half-written too, so a plain `package` is not enough).
    _hermetic_build_env(tmp_path, monkeypatch)
    mod = tmp_path / MODULE
    _mk(mod / "src" / "main" / "java" / "X.java", 1000)
    jar = mod / "target" / "ReferenceOpV1-5.4.jar"
    _jar(jar, fp=_fp(RELEASE_54), major=61)
    jar.write_bytes(jar.read_bytes()[:-20])   # truncate: the zip END header is gone
    os.utime(jar, (2000, 2000))               # newer than every source -> mtime says reuse
    goals, reasons = [], []

    def fake_run(argv, cwd, env):
        goals.extend(argv)
        _jar(jar, fp=_fp(RELEASE_54), major=61)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    built = opartifacts.build_jar(MODULE, RELEASE_54, run=fake_run, report=reasons.append)
    assert built.path == jar
    assert "clean" in goals, "a corrupt jar must force `mvn clean package`, not a reuse"
    assert "not a readable jar" in reasons[0] and "clean rebuild" in reasons[0]


def test_build_jar_reuses_a_valid_up_to_date_jar(tmp_path, monkeypatch):
    # Baseline for the two triggers above: one readable, release-matched jar newer than
    # every source is reused with NO mvn invocation and NO report.
    _hermetic_build_env(tmp_path, monkeypatch)
    mod = tmp_path / MODULE
    _mk(mod / "src" / "main" / "java" / "X.java", 1000)
    jar = mod / "target" / "ReferenceOpV1-5.4.jar"
    _jar(jar, fp=_fp(RELEASE_54), major=61)
    os.utime(jar, (2000, 2000))
    reasons = []
    built = opartifacts.build_jar(
        MODULE, RELEASE_54,
        run=lambda *a: pytest.fail("must not build"), report=reasons.append)
    assert built.path == jar
    assert reasons == []


def test_build_jar_still_reuses_a_valid_jar_with_no_stamp_and_no_own_classes(tmp_path, monkeypatch):
    # Guard against the corruption probe over-firing: a READABLE jar for which both
    # guards genuinely can't determine anything (no Striim-Build-* stamp, no
    # com/example class) is "unknown but valid" and must still be reused -- the
    # corrupt-jar fix must not turn it into a rebuild storm.
    _hermetic_build_env(tmp_path, monkeypatch)
    mod = tmp_path / MODULE
    _mk(mod / "src" / "main" / "java" / "X.java", 1000)
    jar = mod / "target" / "ReferenceOpV1-5.4.jar"
    jar.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(jar, "w") as z:
        z.writestr("com/other/Dep.class", b"x")   # valid zip, nothing determinable
    os.utime(jar, (2000, 2000))                   # newer than every source
    opartifacts.build_jar(MODULE, RELEASE_54, run=lambda *a: pytest.fail("must not build"))


def test_jar_own_class_major_finds_a_project_package_and_a_relocated_one(tmp_path):
    mod = tmp_path / "FooOp"
    (mod / "src/main/java/com/acme/FooOp").mkdir(parents=True)
    (mod / "src/main/java/com/acme/FooOp/Processor.java").write_text("package com.acme.FooOp;")
    jar = tmp_path / "FooOpV2C-5.4.jar"
    _write_jar_with_class(jar, "com/acme/FooOpV2C/Processor.class", 61)
    assert opartifacts._jar_own_class_major(jar, mod) == 61
    assert opartifacts._jar_own_class_major(jar) is None


def test_a_dependency_class_sharing_a_source_name_is_not_own(tmp_path):
    # An OP may ship com/google/cloud/spanner/Dialect (Java 8) beside its Orm/Dialect.java.
    # The impostors here sort BEFORE the module's own class, so a name-only match would read them.
    mod = tmp_path / "FooOp"
    (mod / "src/main/java/com/zeta/FooOp/Orm").mkdir(parents=True)
    (mod / "src/main/java/com/zeta/FooOp/Orm/Dialect.java").write_text("package com.zeta.FooOp.Orm;")
    jar = tmp_path / "FooOpV8F-5.4.jar"
    header = lambda major: b"\xca\xfe\xba\xbe" + struct.pack(">HH", 0, major) + b"\x00" * 4
    with zipfile.ZipFile(jar, "w") as z:
        z.writestr("com/aaa/shaded/FooOpV8F/Orm/Dialect.class", header(51))   # a shaded copy
        z.writestr("com/google/cloud/spanner/Dialect.class", header(52))      # a dependency
        z.writestr("com/zeta/FooOpV8F/Orm/Dialect.class", header(61))         # the module's own
    assert opartifacts._jar_own_class_major(jar, mod) == 61
    opartifacts._assert_jar_java_version(jar, "17", mod)      # no false wrong-JDK error
