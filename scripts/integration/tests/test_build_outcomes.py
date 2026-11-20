"""What a module that could not be built does to a case and to the run.

A case whose module could not be built here was never tested, so it may skip only for a missing
build prerequisite (no `mvn`, no JDK for the release, no STRIIM_HOME) -- and that skip fails the run
whatever else ran (live's `_slt_infra_guard`, the same rule). Anything the case or the run
asked for that is wrong -- a module path that is not there, a prebuilt jar that cannot be used, a
jar the build produced for another Java level -- fails the case.
"""
from __future__ import annotations

import json
import types
import xml.etree.ElementTree as ET

import pytest

from inttest import opartifacts as O, plugin as P

from tests.test_opartifacts import MODULE, RELEASE_54, _hermetic_build_env, _jar


@pytest.fixture
def striim_home(tmp_path, monkeypatch):
    home = tmp_path / "striim"
    (home / "lib").mkdir(parents=True)
    (home / "lib" / "Platform-5.4.0.6.jar").write_bytes(b"")
    monkeypatch.setenv("STRIIM_HOME", str(home))
    monkeypatch.setenv("SLT_STATE_DIR", str(tmp_path))
    for var in ("SLT_PREBUILT_JAR", "SLT_PREBUILT_DIR", O.prebuilt_env_var(MODULE)):
        monkeypatch.delenv(var, raising=False)
    _hermetic_build_env(tmp_path, monkeypatch)
    return home


def _outcome(ref):
    """("failed"|"skipped"|"built", message). A skip must not escape: it would skip THIS test."""
    try:
        P._build_operator_jar("my-case", ref)
    except P.EnvSkip as e:
        return "skipped", str(e)
    except pytest.fail.Exception as e:
        return "failed", str(e)
    return "built", ""


# --- the case ---------------------------------------------------------------------------------

def test_a_module_path_that_does_not_exist_fails_the_case(striim_home):
    """`op.jar` names a directory that is not there (a rename the case missed): an authoring
    mistake, not this host lacking something."""
    outcome, message = _outcome("java/OpenProcessors/RenamedAwayOp")
    assert outcome == "failed" and "RenamedAwayOp" in message, (outcome, message)


def test_a_missing_module_raises_before_any_build_runs(tmp_path, monkeypatch):
    _hermetic_build_env(tmp_path, monkeypatch)
    with pytest.raises(O.OpModuleMissing, match="does not exist"):
        O.build_jar("java/OpenProcessors/RenamedAwayOp", RELEASE_54,
                    run=lambda *a: pytest.fail("must not run mvn"))


def test_a_prebuilt_jar_that_is_not_there_fails_the_case(striim_home, tmp_path, monkeypatch):
    monkeypatch.setenv("SLT_PREBUILT_JAR", str(tmp_path / "missing.jar"))
    outcome, message = _outcome(MODULE)
    assert outcome == "failed" and "prebuilt jar not found" in message, (outcome, message)


def test_a_prebuilt_jar_for_the_wrong_java_level_fails_the_case(striim_home, tmp_path, monkeypatch):
    prebuilt = tmp_path / "ReferenceOpV1-5.4.jar"
    _jar(prebuilt, major=55)                        # Java 11 classes for a Java 17 release
    monkeypatch.setenv("SLT_PREBUILT_JAR", str(prebuilt))
    outcome, message = _outcome(MODULE)
    assert outcome == "failed" and "wrong JDK" in message, (outcome, message)


@pytest.mark.parametrize("make", [
    lambda d: None,                                                          # no match
    lambda d: (_jar(d / "a" / "ReferenceOpV1-5.4.jar", major=61),
               _jar(d / "b" / "ReferenceOpV2-5.4.jar", major=61)),            # two matches
])
def test_an_unusable_prebuilt_dir_is_rejected_not_skipped(tmp_path, monkeypatch, make):
    _hermetic_build_env(tmp_path, monkeypatch)
    stage = tmp_path / "stage"
    stage.mkdir()
    make(stage)
    monkeypatch.setenv("SLT_PREBUILT_DIR", str(stage))
    with pytest.raises(O.OpJarRejected):
        O.build_jar(MODULE, RELEASE_54)


def test_a_jar_this_run_built_for_the_wrong_java_level_is_a_build_failure(tmp_path, monkeypatch):
    _hermetic_build_env(tmp_path, monkeypatch)
    (tmp_path / MODULE).mkdir(parents=True)

    def build(argv, cwd, env):
        _jar(tmp_path / MODULE / "target" / "ReferenceOpV1-5.4.jar", major=55)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    with pytest.raises(O.OpBuildFailed, match="wrong JDK"):
        O.build_jar(MODULE, RELEASE_54, run=build)


def test_no_mvn_on_this_host_is_a_build_skip(striim_home, tmp_path, monkeypatch):
    (tmp_path / MODULE).mkdir(parents=True)

    def no_mvn(argv, cwd, env):
        raise FileNotFoundError(2, "No such file or directory", "mvn")

    monkeypatch.setattr(O, "_default_run", no_mvn)
    with pytest.raises(P.EnvSkip) as e:
        P._build_operator_jar("my-case", MODULE)
    assert e.value.cause == "build"


def test_no_striim_home_is_a_build_skip(monkeypatch):
    monkeypatch.delenv("STRIIM_HOME", raising=False)
    with pytest.raises(P.EnvSkip) as e:
        P._build_operator_jar("my-case", MODULE)
    assert e.value.cause == "build" and "STRIIM_HOME" in str(e.value)


# --- the run ----------------------------------------------------------------------------------

CONFTEST = r'''
from pathlib import Path
import pytest
from inttest import harness as H, opartifacts as O, plugin as P

def build(name, ref, report=None):
    if "Unbuildable" in ref:
        P._env_skip("build", f"{name}: cannot build operator jar {ref!r}: build needs JDK 17")
    return O.BuiltArtifact(path=Path("op.jar"), name="op-5.4.jar", op_name="op")

P._provision_requires = lambda *a, **k: None
P._build_operator_jar = build
P._build_tokens = lambda test_dir, requires, parallel=False: {
    "TID": "", "TID_ORACLE": "", "TEST_DIR": str(test_dir)}
H.ensure_harness_jar = lambda: Path("harness.jar")
H.drive = lambda *a, **k: [{"metadata": {"TableName": "T"}, "data": {"values": [1], "present": [True]}}]

@pytest.fixture(autouse=True)
def cleanup_postgres():
    yield
'''

CASE = """
name: {name}
purpose: p
op: {{jar: java/OpenProcessors/{module}}}
assert:
  data:
    - input: in.json
      match: out.json
"""

EVENT = {"metadata": {"TableName": "T"}, "data": {"values": [1], "present": [True]}}


def _run(pytester, monkeypatch, tmp_path, modules: dict):
    for name, module in modules.items():
        d = pytester.mkdir(name)
        (d / "test.yaml").write_text(CASE.format(name=name, module=module))
        (d / "in.json").write_text(json.dumps([EVENT]))
        (d / "out.json").write_text(json.dumps([EVENT]))
    pytester.makeconftest(CONFTEST)
    (tmp_path / "state").mkdir()
    monkeypatch.setenv("SLT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("INT_ALLOW_ALL_SKIPPED", raising=False)
    junit = tmp_path / "junit.xml"
    result = pytester.runpytest_subprocess("-p", "inttest.plugin", "-p", "no:cacheprovider",
                                           f"--junitxml={junit}", *modules)
    outcomes = {}
    for el in ET.parse(junit).iter("testcase"):
        outcomes[el.get("name")] = ("failed" if el.find("failure") is not None
                                    else "skipped" if el.find("skipped") is not None else "passed")
    return result, outcomes


def test_a_build_skip_fails_a_run_in_which_another_case_passed(pytester, monkeypatch, tmp_path):
    result, outcomes = _run(pytester, monkeypatch, tmp_path,
                            {"good": "GoodOp", "unbuilt": "UnbuildableOp"})
    assert outcomes == {"good": "passed", "unbuilt": "skipped"}, result.stdout.str()
    assert result.ret == 1, result.stdout.str()
    out = result.stdout.str()
    assert "could not be built" in out and "unbuilt" in out


def test_a_run_with_no_build_skip_keeps_its_green(pytester, monkeypatch, tmp_path):
    result, outcomes = _run(pytester, monkeypatch, tmp_path, {"good": "GoodOp"})
    assert outcomes == {"good": "passed"} and result.ret == 0, result.stdout.str()
