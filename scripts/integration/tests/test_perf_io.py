"""Tests for inttest.perf's I/O orchestration pieces (PERF_SPEC.md §5, §7,
§9, §10). No Docker, no STRIIM_HOME, no real Java -- process-tree tests spawn plain
Python subprocesses; `launch_and_measure` is exercised against a tiny shebang script
standing in for `java` (it reads the request file and writes a canned result, so the
subprocess/sampling/timeout wiring is exercised for real without needing a JVM).
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

from inttest import perf
from inttest.manifest import AssertSpec, OpRef, TestManifest
from inttest.perfmanifest import PerfCorrectnessSpec, PerfJvmSpec, PerfMetricsSpec, PerformanceSpec, RunSize


# --- preflight (§3 stage 2) ---------------------------------------------------

class _FakeCorrectness:
    def __init__(self, mode):
        self.mode = mode


class _FakePerformance:
    def __init__(self, input_path, expected_path, mode):
        self.input_path = input_path
        self.expected_path = expected_path
        self.correctness = _FakeCorrectness(mode)


_ONE_RECORD = json.dumps([{"metadata": {"TableName": "T"}, "data": {"values": ["a"], "present": [True]}}])


def test_preflight_disabled_mode_needs_no_expected(tmp_path):
    input_path = tmp_path / "in.json"
    input_path.write_text(_ONE_RECORD)
    p = _FakePerformance(input_path, None, "disabled")

    result = perf.preflight(p)
    assert result.events_in_file == 1
    assert result.expected_events is None
    assert result.sample_indices is None


def test_preflight_sampled_mode_computes_sample_indices(tmp_path):
    input_path = tmp_path / "in.json"
    input_path.write_text(_ONE_RECORD)
    expected_path = tmp_path / "expected.json"
    expected_path.write_text(_ONE_RECORD)
    p = _FakePerformance(input_path, expected_path, "sampled")

    result = perf.preflight(p)
    assert result.expected_events is not None
    assert result.sample_indices == [0]  # len(expected)=1 -> everything


def test_preflight_missing_input_raises(tmp_path):
    p = _FakePerformance(tmp_path / "missing.json", None, "disabled")
    with pytest.raises(perf.PerfPreflightError, match="does not exist"):
        perf.preflight(p)


def test_preflight_empty_input_raises(tmp_path):
    input_path = tmp_path / "in.json"
    input_path.write_text("[]")
    p = _FakePerformance(input_path, None, "disabled")
    with pytest.raises(perf.PerfPreflightError, match="empty"):
        perf.preflight(p)


def test_preflight_full_mode_missing_expected_raises(tmp_path):
    input_path = tmp_path / "in.json"
    input_path.write_text(_ONE_RECORD)
    p = _FakePerformance(input_path, tmp_path / "nope.json", "full")
    with pytest.raises(perf.PerfPreflightError, match="expected"):
        perf.preflight(p)


def test_preflight_requires_psutil(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)
    input_path = tmp_path / "in.json"
    input_path.write_text(_ONE_RECORD)
    p = _FakePerformance(input_path, None, "disabled")
    with pytest.raises(perf.PerfPreflightError, match="psutil"):
        perf.preflight(p)


def test_preflight_renders_tokens_when_given(tmp_path):
    # A performance fixture referencing ${...} tokens must be substituted before
    # parsing -- same as IntYamlItem already does for assert.data[].input/match --
    # not silently handed to the operator as the literal token text.
    input_path = tmp_path / "in.json"
    input_path.write_text(json.dumps(
        [{"metadata": {"TableName": "${TABLE}"}, "data": {"values": ["a"], "present": [True]}}]))
    p = _FakePerformance(input_path, None, "disabled")

    result = perf.preflight(p, tokens={"TABLE": "CUSTOMERS"})
    assert result.input_events[0]["metadata"]["TableName"] == "CUSTOMERS"


def test_preflight_without_tokens_leaves_input_events_unrendered(tmp_path):
    input_path = tmp_path / "in.json"
    input_path.write_text(_ONE_RECORD)
    p = _FakePerformance(input_path, None, "disabled")

    result = perf.preflight(p)
    assert result.input_events == json.loads(_ONE_RECORD)


# --- attribute_samples (§7) ---------------------------------------------------

def _sample(start, end, cpu, rss):
    return perf.Sample(window_start_epoch_millis=start, window_end_epoch_millis=end, cpu_percent=cpu, rss_bytes=rss)


def test_attribute_samples_filters_to_window():
    samples = [
        _sample(0, 50, 10.0, 100),      # before window
        _sample(100, 150, 20.0, 200),   # in window
        _sample(150, 200, 30.0, 300),   # in window
        _sample(200, 250, 40.0, 400),   # in window
        _sample(900, 950, 999.0, 999),  # after window
    ]
    result = perf.attribute_samples(samples, measured_start_epoch_millis=100, measured_end_epoch_millis=250)
    assert result.sample_count == 3
    assert result.insufficient_samples is False
    # in-window cpu values: 20.0, 30.0, 40.0 -> avg 30.0, peak 40.0
    assert result.cpu_percent_avg == pytest.approx(30.0)
    assert result.cpu_percent_peak == 40.0
    assert result.memory_rss_peak_bytes == 400
    assert result.cpu_count >= 1
    assert result.cpu_percent_avg_normalized == pytest.approx(30.0 / result.cpu_count)
    assert result.cpu_percent_peak_normalized == pytest.approx(40.0 / result.cpu_count)


def test_attribute_samples_capture_cpu_false_nulls_only_cpu():
    # capture_cpu/capture_memory are independent toggles (PERF_SPEC.md §3) -- both
    # families are always SAMPLED together (one background thread collects both),
    # but disabling one must null out only that family, not the other.
    samples = [
        _sample(100, 150, 20.0, 200),
        _sample(150, 200, 30.0, 300),
        _sample(200, 250, 40.0, 400),
    ]
    result = perf.attribute_samples(samples, measured_start_epoch_millis=100, measured_end_epoch_millis=250,
                                     capture_cpu=False, capture_memory=True)
    assert result.cpu_percent_avg is None
    assert result.cpu_percent_peak is None
    assert result.cpu_percent_avg_normalized is None
    assert result.cpu_percent_peak_normalized is None
    assert result.memory_rss_peak_bytes == 400
    assert result.insufficient_samples is False
    assert result.sample_count == 3


def test_attribute_samples_capture_memory_false_nulls_only_memory():
    samples = [
        _sample(100, 150, 20.0, 200),
        _sample(150, 200, 30.0, 300),
        _sample(200, 250, 40.0, 400),
    ]
    result = perf.attribute_samples(samples, measured_start_epoch_millis=100, measured_end_epoch_millis=250,
                                     capture_cpu=True, capture_memory=False)
    assert result.cpu_percent_avg == pytest.approx(30.0)
    assert result.cpu_percent_peak == 40.0
    assert result.memory_rss_peak_bytes is None
    assert result.insufficient_samples is False
    assert result.sample_count == 3


def test_attribute_samples_partial_overlap_excluded():
    # A sample whose window starts before or ends after the measured interval must
    # NOT count, even if it partially overlaps.
    samples = [
        _sample(90, 150, 10.0, 100),   # starts before window -> excluded
        _sample(150, 260, 10.0, 100),  # ends after window -> excluded
    ]
    result = perf.attribute_samples(samples, measured_start_epoch_millis=100, measured_end_epoch_millis=250)
    assert result.sample_count == 0
    assert result.insufficient_samples is True


def test_attribute_samples_insufficient_when_fewer_than_three():
    samples = [_sample(100, 150, 10.0, 100), _sample(150, 200, 20.0, 200)]
    result = perf.attribute_samples(samples, measured_start_epoch_millis=100, measured_end_epoch_millis=250)
    assert result.insufficient_samples is True
    assert result.cpu_percent_avg is None
    assert result.memory_rss_peak_bytes is None
    assert result.sample_count == 2


def test_attribute_samples_empty_list():
    result = perf.attribute_samples([], measured_start_epoch_millis=100, measured_end_epoch_millis=250)
    assert result.sample_count == 0
    assert result.insufficient_samples is True


# --- check_correctness (§6) ---------------------------------------------------

def _base_perf_result(**overrides):
    result = {
        "measuredInputEvents": 20,
        "measuredOutputEvents": 20,
        "perReplayOutputEventsStable": True,
        "comparedReplayOutputEvents": 2,
        "comparedRecords": [
            {"metadata": {"TableName": "T"}, "data": {"values": ["a"], "present": [True]}},
            {"metadata": {"TableName": "T"}, "data": {"values": ["b"], "present": [True]}},
        ],
        "errorMessage": None,
        "firstUnstableReplayIndex": None,
    }
    result.update(overrides)
    return result


_EXPECTED_TWO = [
    {"metadata": {"TableName": "T"}, "data": {"values": ["a"], "present": [True]}},
    {"metadata": {"TableName": "T"}, "data": {"values": ["b"], "present": [True]}},
]


def test_check_correctness_operator_error_fails_regardless_of_mode():
    result = _base_perf_result(errorMessage="boom")
    outcome = perf.check_correctness(result, events_in_file=2, run_size=10, correctness_mode="disabled",
                                      sample_indices=None, expected_events=None)
    assert outcome.passed is False
    assert "boom" in outcome.reason


def test_check_correctness_measured_input_mismatch_fails():
    result = _base_perf_result(measuredInputEvents=999)
    outcome = perf.check_correctness(result, events_in_file=2, run_size=10, correctness_mode="disabled",
                                      sample_indices=None, expected_events=None)
    assert outcome.passed is False
    assert "measuredInputEvents" in outcome.reason


def test_check_correctness_unstable_emission_fails():
    result = _base_perf_result(perReplayOutputEventsStable=False, firstUnstableReplayIndex=3)
    outcome = perf.check_correctness(result, events_in_file=2, run_size=10, correctness_mode="disabled",
                                      sample_indices=None, expected_events=None)
    assert outcome.passed is False
    assert "3" in outcome.reason


def test_check_correctness_disabled_mode_skips_record_comparison():
    result = _base_perf_result(comparedRecords=[{"garbage": True}], comparedReplayOutputEvents=1)
    outcome = perf.check_correctness(result, events_in_file=2, run_size=10, correctness_mode="disabled",
                                      sample_indices=None, expected_events=None)
    assert outcome.passed is True


def test_check_correctness_full_mode_pass():
    result = _base_perf_result()
    outcome = perf.check_correctness(result, events_in_file=2, run_size=10, correctness_mode="full",
                                      sample_indices=None, expected_events=_EXPECTED_TWO)
    assert outcome.passed is True


def test_check_correctness_full_mode_mismatch_fails():
    result = _base_perf_result()
    bad_expected = [
        {"metadata": {"TableName": "T"}, "data": {"values": ["WRONG"], "present": [True]}},
        _EXPECTED_TWO[1],
    ]
    outcome = perf.check_correctness(result, events_in_file=2, run_size=10, correctness_mode="full",
                                      sample_indices=None, expected_events=bad_expected)
    assert outcome.passed is False


def test_check_correctness_full_mode_length_mismatch_fails_immediately():
    result = _base_perf_result(comparedReplayOutputEvents=1)
    outcome = perf.check_correctness(result, events_in_file=2, run_size=10, correctness_mode="full",
                                      sample_indices=None, expected_events=_EXPECTED_TWO)
    assert outcome.passed is False
    assert "length mismatch" in outcome.reason


def test_check_correctness_sampled_mode_pass():
    result = _base_perf_result()
    outcome = perf.check_correctness(result, events_in_file=2, run_size=10, correctness_mode="sampled",
                                      sample_indices=[0, 1], expected_events=_EXPECTED_TWO)
    assert outcome.passed is True


def test_check_correctness_nonzero_returncode_fails_even_with_clean_result():
    # §6 mandatory checks: "no ... nonzero subprocess exit" -- a JVM that writes a
    # clean result and then dies nonzero during shutdown must still fail.
    result = _base_perf_result()
    outcome = perf.check_correctness(result, events_in_file=2, run_size=10, correctness_mode="disabled",
                                      sample_indices=None, expected_events=None, returncode=1)
    assert outcome.passed is False
    assert "1" in outcome.reason


def test_check_correctness_missing_stability_field_fails_loudly():
    # A missing (not merely False) perReplayOutputEventsStable must not silently
    # default to "stable" -- that would disable the mandatory check on schema drift.
    result = _base_perf_result()
    del result["perReplayOutputEventsStable"]
    outcome = perf.check_correctness(result, events_in_file=2, run_size=10, correctness_mode="disabled",
                                      sample_indices=None, expected_events=None)
    assert outcome.passed is False
    assert "perReplayOutputEventsStable" in outcome.reason


def test_check_correctness_sampled_mode_records_compared_matches_pairs_length():
    # records_compared on a passing outcome must reflect what was ACTUALLY compared
    # (len(pairs)), not just the requested sample_indices count -- these are the
    # same length here on purpose (the failure-mode variant below covers a mismatch).
    result = _base_perf_result()
    outcome = perf.check_correctness(result, events_in_file=2, run_size=10, correctness_mode="sampled",
                                      sample_indices=[0, 1], expected_events=_EXPECTED_TWO)
    assert outcome.passed is True
    assert outcome.records_compared == 2


def test_check_correctness_sampled_mode_length_mismatch_fails_loudly():
    # The driver is handed sample_indices as an input and must retain exactly that
    # many records -- a shorter comparedRecords list (a driver-side truncation/bug)
    # must fail the iteration, not silently zip-truncate to the shorter list and
    # under-report how many records were actually checked.
    result = _base_perf_result(comparedRecords=[
        {"metadata": {"TableName": "T"}, "data": {"values": ["a"], "present": [True]}},
    ])
    outcome = perf.check_correctness(result, events_in_file=2, run_size=10, correctness_mode="sampled",
                                      sample_indices=[0, 1], expected_events=_EXPECTED_TWO)
    assert outcome.passed is False
    assert "length mismatch" in outcome.reason
    assert "expected 2" in outcome.reason and "got 1" in outcome.reason


def test_check_correctness_sampled_mode_reports_absolute_index():
    result = _base_perf_result(comparedRecords=[
        {"metadata": {"TableName": "T"}, "data": {"values": ["a"], "present": [True]}},
        {"metadata": {"TableName": "T"}, "data": {"values": ["WRONG"], "present": [True]}},
    ])
    outcome = perf.check_correctness(result, events_in_file=2, run_size=10, correctness_mode="sampled",
                                      sample_indices=[0, 1], expected_events=_EXPECTED_TWO)
    assert outcome.passed is False
    assert "event 1" in outcome.reason


# --- build_perf_request (§4) ---------------------------------------------------

def test_build_perf_request_shape(tmp_path):
    request = perf.build_perf_request(
        op_jar=tmp_path / "op.jar", properties={"Foo": "bar"}, input_file=tmp_path / "in.json",
        result_file=tmp_path / "result.json", types={"T": ["A"]}, namespace=None, source_name=None,
        password_properties=["Password"], run_size=100, warmup_runs=1, correctness_mode="sampled",
        sample_indices=[0, 1, 2],
    )
    assert request["runSize"] == 100
    assert request["warmupRuns"] == 1
    assert request["correctnessMode"] == "sampled"
    assert request["sampleIndices"] == [0, 1, 2]
    assert request["properties"] == {"Foo": "bar"}
    assert request["passwordProperties"] == ["Password"]
    assert request["opJar"] == str(tmp_path / "op.jar")


# --- process-tree management (§7, §10) -----------------------------------------

def _spawn_sleep_tree(tmp_path, seconds: float):
    """Spawns a parent shell process that execs a child `sleep` -- a minimal real
    process tree with no Java/Docker dependency."""
    return subprocess.Popen(["sh", "-c", f"exec sleep {seconds}"])


def _spawn_two_level_tree(seconds: float):
    """A root shell that does NOT exec -- it backgrounds a real child `sleep` and
    then sleeps itself, so `root.children()` has a genuine descendant, unlike
    `_spawn_sleep_tree`'s single-process exec."""
    return subprocess.Popen(["sh", "-c", f"sleep {seconds} & sleep {seconds}"])


def test_kill_process_tree_leaves_no_survivors(tmp_path):
    proc = _spawn_sleep_tree(tmp_path, 30)
    time.sleep(0.2)  # let it actually start
    survivor = perf._kill_process_tree(proc.pid)
    proc.wait(timeout=5)
    assert survivor is False


def test_kill_process_tree_already_exited_process_returns_false():
    proc = subprocess.Popen(["sh", "-c", "exit 0"])
    proc.wait(timeout=5)
    assert perf._kill_process_tree(proc.pid) is False


def test_kill_process_tree_kills_multi_level_tree_and_reports_no_survivors():
    # Regression test for the bug where verification used a FRESH psutil.Process(pid)
    # lookup after the root was already reaped -- which raises NoSuchProcess
    # immediately and so always (wrongly) reported "no survivors" without ever
    # checking whether a real descendant was still alive. Verification must instead
    # use Process objects captured BEFORE any signal is sent.
    proc = _spawn_two_level_tree(30)
    time.sleep(0.3)  # let the child actually fork and start
    root = psutil.Process(proc.pid)
    child_pids = [c.pid for c in root.children(recursive=True)]
    assert child_pids, "sanity check: the tree must have a real descendant before we kill it"

    survivor = perf._kill_process_tree(proc.pid)
    proc.wait(timeout=5)

    assert survivor is False
    for pid in child_pids:
        assert not psutil.pid_exists(pid), f"descendant {pid} was not actually killed"


def test_sample_process_tree_collects_plausible_samples():
    # A short CPU-busy child so cpu_percent/rss are non-trivial.
    proc = subprocess.Popen([sys.executable, "-c", "import time; t=time.time()\nwhile time.time()-t<0.6: pass"])
    samples: list = []
    stop = threading.Event()
    thread = threading.Thread(target=perf._sample_process_tree, args=(proc.pid, samples, stop, 0.1))
    thread.start()
    proc.wait(timeout=5)
    stop.set()
    thread.join(timeout=5)

    assert len(samples) >= 1
    # A sample taken right as the process exits can legitimately read rss_bytes==0
    # (the process vanished between the liveness check and memory_info()) -- assert
    # real values were captured at least once, not on every single sample.
    assert any(s.rss_bytes > 0 for s in samples)
    assert all(s.window_end_epoch_millis >= s.window_start_epoch_millis for s in samples)


# --- launch_and_measure (§5, §14) -----------------------------------------------

def _write_fake_java(tmp_path, *, sleep_seconds: float, result: dict | None):
    """A `java`-executable stand-in: reads the request file (its last argv), sleeps,
    then (if `result` is given) writes it to `request['resultFile']`. Lets
    launch_and_measure's subprocess/sampling/timeout wiring be exercised for real
    without a JVM."""
    script = tmp_path / "fake_java"
    # Embed the result as a JSON *string literal* (via repr), parsed back with
    # json.loads at script runtime -- json.dumps(result) alone isn't valid Python
    # source (JSON's null/true/false aren't Python's None/True/False).
    result_json_repr = repr("null" if result is None else json.dumps(result))
    script.write_text(f"""#!/usr/bin/env python3
import json, sys, time
request = json.load(open(sys.argv[-1]))
time.sleep({sleep_seconds})
result = json.loads({result_json_repr})
if result is not None:
    with open(request["resultFile"], "w") as f:
        json.dump(result, f)
""")
    script.chmod(0o755)
    return script


_CANNED_RESULT = {
    "measuredStartEpochMillis": 0, "measuredEndEpochMillis": 100,
    "measuredDurationNanos": 100_000_000, "warmupInputEvents": 0, "warmupOutputEvents": 0,
    "measuredInputEvents": 10, "measuredOutputEvents": 10, "comparedReplayOutputEvents": 10,
    "perReplayOutputEventsStable": True, "comparedRecords": [], "errorMessage": None,
    "errorReplayIndex": None, "errorRecordIndex": None, "firstUnstableReplayIndex": None,
}


def test_launch_and_measure_success_writes_and_parses_result(tmp_path):
    fake_java = _write_fake_java(tmp_path, sleep_seconds=0.2, result=_CANNED_RESULT)
    request_file = tmp_path / "request.json"
    result_file = tmp_path / "result.json"
    request = {"resultFile": str(result_file)}

    launch = perf.launch_and_measure(
        java_bin=str(fake_java), harness_jar=tmp_path / "harness.jar", op_jar=tmp_path / "op.jar",
        request=request, request_file=request_file, result_file=result_file, cwd=tmp_path,
        jvm=PerfJvmSpec(), timeout=10,
    )

    assert launch["timed_out"] is False
    assert launch["survivor"] is False
    assert launch["result"] == _CANNED_RESULT
    assert launch["returncode"] == 0
    # The sampler ran concurrently with the 0.2s fake-java sleep.
    assert isinstance(launch["samples"], list)


def test_launch_and_measure_timeout_kills_cleanly(tmp_path):
    fake_java = _write_fake_java(tmp_path, sleep_seconds=30, result=_CANNED_RESULT)
    request_file = tmp_path / "request.json"
    result_file = tmp_path / "result.json"
    request = {"resultFile": str(result_file)}

    launch = perf.launch_and_measure(
        java_bin=str(fake_java), harness_jar=tmp_path / "harness.jar", op_jar=tmp_path / "op.jar",
        request=request, request_file=request_file, result_file=result_file, cwd=tmp_path,
        jvm=PerfJvmSpec(), timeout=1,
    )

    assert launch["timed_out"] is True
    assert launch["survivor"] is False  # killed cleanly -- recoverable per §10, not unrecreatable
    assert launch["result"] is None  # never got to write it


def test_launch_and_measure_jvm_args_placed_before_classpath(tmp_path):
    # A fake "java" that just echoes its argv to the result file, so we can assert
    # on exact placement (PERF_SPEC.md §3: args before -cp).
    script = tmp_path / "fake_java_argv"
    script.write_text("""#!/usr/bin/env python3
import json, sys
request = json.load(open(sys.argv[-1]))
with open(request["resultFile"], "w") as f:
    json.dump({"argv": sys.argv[1:]}, f)
""")
    script.chmod(0o755)
    request_file = tmp_path / "request.json"
    result_file = tmp_path / "result.json"
    request = {"resultFile": str(result_file)}

    launch = perf.launch_and_measure(
        java_bin=str(script), harness_jar=tmp_path / "harness.jar", op_jar=tmp_path / "op.jar",
        request=request, request_file=request_file, result_file=result_file, cwd=tmp_path,
        jvm=PerfJvmSpec(min_heap="512m", max_heap="2g", args=["-XX:+UseG1GC"]), timeout=10,
    )

    argv = launch["result"]["argv"]
    cp_index = argv.index("-cp")
    assert argv[:cp_index] == ["-Xms512m", "-Xmx2g", "-XX:+UseG1GC"]
    assert argv[cp_index + 2] == "com.striim.testing.inttest.PerformanceProcessor"


def test_launch_and_measure_sample_false_collects_no_samples(tmp_path):
    # capture_cpu/capture_memory both false must actually stop the sampler thread
    # from running at all, not just discard its output -- starting it anyway would
    # perturb exactly the measurement the toggles exist to avoid.
    fake_java = _write_fake_java(tmp_path, sleep_seconds=0.3, result=_CANNED_RESULT)
    request_file = tmp_path / "request.json"
    result_file = tmp_path / "result.json"
    request = {"resultFile": str(result_file)}

    launch = perf.launch_and_measure(
        java_bin=str(fake_java), harness_jar=tmp_path / "harness.jar", op_jar=tmp_path / "op.jar",
        request=request, request_file=request_file, result_file=result_file, cwd=tmp_path,
        jvm=PerfJvmSpec(), timeout=10, sample=False,
    )

    assert launch["samples"] == []
    assert launch["result"] == _CANNED_RESULT


# --- _reset_once / _probe_once gcs branch (mirrors the existing spanner branch) --

def _manifest_requiring(requires: list) -> TestManifest:
    return TestManifest(
        name="fake-case", op=OpRef(jar="java/OpenProcessors/FakeOp"), properties={},
        assert_=AssertSpec(data=[], smoke=True), dir=Path("."),
        requires=requires, ddl=[], seed=[], types={}, password_properties=[],
    )


def test_reset_once_ensures_gcs_bucket_when_required(monkeypatch):
    calls = []
    monkeypatch.setattr("inttest.plugin._ensure_gcs_bucket", lambda tokens: calls.append(tokens))

    tokens = {"GCS_BUCKET": "int-test-bucket"}
    result = perf._reset_once(_manifest_requiring(["gcs"]), tokens)

    assert len(calls) == 1
    # Content matches the input tokens...
    assert calls[0] == tokens
    # ...but `_reset_once`'s own docstring promises a DEFENSIVE COPY, not the
    # original dict passed through unchanged -- a bug that skipped the `dict(tokens)`
    # copy and forwarded `tokens` itself would still satisfy `calls[0] == tokens`
    # (equal content) without this identity check.
    assert calls[0] is not tokens
    # And it must be the SAME copy `_reset_once` hands back to its own caller, not
    # some other throwaway dict built along the way -- i.e. `_ensure_gcs_bucket` is
    # called on the one defensive copy this invocation produces, not a stray one.
    assert calls[0] is result


def test_reset_once_skips_gcs_bucket_when_not_required(monkeypatch):
    calls = []
    monkeypatch.setattr("inttest.plugin._ensure_gcs_bucket", lambda tokens: calls.append(tokens))

    perf._reset_once(_manifest_requiring([]), {})

    assert calls == []


def test_probe_once_ensures_gcs_bucket_when_required(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr("inttest.plugin._ensure_gcs_bucket", lambda tokens: calls.append(tokens))

    tokens = {"GCS_BUCKET": "int-test-bucket"}
    perf._probe_once(_manifest_requiring(["gcs"]), tokens, tmp_path)

    assert len(calls) == 1
    # Unlike `_reset_once` (which defensively copies before calling
    # `_ensure_gcs_bucket`), `_probe_once` makes no copy of its own -- it forwards
    # the exact `tokens` dict it was given. Asserting identity here (not just
    # equality) pins that asymmetry deliberately, so a future refactor that added
    # an unnecessary copy to `_probe_once`, or that broke `_reset_once`'s existing
    # copy, would both be caught by their respective test's identity check.
    assert calls[0] is tokens


def test_probe_once_skips_gcs_bucket_when_not_required(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr("inttest.plugin._ensure_gcs_bucket", lambda tokens: calls.append(tokens))

    perf._probe_once(_manifest_requiring([]), {}, tmp_path)

    assert calls == []


# --- run_measured_iteration end to end (§5 steps 1-7, no DB) --------------------

def test_run_measured_iteration_success_end_to_end(tmp_path):
    # A `requires: []` manifest keeps reset_environment/_probe_once DB-free -- only
    # the temp-dir-writable check runs -- so this exercises the full orchestrator
    # (reset -> request build -> launch_and_measure -> metrics -> correctness) with
    # no Docker/Postgres/Oracle/Spanner involved.
    regression_dir = tmp_path / "regression-case"
    regression_dir.mkdir()
    test_manifest = TestManifest(
        name="fake-case", op=OpRef(jar="java/OpenProcessors/FakeOp"), properties={"Foo": "bar"},
        assert_=AssertSpec(data=[], smoke=True), dir=regression_dir,
        requires=[], ddl=[], seed=[], types={}, password_properties=[],
    )

    perf_dir = tmp_path / "perf-case"
    perf_dir.mkdir()
    input_path = perf_dir / "in.json"
    input_path.write_text(_ONE_RECORD)
    performance = PerformanceSpec(
        input="in.json", input_path=input_path,
        run_sizes=[RunSize(authored="10", value=10)],
        correctness=PerfCorrectnessSpec(mode="disabled"),
        metrics=PerfMetricsSpec(capture_cpu=True, capture_memory=True),
    )
    preflight_result = perf.preflight(performance)

    fake_java = _write_fake_java(tmp_path, sleep_seconds=0.1, result=_CANNED_RESULT)

    result = perf.run_measured_iteration(
        manifest=test_manifest, performance=performance, preflight_result=preflight_result,
        run_size=performance.run_sizes[0], tokens={},
        java_bin=str(fake_java), harness_jar=tmp_path / "harness.jar", op_jar=tmp_path / "op.jar",
        scratch_dir=tmp_path,
    )

    assert result.status == "success"
    assert result.failure_reason is None
    assert result.run_size_authored == "10"
    assert result.run_size_value == 10
    assert result.duration_seconds == pytest.approx(0.1)
    assert result.measured_input_events == 10
    assert result.measured_output_events == 10
    assert result.reset_duration_seconds is not None
    assert result.metrics is not None
    assert result.raw_result == _CANNED_RESULT


def test_run_measured_iteration_operator_error_reports_failed(tmp_path):
    regression_dir = tmp_path / "regression-case"
    regression_dir.mkdir()
    test_manifest = TestManifest(
        name="fake-case", op=OpRef(jar="java/OpenProcessors/FakeOp"), properties={},
        assert_=AssertSpec(data=[], smoke=True), dir=regression_dir,
        requires=[], ddl=[], seed=[], types={}, password_properties=[],
    )

    perf_dir = tmp_path / "perf-case"
    perf_dir.mkdir()
    input_path = perf_dir / "in.json"
    input_path.write_text(_ONE_RECORD)
    performance = PerformanceSpec(
        input="in.json", input_path=input_path,
        run_sizes=[RunSize(authored="10", value=10)],
        correctness=PerfCorrectnessSpec(mode="disabled"),
        metrics=PerfMetricsSpec(capture_cpu=False, capture_memory=False),
    )
    preflight_result = perf.preflight(performance)

    errored_result = dict(_CANNED_RESULT, errorMessage="boom", errorReplayIndex=-2)
    fake_java = _write_fake_java(tmp_path, sleep_seconds=0.05, result=errored_result)

    result = perf.run_measured_iteration(
        manifest=test_manifest, performance=performance, preflight_result=preflight_result,
        run_size=performance.run_sizes[0], tokens={},
        java_bin=str(fake_java), harness_jar=tmp_path / "harness.jar", op_jar=tmp_path / "op.jar",
        scratch_dir=tmp_path,
    )

    assert result.status == "failed"
    assert result.failure_reason == "operator error: boom"
    assert result.metrics is None  # both capture flags off


def test_run_measured_iteration_survivor_raises_with_partial_result(tmp_path, monkeypatch):
    # A genuinely un-killable process tree can't be reliably reproduced in a unit
    # test (SIGKILL cannot be "survived" by a normal process), so this monkeypatches
    # launch_and_measure directly to simulate the survivor verdict -- the point is
    # to test run_measured_iteration's OWN handling of that verdict: §10 says
    # partial results are always written and reported, so the iteration that was
    # actually in flight when the survivor was detected must not simply vanish.
    regression_dir = tmp_path / "regression-case"
    regression_dir.mkdir()
    test_manifest = TestManifest(
        name="fake-case", op=OpRef(jar="java/OpenProcessors/FakeOp"), properties={},
        assert_=AssertSpec(data=[], smoke=True), dir=regression_dir,
        requires=[], ddl=[], seed=[], types={}, password_properties=[],
    )
    perf_dir = tmp_path / "perf-case"
    perf_dir.mkdir()
    input_path = perf_dir / "in.json"
    input_path.write_text(_ONE_RECORD)
    performance = PerformanceSpec(
        input="in.json", input_path=input_path,
        run_sizes=[RunSize(authored="10", value=10)],
        correctness=PerfCorrectnessSpec(mode="disabled"),
        metrics=PerfMetricsSpec(capture_cpu=False, capture_memory=False),
    )
    preflight_result = perf.preflight(performance)

    monkeypatch.setattr(perf, "launch_and_measure", lambda **kw: {
        "result": None, "samples": [], "returncode": None,
        "timed_out": True, "survivor": True, "stdout": "", "stderr": "",
    })

    with pytest.raises(perf.UnrecreatableEnvironmentError) as exc_info:
        perf.run_measured_iteration(
            manifest=test_manifest, performance=performance, preflight_result=preflight_result,
            run_size=performance.run_sizes[0], tokens={},
            java_bin="unused", harness_jar=tmp_path / "harness.jar", op_jar=tmp_path / "op.jar",
            scratch_dir=tmp_path,
        )

    e = exc_info.value
    assert e.failure_class == "surviving-process"
    partial = e.partial_iteration_result
    assert partial is not None
    assert partial.status == "failed"
    assert partial.run_size_authored == "10"
    assert partial.run_size_value == 10
    assert "survived" in partial.failure_reason
    assert partial.reset_duration_seconds is not None
    assert partial.raw_result is None


# --- plugin._run_all_iterations (run_sizes x measured_runs bookkeeping, PERF_SPEC.md
# §5/§10) -- extracted out of PerfYamlItem.runtest() so the not_run/db_failed logic
# around an UnrecreatableEnvironmentError is testable without a real JVM/DB. Only the
# LOOP is plugin.py's; run_measured_iteration itself is monkeypatched away. ---

def test_run_all_iterations_marks_remaining_run_sizes_not_run_after_unrecreatable_error(monkeypatch):
    from inttest.plugin import _run_all_iterations

    run_sizes = [RunSize(authored="10", value=10), RunSize(authored="100", value=100)]
    performance = PerformanceSpec(
        input="in.json", input_path=Path("in.json"),
        run_sizes=run_sizes, measured_runs=3,
        correctness=PerfCorrectnessSpec(mode="disabled"),
        metrics=PerfMetricsSpec(capture_cpu=False, capture_memory=False),
    )

    success_result = perf.IterationResult(
        status="success", run_size_authored="10", run_size_value=10,
        duration_seconds=1.0, input_throughput=10.0, output_throughput=10.0,
        measured_input_events=10, measured_output_events=10,
        warmup_input_events=0, warmup_output_events=0,
        metrics=None, reset_duration_seconds=0.01, failure_reason=None, raw_result=None,
    )
    partial_result = perf.IterationResult(
        status="failed", run_size_authored="10", run_size_value=10,
        duration_seconds=None, input_throughput=None, output_throughput=None,
        measured_input_events=None, measured_output_events=None,
        warmup_input_events=None, warmup_output_events=None,
        metrics=None, reset_duration_seconds=0.01,
        failure_reason="operator subprocess tree survived termination + kill after a timeout",
        raw_result=None,
    )

    calls = {"n": 0}

    def fake_run_measured_iteration(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return success_result
        raise perf.UnrecreatableEnvironmentError(
            "boom", "surviving-process", partial_iteration_result=partial_result)

    monkeypatch.setattr(perf, "run_measured_iteration", fake_run_measured_iteration)

    run_size_reports, unrecreatable_error, db_failed = _run_all_iterations(
        manifest=object(), performance=performance, preflight_result=object(),
        tokens={}, java_bin="java", harness_jar=Path("harness.jar"), op_jar=Path("op.jar"),
        scratch_dir=Path("/tmp/scratch"),
    )

    # The 2nd call raises -- the 3rd measured run of run_size 10, and all of run_size
    # 100, must never be attempted.
    assert calls["n"] == 2
    assert db_failed is True
    assert unrecreatable_error is not None
    assert unrecreatable_error.failure_class == "surviving-process"

    assert run_size_reports[0]["run_size"] == run_sizes[0]
    assert run_size_reports[0]["iterations"] == [success_result, partial_result]
    assert run_size_reports[0]["not_run"] is True  # only 2 of 3 measured_runs completed

    assert run_size_reports[1]["run_size"] == run_sizes[1]
    assert run_size_reports[1]["iterations"] == []
    assert run_size_reports[1]["not_run"] is True


def test_run_all_iterations_all_succeed_marks_nothing_not_run(monkeypatch):
    from inttest.plugin import _run_all_iterations

    run_sizes = [RunSize(authored="10", value=10)]
    performance = PerformanceSpec(
        input="in.json", input_path=Path("in.json"),
        run_sizes=run_sizes, measured_runs=2,
        correctness=PerfCorrectnessSpec(mode="disabled"),
        metrics=PerfMetricsSpec(capture_cpu=False, capture_memory=False),
    )
    success_result = perf.IterationResult(
        status="success", run_size_authored="10", run_size_value=10,
        duration_seconds=1.0, input_throughput=10.0, output_throughput=10.0,
        measured_input_events=10, measured_output_events=10,
        warmup_input_events=0, warmup_output_events=0,
        metrics=None, reset_duration_seconds=0.01, failure_reason=None, raw_result=None,
    )
    monkeypatch.setattr(perf, "run_measured_iteration", lambda **kwargs: success_result)

    run_size_reports, unrecreatable_error, db_failed = _run_all_iterations(
        manifest=object(), performance=performance, preflight_result=object(),
        tokens={}, java_bin="java", harness_jar=Path("harness.jar"), op_jar=Path("op.jar"),
        scratch_dir=Path("/tmp/scratch"),
    )

    assert unrecreatable_error is None
    assert db_failed is False
    assert run_size_reports == [{
        "run_size": run_sizes[0],
        "iterations": [success_result, success_result],
        "not_run": False,
    }]


def test_run_all_iterations_does_not_retain_raw_result(monkeypatch):
    # DEFERRED_ISSUES: raw_result (the operator's full parsed JSON) was retained for
    # every iteration of a whole --perf session. Nothing downstream reads it, so the
    # retained copy is cleared -- while every scored field survives intact.
    from inttest.plugin import _run_all_iterations

    run_sizes = [RunSize(authored="10", value=10)]
    performance = PerformanceSpec(
        input="in.json", input_path=Path("in.json"),
        run_sizes=run_sizes, measured_runs=2,
        correctness=PerfCorrectnessSpec(mode="disabled"),
        metrics=PerfMetricsSpec(capture_cpu=False, capture_memory=False),
    )
    heavy = perf.IterationResult(
        status="success", run_size_authored="10", run_size_value=10,
        duration_seconds=1.0, input_throughput=10.0, output_throughput=10.0,
        measured_input_events=10, measured_output_events=10,
        warmup_input_events=0, warmup_output_events=0,
        metrics=None, reset_duration_seconds=0.01, failure_reason=None,
        raw_result={"events": [{"payload": "x" * 1000}]},
    )
    monkeypatch.setattr(perf, "run_measured_iteration", lambda **kwargs: heavy)

    run_size_reports, _unrecreatable, _db_failed = _run_all_iterations(
        manifest=object(), performance=performance, preflight_result=object(),
        tokens={}, java_bin="java", harness_jar=Path("harness.jar"), op_jar=Path("op.jar"),
        scratch_dir=Path("/tmp/scratch"),
    )

    retained = run_size_reports[0]["iterations"]
    assert len(retained) == 2
    assert all(it.raw_result is None for it in retained)
    # Everything perfreport actually reads is untouched.
    assert [it.status for it in retained] == ["success", "success"]
    assert retained[0].duration_seconds == 1.0
    assert retained[0].measured_output_events == 10
    assert retained[0].reset_duration_seconds == 0.01
    # run_measured_iteration's own return value is NOT mutated -- only the copy.
    assert heavy.raw_result == {"events": [{"payload": "x" * 1000}]}


def test_run_all_iterations_does_not_retain_raw_result_on_partial(monkeypatch):
    # The partial iteration carried out of an UnrecreatableEnvironmentError takes the
    # same path -- it is appended by a different branch, so it needs its own pin.
    from inttest.plugin import _run_all_iterations

    run_sizes = [RunSize(authored="10", value=10)]
    performance = PerformanceSpec(
        input="in.json", input_path=Path("in.json"),
        run_sizes=run_sizes, measured_runs=2,
        correctness=PerfCorrectnessSpec(mode="disabled"),
        metrics=PerfMetricsSpec(capture_cpu=False, capture_memory=False),
    )
    partial_result = perf.IterationResult(
        status="failed", run_size_authored="10", run_size_value=10,
        duration_seconds=None, input_throughput=None, output_throughput=None,
        measured_input_events=None, measured_output_events=None,
        warmup_input_events=None, warmup_output_events=None,
        metrics=None, reset_duration_seconds=0.01,
        failure_reason="operator subprocess tree survived termination + kill after a timeout",
        raw_result={"partial": "y" * 1000},
    )

    def fake_run_measured_iteration(**kwargs):
        raise perf.UnrecreatableEnvironmentError(
            "boom", "surviving-process", partial_iteration_result=partial_result)

    monkeypatch.setattr(perf, "run_measured_iteration", fake_run_measured_iteration)

    run_size_reports, unrecreatable_error, _db_failed = _run_all_iterations(
        manifest=object(), performance=performance, preflight_result=object(),
        tokens={}, java_bin="java", harness_jar=Path("harness.jar"), op_jar=Path("op.jar"),
        scratch_dir=Path("/tmp/scratch"),
    )

    retained = run_size_reports[0]["iterations"]
    assert len(retained) == 1
    assert retained[0].raw_result is None
    assert retained[0].status == "failed"
    assert "survived" in retained[0].failure_reason
    # The exception still carries the original, for whoever inspects the error itself.
    assert unrecreatable_error.partial_iteration_result.raw_result == {"partial": "y" * 1000}


def test_run_measured_iteration_timeout_reports_failed_not_unrecreatable(tmp_path):
    # A timeout whose kill succeeds cleanly (no survivor) is a recoverable per-iteration
    # failure (PERF_SPEC.md §10) -- must NOT raise UnrecreatableEnvironmentError.
    regression_dir = tmp_path / "regression-case"
    regression_dir.mkdir()
    test_manifest = TestManifest(
        name="fake-case", op=OpRef(jar="java/OpenProcessors/FakeOp"), properties={},
        assert_=AssertSpec(data=[], smoke=True), dir=regression_dir,
        requires=[], ddl=[], seed=[], types={}, password_properties=[],
    )

    perf_dir = tmp_path / "perf-case"
    perf_dir.mkdir()
    input_path = perf_dir / "in.json"
    input_path.write_text(_ONE_RECORD)
    performance = PerformanceSpec(
        input="in.json", input_path=input_path,
        run_sizes=[RunSize(authored="10", value=10)],
        correctness=PerfCorrectnessSpec(mode="disabled"),
        metrics=PerfMetricsSpec(capture_cpu=False, capture_memory=False),
        timeout=1,
    )
    preflight_result = perf.preflight(performance)

    fake_java = _write_fake_java(tmp_path, sleep_seconds=30, result=_CANNED_RESULT)

    result = perf.run_measured_iteration(
        manifest=test_manifest, performance=performance, preflight_result=preflight_result,
        run_size=performance.run_sizes[0], tokens={},
        java_bin=str(fake_java), harness_jar=tmp_path / "harness.jar", op_jar=tmp_path / "op.jar",
        scratch_dir=tmp_path,
    )

    assert result.status == "failed"
    assert "timed out" in result.failure_reason


def test_run_measured_iteration_malformed_result_reports_failed_not_crash(tmp_path):
    # A parseable-but-unexpected result shape (e.g. a schema-drifted driver) must not
    # raise out of run_measured_iteration -- that would escape §10's not_run
    # bookkeeping in the caller's run_size loop entirely.
    regression_dir = tmp_path / "regression-case"
    regression_dir.mkdir()
    test_manifest = TestManifest(
        name="fake-case", op=OpRef(jar="java/OpenProcessors/FakeOp"), properties={},
        assert_=AssertSpec(data=[], smoke=True), dir=regression_dir,
        requires=[], ddl=[], seed=[], types={}, password_properties=[],
    )

    perf_dir = tmp_path / "perf-case"
    perf_dir.mkdir()
    input_path = perf_dir / "in.json"
    input_path.write_text(_ONE_RECORD)
    performance = PerformanceSpec(
        input="in.json", input_path=input_path,
        run_sizes=[RunSize(authored="10", value=10)],
        correctness=PerfCorrectnessSpec(mode="disabled"),
        metrics=PerfMetricsSpec(capture_cpu=True, capture_memory=True),
    )
    preflight_result = perf.preflight(performance)

    fake_java = _write_fake_java(tmp_path, sleep_seconds=0.05, result={"unexpected": "shape"})

    result = perf.run_measured_iteration(
        manifest=test_manifest, performance=performance, preflight_result=preflight_result,
        run_size=performance.run_sizes[0], tokens={},
        java_bin=str(fake_java), harness_jar=tmp_path / "harness.jar", op_jar=tmp_path / "op.jar",
        scratch_dir=tmp_path,
    )

    assert result.status == "failed"
    assert "PerfResult shape" in result.failure_reason


def test_run_measured_iteration_uses_fresh_iteration_directory(tmp_path):
    # PERF_SPEC.md §9: the per-test temp directory is reset before every measured
    # iteration -- confirm request/result/input files don't accumulate in scratch_dir
    # across iterations (each iteration's own subdirectory is removed after use).
    regression_dir = tmp_path / "regression-case"
    regression_dir.mkdir()
    test_manifest = TestManifest(
        name="fake-case", op=OpRef(jar="java/OpenProcessors/FakeOp"), properties={},
        assert_=AssertSpec(data=[], smoke=True), dir=regression_dir,
        requires=[], ddl=[], seed=[], types={}, password_properties=[],
    )

    perf_dir = tmp_path / "perf-case"
    perf_dir.mkdir()
    input_path = perf_dir / "in.json"
    input_path.write_text(_ONE_RECORD)
    performance = PerformanceSpec(
        input="in.json", input_path=input_path,
        run_sizes=[RunSize(authored="10", value=10)],
        correctness=PerfCorrectnessSpec(mode="disabled"),
        metrics=PerfMetricsSpec(capture_cpu=False, capture_memory=False),
    )
    preflight_result = perf.preflight(performance)
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()

    for _ in range(2):
        fake_java = _write_fake_java(tmp_path, sleep_seconds=0.02, result=_CANNED_RESULT)
        result = perf.run_measured_iteration(
            manifest=test_manifest, performance=performance, preflight_result=preflight_result,
            run_size=performance.run_sizes[0], tokens={},
            java_bin=str(fake_java), harness_jar=tmp_path / "harness.jar", op_jar=tmp_path / "op.jar",
            scratch_dir=scratch_dir,
        )
        assert result.status == "success"

    # Only reset_environment's own .perf-probe-adjacent state may live directly under
    # scratch_dir; no per-iteration subdirectory should survive past its iteration.
    assert list(scratch_dir.iterdir()) == []


# --- ConfigFile content rendering: run_measured_iteration must render a
# ConfigFile's own file CONTENTS the same way IntYamlItem.runtest() does for
# integration mode (plugin.render_config_file), not just the properties: VALUE
# pointing at it -- otherwise a config referencing e.g. ${TID} would reach the
# operator as the literal, un-substituted token text. ---

def test_run_measured_iteration_configfile_render_uses_iter_dir_and_is_readable_at_call_time(
    tmp_path, monkeypatch,
):
    # Separate test (rather than extending the one above) so the read-back of the
    # rendered file's actual CONTENT happens synchronously inside the fake
    # launch_and_measure, before run_measured_iteration's finally block removes
    # iter_dir -- confirms both the substitution itself and that the file is placed
    # somewhere still alive at the moment the operator would read it.
    regression_dir = tmp_path / "regression-case"
    regression_dir.mkdir()
    config_path = regression_dir / "config.json"
    config_path.write_text('{"tableName": "${TID}CUSTOMERS"}')
    test_manifest = TestManifest(
        name="fake-case", op=OpRef(jar="java/OpenProcessors/FakeOp"),
        properties={"ConfigFile": str(config_path)},
        assert_=AssertSpec(data=[], smoke=True), dir=regression_dir,
        requires=[], ddl=[], seed=[], types={}, password_properties=[],
    )

    perf_dir = tmp_path / "perf-case"
    perf_dir.mkdir()
    input_path = perf_dir / "in.json"
    input_path.write_text(_ONE_RECORD)
    performance = PerformanceSpec(
        input="in.json", input_path=input_path,
        run_sizes=[RunSize(authored="10", value=10)],
        correctness=PerfCorrectnessSpec(mode="disabled"),
        metrics=PerfMetricsSpec(capture_cpu=False, capture_memory=False),
    )
    preflight_result = perf.preflight(performance)

    read_back = {}

    def fake_launch_and_measure(**kwargs):
        rendered_path = kwargs["request"]["properties"]["ConfigFile"]
        read_back["content"] = Path(rendered_path).read_text()
        read_back["path"] = rendered_path
        return {
            "result": _CANNED_RESULT, "samples": [], "returncode": 0,
            "timed_out": False, "survivor": False, "stdout": "", "stderr": "",
        }

    monkeypatch.setattr(perf, "launch_and_measure", fake_launch_and_measure)

    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()
    perf.run_measured_iteration(
        manifest=test_manifest, performance=performance, preflight_result=preflight_result,
        run_size=performance.run_sizes[0], tokens={"TID": "t1_"},
        java_bin="unused", harness_jar=tmp_path / "harness.jar", op_jar=tmp_path / "op.jar",
        scratch_dir=scratch_dir,
    )

    assert read_back["content"] == '{"tableName": "t1_CUSTOMERS"}'
    # iter_dir (and this rendered copy inside it) is removed once launch_and_measure
    # returns -- confirms the render targeted iter_dir, not a leaked standalone
    # tempfile.mkdtemp() that would outlive it.
    assert not Path(read_back["path"]).exists()


def test_run_measured_iteration_configfile_path_unchanged_when_no_tokens(tmp_path, monkeypatch):
    regression_dir = tmp_path / "regression-case"
    regression_dir.mkdir()
    config_path = regression_dir / "config.json"
    config_path.write_text('{"tableName": "CUSTOMERS"}')
    test_manifest = TestManifest(
        name="fake-case", op=OpRef(jar="java/OpenProcessors/FakeOp"),
        properties={"ConfigFile": str(config_path)},
        assert_=AssertSpec(data=[], smoke=True), dir=regression_dir,
        requires=[], ddl=[], seed=[], types={}, password_properties=[],
    )

    perf_dir = tmp_path / "perf-case"
    perf_dir.mkdir()
    input_path = perf_dir / "in.json"
    input_path.write_text(_ONE_RECORD)
    performance = PerformanceSpec(
        input="in.json", input_path=input_path,
        run_sizes=[RunSize(authored="10", value=10)],
        correctness=PerfCorrectnessSpec(mode="disabled"),
        metrics=PerfMetricsSpec(capture_cpu=False, capture_memory=False),
    )
    preflight_result = perf.preflight(performance)

    captured = {}

    def fake_launch_and_measure(**kwargs):
        captured["request"] = kwargs["request"]
        return {
            "result": _CANNED_RESULT, "samples": [], "returncode": 0,
            "timed_out": False, "survivor": False, "stdout": "", "stderr": "",
        }

    monkeypatch.setattr(perf, "launch_and_measure", fake_launch_and_measure)

    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()
    perf.run_measured_iteration(
        manifest=test_manifest, performance=performance, preflight_result=preflight_result,
        run_size=performance.run_sizes[0], tokens={},
        java_bin="unused", harness_jar=tmp_path / "harness.jar", op_jar=tmp_path / "op.jar",
        scratch_dir=scratch_dir,
    )

    assert captured["request"]["properties"]["ConfigFile"] == str(config_path)


# --- performance.gcs_prune_prefix (PERF_SPEC.md §9) ------------------------------------------

def test_reset_once_prunes_the_rendered_gcs_prefix_each_iteration(monkeypatch):
    monkeypatch.setattr("inttest.plugin._ensure_gcs_bucket", lambda tokens: None)
    pruned = []
    monkeypatch.setattr("inttest.gcsadmin.delete_prefix", lambda tokens, prefix: pruned.append(prefix) or 0)

    perf._reset_once(_manifest_requiring(["gcs"]), {"GCS_BUCKET": "b", "TID": "t7_"}, "${TID}gcsbw-perf-multi")

    assert pruned == ["t7_gcsbw-perf-multi"]


def test_reset_once_prunes_nothing_without_a_declared_prefix(monkeypatch):
    monkeypatch.setattr("inttest.plugin._ensure_gcs_bucket", lambda tokens: None)
    pruned = []
    monkeypatch.setattr("inttest.gcsadmin.delete_prefix", lambda tokens, prefix: pruned.append(prefix) or 0)

    perf._reset_once(_manifest_requiring(["gcs"]), {"GCS_BUCKET": "b"})

    assert pruned == []


@pytest.mark.parametrize("prefix", ["", "/", "//"])
def test_delete_prefix_refuses_an_empty_prefix(prefix):
    from inttest import gcsadmin
    with pytest.raises(ValueError, match="empty prefix"):
        gcsadmin.delete_prefix({"GCS_BUCKET": "b"}, prefix)
