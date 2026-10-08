"""`_build_operator_jar` holds a per-module file lock across `build_jar`.

Without it, xdist workers reaching the same module at once ran `mvn package`/`clean` in one
target/ concurrently, and one tore or deleted another's output: those cases SKIPPED with
"cannot build operator jar". The probe runs in a SUBPROCESS because the workers are processes,
and only a second process shows whether the lock actually excludes them."""
import subprocess
import sys

import pytest

from inttest import opartifacts, plugin

_PROBE = ("import sys; from filelock import FileLock, Timeout\n"
          "try:\n"
          "    FileLock(sys.argv[1], timeout=0).acquire()\n"
          "except Timeout:\n"
          "    sys.exit(3)\n")


def _held_by_another_process(path) -> bool:
    return subprocess.run([sys.executable, "-c", _PROBE, str(path)]).returncode == 3


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("SLT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("STRIIM_HOME", str(tmp_path))
    monkeypatch.setattr(plugin.releases_mod, "resolve_release", lambda env: {"STRIIM_SERIES": "5.4"})


def test_build_runs_under_the_modules_lock(isolated, monkeypatch):
    seen = {}

    def fake_build(ref, release, report=None):
        seen["held"] = _held_by_another_process(plugin._op_build_lock_path(ref))
        return "artifact"

    monkeypatch.setattr(plugin.opartifacts_mod, "build_jar", fake_build)
    assert plugin._build_operator_jar("case", "java/OpenProcessors/SomeOp") == "artifact"
    assert seen["held"], "build_jar ran without the module's build lock held"
    assert not _held_by_another_process(plugin._op_build_lock_path("java/OpenProcessors/SomeOp"))


def test_a_failed_build_releases_the_lock_and_fails(isolated, monkeypatch):
    def failing_build(ref, release, report=None):
        raise opartifacts.OpBuildFailed("mvn package failed")

    monkeypatch.setattr(plugin.opartifacts_mod, "build_jar", failing_build)
    with pytest.raises(pytest.fail.Exception, match="failed to build"):
        plugin._build_operator_jar("case", "java/OpenProcessors/SomeOp")
    assert not _held_by_another_process(plugin._op_build_lock_path("java/OpenProcessors/SomeOp"))


def test_an_environment_that_cannot_build_still_skips(isolated, monkeypatch):
    def no_jdk(ref, release, report=None):
        raise opartifacts.OpArtifactError("build needs JDK 17")

    monkeypatch.setattr(plugin.opartifacts_mod, "build_jar", no_jdk)
    with pytest.raises(pytest.skip.Exception, match="cannot build operator jar"):
        plugin._build_operator_jar("case", "java/OpenProcessors/SomeOp")


def test_each_module_has_its_own_lock(isolated):
    a = plugin._op_build_lock_path("java/OpenProcessors/AOp")
    b = plugin._op_build_lock_path("java/OpenProcessors/BOp/pom.xml")
    assert a != b and a.parent == b.parent
    assert a == plugin._op_build_lock_path("java/OpenProcessors/AOp/pom.xml")


def test_the_harness_jar_builds_under_its_lock_and_is_rechecked_inside_it(monkeypatch, tmp_path):
    from inttest import harness

    jar = tmp_path / "inttest-harness-1.jar"
    seen = {}
    calls = iter([[], [jar]])        # stale before the lock; a sibling built it while we waited

    def fake_build():
        seen["held"] = _held_by_another_process(harness._JAVA_DIR / ".int-harness-build.lock")
        return jar

    monkeypatch.setattr(harness, "_candidate_jars", lambda: next(calls))
    monkeypatch.setattr(harness, "_is_stale", lambda j: False)
    monkeypatch.setattr(harness, "_build_harness_jar", fake_build)
    assert harness.ensure_harness_jar() == jar
    assert "held" not in seen, "the re-check inside the lock must reuse a sibling's fresh jar"

    calls = iter([[], []])
    monkeypatch.setattr(harness, "_candidate_jars", lambda: next(calls))
    assert harness.ensure_harness_jar() == jar
    assert seen["held"], "the harness was built without its lock held"
