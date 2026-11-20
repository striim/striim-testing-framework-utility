"""The real PerfYamlItem.runtest, run by pytester with a stub iteration driver.

`perf.run_measured_iteration` (the subprocess launch), service bring-up, the op build, the harness
jar, `java` and the tokens are faked in the child's conftest; pre-flight, the iteration loop, the
report, the console block, `--perf-json` and the failure decision are the plugin's own code.
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import pytest

CONFTEST = r'''
import json, os
from pathlib import Path

from inttest import harness as H, opartifacts as O, perf as PF, plugin as P

SC = json.loads(os.environ["XPERF_SCENARIO"])
LOG = os.environ["XPERF_LOG"]


def log(event, **kw):
    with open(LOG, "a") as f:
        f.write(json.dumps({"event": event, **kw}) + "\n")


def run_measured_iteration(**kw):
    rs = kw["run_size"]
    log("iteration", run_size=rs.authored, events=len(kw["preflight_result"].input_events))
    if rs.authored in SC.get("fail", []):
        return PF.IterationResult(
            status="failed", run_size_authored=rs.authored, run_size_value=rs.value,
            duration_seconds=None, input_throughput=None, output_throughput=None,
            measured_input_events=None, measured_output_events=None, warmup_input_events=None,
            warmup_output_events=None, metrics=None, reset_duration_seconds=0.1,
            failure_reason="operator exited 1: boom", raw_result=None)
    n = rs.value
    return PF.IterationResult(
        status="success", run_size_authored=rs.authored, run_size_value=n, duration_seconds=2.0,
        input_throughput=n / 2.0, output_throughput=n / 2.0, measured_input_events=n,
        measured_output_events=n, warmup_input_events=n, warmup_output_events=n, metrics=None,
        reset_duration_seconds=0.1, failure_reason=None, raw_result=None)


P._provision_requires = lambda *a, **k: None
P._build_operator_jar = lambda name, ref, report=None: O.BuiltArtifact(
    path=Path(os.environ["XPERF_OP_JAR"]), name="op-5.4.jar", op_name="op")
P._build_tokens = lambda test_dir, requires, parallel=False: {
    "TID": "", "TID_ORACLE": "", "TEST_DIR": str(test_dir)}
H.ensure_harness_jar = lambda: Path("harness.jar")
H._resolve_java = lambda _java_home: "java-stub-not-on-path"
PF.run_measured_iteration = run_measured_iteration
'''

CASE = """
name: perf-{name}
purpose: p
op: {{jar: java/OpenProcessors/X}}
requires: [{requires}]
performance:
  input: in.json
  run_sizes: [10, 20]
  warmup_runs: 1
  measured_runs: 2
  correctness: {{mode: disabled}}
"""

EVENT = {"metadata": {"TableName": "T"}, "data": {"values": [1], "present": [True]}}


def _run(pytester, monkeypatch, tmp_path, scenario: dict, *args, requires="postgres", name="a"):
    case = pytester.path / "perf" / name
    case.mkdir(parents=True)
    (case / "test.yaml").write_text(CASE.format(name=name, requires=requires))
    (case / "in.json").write_text(json.dumps([EVENT, EVENT, EVENT]))
    (pytester.path / "regression").mkdir(exist_ok=True)
    pytester.makeconftest(CONFTEST)
    log = tmp_path / "log.jsonl"
    log.write_text("")
    op_jar = tmp_path / "op-5.4.jar"
    op_jar.write_bytes(b"jar")
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    monkeypatch.setenv("XPERF_SCENARIO", json.dumps(scenario))
    monkeypatch.setenv("XPERF_LOG", str(log))
    monkeypatch.setenv("XPERF_OP_JAR", str(op_jar))
    monkeypatch.setenv("SLT_STATE_DIR", str(state))
    # _PERF_DIR is SLT_INT_CASES' sibling `perf/`, so the case above is a perf case.
    monkeypatch.setenv("SLT_INT_CASES", str(pytester.path / "regression"))
    monkeypatch.delenv("STRIIM_HOME", raising=False)
    junit = tmp_path / "junit.xml"
    result = pytester.runpytest_subprocess("-p", "inttest.plugin", "-p", "no:cacheprovider", "-s",
                                           f"--junitxml={junit}", "--perf", *args, "perf",
                                           timeout=300)
    failure = None
    cases = list(ET.parse(junit).iter("testcase")) if junit.exists() else []
    for el in cases:
        assert el.find("error") is None, result.stdout.str()
        f = el.find("failure")
        if f is not None:
            failure = (f.get("message", "") + (f.text or ""))
    events = [json.loads(line) for line in log.read_text().splitlines()]
    return result, cases, failure, events, state


def test_a_perf_case_runs_every_iteration_and_prints_the_console_block(pytester, monkeypatch, tmp_path):
    result, cases, failure, events, state = _run(pytester, monkeypatch, tmp_path, {})
    assert len(cases) == 1 and failure is None, result.stdout.str()
    assert [(e["run_size"], e["events"]) for e in events] == [("10", 3), ("10", 3), ("20", 3), ("20", 3)]
    out = result.stdout.str()
    assert "Performance Test: perf-a" in out
    assert "Run Size: 10 replays" in out and "Run Size: 20 replays" in out
    assert "Report:" not in out


def test_without_perf_json_no_report_is_written(pytester, monkeypatch, tmp_path):
    _, _, failure, _, state = _run(pytester, monkeypatch, tmp_path, {})
    assert failure is None
    assert not (state / ".perf-results").exists()
    assert [p for p in tmp_path.rglob("*.json") if p.name.startswith("perf")] == []


def test_perf_json_writes_the_report_where_it_is_told(pytester, monkeypatch, tmp_path):
    target = tmp_path / "out" / "report.json"
    result, _, failure, _, state = _run(pytester, monkeypatch, tmp_path, {},
                                        f"--perf-json={target}")
    assert failure is None, result.stdout.str()
    report = json.loads(target.read_text())
    assert report["test"]["name"] == "perf-a"
    assert [b["run_size"]["value"] for b in report["run_sizes"]] == [10, 20]
    assert f"Report: {target}" in result.stdout.str()
    assert not (state / ".perf-results").exists()


def test_a_failed_iteration_fails_the_item_and_still_reports(pytester, monkeypatch, tmp_path):
    target = tmp_path / "report.json"
    result, _, failure, events, _ = _run(pytester, monkeypatch, tmp_path, {"fail": ["20"]},
                                         f"--perf-json={target}")
    assert failure is not None, result.stdout.str()
    assert "perf-a: performance run FAILED" in failure
    assert "run_size=20: operator exited 1: boom" in failure
    assert f"JSON report: {target}" in failure
    assert len(events) == 4           # a failed iteration does not stop the next one
    assert "Performance Test: perf-a" in result.stdout.str()
    assert json.loads(target.read_text())["test"]["name"] == "perf-a"


@pytest.mark.parametrize("service", ["postgres", "oracle", "spanner", "gcs", "sqlserver", "mysql",
                                     "teradata", "vertica", "mariadb", "bigtable", "servicenow"])
def test_a_perf_item_carries_a_marker_for_the_service_it_requires(pytester, monkeypatch, tmp_path, service):
    """`--perf -m <service>` selects the perf cases needing it, as `-m <service>` does in regression."""
    result, _, _, events, _ = _run(pytester, monkeypatch, tmp_path, {}, "--collect-only", "-q",
                                   "-m", service, requires=service)
    assert "perf/a/test.yaml::perf-a" in result.stdout.str(), result.stdout.str()
    assert events == []
