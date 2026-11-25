"""`assert.junit:` -- load-time validation, the Maven runner (opartifacts.run_module_tests) and the
case's pass/fail decision (plugin IntYamlItem._run_junit).

Hermetic: no Maven, no JVM, no Docker. `run` is injected and writes the surefire reports a real run
would write.
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from inttest import opartifacts, plugin
from inttest.manifest import JunitSpec, ManifestError, load_manifest
from inttest.plugin import IntYamlItem

_BASE = """
name: junit-case
op:
  jar: java/OpenProcessors/BigtableApiWriter
requires: [bigtable]
assert:
{assert_block}
"""

_RELEASE = {"STRIIM_SERIES": "5.4", "STRIIM_VERSION": "5.4.0.6C"}


def _load(tmp_path, assert_block: str, base: str = _BASE):
    d = tmp_path / "case"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "test.yaml"
    p.write_text(base.format(assert_block=assert_block))
    return load_manifest(p)


# --- manifest -------------------------------------------------------------------------------------

def test_junit_alone_is_a_case(tmp_path):
    m = _load(tmp_path, "  junit: {tests: '*EmulatorTest'}\n")
    assert m.assert_.junit == JunitSpec(tests="*EmulatorTest", min_tests=1)
    assert m.target is None and not m.assert_.data


def test_min_tests_is_read(tmp_path):
    m = _load(tmp_path, "  junit: {tests: '*EmulatorTest', min_tests: 8}\n")
    assert m.assert_.junit.min_tests == 8


def test_no_junit_is_none(tmp_path):
    assert _load(tmp_path, "  smoke: true\n").assert_.junit is None


@pytest.mark.parametrize("block, needle", [
    ("  junit: '*EmulatorTest'\n", "must be a mapping"),
    ("  junit: {tests: '*EmulatorTest', min_test: 2}\n", "unknown key"),
    ("  junit: {min_tests: 2}\n", "non-empty surefire"),
    ("  junit: {tests: ''}\n", "non-empty surefire"),
    ("  junit: {tests: '   '}\n", "non-empty surefire"),
    ("  junit: {tests: 'A B'}\n", "no spaces"),
    ("  junit: {tests: 5}\n", "non-empty surefire"),
    ("  junit: {tests: '*Test', min_tests: 0}\n", "positive integer"),
    ("  junit: {tests: '*Test', min_tests: -1}\n", "positive integer"),
    ("  junit: {tests: '*Test', min_tests: true}\n", "positive integer"),
    ("  junit: {tests: '*Test', min_tests: '2'}\n", "positive integer"),
])
def test_bad_junit_is_refused(tmp_path, block, needle):
    with pytest.raises(ManifestError, match=needle):
        _load(tmp_path, block)


def test_junit_needs_op(tmp_path):
    base = """
name: junit-udf
udf:
  jar: java/UserDefinedFunctions/ReferenceUdf
  class: com.example.ReferenceUdf
  pipeline:
    - function: ReferenceUdfMarkProcessed
      args: ['$']
requires: [bigtable]
assert:
{assert_block}
"""
    with pytest.raises(ManifestError, match="needs 'op:'"):
        _load(tmp_path, "  junit: {tests: '*Test'}\n", base=base)


def test_junit_needs_requires(tmp_path):
    base = _BASE.replace("requires: [bigtable]\n", "")
    with pytest.raises(ManifestError, match="needs 'requires:'"):
        _load(tmp_path, "  junit: {tests: '*Test'}\n", base=base)


def test_junit_refuses_matrix(tmp_path):
    base = _BASE.replace("requires: [bigtable]\n",
                         "requires: [bigtable]\nmatrix: {{A: ['1', '2']}}\n")
    with pytest.raises(ManifestError, match="'matrix:'"):
        _load(tmp_path, "  junit: {tests: '*Test'}\n", base=base)


# --- opartifacts.run_module_tests -------------------------------------------------------------------

def _suite(name: str, cases: list[tuple[str, str | None]]) -> str:
    """A surefire TEST-*.xml: `cases` is [(method, None|'failure'|'error'|'skipped')]."""
    body = "".join(
        f'<testcase classname="{name}" name="{method}">'
        + (f"<{kind} message='x'/>" if kind else "") + "</testcase>"
        for method, kind in cases)
    count = lambda k: sum(1 for _, kind in cases if kind == k)   # noqa: E731
    return (f'<?xml version="1.0" encoding="UTF-8"?><testsuite name="{name}" tests="{len(cases)}" '
            f'failures="{count("failure")}" errors="{count("error")}" '
            f'skipped="{count("skipped")}">{body}</testsuite>')


@pytest.fixture
def module(tmp_path, monkeypatch):
    monkeypatch.setattr(opartifacts, "_root", lambda: tmp_path)
    monkeypatch.setattr(opartifacts, "resolve_build_java_home", lambda *a, **k: None)
    monkeypatch.delenv("STRIIM_HOME", raising=False)
    d = tmp_path / "java/OpenProcessors/Mod"
    (d / "target/surefire-reports").mkdir(parents=True)
    (d / "pom.xml").write_text("<project/>")
    return d


def _runner(module_dir: Path, reports: dict, returncode: int = 0, out: str = ""):
    calls = []

    def run(argv, cwd, env):
        calls.append((argv, cwd, env))
        for name, text in reports.items():
            (module_dir / "target/surefire-reports" / name).write_text(text)
        return SimpleNamespace(returncode=returncode, stdout=out, stderr="")

    return calls, run


def test_runner_passes_the_pattern_and_the_process_environment(module, monkeypatch):
    monkeypatch.setenv("BIGTABLE_EMULATOR_HOST", "localhost:19470")
    calls, run = _runner(module, {"TEST-a.WriteEmulatorTest.xml": _suite("a.WriteEmulatorTest",
                                                                           [("one", None), ("two", None)])})
    r = opartifacts.run_module_tests("java/OpenProcessors/Mod", _RELEASE, "*EmulatorTest", run=run)
    (argv, cwd, env), = calls
    assert argv == ["mvn", "-q", "test", "-Dtest=*EmulatorTest", "-Dsurefire.failIfNoSpecifiedTests=false"]
    assert cwd == str(module)
    assert env["BIGTABLE_EMULATOR_HOST"] == "localhost:19470"
    assert env["STRIIM_SERIES"] == "5.4" and env["STRIIM_VERSION"] == "5.4.0.6C"
    assert (r.tests, r.ran, r.failures, r.errors, r.skipped) == (2, 2, 0, 0, 0)
    assert opartifacts.junit_problems(r, 2) == []


def test_runner_reads_only_this_runs_reports(module):
    reports = module / "target/surefire-reports"
    (reports / "TEST-old.StaleTest.xml").write_text(_suite("old.StaleTest", [("x", "failure")]))
    (reports / "keep.txt").write_text("not a report")
    _, run = _runner(module, {"TEST-a.T.xml": _suite("a.T", [("one", None)])})
    r = opartifacts.run_module_tests("java/OpenProcessors/Mod", _RELEASE, "*T", run=run)
    assert (r.tests, r.failures) == (1, 0)
    assert not (reports / "TEST-old.StaleTest.xml").exists()
    assert (reports / "keep.txt").exists()


def test_a_failing_test_fails_and_is_named(module):
    _, run = _runner(module, {"TEST-a.T.xml": _suite("a.T", [("ok", None), ("bad", "failure"),
                                                              ("boom", "error")])},
                     returncode=1, out="Tests run: 3, Failures: 1, Errors: 1")
    r = opartifacts.run_module_tests("java/OpenProcessors/Mod", _RELEASE, "*T", run=run)
    (problem,) = opartifacts.junit_problems(r, 1)
    assert "mvn test exited 1" in problem
    assert "a.T.bad" in problem and "a.T.boom" in problem and "a.T.ok" not in problem
    assert "Tests run: 3" in problem


def test_a_build_failure_shows_the_output_tail(module):
    _, run = _runner(module, {}, returncode=1, out="x" * 5000 + "COMPILATION ERROR")
    r = opartifacts.run_module_tests("java/OpenProcessors/Mod", _RELEASE, "*T", run=run)
    problems = opartifacts.junit_problems(r, 1)
    assert "COMPILATION ERROR" in problems[0]
    assert len(problems[0]) < 2000
    assert "0 test(s) ran" in problems[-1]


def test_a_skip_fails_and_names_the_skipped_tests(module):
    _, run = _runner(module, {"TEST-a.ReadEmulatorTest.xml": _suite(
        "a.ReadEmulatorTest", [("reads", "skipped"), ("writes", "skipped")])})
    r = opartifacts.run_module_tests("java/OpenProcessors/Mod", _RELEASE, "*EmulatorTest", run=run)
    assert r.returncode == 0 and r.skipped == 2
    problems = opartifacts.junit_problems(r, 1)
    assert any("2 test(s) skipped: a.ReadEmulatorTest.reads, a.ReadEmulatorTest.writes" in p
               and "BIGTABLE_EMULATOR_HOST" in p for p in problems)
    assert any("0 test(s) ran" in p for p in problems)


def test_zero_tests_fails(module):
    _, run = _runner(module, {})
    r = opartifacts.run_module_tests("java/OpenProcessors/Mod", _RELEASE, "*Nothing", run=run)
    assert r.returncode == 0 and r.tests == 0
    (problem,) = opartifacts.junit_problems(r, 1)
    assert "0 test(s) ran, fewer than min_tests 1" in problem


def test_fewer_than_min_tests_fails(module):
    _, run = _runner(module, {"TEST-a.T.xml": _suite("a.T", [("one", None)])})
    r = opartifacts.run_module_tests("java/OpenProcessors/Mod", _RELEASE, "*T", run=run)
    assert opartifacts.junit_problems(r, 1) == []
    (problem,) = opartifacts.junit_problems(r, 8)
    assert "1 test(s) ran, fewer than min_tests 8" in problem


def test_an_unreadable_report_fails(module):
    _, run = _runner(module, {"TEST-a.T.xml": _suite("a.T", [("one", None)]),
                              "TEST-b.T.xml": "<testsuite"})
    r = opartifacts.run_module_tests("java/OpenProcessors/Mod", _RELEASE, "*T", run=run)
    (problem,) = opartifacts.junit_problems(r, 1)
    assert "TEST-b.T.xml" in problem


def test_a_missing_module_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(opartifacts, "_root", lambda: tmp_path)
    with pytest.raises(opartifacts.OpModuleMissing):
        opartifacts.run_module_tests("java/OpenProcessors/Gone", _RELEASE, "*T",
                                     run=lambda *a: pytest.fail("ran"))


def test_shared_series_runs_in_the_releases_build_directory(module, monkeypatch):
    from inttest import opjarrelease
    monkeypatch.setattr(opjarrelease, "configured_versions", lambda *a, **k: {"5.4.0.6C", "5.4.4"})
    output = module / "target/.releases/5.4.0.6C"
    seen = {}

    def run(argv, cwd, env):
        pom = Path(argv[argv.index("-f") + 1])
        seen["pom"] = pom
        seen["text"] = pom.read_text()
        (output / "surefire-reports").mkdir(parents=True, exist_ok=True)
        (output / "surefire-reports/TEST-a.T.xml").write_text(_suite("a.T", [("one", None)]))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    r = opartifacts.run_module_tests("java/OpenProcessors/Mod", _RELEASE, "*T", run=run)
    assert str(output) in seen["text"]
    assert seen["pom"].parent == module and not seen["pom"].exists()   # removed after the run
    assert r.tests == 1 and r.reports == str(output / "surefire-reports")


# --- plugin: the case's decision ----------------------------------------------------------------------

def _item():
    return SimpleNamespace(user_properties=[])


def _case(tmp_path, block="  junit: {tests: '*EmulatorTest', min_tests: 2}\n"):
    return _load(tmp_path, block)


@pytest.fixture
def engine(monkeypatch, tmp_path):
    monkeypatch.setenv("STRIIM_HOME", str(tmp_path / "striim"))
    monkeypatch.setattr(plugin.releases_mod, "resolve_release", lambda env: dict(_RELEASE))
    monkeypatch.setattr(plugin, "_op_build_lock_path", lambda ref: tmp_path / "op.lock")
    monkeypatch.setattr(plugin.tokens_mod, "service_tokens", lambda name: {})
    calls = []

    def use(result=None, exc=None):
        def fake(module_ref, release, pattern, run=None):
            calls.append((module_ref, pattern, os.environ.get("BIGTABLE_EMULATOR_HOST")))
            if exc is not None:
                raise exc
            return result
        monkeypatch.setattr(opartifacts, "run_module_tests", fake)
        return calls

    return use


def test_case_passes_and_records_the_counts(tmp_path, engine, monkeypatch):
    monkeypatch.setenv("BIGTABLE_EMULATOR_HOST", "localhost:19470")
    calls = engine(opartifacts.JUnitResult(returncode=0, tests=8))
    item = _item()
    IntYamlItem._run_junit(item, _case(tmp_path))
    assert calls == [("java/OpenProcessors/BigtableApiWriter", "*EmulatorTest", "localhost:19470")]
    (key, value), = item.user_properties
    assert key == "junit" and '"ran": 8' in value and '"skipped": 0' in value


def test_case_fails_on_a_skip(tmp_path, engine):
    engine(opartifacts.JUnitResult(returncode=0, tests=8, skipped=8,
                                   skipped_names=("a.ReadEmulatorTest",)))
    with pytest.raises(pytest.fail.Exception, match="8 test\\(s\\) skipped: a.ReadEmulatorTest"):
        IntYamlItem._run_junit(_item(), _case(tmp_path))


def test_case_fails_on_zero_tests(tmp_path, engine):
    engine(opartifacts.JUnitResult(returncode=0))
    with pytest.raises(pytest.fail.Exception, match="0 test\\(s\\) ran, fewer than min_tests 2"):
        IntYamlItem._run_junit(_item(), _case(tmp_path))


def test_case_fails_on_a_failing_test(tmp_path, engine):
    engine(opartifacts.JUnitResult(returncode=1, tests=8, failures=1, failed=("a.T.bad",),
                                   output="BUILD FAILURE"))
    with pytest.raises(pytest.fail.Exception, match="(?s)failing: a.T.bad.*BUILD FAILURE"):
        IntYamlItem._run_junit(_item(), _case(tmp_path))


def test_missing_prerequisite_skips_with_cause_build(tmp_path, engine):
    engine(exc=opartifacts.OpArtifactError("build needs JDK 17"))
    with pytest.raises(plugin.EnvSkip) as e:
        IntYamlItem._run_junit(_item(), _case(tmp_path))
    assert e.value.cause == "build"


def test_no_mvn_skips_with_cause_build(tmp_path, engine):
    engine(exc=FileNotFoundError("mvn"))
    with pytest.raises(plugin.EnvSkip) as e:
        IntYamlItem._run_junit(_item(), _case(tmp_path))
    assert e.value.cause == "build"


def test_no_striim_home_skips_with_cause_build(tmp_path, engine, monkeypatch):
    engine(opartifacts.JUnitResult(returncode=0, tests=8))
    monkeypatch.delenv("STRIIM_HOME")
    with pytest.raises(plugin.EnvSkip) as e:
        IntYamlItem._run_junit(_item(), _case(tmp_path))
    assert e.value.cause == "build"


def test_missing_module_fails(tmp_path, engine):
    engine(exc=opartifacts.OpModuleMissing("module directory x does not exist"))
    with pytest.raises(pytest.fail.Exception, match="does not exist"):
        IntYamlItem._run_junit(_item(), _case(tmp_path))


# --- plugin: the requires: services' tokens reach the test JVM ----------------------------------------

_VERTICA_CASE = _BASE.replace("requires: [bigtable]", "requires: [vertica, postgres]")


@pytest.fixture
def service_env(monkeypatch, tmp_path):
    monkeypatch.setenv("STRIIM_HOME", str(tmp_path / "striim"))
    monkeypatch.setattr(plugin.releases_mod, "resolve_release", lambda env: dict(_RELEASE))
    monkeypatch.setattr(plugin, "_op_build_lock_path", lambda ref: tmp_path / "op.lock")
    provides = {
        "vertica": {"VERTICA_URL": "jdbc:vertica://h:1/db", "VERTICA_TARGET_USER": "qatarget",
                    "VERTICA_TARGET_PASSWORD": "s3cr3t-pw", "SHARED": "from-vertica"},
        "postgres": {"PG_URL": "jdbc:postgresql://h:2/db", "SHARED": "from-postgres"},
    }
    monkeypatch.setattr(plugin.tokens_mod, "service_tokens", lambda name: provides[name])
    for k in ("VERTICA_URL", "VERTICA_TARGET_USER", "VERTICA_TARGET_PASSWORD", "PG_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SHARED", "prior")
    seen = {}

    def use(exc=None):
        def fake(module_ref, release, pattern, run=None):
            seen.update({k: os.environ.get(k) for k in
                         ("VERTICA_URL", "VERTICA_TARGET_USER", "VERTICA_TARGET_PASSWORD",
                          "PG_URL", "SHARED")})
            if exc is not None:
                raise exc
            return opartifacts.JUnitResult(returncode=0, tests=1)
        monkeypatch.setattr(opartifacts, "run_module_tests", fake)
        return seen

    return use


def _vertica_case(tmp_path):
    return _load(tmp_path, "  junit: {tests: '*IT'}\n", base=_VERTICA_CASE)


def _assert_restored():
    for k in ("VERTICA_URL", "VERTICA_TARGET_USER", "VERTICA_TARGET_PASSWORD", "PG_URL"):
        assert k not in os.environ
    assert os.environ["SHARED"] == "prior"


def test_requires_tokens_are_exported_then_restored(tmp_path, service_env):
    seen = service_env()
    IntYamlItem._run_junit(_item(), _vertica_case(tmp_path))
    assert seen == {"VERTICA_URL": "jdbc:vertica://h:1/db", "VERTICA_TARGET_USER": "qatarget",
                    "VERTICA_TARGET_PASSWORD": "s3cr3t-pw", "PG_URL": "jdbc:postgresql://h:2/db",
                    "SHARED": "from-postgres"}
    _assert_restored()


def test_requires_tokens_are_restored_when_the_run_raises(tmp_path, service_env):
    seen = service_env(exc=opartifacts.OpModuleMissing("module directory x does not exist"))
    with pytest.raises(pytest.fail.Exception):
        IntYamlItem._run_junit(_item(), _vertica_case(tmp_path))
    assert seen["VERTICA_URL"] == "jdbc:vertica://h:1/db"
    _assert_restored()


def test_requires_token_values_are_not_shown(tmp_path, service_env, capsys):
    service_env(exc=opartifacts.OpArtifactError("build needs JDK 17"))
    with pytest.raises(plugin.EnvSkip) as e:
        IntYamlItem._run_junit(_item(), _vertica_case(tmp_path))
    out = capsys.readouterr()
    assert "s3cr3t-pw" not in str(e.value) and "s3cr3t-pw" not in out.out + out.err
