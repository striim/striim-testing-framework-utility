"""Verify: synthetic external runners for success, nonzero exit, timeout, stale/missing report and
zero tests; every failure path nonzero and naming the runner or prerequisite; the frozen C2
fixtures."""
import json
import os
import shutil
import sys
import time

import pytest
import yaml

from _clikit import CONTRACT_FIXTURES, FIXTURES, framework_env, run_cli

FAKE = FIXTURES / "bin" / "fake_runner.py"
REPORT = "reports/junit.xml"


def _spec(name, argv, **over):
    spec = {
        "schemaVersion": 1, "name": name, "tier": "unit", "argv": argv, "shell": False,
        "selection": {"default": [], "allowEmpty": False},
        "timeout": {"total": 120, "cancelGrace": 5}, "processGroup": True,
        "artifacts": {"reports": [{"kind": "junit-xml", "path": REPORT, "required": True,
                                   "freshness": "run"}]},
        "result": {"adapter": "junit-xml",
                   "policy": {"zeroTests": "fail", "requiredSkip": "fail", "knownFailures": []}},
    }
    spec.update(over)
    return spec


def _consumer(tmp_path, specs=(), files=()):
    root = tmp_path / "consumer"
    (root / "cases" / "live").mkdir(parents=True)
    (root / "reports").mkdir()
    (root / "runners").mkdir()
    entries = []
    for spec in specs:
        (root / "runners" / f"{spec['name']}.yaml").write_text(yaml.safe_dump(spec, sort_keys=False))
        entries.append(f"runners/{spec['name']}.yaml")
    for f in files:
        shutil.copyfile(f, root / "runners" / f.name)
        entries.append(f"runners/{f.name}")
    manifest = root / "gold-targets.yaml"
    manifest.write_text(yaml.safe_dump({"schemaVersion": 1, "targets": [],
                                        "suites": {"live": "cases/live"}, "stateDir": ".state",
                                        "runners": entries}, sort_keys=False))
    return manifest


def _result(r, name):
    return json.loads((r.run_dir / "runners" / name / "result.json").read_text())


@pytest.mark.parametrize("mode,code,reason", [
    ("pass", 0, "ok"),
    ("fail-exit", 1, "process-exit:1"),
    ("exit0-report-fail", 1, "report-failures:1"),
    ("exit1-report-pass", 1, "process-exit:1"),        # NC: parsed success cannot override exit
    ("missing-report", 1, "report-missing"),
    ("stale-report", 1, "report-stale"),               # NC: a pre-existing report is not fresh
    ("zero-tests", 1, "report-zero-tests"),
    ("malformed", 1, "report-malformed"),
    ("skip", 1, "required-skip:1"),
], ids=["pass", "fail-exit", "exit0-report-fail", "exit1-report-pass", "missing-report", "stale-report",
        "zero-tests", "malformed", "skip"])
def test_runner_outcome(tmp_path, elsewhere, mode, code, reason):
    manifest = _consumer(tmp_path, [_spec(mode, [sys.executable, str(FAKE), mode, REPORT])])
    if mode == "stale-report":
        old = manifest.parent / REPORT
        old.write_text('<testsuite name="old" tests="1"><testcase classname="c" name="n"/></testsuite>')
        past = time.time() - 3600
        os.utime(old, (past, past))
    r = run_cli(["run", "--suite", mode, "--targets", manifest], cwd=elsewhere)
    assert r.rc == code, (r.stdout, r.stderr)
    res = _result(r, mode)
    assert (res["runner"], res["reason"], res["exit"]) == (mode, reason, code)
    assert "detail" in res and res["launched"] is True
    if code:
        assert f"runner {mode}: {reason}" in r.stderr


def test_timeout_kills_process_group(tmp_path, elsewhere):
    pidfile = tmp_path / "child.pid"
    spec = _spec("timeout", [sys.executable, str(FAKE), "timeout", REPORT, str(pidfile)],
                 timeout={"total": 2, "cancelGrace": 1})
    manifest = _consumer(tmp_path, [spec])
    started = time.monotonic()
    r = run_cli(["run", "--suite", "timeout", "--targets", manifest], cwd=elsewhere)
    assert r.rc == 4, r.stderr
    res = _result(r, "timeout")
    assert res["reason"] == "timeout" and res["processGroupCleared"] is True
    assert time.monotonic() - started < 120
    child = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


def test_launch_failure_is_3(tmp_path, elsewhere):
    manifest = _consumer(tmp_path, [_spec("launch-fail", [str(tmp_path / "no-such-runner")])])
    r = run_cli(["run", "--suite", "launch-fail", "--targets", manifest], cwd=elsewhere)
    assert r.rc == 3 and _result(r, "launch-fail")["reason"] == "launch-failed"
    assert "runner launch-fail: launch-failed" in r.stderr


def test_prereq_failure_is_3_and_runner_not_launched(tmp_path, elsewhere):
    marker = tmp_path / "launched"
    spec = _spec("prereq-fail", [sys.executable, str(FAKE), "marker", REPORT, str(marker)],
                 prereq=[{"kind": "file", "path": "cases"},
                         {"kind": "cmd", "argv": [sys.executable, "-c", "raise SystemExit(3)"],
                          "expectExit": 0}])
    manifest = _consumer(tmp_path, [spec])
    r = run_cli(["run", "--suite", "prereq-fail", "--targets", manifest], cwd=elsewhere)
    assert r.rc == 3
    res = _result(r, "prereq-fail")
    assert res["reason"] == "prereq[1]:cmd" and res["launched"] is False
    assert "runner prereq-fail: prereq[1]:cmd" in r.stderr
    assert not marker.exists()


@pytest.mark.parametrize("fixture,field", [
    ("runner.invalid-shell-unreviewed.yaml", "shellReviewed"),
    ("runner.invalid-zero-tests-pass.yaml", "result.policy.zeroTests"),
    ("runner.invalid-unsupported-capability.yaml", "result.adapter 'gherkin-json'"),
], ids=["shell-unreviewed", "zero-tests-pass", "unsupported-capability"])
def test_frozen_invalid_runner_fixtures(tmp_path, elsewhere, fixture, field):
    manifest = _consumer(tmp_path, files=[CONTRACT_FIXTURES / fixture])
    r = run_cli(["list", "--targets", manifest], cwd=elsewhere)    # runners load before any run
    assert r.rc == 2 and field in r.stderr and fixture in r.stderr, r.stderr
    assert r.run_dir is None


def test_frozen_valid_surefire_lists_without_executing(tmp_path, elsewhere):
    manifest = _consumer(tmp_path, files=[CONTRACT_FIXTURES / "runner.surefire.valid.yaml"])
    copy = tmp_path / "product-copy"
    copy.mkdir()
    env = framework_env(PT_PRODUCT_HOME_COPY=copy)
    r = run_cli(["list", "--suite", "unit-surefire-platform", "--targets", manifest],
                cwd=elsewhere, env=env)
    assert r.rc == 0 and r.ids() == ["runner:unit-surefire-platform"], r.stderr
    assert not (r.run_dir / "runners").exists()


def test_env_expand_only_listed_keys(tmp_path, elsewhere):
    dump = tmp_path / "dump.json"
    spec = _spec("env-dump", [sys.executable, str(FAKE), "env-dump", REPORT, str(dump),
                              "$XTR_RUNNER_VALUE"],
                 env={"EXPANDED": "${XTR_RUNNER_VALUE}/x", "LITERAL": "$XTR_RUNNER_VALUE"},
                 envExpand=["EXPANDED"])
    manifest = _consumer(tmp_path, [spec])
    r = run_cli(["run", "--suite", "env-dump", "--targets", manifest], cwd=elsewhere,
                env=framework_env(XTR_RUNNER_VALUE="hello"))
    assert r.rc == 0, r.stderr
    seen = json.loads(dump.read_text())
    assert seen["EXPANDED"] == "hello/x"
    assert seen["LITERAL"] == "$XTR_RUNNER_VALUE"
    assert seen["argv"][-1] == "$XTR_RUNNER_VALUE"


def test_unset_path_var_is_config_error(tmp_path, elsewhere):
    spec = _spec("unset-var", [sys.executable, str(FAKE), "pass", REPORT],
                 cwd="$XTR_UNSET_RUNNER_HOME/work")
    manifest = _consumer(tmp_path, [spec])
    env = framework_env(XTR_UNSET_RUNNER_HOME=None)
    for argv in (["list"], ["run", "--suite", "unset-var"]):
        r = run_cli([*argv, "--targets", manifest], cwd=elsewhere, env=env)
        assert r.rc == 2 and "$XTR_UNSET_RUNNER_HOME" in r.stderr, r.stderr
        assert r.run_dir is None


def test_escaping_report_path_is_config_error(tmp_path, elsewhere):
    spec = _spec("escape", [sys.executable, str(FAKE), "pass", REPORT],
                 artifacts={"reports": [{"kind": "junit-xml", "path": "../outside/junit.xml",
                                         "required": True, "freshness": "run"}]})
    manifest = _consumer(tmp_path, [spec])
    r = run_cli(["list", "--targets", manifest], cwd=elsewhere)
    assert r.rc == 2 and "escapes the consumer root" in r.stderr


def test_known_failures_unsupported_in_24(tmp_path, elsewhere):
    spec = _spec("known", [sys.executable, str(FAKE), "pass", REPORT])
    spec["result"]["policy"]["knownFailures"] = [
        {"id": "x", "reason": "synthetic", "owner": "t", "since": "2026-09-12"}]
    manifest = _consumer(tmp_path, [spec])
    r = run_cli(["list", "--targets", manifest], cwd=elsewhere)
    assert r.rc == 2 and "knownFailures" in r.stderr and "unsupported in 2.4" in r.stderr


def test_case_flag_with_runner_tier(tmp_path, elsewhere):
    manifest = _consumer(tmp_path, [_spec("pass", [sys.executable, str(FAKE), "pass", REPORT])])
    for argv in (["--suite", "pass"], ["--tier", "unit"]):
        r = run_cli(["run", *argv, "--case", "x", "--targets", manifest], cwd=elsewhere)
        assert r.rc == 2 and "--case cannot select inside" in r.stderr
        assert not (r.run_dir / "runners").exists()


def test_unselected_runner_not_executed(tmp_path, elsewhere):
    marker = tmp_path / "launched"
    manifest = _consumer(tmp_path, [_spec("marker", [sys.executable, str(FAKE), "marker", REPORT,
                                                     str(marker)])])
    r = run_cli(["run", "--targets", manifest], cwd=elsewhere)
    assert r.rc == 5, r.stderr              # the live suite is empty; the runner is not selected
    assert not marker.exists() and not (r.run_dir / "runners").exists()
