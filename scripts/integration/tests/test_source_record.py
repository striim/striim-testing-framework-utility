"""A deleted source must make the jar stale: mtimes alone cannot see it (op jars and the harness)."""
from __future__ import annotations

import os
import types

import pytest

from inttest import harness, opartifacts
from tests.test_opartifacts import MODULE, RELEASE_54, _fp, _hermetic_build_env, _jar, _mk


@pytest.fixture
def built(tmp_path, monkeypatch):
    """An op module whose jar is newer than every source and carries its source record."""
    _hermetic_build_env(tmp_path, monkeypatch)
    mod = tmp_path / MODULE
    _mk(mod / "pom.xml", 1000)
    _mk(mod / "src" / "main" / "java" / "x" / "A.java", 1000)
    _mk(mod / "src" / "main" / "java" / "x" / "B.java", 1000)
    _mk(mod / "src" / "main" / "resources" / "defaults.properties", 1000)
    common = tmp_path / "java" / "OpenProcessors" / "OpenProcessorCommon"
    _mk(common / "src" / "main" / "java" / "Logger.java", 1000)
    jar = mod / "target" / "ReferenceOpV1-5.4.jar"
    _jar(jar, fp=_fp(RELEASE_54), major=61)
    opartifacts._write_source_record(jar, mod)
    os.utime(jar, (2000, 2000))
    assert opartifacts._stale_trigger(jar, mod) is None
    return mod, common, jar


@pytest.mark.parametrize("removed", [
    "src/main/java/x/B.java",
    "src/main/resources/defaults.properties",
])
def test_a_deleted_module_source_is_stale(built, removed):
    mod, _, jar = built
    (mod / removed).unlink()
    trigger = opartifacts._stale_trigger(jar, mod)
    assert trigger == mod / removed and not trigger.exists()


def test_a_deleted_common_source_is_stale(built):
    mod, common, jar = built
    (common / "src" / "main" / "java" / "Logger.java").unlink()
    assert opartifacts._stale_trigger(jar, mod) is not None


def test_an_added_source_with_an_old_mtime_is_stale(built):
    mod, _, jar = built
    _mk(mod / "src" / "main" / "java" / "x" / "C.java", 500)
    assert opartifacts._stale_trigger(jar, mod) == mod / "src" / "main" / "java" / "x" / "C.java"


def test_a_jar_without_a_source_record_is_stale(built):
    mod, _, jar = built
    opartifacts._source_record(jar).unlink()
    assert opartifacts._stale_trigger(jar, mod) == opartifacts._source_record(jar)


@pytest.mark.parametrize("break_it,reason", [
    (lambda mod, jar: (mod / "src" / "main" / "java" / "x" / "B.java").unlink(), "source removed"),
    (lambda mod, jar: opartifacts._source_record(jar).unlink(), "no source record"),
])
def test_build_jar_clean_rebuilds_and_records_the_new_sources(built, break_it, reason):
    mod, _, jar = built
    break_it(mod, jar)
    goals, reasons = [], []

    def fake_run(argv, cwd, env):
        goals.extend(argv)
        _jar(jar, fp=_fp(RELEASE_54), major=61)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    opartifacts.build_jar(MODULE, RELEASE_54, run=fake_run, report=reasons.append)
    assert "clean" in goals, "a removed class stays in target/classes unless the build is clean"
    assert reason in reasons[0] and "clean rebuild" in reasons[0]
    assert opartifacts._stale_trigger(jar, mod) is None
    opartifacts.build_jar(MODULE, RELEASE_54, run=lambda *a: pytest.fail("must not build"))


@pytest.fixture
def harness_dir(tmp_path, monkeypatch):
    java = tmp_path / "java"
    monkeypatch.setattr(harness, "_JAVA_DIR", java)
    monkeypatch.setattr(harness, "_JAVA_SRC", java / "src")
    monkeypatch.setattr(harness, "_JAVA_POM", java / "pom.xml")
    _mk(java / "pom.xml", 1000)
    _mk(java / "src" / "main" / "java" / "A.java", 1000)
    _mk(java / "src" / "main" / "java" / "B.java", 1000)
    jar = java / "target" / "inttest-harness.jar"

    def fake_mvn(argv, **kwargs):
        fake_mvn.argv = argv
        _mk(jar, 2000)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(harness.subprocess, "run", fake_mvn)
    return java, jar, fake_mvn


def test_the_harness_build_is_clean_and_records_its_sources(harness_dir):
    java, jar, fake_mvn = harness_dir
    assert harness._build_harness_jar() == jar
    assert "clean" in fake_mvn.argv
    assert harness._is_stale(jar) is False


def test_a_deleted_harness_source_makes_the_harness_stale(harness_dir):
    java, jar, _ = harness_dir
    harness._build_harness_jar()
    (java / "src" / "main" / "java" / "B.java").unlink()
    assert harness._is_stale(jar) is True


def test_a_harness_jar_without_a_source_record_is_stale(harness_dir):
    java, jar, _ = harness_dir
    _mk(jar, 2000)
    assert harness._is_stale(jar) is True


def test_a_case_only_rename_is_a_removal_and_rebuilds_clean(built):
    # On a case-insensitive FS the old name still "exists"; target/classes keeps its old .class.
    mod, _, jar = built
    src = mod / "src" / "main" / "java" / "x"
    os.rename(src / "B.java", src / "b.java")
    goals, reasons = [], []

    def fake_run(argv, cwd, env):
        goals.extend(argv)
        _jar(jar, fp=_fp(RELEASE_54), major=61)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    opartifacts.build_jar(MODULE, RELEASE_54, run=fake_run, report=reasons.append)
    assert "clean" in goals and "source removed" in reasons[0], reasons
