import types
import os
import struct
import zipfile
from pathlib import Path

import pytest

from livetest import opartifacts
from livetest.opartifacts import (
    _module_dir, module_name, op_name_for, build_jar, upload_artifacts,
    place_on_agent, resolve_build_java_home, OpArtifactError,
)

MODULE = "java/OpenProcessors/ExampleMapOp"
UDF_MODULE = "java/UserDefinedFunctions/ExampleJsonUdf"

RELEASE_54 = {"STRIIM_RELEASE": "5.4.0.6", "STRIIM_VERSION": "5.4.0.6",
              "STRIIM_SERIES": "5.4", "JAVA_RELEASE": "17"}
RELEASE_50 = {"STRIIM_RELEASE": "5.0.6.2F", "STRIIM_VERSION": "5.0.6.2F",
              "STRIIM_SERIES": "5.0", "JAVA_RELEASE": "11",
              "MSSQL_JDBC_VERSION": "7.2.2.jre8"}

# ---- module dir / name inference (OP or UDF, module-dir or legacy target-path) ----

def test_module_dir_accepts_bare_module_dir():
    assert _module_dir(MODULE).as_posix() == MODULE
    assert _module_dir(UDF_MODULE).as_posix() == UDF_MODULE

def test_module_dir_accepts_pom_xml_path():
    assert _module_dir(f"{MODULE}/pom.xml").as_posix() == MODULE

def test_module_dir_back_compat_target_jar_path():
    # legacy shape: <module>/target/<versioned>.jar -> still resolves to the module dir
    assert _module_dir(f"{MODULE}/target/ExampleMapOpV8G.jar").as_posix() == MODULE

def test_module_name_returns_module_dir_basename():
    assert module_name(MODULE) == "ExampleMapOp"
    assert module_name("java/OpenProcessors/LookupOp") == "LookupOp"

# ---- OP_JAR -> OP_NAME derivation --------------------------------------------

def test_op_name_for_strips_series_suffix():
    assert op_name_for("ExampleMapOpV8G-5.4.jar", "5.4") == "ExampleMapOpV8G"
    assert op_name_for("ExampleJsonUdfV1-5.4.jar", "5.4") == "ExampleJsonUdfV1"

def test_op_name_for_raises_when_suffix_missing():
    with pytest.raises(OpArtifactError, match="5.4"):
        op_name_for("ExampleMapOpV8G.jar", "5.4")

# ---- build_jar ----------------------------------------------------------------

def _touch(p: Path, mtime: float, text="x"):
    p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text)
    os.utime(p, (mtime, mtime))

def _hermetic_build_env(monkeypatch):
    # Keep build_jar's environment probing out of these hermetic tests: JAVA_HOME resolution
    # shells out to `java`/`java_home`, and the STRIIM_HOME<->STRIIM_VERSION install guard hits
    # the real filesystem. Both have their own focused tests below.
    monkeypatch.setattr(opartifacts, "resolve_build_java_home", lambda *a, **k: None)
    monkeypatch.setattr(opartifacts, "_verify_striim_home", lambda *a, **k: None)

def test_build_jar_reuses_when_up_to_date(tmp_path, monkeypatch):
    monkeypatch.setattr(opartifacts, "_root", lambda: tmp_path)
    _hermetic_build_env(monkeypatch)
    mod = tmp_path / MODULE
    _touch(mod / "src/main/java/X.java", 1000)
    _touch(mod / "pom.xml", 1000)
    jar = mod / "target" / "ExampleMapOpV8G-5.4.jar"
    _jar(jar, fp=_fp(RELEASE_54), major=61)   # a real (readable) jar -- a text stand-in
    os.utime(jar, (2000, 2000))               # would now correctly count as corrupt
    calls = []
    built = build_jar(MODULE, RELEASE_54, run=lambda argv, cwd, env: calls.append(argv))
    assert built.path == jar
    assert built.name == "ExampleMapOpV8G-5.4.jar"
    assert built.op_name == "ExampleMapOpV8G"
    assert calls == []                                    # up-to-date -> no build

def test_build_jar_rebuilds_when_source_newer(tmp_path, monkeypatch):
    # a source edited after the jar was built -> stale -> CLEAN rebuild (the footgun fix:
    # an old jar must NOT silently mask a source change). `clean` is unconditional for a
    # source change: this harness never builds incrementally.
    monkeypatch.setattr(opartifacts, "_root", lambda: tmp_path)
    _hermetic_build_env(monkeypatch)
    mod = tmp_path / MODULE
    jar = mod / "target" / "ExampleMapOpV8G-5.4.jar"
    _jar(jar, fp=_fp(RELEASE_54), major=61)               # readable jar, but stale
    os.utime(jar, (1000, 1000))
    _touch(mod / "src/main/java/X.java", 2000)            # source newer than jar
    calls = []
    def fake_run(argv, cwd, env):
        calls.append((argv, cwd, env)); jar.write_text("rebuilt")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    built = build_jar(MODULE, RELEASE_54, run=fake_run)
    assert built.path == jar
    assert len(calls) == 1
    argv, cwd, env = calls[0]
    assert argv == ["mvn", "-q", "clean", "package", "-DskipTests=true"]
    assert cwd == str(mod)
    assert env["STRIIM_RELEASE"] == "5.4.0.6" and env["STRIIM_SERIES"] == "5.4"

def test_build_jar_merges_release_env_incl_lib_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr(opartifacts, "_root", lambda: tmp_path)
    _hermetic_build_env(monkeypatch)
    mod = tmp_path / "java/OpenProcessors/ExampleCdcHelper"
    jar = mod / "target" / "ExampleCdcHelperV3-5.0.jar"
    seen = {}
    def fake_run(argv, cwd, env):
        seen["env"] = env
        jar.parent.mkdir(parents=True, exist_ok=True); jar.write_text("built")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    build_jar("java/OpenProcessors/ExampleCdcHelper", RELEASE_50, run=fake_run)
    assert seen["env"]["MSSQL_JDBC_VERSION"] == "7.2.2.jre8"
    assert seen["env"]["JAVA_RELEASE"] == "11"

def test_build_jar_rebuilds_when_pom_newer(tmp_path, monkeypatch):
    monkeypatch.setattr(opartifacts, "_root", lambda: tmp_path)
    _hermetic_build_env(monkeypatch)
    mod = tmp_path / MODULE
    jar = mod / "target" / "ExampleMapOpV8G-5.4.jar"
    _jar(jar, fp=_fp(RELEASE_54), major=61)               # readable jar, but stale
    os.utime(jar, (1000, 1000))
    _touch(mod / "src/main/java/X.java", 500)             # source older
    _touch(mod / "pom.xml", 2000)                          # pom newer than jar
    calls = []
    def fake_run(argv, cwd, env):
        calls.append(argv); jar.write_text("rebuilt")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    build_jar(MODULE, RELEASE_54, run=fake_run)
    assert calls == [["mvn", "-q", "clean", "package", "-DskipTests=true"]]

def test_build_jar_runs_mvn_package_in_module_dir_then_returns_built(tmp_path, monkeypatch):
    monkeypatch.setattr(opartifacts, "_root", lambda: tmp_path)
    _hermetic_build_env(monkeypatch)
    jar = tmp_path / UDF_MODULE / "target" / "ExampleJsonUdfV1-5.4.jar"   # a UDF module
    seen = {}
    def fake_run(argv, cwd, env):
        seen["argv"] = argv; seen["cwd"] = cwd
        jar.parent.mkdir(parents=True, exist_ok=True); jar.write_text("built")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    built = build_jar(UDF_MODULE, RELEASE_54, run=fake_run)
    assert built.path == jar
    assert built.op_name == "ExampleJsonUdfV1"
    assert seen["argv"] == ["mvn", "-q", "package", "-DskipTests=true"]
    assert seen["cwd"].endswith("java/UserDefinedFunctions/ExampleJsonUdf")   # built in the module dir

def test_build_jar_raises_on_mvn_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(opartifacts, "_root", lambda: tmp_path)
    _hermetic_build_env(monkeypatch)
    fail = lambda argv, cwd, env: types.SimpleNamespace(returncode=1, stdout="", stderr="boom")
    with pytest.raises(OpArtifactError, match="mvn package failed"):
        build_jar(MODULE, RELEASE_54, run=fail)

def test_build_jar_raises_when_jar_absent_after_build(tmp_path, monkeypatch):
    monkeypatch.setattr(opartifacts, "_root", lambda: tmp_path)
    _hermetic_build_env(monkeypatch)
    ok_but_no_jar = lambda argv, cwd, env: types.SimpleNamespace(returncode=0, stdout="", stderr="")
    with pytest.raises(OpArtifactError, match="no unambiguous"):
        build_jar(MODULE, RELEASE_54, run=ok_but_no_jar)

def test_build_jar_raises_on_missing_series():
    with pytest.raises(OpArtifactError, match="STRIIM_SERIES"):
        build_jar(MODULE, {"STRIIM_VERSION": "5.4.0.6"})

def test_build_jar_clean_rebuilds_on_ambiguous_output(tmp_path, monkeypatch):
    # Several *-5.4.jar in target/ is stale build output (a version bump writes the new
    # jar beside the old one), not a broken repo. It must trigger a CLEAN rebuild -- a
    # plain `package` would leave the stale jar and stay ambiguous forever. Previously
    # this raised, which made the live test SKIP: a skip reads like a pass.
    monkeypatch.setattr(opartifacts, "_root", lambda: tmp_path)
    _hermetic_build_env(monkeypatch)
    mod = tmp_path / MODULE
    old = mod / "target" / "ExampleJsonUdfV1B-5.4.jar"
    new = mod / "target" / "ExampleMapOpV8G-5.4.jar"
    _touch(old, 1000)
    _touch(new, 1000)
    goals, reasons = [], []

    def fake_run(argv, cwd, env):
        goals.extend(argv)
        old.unlink()                       # what `mvn clean` does to the stale jar
        _jar(new, fp=_fp(RELEASE_54), major=61)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    built = build_jar(MODULE, RELEASE_54, run=fake_run, report=reasons.append)
    assert "clean" in goals, "ambiguity must force `mvn clean package`, not a plain package"
    assert built.name == "ExampleMapOpV8G-5.4.jar"
    assert "clean rebuild" in reasons[0]
    assert "ExampleJsonUdfV1B-5.4.jar" in reasons[0]   # names what made it ambiguous


def test_build_jar_still_raises_if_ambiguous_after_the_clean_rebuild(tmp_path, monkeypatch):
    # The clean rebuild is the recovery, not a licence to guess: if target/ STILL holds
    # two candidates afterwards, something is genuinely wrong and it must fail loudly.
    monkeypatch.setattr(opartifacts, "_root", lambda: tmp_path)
    _hermetic_build_env(monkeypatch)
    mod = tmp_path / MODULE
    _touch(mod / "target" / "ExampleMapOpV8G-5.4.jar", 1000)
    _touch(mod / "target" / "ExampleMapOpOther-5.4.jar", 1000)
    noop = lambda argv, cwd, env: types.SimpleNamespace(returncode=0, stdout="", stderr="")
    with pytest.raises(OpArtifactError, match="no unambiguous"):
        build_jar(MODULE, RELEASE_54, run=noop)

def test_build_jar_clean_rebuilds_a_corrupt_jar(tmp_path, monkeypatch):
    # A truncated jar (interrupted/concurrent `mvn`) is NEWER than every source, so the
    # mtime check says reuse, and the manifest/bytecode guards read None ("unknown") from
    # it, which must not block for a VALID jar. Reusing it uploads a jar Striim dies on
    # (`ZipException: zip END header not found`) -- a failure blamed on the test. It must
    # instead trigger a CLEAN rebuild (the interrupted build may have left target/classes
    # half-written too, so a plain `package` is not enough).
    monkeypatch.setattr(opartifacts, "_root", lambda: tmp_path)
    _hermetic_build_env(monkeypatch)
    mod = tmp_path / MODULE
    _touch(mod / "src/main/java/X.java", 1000)
    jar = mod / "target" / "ExampleMapOpV8G-5.4.jar"
    _jar(jar, fp=_fp(RELEASE_54), major=61)
    jar.write_bytes(jar.read_bytes()[:-20])   # truncate: the zip END header is gone
    os.utime(jar, (2000, 2000))               # newer than every source -> mtime says reuse
    goals, reasons = [], []

    def fake_run(argv, cwd, env):
        goals.extend(argv)
        _jar(jar, fp=_fp(RELEASE_54), major=61)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    built = build_jar(MODULE, RELEASE_54, run=fake_run, report=reasons.append)
    assert built.path == jar
    assert "clean" in goals, "a corrupt jar must force `mvn clean package`, not a reuse"
    assert "not a readable jar" in reasons[0] and "clean rebuild" in reasons[0]


def test_build_jar_still_reuses_a_valid_jar_with_no_stamp_and_no_own_classes(tmp_path, monkeypatch):
    # Guard against the corruption probe over-firing: a READABLE jar for which both
    # guards genuinely can't determine anything (no Striim-Build-* stamp, no
    # com/example class) is "unknown but valid" and must still be reused -- the
    # corrupt-jar fix must not turn it into a rebuild storm.
    monkeypatch.setattr(opartifacts, "_root", lambda: tmp_path)
    _hermetic_build_env(monkeypatch)
    mod = tmp_path / MODULE
    _touch(mod / "src/main/java/X.java", 1000)
    jar = mod / "target" / "ExampleMapOpV8G-5.4.jar"
    jar.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(jar, "w") as z:
        z.writestr("com/other/Dep.class", b"x")   # valid zip, nothing determinable
    os.utime(jar, (2000, 2000))                   # newer than every source
    build_jar(MODULE, RELEASE_54, run=lambda *a: pytest.fail("must not build"))

# ---- release MANIFEST stamp + bytecode guard -----------------------------------

def _fp(release):
    return {"STRIIM_VERSION": release["STRIIM_VERSION"], "STRIIM_SERIES": release["STRIIM_SERIES"],
            "JAVA_RELEASE": release["JAVA_RELEASE"]}

def _jar(jar: Path, *, fp=None, major=None):
    # A real (zip) jar, optionally carrying a Striim-Build-* MANIFEST stamp and/or a
    # com/example class of a given bytecode major version.
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
                       b"\xca\xfe\xba\xbe\x00\x00" + struct.pack(">H", major))  # magic + minor + major

def test_build_jar_reuses_when_manifest_matches(tmp_path, monkeypatch):
    monkeypatch.setattr(opartifacts, "_root", lambda: tmp_path)
    _hermetic_build_env(monkeypatch)
    mod = tmp_path / MODULE
    _touch(mod / "src/main/java/X.java", 1000)
    jar = mod / "target" / "ExampleMapOpV8G-5.4.jar"
    _jar(jar, fp=_fp(RELEASE_54), major=61)
    os.utime(jar, (2000, 2000))            # newer than source
    calls = []
    build_jar(MODULE, RELEASE_54, run=lambda argv, cwd, env: calls.append(argv))
    assert calls == []                     # manifest matches + up-to-date -> reuse

def test_build_jar_clean_rebuilds_when_manifest_is_for_a_different_release(tmp_path, monkeypatch):
    # Same series (5.4) jar, but the MANIFEST is stamped for a different STRIIM_VERSION.
    # This used to RAISE and tell the operator to run `mvn clean` by hand. It now performs
    # that clean rebuild itself -- the jar is still never REUSED, which is the property that
    # mattered, but a stale jar from a previous release no longer fails the run. `clean` is
    # mandatory: a plain `package` can shade stale .class files from the other release.
    monkeypatch.setattr(opartifacts, "_root", lambda: tmp_path)
    _hermetic_build_env(monkeypatch)
    mod = tmp_path / MODULE
    _touch(mod / "src/main/java/X.java", 1000)
    jar = mod / "target" / "ExampleMapOpV8G-5.4.jar"
    _jar(jar, fp={"STRIIM_VERSION": "5.4.0.6C", "STRIIM_SERIES": "5.4", "JAVA_RELEASE": "17"}, major=61)
    os.utime(jar, (2000, 2000))
    goals, reasons = [], []

    def fake_run(argv, cwd, env):
        goals.extend(argv)
        _jar(jar, fp=_fp(RELEASE_54), major=61)   # the rebuild restamps it correctly
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    built = build_jar(MODULE, RELEASE_54, run=fake_run, report=reasons.append)
    assert built.path == jar
    assert goals, "a jar stamped for another release must be rebuilt, never reused as-is"
    assert "clean" in goals, "a different release must force `mvn clean package`"
    assert "was built for" in reasons[0] and "clean rebuild" in reasons[0]


def test_build_jar_still_raises_if_the_release_stamp_survives_the_clean_rebuild(tmp_path, monkeypatch):
    # The clean rebuild is the recovery, not a licence to guess -- the same principle as
    # test_build_jar_still_raises_if_ambiguous_after_the_clean_rebuild. If the jar that
    # LANDS is still stamped for another release, the build is not producing what this run
    # asked for, and shipping it would upload a jar for the wrong Striim release -- exactly
    # what the pre-rebuild guard existed to prevent.
    #
    # Note the bytecode guard cannot catch this: both releases here are Java 17, so
    # _assert_jar_java_version passes and only the manifest stamp reveals the mismatch.
    monkeypatch.setattr(opartifacts, "_root", lambda: tmp_path)
    _hermetic_build_env(monkeypatch)
    mod = tmp_path / MODULE
    _touch(mod / "src/main/java/X.java", 1000)
    jar = mod / "target" / "ExampleMapOpV8G-5.4.jar"
    wrong = {"STRIIM_VERSION": "5.4.0.6C", "STRIIM_SERIES": "5.4", "JAVA_RELEASE": "17"}
    _jar(jar, fp=wrong, major=61)
    os.utime(jar, (2000, 2000))

    def fake_run(argv, cwd, env):
        _jar(jar, fp=wrong, major=61)             # the rebuild does NOT fix the stamp
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    with pytest.raises(OpArtifactError, match="still stamped for"):
        build_jar(MODULE, RELEASE_54, run=fake_run)

def test_build_jar_absent_manifest_reused_when_bytecode_matches(tmp_path, monkeypatch):
    # a jar with NO Striim-Build-* stamp but the right Java level is accepted, not rejected.
    monkeypatch.setattr(opartifacts, "_root", lambda: tmp_path)
    _hermetic_build_env(monkeypatch)
    mod = tmp_path / MODULE
    _touch(mod / "src/main/java/X.java", 1000)
    jar = mod / "target" / "ExampleMapOpV8G-5.4.jar"
    _jar(jar, major=61)                    # no manifest stamp, Java-17 bytecode
    os.utime(jar, (2000, 2000))
    calls = []
    build_jar(MODULE, RELEASE_54, run=lambda argv, cwd, env: calls.append(argv))
    assert calls == []                     # reused (no stamp, but bytecode matches Java 17)

def test_build_jar_fails_on_wrong_bytecode_java_level(tmp_path, monkeypatch):
    # no stamp, but the jar's own bytecode is Java 17 while the release needs Java 11 -> fail.
    monkeypatch.setattr(opartifacts, "_root", lambda: tmp_path)
    _hermetic_build_env(monkeypatch)
    mod = tmp_path / MODULE
    _touch(mod / "src/main/java/X.java", 1000)
    jar = mod / "target" / "ExampleMapOpV8G-5.0.jar"
    _jar(jar, major=61)                    # Java-17 bytecode
    os.utime(jar, (2000, 2000))
    with pytest.raises(OpArtifactError, match="class-file major version 61"):
        build_jar(MODULE, RELEASE_50, run=lambda *a: pytest.fail("must not build"))

def test_build_jar_cleans_when_target_holds_a_prior_release(tmp_path, monkeypatch):
    # no *-5.0.jar yet, but target/classes exists from a prior 5.4 build -> `mvn clean package`.
    monkeypatch.setattr(opartifacts, "_root", lambda: tmp_path)
    _hermetic_build_env(monkeypatch)
    mod = tmp_path / MODULE
    (mod / "target" / "classes").mkdir(parents=True)          # stale output from another release
    jar = mod / "target" / "ExampleMapOpV8G-5.0.jar"
    calls = []
    def fake_run(argv, cwd, env):
        calls.append(argv); jar.write_text("built")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    build_jar(MODULE, RELEASE_50, run=fake_run)
    assert calls == [["mvn", "-q", "clean", "package", "-DskipTests=true"]]

def test_build_jar_does_not_write_a_sidecar(tmp_path, monkeypatch):
    # the release stamp comes from the pom's MANIFEST; the harness must not create any sidecar.
    monkeypatch.setattr(opartifacts, "_root", lambda: tmp_path)
    _hermetic_build_env(monkeypatch)
    mod = tmp_path / MODULE
    jar = mod / "target" / "ExampleMapOpV8G-5.4.jar"
    def fake_run(argv, cwd, env):
        jar.parent.mkdir(parents=True, exist_ok=True); jar.write_text("built")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    build_jar(MODULE, RELEASE_54, run=fake_run)
    assert list((mod / "target").glob("*.slt-build.json")) == []   # no sidecar written

def test_build_jar_reports_reason_on_stale_rebuild(tmp_path, monkeypatch):
    # a source newer than the jar triggers a clean rebuild; `report` names the file.
    monkeypatch.setattr(opartifacts, "_root", lambda: tmp_path)
    _hermetic_build_env(monkeypatch)
    mod = tmp_path / MODULE
    jar = mod / "target" / "ExampleMapOpV8G-5.4.jar"
    _jar(jar, fp=_fp(RELEASE_54), major=61)
    os.utime(jar, (1000, 1000))
    _touch(mod / "src/main/java/X.java", 3000)     # source newer than jar -> stale
    reasons = []
    def fake_run(argv, cwd, env):
        jar.write_text("rebuilt"); return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    build_jar(MODULE, RELEASE_54, run=fake_run, report=reasons.append)
    assert len(reasons) == 1
    assert "source changed" in reasons[0] and "X.java" in reasons[0] and "clean rebuild" in reasons[0]

def test_build_jar_reports_reason_on_clean_rebuild(tmp_path, monkeypatch):
    # no *-5.0.jar but stale target/classes from a prior build -> clean rebuild, reason names it.
    monkeypatch.setattr(opartifacts, "_root", lambda: tmp_path)
    _hermetic_build_env(monkeypatch)
    mod = tmp_path / MODULE
    (mod / "target" / "classes").mkdir(parents=True)
    jar = mod / "target" / "ExampleMapOpV8G-5.0.jar"
    reasons = []
    def fake_run(argv, cwd, env):
        jar.write_text("built"); return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    build_jar(MODULE, RELEASE_50, run=fake_run, report=reasons.append)
    assert reasons == ["no *-5.0.jar and stale target/classes present -> clean rebuild"]

def test_build_jar_does_not_report_on_reuse(tmp_path, monkeypatch):
    # an up-to-date jar is reused with no rebuild -> the report callback is never called.
    monkeypatch.setattr(opartifacts, "_root", lambda: tmp_path)
    _hermetic_build_env(monkeypatch)
    mod = tmp_path / MODULE
    _touch(mod / "src/main/java/X.java", 1000)
    jar = mod / "target" / "ExampleMapOpV8G-5.4.jar"
    _jar(jar, fp=_fp(RELEASE_54), major=61)
    os.utime(jar, (2000, 2000))                    # newer than source -> reuse
    reasons = []
    build_jar(MODULE, RELEASE_54, run=lambda *a: pytest.fail("must not build"), report=reasons.append)
    assert reasons == []

def test_read_manifest_fingerprint_roundtrip(tmp_path):
    jar = tmp_path / "m.jar"; _jar(jar, fp=_fp(RELEASE_50))
    assert opartifacts._read_manifest_fingerprint(jar) == _fp(RELEASE_50)

def test_read_manifest_fingerprint_none_when_absent_or_unresolved(tmp_path):
    j1 = tmp_path / "none.jar"; _jar(j1, major=61)                       # no stamp
    assert opartifacts._read_manifest_fingerprint(j1) is None
    j2 = tmp_path / "unresolved.jar"                                     # an unresolved ${property}
    _jar(j2, fp={"STRIIM_VERSION": "${STRIIM_VERSION}", "STRIIM_SERIES": "5.4", "JAVA_RELEASE": "17"})
    assert opartifacts._read_manifest_fingerprint(j2) is None

def test_expected_class_major_maps_feature_to_major():
    assert opartifacts._expected_class_major("8") == 52
    assert opartifacts._expected_class_major("11") == 55
    assert opartifacts._expected_class_major("17") == 61
    assert opartifacts._expected_class_major(None) is None

def test_jar_own_class_major_reads_module_class_only(tmp_path):
    jar = tmp_path / "m.jar"
    jar.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(jar, "w") as z:
        z.writestr("com/fasterxml/jackson/Dep.class", b"\xca\xfe\xba\xbe\x00\x00" + struct.pack(">H", 52))  # dep: Java 8
        z.writestr("com/example/X.class", b"\xca\xfe\xba\xbe\x00\x00" + struct.pack(">H", 55))         # own: Java 11
    assert opartifacts._jar_own_class_major(jar) == 55   # the module's own class, not the shaded dep

def test_jar_own_class_major_none_when_unreadable(tmp_path):
    j = tmp_path / "notajar.jar"; j.write_text("plain text")
    assert opartifacts._jar_own_class_major(j) is None

def test_build_jar_rejects_striim_home_without_matching_version(tmp_path, monkeypatch):
    # STRIIM_HOME whose lib/ lacks Platform-<STRIIM_VERSION>.jar => fail fast, don't hand mvn
    # an impossible systemPath. (Guard NOT stubbed here — this is its focused test.)
    monkeypatch.setattr(opartifacts, "_root", lambda: tmp_path)
    monkeypatch.setattr(opartifacts, "resolve_build_java_home", lambda *a, **k: None)
    home = tmp_path / "striim_home"; (home / "lib").mkdir(parents=True)   # no Platform jar
    monkeypatch.setenv("STRIIM_HOME", str(home))
    mod = tmp_path / MODULE
    _touch(mod / "src/main/java/X.java", 1000)                            # no jar -> build path
    with pytest.raises(OpArtifactError, match="does not contain STRIIM_VERSION"):
        build_jar(MODULE, RELEASE_54, run=lambda *a, **k: types.SimpleNamespace(returncode=0))

def test_build_jar_accepts_striim_home_with_matching_version(tmp_path, monkeypatch):
    monkeypatch.setattr(opartifacts, "_root", lambda: tmp_path)
    monkeypatch.setattr(opartifacts, "resolve_build_java_home", lambda *a, **k: None)
    home = tmp_path / "striim_home"; (home / "lib").mkdir(parents=True)
    (home / "lib" / "Platform-5.4.0.6.jar").write_text("x")               # marker present
    monkeypatch.setenv("STRIIM_HOME", str(home))
    mod = tmp_path / MODULE
    jar = mod / "target" / "ExampleMapOpV8G-5.4.jar"
    _touch(mod / "src/main/java/X.java", 1000)
    def fake_run(argv, cwd, env):
        jar.parent.mkdir(parents=True, exist_ok=True); jar.write_text("built")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    assert build_jar(MODULE, RELEASE_54, run=fake_run).path == jar

# ---- resolve_build_java_home --------------------------------------------------

def test_resolve_build_java_home_noop_when_running_jdk_already_matches():
    run = lambda argv: types.SimpleNamespace(returncode=0, stdout="", stderr='openjdk version "17.0.9" 2023-10-17')
    assert resolve_build_java_home("17", run=run) is None

def test_resolve_build_java_home_switches_when_different(monkeypatch):
    calls = []
    def run(argv):
        calls.append(argv)
        if argv[0].endswith("java") or argv[0] == "java":
            return types.SimpleNamespace(returncode=0, stdout="", stderr='openjdk version "17.0.9" 2023-10-17')
        return types.SimpleNamespace(returncode=0, stdout="/Library/Java/JavaVirtualMachines/11.jdk/Contents/Home\n", stderr="")
    home = resolve_build_java_home("11", run=run, platform="Darwin")   # the macOS branch
    assert home == "/Library/Java/JavaVirtualMachines/11.jdk/Contents/Home"
    assert any("java_home" in a[0] for a in calls)

def test_resolve_build_java_home_raises_when_jdk_not_found(monkeypatch):
    def run(argv):
        if argv[0].endswith("java") or argv[0] == "java":
            return types.SimpleNamespace(returncode=0, stdout="", stderr='openjdk version "17.0.9" 2023-10-17')
        return types.SimpleNamespace(returncode=1, stdout="", stderr="Unable to find any JVMs matching version \"11\".")
    with pytest.raises(OpArtifactError, match="JDK 11"):
        resolve_build_java_home("11", run=run, platform="Darwin")

# ---- upload_artifacts -------------------------------------------------------

def test_upload_docker_cps_to_each_cluster_node(tmp_path):
    f = tmp_path / "ExampleMapOpV8G-5.4.jar"; f.write_text("x")
    cfg = tmp_path / "passthrough.json"; cfg.write_text("{}")
    calls = []
    upload_artifacts(types.SimpleNamespace(mode="docker"), [f, cfg],
                     run=lambda argv: calls.append(argv))
    cps = [c for c in calls if c[:2] == ["docker", "cp"]]
    # both files -> both cluster nodes
    assert {c[3].split(":")[0] for c in cps} == {"slt-striim", "slt-node"}
    assert any("ExampleMapOpV8G-5.4.jar" in c[3] for c in cps)
    assert any("passthrough.json" in c[3] for c in cps)
    assert any(c[:2] == ["docker", "exec"] and "chmod" in c for c in calls)
    # As root: Docker Desktop's `docker cp` keeps the host owner, which the striim user cannot chmod.
    assert all(c[2:4] == ["-u", "0"] for c in calls if c[:2] == ["docker", "exec"] and "chmod" in c)

def test_upload_native_copies_to_striim_home(tmp_path, monkeypatch):
    home = tmp_path / "striim"; (home / "UploadedFiles").mkdir(parents=True)
    monkeypatch.setenv("STRIIM_HOME", str(home))
    f = tmp_path / "ExampleMapOpV8G-5.4.jar"; f.write_text("x")
    upload_artifacts(types.SimpleNamespace(mode="native"), [f])
    assert (home / "UploadedFiles" / "ExampleMapOpV8G-5.4.jar").read_text() == "x"

def test_upload_native_without_home_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("STRIIM_HOME", raising=False)
    f = tmp_path / "x.jar"; f.write_text("x")
    with pytest.raises(OpArtifactError, match="STRIIM_HOME"):
        upload_artifacts(types.SimpleNamespace(mode="native"), [f])

def test_upload_native_home_without_uploadedfiles_is_a_named_error(tmp_path, monkeypatch):
    # STRIIM_HOME is an install, but not the running server's: no UploadedFiles/
    monkeypatch.setenv("STRIIM_HOME", str(tmp_path / "other-install"))
    f = tmp_path / "ReferenceUdfV1-5.4.jar"; f.write_text("x")
    with pytest.raises(OpArtifactError, match=r"^native-uploads-dir-missing: .*UploadedFiles does not exist; "
                                              r"STRIIM_HOME must be the install root of the running native server"):
        upload_artifacts(types.SimpleNamespace(mode="native"), [f])

def test_upload_native_unwritable_uploadedfiles_is_a_named_error(tmp_path, monkeypatch):
    home = tmp_path / "striim"; (home / "UploadedFiles").mkdir(parents=True)
    monkeypatch.setenv("STRIIM_HOME", str(home))
    f = tmp_path / "x.jar"; f.write_text("x")
    def denied(src, dst):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr("shutil.copy", denied)
    with pytest.raises(OpArtifactError, match=r"^native-uploads-dir-unwritable: placing x.jar in .*Permission denied"):
        upload_artifacts(types.SimpleNamespace(mode="native"), [f])


# ---- add-sourced shared libraries count as this module's source (rebuild trigger) ----

_POM_WITH_SHARED = """<project>
  <build><plugins><plugin>
    <artifactId>build-helper-maven-plugin</artifactId>
    <executions><execution><configuration><sources>
      <source>${project.basedir}/../OpenProcessorCommon/src/main/java</source>
      <source>${project.basedir}/../../SampleCommon/src/main/java</source>
    </sources></configuration></execution></executions>
  </plugin></plugins></build>
</project>
"""


def _module_with_shared_roots(tmp_path):
    mod = tmp_path / MODULE
    mod.mkdir(parents=True, exist_ok=True)
    # pom mtime pinned old too -- otherwise it is the newest file and wins the trigger,
    # masking whichever shared file the test is actually about.
    _touch(mod / "pom.xml", 1000, _POM_WITH_SHARED)
    _touch(mod / "src/main/java/X.java", 1000)
    _touch(mod.parent / "OpenProcessorCommon/src/main/java/Shared.java", 1000)
    _touch(tmp_path / "java/SampleCommon/src/main/java/com/example/common/WAEvents.java", 1000)
    return mod


def test_add_source_roots_resolves_basedir_relative_paths(tmp_path):
    mod = _module_with_shared_roots(tmp_path)
    roots = {p.name for p in opartifacts.add_source_roots(mod)}
    assert roots == {"java"}                       # both resolve to .../src/main/java
    assert len(opartifacts.add_source_roots(mod)) == 2


def test_add_source_roots_skips_unresolved_properties(tmp_path):
    mod = tmp_path / MODULE
    mod.mkdir(parents=True, exist_ok=True)
    (mod / "pom.xml").write_text(
        "<project><source>${some.other.prop}/src/main/java</source></project>")
    assert opartifacts.add_source_roots(mod) == []


def test_add_source_roots_empty_without_a_pom(tmp_path):
    mod = tmp_path / MODULE
    mod.mkdir(parents=True, exist_ok=True)
    assert opartifacts.add_source_roots(mod) == []


def test_stale_trigger_fires_when_an_add_sourced_shared_file_changes(tmp_path):
    # THE POINT: shared code compiles into this jar, so editing SampleCommon/WAEvents.java
    # must rebuild it. Before this, only module_dir/src was scanned and the live suite
    # uploaded the OLD jar -- green over a real regression.
    mod = _module_with_shared_roots(tmp_path)
    jar = mod / "target" / "ExampleMapOpV8G-5.4.jar"
    _touch(jar, 2000)
    shared = tmp_path / "java/SampleCommon/src/main/java/com/example/common/WAEvents.java"
    _touch(shared, 3000)                            # newer than the jar
    trigger = opartifacts._stale_trigger(jar, mod)
    assert trigger is not None and trigger.name == "WAEvents.java"


def test_stale_trigger_fires_for_openprocessorcommon_too(tmp_path):
    mod = _module_with_shared_roots(tmp_path)
    jar = mod / "target" / "ExampleMapOpV8G-5.4.jar"
    _touch(jar, 2000)
    _touch(mod.parent / "OpenProcessorCommon/src/main/java/Shared.java", 3000)
    trigger = opartifacts._stale_trigger(jar, mod)
    assert trigger is not None and trigger.name == "Shared.java"


def test_stale_trigger_still_none_when_shared_roots_are_older(tmp_path):
    # Guard against the fix over-firing: an up-to-date jar must still be REUSED.
    mod = _module_with_shared_roots(tmp_path)
    jar = mod / "target" / "ExampleMapOpV8G-5.4.jar"
    _touch(jar, 5000)                               # newer than every source
    assert opartifacts._stale_trigger(jar, mod) is None

# ---- place_on_agent ---------------------------------------------------------
#
# `LOAD OPEN PROCESSOR` cannot reach an agent: it distributes the module through LoadSCMTask,
# whose call() needs Server.server (absent on an AgentNode) and which is dispatched to Hazelcast
# MEMBERS while an agent is a CLIENT. So an agent-deployed flow whose source is an OP fails at
# DEPLOY with ClassNotFoundException however correctly the servers loaded the same jar. The only
# way in is the agent's own classpath plus a restart.

def _agent_run(calls, *, digest_matches=False, present=True):
    """A fake `docker` that records argv and answers the two probes place_on_agent makes."""
    def run(argv):
        calls.append(argv)
        if argv[:2] == ["docker", "inspect"]:
            return types.SimpleNamespace(returncode=0 if present else 1, stdout="", stderr="")
        if "sha256sum" in argv:
            if digest_matches:
                # place_on_agent compares the FIRST space-separated field against its own
                # digest, so echo back exactly that.
                import hashlib
                h = hashlib.sha256(Path(_agent_run.jar).read_bytes()).hexdigest()
                return types.SimpleNamespace(returncode=0, stdout=f"{h}  x", stderr="")
            return types.SimpleNamespace(returncode=1, stdout="", stderr="no such file")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    return run


def test_place_on_agent_copies_into_the_agent_lib_and_restarts(tmp_path):
    f = tmp_path / "StatusReaderV1-5.4.jar"; f.write_bytes(b"jar-bytes")
    _agent_run.jar = str(f)
    calls = []
    place_on_agent(types.SimpleNamespace(mode="docker"), [f], run=_agent_run(calls))

    cps = [c for c in calls if c[:2] == ["docker", "cp"]]
    assert len(cps) == 1
    assert cps[0][3].startswith("slt-agent:/opt/striim/agent/lib/")

    # Published by rename, like upload_artifacts: `docker cp` writes straight into the
    # destination, and here the reader is the agent JVM at startup -- which is exactly when a
    # truncated jar becomes ClassNotFoundException again, the symptom this exists to remove.
    assert cps[0][3].endswith(".tmp." + str(os.getpid()))
    movs = [c for c in calls if c[:2] == ["docker", "exec"] and "mv" in c]
    assert movs and movs[-1][-1] == "/opt/striim/agent/lib/StatusReaderV1-5.4.jar"

    # The restart is the point. A copy without it has no effect at all.
    assert ["docker", "restart", "slt-agent"] in calls


def test_place_on_agent_does_nothing_when_the_agent_already_holds_the_bytes(tmp_path):
    # An agent restart costs ~90s of re-registration, so an unchanged jar must not trigger one.
    f = tmp_path / "StatusReaderV1-5.4.jar"; f.write_bytes(b"jar-bytes")
    _agent_run.jar = str(f)
    calls = []
    place_on_agent(types.SimpleNamespace(mode="docker"), [f],
                   run=_agent_run(calls, digest_matches=True))

    assert not [c for c in calls if c[:2] == ["docker", "cp"]]
    assert not [c for c in calls if c[:2] == ["docker", "restart"]]


def test_place_on_agent_skips_when_there_is_no_agent_container(tmp_path):
    f = tmp_path / "StatusReaderV1-5.4.jar"; f.write_bytes(b"jar-bytes")
    _agent_run.jar = str(f)
    calls = []
    place_on_agent(types.SimpleNamespace(mode="docker"), [f],
                   run=_agent_run(calls, present=False))

    assert [c[:2] for c in calls] == [["docker", "inspect"]]


def test_place_on_agent_is_a_noop_in_native_mode(tmp_path):
    # A native single-node install has no server/agent split to exercise and no container to
    # copy into. Reported, not attempted.
    f = tmp_path / "StatusReaderV1-5.4.jar"; f.write_bytes(b"jar-bytes")
    notes = []
    calls = []
    place_on_agent(types.SimpleNamespace(mode="native"), [f],
                   run=lambda argv: calls.append(argv),
                   progress=lambda label, msg: notes.append(msg))
    assert not calls
    assert any("not a dockerized cluster" in n for n in notes)


def test_place_on_agent_with_no_files_does_nothing(tmp_path):
    calls = []
    place_on_agent(types.SimpleNamespace(mode="docker"), [], run=lambda argv: calls.append(argv))
    assert not calls


def test_place_on_agent_waits_for_the_agent_to_re_register(tmp_path, monkeypatch):
    # Deploying to an agent that has not rejoined its deployment group resolves ZERO agent
    # nodes, and the flow silently lands nowhere -- which looks exactly like success.
    f = tmp_path / "StatusReaderV1-5.4.jar"; f.write_bytes(b"jar-bytes")
    _agent_run.jar = str(f)
    waited = []
    from livetest import striim_provision as sp
    monkeypatch.setattr(sp, "wait_agent_registered",
                        lambda client, timeout=300: waited.append(timeout))
    monkeypatch.setattr(sp, "_reauthenticate", lambda client: waited.append("reauth"))

    place_on_agent(types.SimpleNamespace(mode="docker"), [f],
                   client=object(), run=_agent_run([]))

    # Re-auth first: the agent restart does not kill the server token, but the caller's client
    # may have been idle across it, and a 401 on the readiness poll reads as "agent never came
    # back" while the agent is fine.
    assert waited == ["reauth", 300]



# --- content-named OP jars ------------------------------------------------------------------
# One name must never hold two sets of bytes on a cluster: an UNLOAD while UploadedFiles/<name>
# holds other bytes than were loaded poisons Striim's cached jar: URL handle until restart.

def _zip(path: Path, entries: dict, when=(2020, 1, 1, 0, 0, 0)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries.items():
            z.writestr(zipfile.ZipInfo(name, date_time=when), data)
    return path


def test_content_addressed_name_puts_the_tag_before_the_first_dot():
    assert opartifacts.content_addressed_name("FooOpV2-5.4.jar", "abababababab", "5.4") == \
        "FooOpV2-abababababab-5.4.jar"
    # Striim derives a temp path from the name up to its first dot; the tag must be inside it.
    assert opartifacts.content_addressed_name("Dup_5.4.2.scm", "cdcdcdcdcdcd", "5.4") == \
        "Dup_5-cdcdcdcdcdcd.4.2.scm"
    assert opartifacts.content_addressed_name("Foo.jar", "cdcdcdcdcdcd", None) == \
        "Foo-cdcdcdcdcdcd.jar"


def _built(tmp_path, entries: dict, op="FooOp", when=(2020, 1, 1, 0, 0, 0)):
    src = _zip(tmp_path / "target" / f"{op}-5.4.jar", entries, when)
    return opartifacts.BuiltArtifact(src, src.name, op)


def test_content_addressed_copies_under_the_content_tag(tmp_path):
    store = tmp_path / "store"
    b = opartifacts.content_addressed(_built(tmp_path, {"a.class": b"v1"}), store)
    tag = opartifacts.jar_content_fingerprint(tmp_path / "target" / "FooOp-5.4.jar")[:12]
    assert b.name == f"FooOp-{tag}-5.4.jar" and b.content_tag == tag
    assert b.path == store / b.name
    assert b.path.read_bytes() == (tmp_path / "target" / "FooOp-5.4.jar").read_bytes()
    assert b.op_name == "FooOp"          # ${<TOKEN>_NAME} is the module, not the file
    assert sorted(p.name for p in store.iterdir()) == [".gitignore", b.name]   # no staging file left
    assert (store / ".gitignore").read_text() == "*\n"     # kept out of the consumer's git


def test_a_timestamp_only_rebuild_keeps_the_name_and_the_first_bytes(tmp_path):
    # These builds are not reproducible: an unchanged tree re-stamps every entry. That must not
    # cost a new name (a reload, another jar on every node), and the name keeps one set of bytes.
    store = tmp_path / "store"
    a = opartifacts.content_addressed(_built(tmp_path, {"a.class": b"v1"}), store)
    first = a.path.read_bytes()
    again = opartifacts.content_addressed(
        _built(tmp_path, {"a.class": b"v1"}, when=(2021, 6, 1, 0, 0, 0)), store)
    assert (tmp_path / "target" / "FooOp-5.4.jar").read_bytes() != first   # the rebuild differs
    assert again.name == a.name and again.path.read_bytes() == first
    changed = opartifacts.content_addressed(_built(tmp_path, {"a.class": b"v2"}), store)
    assert changed.name != a.name


def test_content_addressed_prunes_old_copies_of_the_same_op_only(tmp_path):
    store = tmp_path / "store"
    kept = opartifacts._CONTENT_KEEP
    for i in range(kept + 3):
        b = opartifacts.content_addressed(_built(tmp_path, {"a.class": f"v{i}".encode()}), store)
        os.utime(b.path, (1000 + i, 1000 + i))
    bar = opartifacts.content_addressed(_built(tmp_path, {"b.class": b"x"}, op="FooOpBar"), store)
    latest = opartifacts.content_addressed(_built(tmp_path, {"a.class": b"latest"}), store)
    foo = sorted(p for p in store.iterdir() if p.name.startswith("FooOp-"))
    assert len(foo) == kept
    assert latest.path in foo
    assert bar.path.exists()              # another OP's copies are never touched


def test_upload_keep_existing_gives_missing_nodes_the_bytes_already_published(tmp_path):
    # One name, one set of bytes on EVERY node: a node missing the file gets a copy of what the
    # other node already holds, never this build's (possibly timestamp-different) bytes.
    f = tmp_path / "FooOp-abababababab-5.4.jar"; f.write_text("new")
    calls = []

    def run(argv):
        calls.append(argv)
        assert "sha256sum" not in argv           # existence is enough; no hashing on the nodes
        if argv[3:4] == ["test"]:                 # present on slt-striim only
            return types.SimpleNamespace(returncode=0 if argv[2] == "slt-striim" else 1)
        if argv[:2] == ["docker", "cp"] and argv[2].startswith("slt-striim:"):
            Path(argv[3]).write_text("published")
        elif argv[:2] == ["docker", "cp"]:
            sent.append(Path(argv[2]).read_text())
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    sent = []
    upload_artifacts(types.SimpleNamespace(mode="docker"), [f], run=run, keep_existing=True)
    cps = [c for c in calls if c[:2] == ["docker", "cp"]]
    assert cps[0][2] == "slt-striim:/opt/striim/UploadedFiles/FooOp-abababababab-5.4.jar"
    assert [c[3].split(":")[0] for c in cps[1:]] == ["slt-node"]
    assert sent == ["published"]
    assert not Path(cps[1][2]).parent.exists()          # the fetched copy is cleaned up


def test_upload_keep_existing_skips_when_every_node_has_it(tmp_path):
    f = tmp_path / "FooOp-abababababab-5.4.jar"; f.write_text("new")
    calls = []
    upload_artifacts(types.SimpleNamespace(mode="docker"), [f], keep_existing=True,
                     run=lambda argv: calls.append(argv) or types.SimpleNamespace(returncode=0))
    assert not [c for c in calls if c[:2] == ["docker", "cp"]]


def test_a_non_op_file_with_a_hash_like_name_is_still_replaced(tmp_path, monkeypatch):
    home = tmp_path / "striim"; (home / "UploadedFiles").mkdir(parents=True)
    monkeypatch.setenv("STRIIM_HOME", str(home))
    (home / "UploadedFiles" / "Util-0123456789ab-1.0.jar").write_text("old")
    f = tmp_path / "Util-0123456789ab-1.0.jar"; f.write_text("new")
    upload_artifacts(types.SimpleNamespace(mode="native"), [f])
    assert (home / "UploadedFiles" / f.name).read_text() == "new"


def test_docker_keep_publishes_without_clobbering(tmp_path):
    f = tmp_path / "FooOp-abababababab-5.4.jar"; f.write_text("new")
    calls = []
    upload_artifacts(types.SimpleNamespace(mode="docker"), [f], keep_existing=True,
                     run=lambda argv: calls.append(argv) or types.SimpleNamespace(
                         returncode=1 if argv[3:4] == ["test"] else 0, stdout="", stderr=""))
    assert not [c for c in calls if "mv" in c]             # never `mv -f` over a content name
    links = [c for c in calls if c[5:7] == ["sh", "-c"]]
    assert len(links) == 2 and all(c[7].startswith('ln "$1" "$2"') for c in links)
    # As root: protected_hardlinks stops the striim user linking the host-owned copy (seen live).
    assert all(c[2:4] == ["-u", "0"] for c in links)


def test_store_copies_are_readable_by_other_users_and_memoised(tmp_path, monkeypatch):
    store = tmp_path / "store"
    b = opartifacts.content_addressed(_built(tmp_path, {"a.class": b"v1"}), store)
    assert b.path.stat().st_mode & 0o777 == 0o644
    calls = []
    monkeypatch.setattr(opartifacts, "jar_content_fingerprint",
                        lambda p: calls.append(p) or "x" * 64)
    again = opartifacts.content_addressed(opartifacts.BuiltArtifact(
        tmp_path / "target" / "FooOp-5.4.jar", "FooOp-5.4.jar", "FooOp"), store)
    assert again.name == b.name and calls == []      # unchanged jar: no new fingerprint


def test_upload_native_keep_existing_leaves_the_file(tmp_path, monkeypatch):
    home = tmp_path / "striim"; (home / "UploadedFiles").mkdir(parents=True)
    monkeypatch.setenv("STRIIM_HOME", str(home))
    (home / "UploadedFiles" / "FooOp-abababababab-5.4.jar").write_text("loaded")
    f = tmp_path / "FooOp-abababababab-5.4.jar"; f.write_text("rebuilt")
    upload_artifacts(types.SimpleNamespace(mode="native"), [f], keep_existing=True)
    assert (home / "UploadedFiles" / f.name).read_text() == "loaded"


def test_content_addressed_carries_the_fingerprint_and_the_built_name(tmp_path):
    b = opartifacts.content_addressed(_built(tmp_path, {"a.class": b"v1"}), tmp_path / "store")
    assert b.fingerprint == opartifacts.jar_content_fingerprint(tmp_path / "target" / "FooOp-5.4.jar")
    assert b.content_tag == b.fingerprint[:12]
    assert b.built_name == "FooOp-5.4.jar"


def test_place_on_agent_uses_the_given_names(tmp_path):
    # A content-named OP jar goes on the agent under its built name, so a new build replaces the
    # last one on the agent classpath instead of sitting beside it.
    f = tmp_path / "FooOp-abababababab-5.4.jar"; f.write_text("x")
    calls = []
    place_on_agent(types.SimpleNamespace(mode="docker"), [f], run=_agent_run(calls),
                   names=["FooOp-5.4.jar"])
    moves = [c for c in calls if "mv" in c]
    assert moves and moves[0][-1].endswith("/FooOp-5.4.jar")


def test_recent_copies_are_not_pruned_even_beyond_the_limit(tmp_path):
    store = tmp_path / "store"
    for i in range(opartifacts._CONTENT_KEEP + 3):
        opartifacts.content_addressed(_built(tmp_path, {"a.class": f"v{i}".encode()}), store)
    assert len([p for p in store.iterdir() if p.name.startswith("FooOp-")]) == opartifacts._CONTENT_KEEP + 3


def test_a_staging_failure_is_a_build_error(tmp_path):
    store = tmp_path / "store"; store.write_text("not a directory")
    with pytest.raises(OpArtifactError, match="could not stage"):
        opartifacts.content_addressed(_built(tmp_path, {"a.class": b"v1"}), store)


def test_content_addressed_file_renames_a_staged_jar_in_place(tmp_path):
    staged = _zip(tmp_path / "stage" / "OldReader-5.4.jar", {"a.class": b"v"})
    b = opartifacts.content_addressed_file(staged, "5.4")
    assert b.name == f"OldReader-{b.content_tag}-5.4.jar" and b.path == staged.with_name(b.name)
    assert b.op_name == "OldReader" and b.built_name == "OldReader-5.4.jar" and not staged.exists()


def test_a_dotted_op_name_gets_the_tag_before_its_first_dot():
    assert opartifacts.content_addressed_name("Foo.Bar-5.4.jar", "abababababab", "5.4") == \
        "Foo-abababababab.Bar-5.4.jar"


def test_native_keep_falls_back_when_hard_links_are_unsupported(tmp_path, monkeypatch):
    home = tmp_path / "striim"; (home / "UploadedFiles").mkdir(parents=True)
    monkeypatch.setenv("STRIIM_HOME", str(home))
    f = tmp_path / "x" / ".slt-op-jars" / "FooOp-abababababab-5.4.jar"
    f.parent.mkdir(parents=True); f.write_text("new")

    def no_links(*a, **k):
        raise OSError(95, "Operation not supported")
    monkeypatch.setattr(os, "link", no_links)
    upload_artifacts(types.SimpleNamespace(mode="native"), [f], keep_existing=True)
    assert (home / "UploadedFiles" / f.name).read_text() == "new"


def test_a_memoised_copy_pruned_meanwhile_is_made_again(tmp_path):
    store = tmp_path / "store"
    b = opartifacts.content_addressed(_built(tmp_path, {"a.class": b"v1"}), store)
    b.path.unlink()
    again = opartifacts.content_addressed(opartifacts.BuiltArtifact(
        tmp_path / "target" / "FooOp-5.4.jar", "FooOp-5.4.jar", "FooOp"), store)
    assert again.path.exists() and again.name == b.name


def test_restore_loaded_copy_puts_striims_copy_back_on_every_node():
    calls = []
    opartifacts.restore_loaded_copy(types.SimpleNamespace(mode="docker"), "FooOp-5.4.jar",
                                    run=lambda argv: calls.append(argv) or types.SimpleNamespace(returncode=0))
    assert [c[4] for c in calls] == ["slt-striim", "slt-node"]
    for c in calls:
        assert c[2:4] == ["-u", "0"]                 # the copy is host-owned; root can read it
        assert c[-3] == "/opt/striim/.striim/OpenProcessor/FooOp-5.4.jar"
        assert c[-1] == "/opt/striim/UploadedFiles/FooOp-5.4.jar"


def test_restore_loaded_copy_native(tmp_path, monkeypatch):
    home = tmp_path / "striim"
    (home / ".striim" / "OpenProcessor").mkdir(parents=True); (home / "UploadedFiles").mkdir()
    loaded = _zip(home / ".striim" / "OpenProcessor" / "FooOp-5.4.jar", {"a.class": b"loaded"})
    (home / "UploadedFiles" / "FooOp-5.4.jar").write_text("newer build")
    monkeypatch.setenv("STRIIM_HOME", str(home))
    opartifacts.restore_loaded_copy(types.SimpleNamespace(mode="native"), "FooOp-5.4.jar")
    assert (home / "UploadedFiles" / "FooOp-5.4.jar").read_bytes() == loaded.read_bytes()
    opartifacts.restore_loaded_copy(types.SimpleNamespace(mode="native"), "NotLoaded-5.4.jar")
    assert not (home / "UploadedFiles" / "NotLoaded-5.4.jar").exists()


def test_dotted_op_copies_are_pruned_too(tmp_path):
    store = tmp_path / "store"
    for i in range(opartifacts._CONTENT_KEEP + 2):
        b = opartifacts.content_addressed(_built(tmp_path, {"a.class": f"v{i}".encode()}, op="Foo.Bar"), store)
        os.utime(b.path, (1000 + i, 1000 + i))
    opartifacts.content_addressed(_built(tmp_path, {"a.class": b"latest"}, op="Foo.Bar"), store)
    assert len([p for p in store.iterdir() if p.name.startswith("Foo-")]) == opartifacts._CONTENT_KEEP


def test_a_jar_from_another_series_gets_the_tag_before_its_version_suffix():
    assert opartifacts.content_addressed_name("ChangeReaderV3B-5.4.jar", "abababababab", "5.5") == \
        "ChangeReaderV3B-abababababab-5.4.jar"


def test_restore_refuses_a_loaded_copy_that_is_not_a_complete_zip(tmp_path, monkeypatch):
    # A write cut short: copying it over UploadedFiles would replace good bytes with a broken jar.
    home = tmp_path / "striim"
    (home / ".striim" / "OpenProcessor").mkdir(parents=True); (home / "UploadedFiles").mkdir()
    (home / ".striim" / "OpenProcessor" / "FooOp-5.4.jar").write_bytes(b"PK\x03\x04 truncated")
    (home / "UploadedFiles" / "FooOp-5.4.jar").write_text("good")
    monkeypatch.setenv("STRIIM_HOME", str(home))
    with pytest.raises(OpArtifactError, match="not a complete zip"):
        opartifacts.restore_loaded_copy(types.SimpleNamespace(mode="native"), "FooOp-5.4.jar")
    assert (home / "UploadedFiles" / "FooOp-5.4.jar").read_text() == "good"


def test_restore_in_docker_checks_the_copy_before_replacing():
    calls = []
    opartifacts.restore_loaded_copy(types.SimpleNamespace(mode="docker"), "FooOp-5.4.jar",
                                    run=lambda argv: calls.append(argv) or types.SimpleNamespace(returncode=0))
    script = calls[0][7]
    assert script.index("cmp -s") < script.index("unzip -tq") < script.index("mv -f")


def test_the_module_name_comes_from_the_manifest(tmp_path):
    jar = _zip(tmp_path / "x.jar", {"META-INF/MANIFEST.MF": b"Manifest-Version: 1.0\r\nStriim-Module-Name: FooOpV2\r\n"})
    assert opartifacts.jar_module_name(jar) == "FooOpV2"
    assert opartifacts.jar_module_name(_zip(tmp_path / "y.jar", {"a": b""})) == ""


def test_stale_staging_files_are_pruned(tmp_path):
    store = tmp_path / "store"; store.mkdir()
    old = store / ".staging-abc"; old.write_text("cut short")
    os.utime(old, (1000, 1000))
    fresh = store / ".staging-def"; fresh.write_text("in progress")
    opartifacts.content_addressed(_built(tmp_path, {"a.class": b"v1"}), store)
    assert not old.exists() and fresh.exists()


def test_jar_own_class_major_finds_a_project_package_and_a_relocated_one(tmp_path):
    # A project's own package (not com/example), relocated per version by the build.
    mod = tmp_path / "FooOp"
    (mod / "src/main/java/com/acme/FooOp").mkdir(parents=True)
    (mod / "src/main/java/com/acme/FooOp/Processor.java").write_text("package com.acme.FooOp;")
    jar = tmp_path / "FooOpV2C-5.4.jar"
    with zipfile.ZipFile(jar, "w") as z:
        z.writestr("org/dep/Dep.class", b"\xca\xfe\xba\xbe\x00\x00" + struct.pack(">H", 52))
        z.writestr("com/acme/FooOpV2C/Processor.class", b"\xca\xfe\xba\xbe\x00\x00" + struct.pack(">H", 61))
    assert opartifacts._jar_own_class_major(jar, mod) == 61
    assert opartifacts._jar_own_class_major(jar) is None        # no module: the template prefix only


def test_a_wrong_jdk_build_is_caught_for_a_project_package(tmp_path):
    mod = tmp_path / "FooOp"
    (mod / "src/main/java/com/acme").mkdir(parents=True)
    (mod / "src/main/java/com/acme/Processor.java").write_text("package com.acme;")
    jar = tmp_path / "FooOp-5.4.jar"
    with zipfile.ZipFile(jar, "w") as z:
        z.writestr("com/acme/Processor.class", b"\xca\xfe\xba\xbe\x00\x00" + struct.pack(">H", 55))
    with pytest.raises(opartifacts.OpArtifactError, match="major version 55"):
        opartifacts._assert_jar_java_version(jar, "17", mod)


def test_a_dependency_class_sharing_a_source_name_is_not_own(tmp_path):
    # A module can ship com/google/cloud/spanner/Dialect (Java 8) beside its own Orm/Dialect.java.
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
