"""Tests for inttest.perfreport (PERF_SPEC.md §11/§12). No Docker, no
STRIIM_HOME, no real Java/psutil-driven measurement -- `build_report`/`format_console`
are pure transforms of already-computed `perf.IterationResult`s, and the metadata
collectors take an injectable `run` (same convention as inttest.opartifacts) so git/
java subprocesses are faked rather than actually shelled out to, except for one
real-git smoke test gated on `git` being on PATH.
"""
from __future__ import annotations

import json
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from inttest import opartifacts, perf, perfreport
from inttest.manifest import AssertSpec, OpRef, TestManifest
from inttest.perfmanifest import (
    PerfCorrectnessSpec, PerfJvmSpec, PerfManifest, PerfMetricsSpec, PerformanceSpec, RunSize,
)

# --------------------------------------------------------------------------------
# Fixture builders (mirrors tests/test_perf_io.py's direct-dataclass-construction
# convention -- no YAML round trip needed for this module's own tests)
# --------------------------------------------------------------------------------


def _test_manifest(tmp_path, name="fake-case"):
    regression_dir = tmp_path / "regression-case"
    regression_dir.mkdir(exist_ok=True)
    return TestManifest(
        name=name, op=OpRef(jar="java/OpenProcessors/FakeOp"), properties={},
        assert_=AssertSpec(data=[], smoke=True), dir=regression_dir,
        requires=[], ddl=[], seed=[], types={}, password_properties=[],
    )


def _perf_manifest(tmp_path, performance=None):
    # A perf test.yaml is fully self-contained (PERF_SPEC.md §3) -- PerfManifest's
    # own `test_manifest` field is unused by build_report (which reads the
    # SEPARATELY passed `test_manifest=` kwarg every test below already supplies),
    # so this is just a valid placeholder satisfying the dataclass's shape.
    perf_dir = tmp_path / "perf-case"
    perf_dir.mkdir(exist_ok=True)
    performance = performance or PerformanceSpec(
        input="customers.json", input_path=perf_dir / "customers.json",
        run_sizes=[RunSize(authored="10", value=10)],
        jvm=PerfJvmSpec(min_heap="512m", max_heap="2g"),
        correctness=PerfCorrectnessSpec(mode="sampled"),
        metrics=PerfMetricsSpec(capture_cpu=True, capture_memory=True),
    )
    return PerfManifest(
        performance=performance,
        test_manifest=_test_manifest(tmp_path),
        dir=perf_dir,
        path=perf_dir / "test.yaml",
    )


def _preflight(events_in_file=1000, expected_events=1000, sample_indices=None):
    return perf.PreflightResult(
        events_in_file=events_in_file,
        input_events=[{}] * events_in_file,
        expected_events=([{}] * expected_events) if expected_events is not None else None,
        sample_indices=sample_indices,
    )


def _metrics(avg=50.0, peak=80.0, cpu_count=4, mem_bytes=100 * 1024 * 1024, samples=10, insufficient=False):
    return perf.AttributedMetrics(
        cpu_percent_avg=avg, cpu_percent_peak=peak,
        cpu_percent_avg_normalized=avg / cpu_count, cpu_percent_peak_normalized=peak / cpu_count,
        cpu_count=cpu_count, memory_rss_peak_bytes=mem_bytes,
        sample_count=samples, insufficient_samples=insufficient,
    )


def _correctness(passed=True, reason=None, mode="sampled", records_compared=100):
    return perf.CorrectnessOutcome(passed=passed, reason=reason, mode=mode, records_compared=records_compared)


_UNSET = object()


def _iteration(status="success", duration=1.0, input_tp=1000.0, output_tp=1000.0,
               measured_in=10000, measured_out=10000, warmup_in=1000, warmup_out=1000,
               metrics=_UNSET, reset=0.5, failure_reason=None, raw_result=None, correctness="default"):
    if correctness == "default":
        correctness = _correctness() if status == "success" else None
    if metrics is _UNSET:
        metrics = _metrics() if status == "success" else None
    return perf.IterationResult(
        status=status, run_size_authored="10", run_size_value=10,
        duration_seconds=duration, input_throughput=input_tp, output_throughput=output_tp,
        measured_input_events=measured_in, measured_output_events=measured_out,
        warmup_input_events=warmup_in, warmup_output_events=warmup_out,
        metrics=metrics, reset_duration_seconds=reset, failure_reason=failure_reason,
        raw_result=raw_result, correctness=correctness,
    )


_METADATA = {
    "git": {"repository_root": "/repo", "commit": "a" * 40, "branch": "main", "dirty": False,
            "unavailable_reason": None},
    "build": {"striim_home": "/opt/striim", "release": {"STRIIM_VERSION": "5.4.0.6C", "STRIIM_SERIES": "5.4",
              "JAVA_RELEASE": "17"}, "op_jar": {"name": "FakeOp-5.4.jar", "path": "/tmp/FakeOp-5.4.jar",
              "size_bytes": 1234, "modified_utc": "2026-07-30T00:00:00+00:00"},
              "manifest_fingerprint": None, "manifest_fingerprint_unavailable_reason": "no stamp",
              "rebuilt": False, "rebuild_reason": None},
    "environment": {"timestamp_utc": "2026-07-30T00:00:00+00:00", "hostname": "host", "os": "Darwin",
                     "os_release": "25.0.0", "platform": "macOS-25", "cpu_count": 10,
                     "memory_total_bytes": 34359738368, "java_version": "17.0.1", "java_version_unavailable_reason": None,
                     "java_path": "/usr/bin/java", "python_version": "3.11.0", "framework_version": "0.1.0"},
}


def _run_size_entry(run_size, iterations, not_run=False):
    return {"run_size": run_size, "iterations": iterations, "not_run": not_run}


# --------------------------------------------------------------------------------
# A. build_report
# --------------------------------------------------------------------------------


def test_build_report_happy_path_two_run_sizes(tmp_path):
    pm = _perf_manifest(tmp_path, performance=PerformanceSpec(
        input="customers.json", input_path=tmp_path / "customers.json",
        run_sizes=[RunSize(authored="10", value=10), RunSize(authored="100", value=100)],
        jvm=PerfJvmSpec(min_heap="512m", max_heap="2g"),
        correctness=PerfCorrectnessSpec(mode="sampled"),
        metrics=PerfMetricsSpec(capture_cpu=True, capture_memory=True),
    ))
    tm = _test_manifest(tmp_path)
    preflight = _preflight(events_in_file=1000, expected_events=1000, sample_indices=list(range(100)))

    # Deliberately DIFFERENT medians per run size (not just different durations
    # producing the same throughput) so a test that silently read the wrong block,
    # or emitted the two run sizes in the wrong order, would fail here.
    rs1_iters = [_iteration(duration=1.0, input_tp=tp) for tp in (900.0, 1000.0, 1100.0)]
    rs2_iters = [_iteration(duration=1.0, input_tp=tp) for tp in (4500.0, 5000.0, 5500.0)]
    run_size_reports = [
        _run_size_entry(pm.performance.run_sizes[0], rs1_iters),
        _run_size_entry(pm.performance.run_sizes[1], rs2_iters),
    ]

    report = perfreport.build_report(
        perf_manifest=pm, test_manifest=tm, preflight_result=preflight,
        run_size_reports=run_size_reports, unrecreatable_error=None, metadata=_METADATA,
    )

    assert report["schema_version"] == 1
    assert isinstance(report["schema_version"], int)
    assert report["status"] == "PASS"
    assert len(report["run_sizes"]) == 2

    block1, block2 = report["run_sizes"]
    assert block1["run_size"] == {"authored": "10", "value": 10}
    assert block2["run_size"] == {"authored": "100", "value": 100}
    assert block1["expected_input_events"] == 1000 * 10
    assert block2["expected_input_events"] == 1000 * 100
    assert block1["aggregates"]["input_throughput"]["median"] == 1000.0
    assert block2["aggregates"]["input_throughput"]["median"] == 5000.0

    expected_agg1 = perf.aggregate_run_size([it.duration_seconds for it in rs1_iters],
                                             [it.input_throughput for it in rs1_iters])
    assert block1["aggregates"]["duration_seconds"]["median"] == expected_agg1.duration_seconds.median
    assert block1["aggregates"]["input_throughput"]["median"] == expected_agg1.input_throughput.median


def test_build_report_operator_config_redacts_password_properties(tmp_path):
    # The configuration.operator block records the actual op/properties/requires
    # measured (PERF_SPEC.md §11) -- password-valued properties must be redacted
    # since this is a persisted, potentially-shared report file.
    regression_dir = tmp_path / "regression-case"
    regression_dir.mkdir(exist_ok=True)
    tm = TestManifest(
        name="fake-case", op=OpRef(jar="java/OpenProcessors/FakeOp"),
        properties={"DbPassword": "hunter2", "BatchSize": "100"},
        assert_=AssertSpec(data=[], smoke=True), dir=regression_dir,
        requires=["postgres"], ddl=[], seed=[], types={}, password_properties=["DbPassword"],
    )
    pm = _perf_manifest(tmp_path)
    preflight = _preflight()
    run_size_reports = [_run_size_entry(pm.performance.run_sizes[0], [_iteration()])]

    report = perfreport.build_report(
        perf_manifest=pm, test_manifest=tm, preflight_result=preflight,
        run_size_reports=run_size_reports, unrecreatable_error=None, metadata=_METADATA,
    )

    operator = report["configuration"]["operator"]
    assert operator["op_jar_ref"] == tm.module_ref
    assert operator["properties"] == {"DbPassword": "***REDACTED***", "BatchSize": "100"}
    assert operator["requires"] == ["postgres"]
    assert operator["password_properties"] == ["DbPassword"]
    # The redaction must not mutate the real TestManifest.properties in place.
    assert tm.properties["DbPassword"] == "hunter2"


def test_build_report_operator_config_op_jar_ref_carries_udf_jar(tmp_path):
    # A UDF case has no `op:` block at all -- op_jar_ref must still name the
    # driven module, via TestManifest.module_ref.
    from inttest.manifest import UdfSpec, UdfStep

    regression_dir = tmp_path / "regression-case"
    regression_dir.mkdir(exist_ok=True)
    tm = TestManifest(
        name="fake-udf-case", op=None, properties={},
        assert_=AssertSpec(data=[], smoke=True), dir=regression_dir,
        requires=[], ddl=[], seed=[], types={}, password_properties=[],
        udf=UdfSpec(
            jar="java/UserDefinedFunctions/ReferenceUdf",
            class_name="com.example.ReferenceUdf",
            pipeline=[UdfStep(function="ReferenceUdfMarkProcessed", args=[{"reg": True}])],
        ),
    )
    pm = _perf_manifest(tmp_path)
    preflight = _preflight()
    run_size_reports = [_run_size_entry(pm.performance.run_sizes[0], [_iteration()])]

    report = perfreport.build_report(
        perf_manifest=pm, test_manifest=tm, preflight_result=preflight,
        run_size_reports=run_size_reports, unrecreatable_error=None, metadata=_METADATA,
    )

    assert report["configuration"]["operator"]["op_jar_ref"] == "java/UserDefinedFunctions/ReferenceUdf"


def test_build_report_json_round_trips(tmp_path):
    pm = _perf_manifest(tmp_path)
    tm = _test_manifest(tmp_path)
    preflight = _preflight()
    iterations = [_iteration(), _iteration(status="failed", failure_reason="boom", correctness=None,
                              duration=None, input_tp=None, output_tp=None, metrics=None)]
    run_size_reports = [_run_size_entry(pm.performance.run_sizes[0], iterations)]

    report = perfreport.build_report(
        perf_manifest=pm, test_manifest=tm, preflight_result=preflight,
        run_size_reports=run_size_reports, unrecreatable_error=None, metadata=_METADATA,
    )

    text = json.dumps(report)
    round_tripped = json.loads(text)
    assert round_tripped == report


def test_build_report_one_failed_iteration_excluded_from_aggregates(tmp_path):
    pm = _perf_manifest(tmp_path)
    tm = _test_manifest(tmp_path)
    preflight = _preflight()

    good = [_iteration(duration=1.0, input_tp=1000.0), _iteration(duration=1.2, input_tp=900.0)]
    bad = _iteration(status="failed", failure_reason="operator crashed", correctness=None,
                      duration=None, input_tp=None, output_tp=None, metrics=None)
    iterations = good + [bad]
    run_size_reports = [_run_size_entry(pm.performance.run_sizes[0], iterations)]

    report = perfreport.build_report(
        perf_manifest=pm, test_manifest=tm, preflight_result=preflight,
        run_size_reports=run_size_reports, unrecreatable_error=None, metadata=_METADATA,
    )

    assert report["status"] == "FAIL"
    block = report["run_sizes"][0]
    expected_median = perf.aggregate_run_size(
        [it.duration_seconds for it in good], [it.input_throughput for it in good]).input_throughput.median
    assert block["aggregates"]["input_throughput"]["median"] == expected_median
    assert len(block["iterations"]) == 3
    assert block["iterations"][2]["status"] == "failed"
    assert block["iterations"][2]["failure_reason"] == "operator crashed"


def test_build_report_all_iterations_failed_run_size_contributes_none_to_trend(tmp_path):
    pm = _perf_manifest(tmp_path, performance=PerformanceSpec(
        input="customers.json", input_path=tmp_path / "customers.json",
        run_sizes=[RunSize(authored="10", value=10), RunSize(authored="100", value=100)],
        correctness=PerfCorrectnessSpec(mode="disabled"),
        metrics=PerfMetricsSpec(capture_cpu=False, capture_memory=False),
    ))
    tm = _test_manifest(tmp_path)
    preflight = _preflight(expected_events=None)

    all_failed = [_iteration(status="failed", failure_reason="x", correctness=None,
                              duration=None, input_tp=None, output_tp=None, metrics=None) for _ in range(3)]
    ok = [_iteration(duration=1.0, input_tp=500.0, correctness=_correctness(mode="disabled", records_compared=0))
          for _ in range(3)]
    run_size_reports = [
        _run_size_entry(pm.performance.run_sizes[0], all_failed),
        _run_size_entry(pm.performance.run_sizes[1], ok),
    ]

    report = perfreport.build_report(
        perf_manifest=pm, test_manifest=tm, preflight_result=preflight,
        run_size_reports=run_size_reports, unrecreatable_error=None, metadata=_METADATA,
    )

    assert report["run_sizes"][0]["aggregates"]["input_throughput"]["median"] is None


def test_build_report_not_run_and_unrecreatable_error(tmp_path):
    pm = _perf_manifest(tmp_path, performance=PerformanceSpec(
        input="customers.json", input_path=tmp_path / "customers.json",
        run_sizes=[RunSize(authored="10", value=10), RunSize(authored="100", value=100)],
        correctness=PerfCorrectnessSpec(mode="disabled"),
        metrics=PerfMetricsSpec(capture_cpu=False, capture_memory=False),
    ))
    tm = _test_manifest(tmp_path)
    preflight = _preflight(expected_events=None)
    err = perf.UnrecreatableEnvironmentError("environment reset/probe failed twice: boom", "reset-or-probe-failed")

    run_size_reports = [
        _run_size_entry(pm.performance.run_sizes[0], [], not_run=True),
        _run_size_entry(pm.performance.run_sizes[1], [], not_run=True),
    ]

    report = perfreport.build_report(
        perf_manifest=pm, test_manifest=tm, preflight_result=preflight,
        run_size_reports=run_size_reports, unrecreatable_error=err, metadata=_METADATA,
    )

    assert report["status"] == "FAIL"
    assert report["unrecreatable_environment"] == {
        "failure_class": "reset-or-probe-failed",
        "message": "environment reset/probe failed twice: boom",
    }
    for block in report["run_sizes"]:
        assert block["not_run"] is True
        assert block["not_run_reason"] == {
            "failure_class": "reset-or-probe-failed",
            "error": "environment reset/probe failed twice: boom",
        }


def test_build_report_not_run_mid_flight_keeps_completed_iterations(tmp_path):
    # plugin.py marks a run size `not_run=True` whenever an UnrecreatableEnvironmentError
    # fires AND fewer than measured_runs iterations completed -- which is true for the
    # run size that was MID-FLIGHT when the error hit, not just ones that never
    # started. Those completed IterationResults must still be reported (PERF_SPEC.md
    # §10: "partial results are always written and reported"), not silently dropped.
    pm = _perf_manifest(tmp_path, performance=PerformanceSpec(
        input="customers.json", input_path=tmp_path / "customers.json",
        run_sizes=[RunSize(authored="10", value=10)],
        correctness=PerfCorrectnessSpec(mode="disabled"),
        metrics=PerfMetricsSpec(capture_cpu=False, capture_memory=False),
    ))
    tm = _test_manifest(tmp_path)
    preflight = _preflight(expected_events=None)
    err = perf.UnrecreatableEnvironmentError("boom", "surviving-process")

    completed = [_iteration(duration=1.0, input_tp=1000.0, correctness=_correctness(mode="disabled", records_compared=0))]
    run_size_reports = [_run_size_entry(pm.performance.run_sizes[0], completed, not_run=True)]

    report = perfreport.build_report(
        perf_manifest=pm, test_manifest=tm, preflight_result=preflight,
        run_size_reports=run_size_reports, unrecreatable_error=err, metadata=_METADATA,
    )

    block = report["run_sizes"][0]
    assert block["not_run"] is True
    assert len(block["iterations"]) == 1
    assert block["iterations"][0]["status"] == "success"
    assert block["iterations"][0]["duration_seconds"] == 1.0
    assert block["aggregates"]["input_throughput"]["median"] == 1000.0
    assert block["input_events"] == completed[0].measured_input_events

    text = perfreport.format_console(report)
    assert "Median Throughput: 1,000 events/sec" in text
    assert "NOT RUN (remaining iterations): surviving-process (boom)" in text


def test_build_report_metrics_none_when_capture_disabled(tmp_path):
    pm = _perf_manifest(tmp_path)
    tm = _test_manifest(tmp_path)
    preflight = _preflight()
    iterations = [_iteration(metrics=None) for _ in range(2)]
    run_size_reports = [_run_size_entry(pm.performance.run_sizes[0], iterations)]

    report = perfreport.build_report(
        perf_manifest=pm, test_manifest=tm, preflight_result=preflight,
        run_size_reports=run_size_reports, unrecreatable_error=None, metadata=_METADATA,
    )

    block = report["run_sizes"][0]
    assert block["cpu"] == {"avg_percent": None, "peak_percent": None, "cpu_count": None}
    assert block["memory_rss_peak_bytes"] is None
    assert block["iterations"][0]["cpu_percent_avg"] is None


def test_build_report_insufficient_samples_excluded_from_cpu_memory(tmp_path):
    pm = _perf_manifest(tmp_path)
    tm = _test_manifest(tmp_path)
    preflight = _preflight()
    good = _iteration(metrics=_metrics(avg=40.0, peak=60.0, mem_bytes=10 * 1024 * 1024))
    # Deliberately larger/different so a bug that failed to exclude this iteration
    # would show up in avg/peak/memory, not just in insufficient_samples itself.
    insufficient = _iteration(metrics=_metrics(insufficient=True, avg=999.0, peak=999.0, mem_bytes=999 * 1024 * 1024))
    run_size_reports = [_run_size_entry(pm.performance.run_sizes[0], [good, insufficient])]

    report = perfreport.build_report(
        perf_manifest=pm, test_manifest=tm, preflight_result=preflight,
        run_size_reports=run_size_reports, unrecreatable_error=None, metadata=_METADATA,
    )

    block = report["run_sizes"][0]
    assert block["cpu"]["avg_percent"] == 40.0
    assert block["cpu"]["peak_percent"] == 60.0
    assert block["memory_rss_peak_bytes"] == 10 * 1024 * 1024
    assert block["iterations"][1]["insufficient_samples"] is True


def test_build_report_correctness_disabled_mode(tmp_path):
    pm = _perf_manifest(tmp_path, performance=PerformanceSpec(
        input="customers.json", input_path=tmp_path / "customers.json",
        run_sizes=[RunSize(authored="10", value=10)],
        correctness=PerfCorrectnessSpec(mode="disabled"),
        metrics=PerfMetricsSpec(capture_cpu=False, capture_memory=False),
    ))
    tm = _test_manifest(tmp_path)
    preflight = _preflight(expected_events=None)
    iterations = [_iteration(correctness=_correctness(mode="disabled", records_compared=0))]
    run_size_reports = [_run_size_entry(pm.performance.run_sizes[0], iterations)]

    report = perfreport.build_report(
        perf_manifest=pm, test_manifest=tm, preflight_result=preflight,
        run_size_reports=run_size_reports, unrecreatable_error=None, metadata=_METADATA,
    )

    correctness = report["run_sizes"][0]["correctness"]
    assert correctness["passed"] is True
    assert correctness["records_compared"] == 0
    assert correctness["mode"] == "disabled"


def test_build_report_single_iteration_stdev_is_none(tmp_path):
    pm = _perf_manifest(tmp_path)
    tm = _test_manifest(tmp_path)
    preflight = _preflight()
    run_size_reports = [_run_size_entry(pm.performance.run_sizes[0], [_iteration()])]

    report = perfreport.build_report(
        perf_manifest=pm, test_manifest=tm, preflight_result=preflight,
        run_size_reports=run_size_reports, unrecreatable_error=None, metadata=_METADATA,
    )

    assert report["run_sizes"][0]["aggregates"]["duration_seconds"]["stdev"] is None


# --------------------------------------------------------------------------------
# B. format_console
# --------------------------------------------------------------------------------


def _spec_example_report(tmp_path):
    """A report reproducing PERF_SPEC.md §11's literal console example's numbers,
    for one run size (100 replays)."""
    pm = _perf_manifest(tmp_path, performance=PerformanceSpec(
        input="performance/customers.json", input_path=tmp_path / "customers.json",
        run_sizes=[RunSize(authored="100", value=100)],
        jvm=PerfJvmSpec(min_heap="512m", max_heap="2g"),
        warmup_runs=1, measured_runs=3,
        correctness=PerfCorrectnessSpec(mode="sampled"),
        metrics=PerfMetricsSpec(capture_cpu=True, capture_memory=True),
    ))
    tm = _test_manifest(tmp_path, name="transform-regex-strip-non-ascii")
    preflight = _preflight(events_in_file=1000, expected_events=100000, sample_indices=list(range(1000)))

    durations = [1.902, 1.913, 1.941]
    # Chosen independently of `durations` so the median lands exactly on 52,300 --
    # PERF_SPEC.md §11's example numbers aren't derived from each other either.
    throughputs = [52576.23, 52300.0, 51520.87]
    metrics = [_metrics(avg=168.0, peak=192.0, cpu_count=10, mem_bytes=220 * 1024 * 1024) for _ in durations]
    iterations = [
        _iteration(duration=d, input_tp=tp, output_tp=tp,
                   measured_in=100000, measured_out=100000, metrics=m,
                   correctness=_correctness(mode="sampled", records_compared=1000))
        for d, tp, m in zip(durations, throughputs, metrics)
    ]
    run_size_reports = [_run_size_entry(pm.performance.run_sizes[0], iterations)]

    return perfreport.build_report(
        perf_manifest=pm, test_manifest=tm, preflight_result=preflight,
        run_size_reports=run_size_reports, unrecreatable_error=None, metadata=_METADATA,
    )


def test_format_console_spec_example_parity(tmp_path):
    report = _spec_example_report(tmp_path)
    text = perfreport.format_console(report)
    lines = text.splitlines()

    assert lines[0] == "Performance Test: transform-regex-strip-non-ascii"
    assert lines[1] == "Input: performance/customers.json (1,000 events)"
    assert lines[2] == "JVM: -Xms512m -Xmx2g"
    assert lines[3] == "Warmup: 1 replay | Measured runs: 3"
    assert lines[4] == ""
    assert lines[5] == "Run Size: 100 replays"
    assert lines[6] == "  Input Events:      100,000 (expected 100,000)"
    assert lines[7] == "  Output Events:     100,000"
    assert lines[8] == "  Median Throughput: 52,300 events/sec"
    assert lines[9] == "  Duration:          1.913 sec median (1.902 / 1.913 / 1.941 min/med/max)"
    assert lines[10] == "  CPU:               168% avg | 192% peak (of 1 core; 10 cores present)"
    assert lines[11] == "  Memory:            220 MB peak RSS"
    assert lines[12] == "  Correctness:       PASS (sampled, 1,000 of 100,000 records)"
    assert lines[13] == ""
    assert lines[14] == "Summary:"
    assert lines[15] == "  Status: PASS"
    assert len(lines) == 16


def test_format_console_report_path_line(tmp_path):
    report = _spec_example_report(tmp_path)
    text = perfreport.format_console(report, report_path="/tmp/x.json")
    assert text.splitlines()[-1] == "  Report: /tmp/x.json"


def test_format_console_partial_failure_shows_iteration_lines(tmp_path):
    pm = _perf_manifest(tmp_path)
    tm = _test_manifest(tmp_path)
    preflight = _preflight()
    good = _iteration(duration=1.0, input_tp=1000.0)
    bad = _iteration(status="failed", failure_reason="operator subprocess exited 1", correctness=None,
                      duration=None, input_tp=None, output_tp=None, metrics=None)
    run_size_reports = [_run_size_entry(pm.performance.run_sizes[0], [good, bad])]
    report = perfreport.build_report(
        perf_manifest=pm, test_manifest=tm, preflight_result=preflight,
        run_size_reports=run_size_reports, unrecreatable_error=None, metadata=_METADATA,
    )

    text = perfreport.format_console(report)
    assert "  Iterations:        1 of 2 succeeded" in text
    assert "    iteration 2 FAILED: operator subprocess exited 1" in text
    # The standard per-run-size lines are still present, in order.
    assert "  Correctness:" in text


def test_format_console_not_run_run_size(tmp_path):
    pm = _perf_manifest(tmp_path, performance=PerformanceSpec(
        input="customers.json", input_path=tmp_path / "customers.json",
        run_sizes=[RunSize(authored="10000", value=10000)],
        correctness=PerfCorrectnessSpec(mode="disabled"),
        metrics=PerfMetricsSpec(capture_cpu=False, capture_memory=False),
    ))
    tm = _test_manifest(tmp_path)
    preflight = _preflight(expected_events=None)
    err = perf.UnrecreatableEnvironmentError("boom", "surviving-process")
    run_size_reports = [_run_size_entry(pm.performance.run_sizes[0], [], not_run=True)]
    report = perfreport.build_report(
        perf_manifest=pm, test_manifest=tm, preflight_result=preflight,
        run_size_reports=run_size_reports, unrecreatable_error=err, metadata=_METADATA,
    )

    text = perfreport.format_console(report)
    assert "Run Size: 10,000 replays" in text
    assert "  NOT RUN: surviving-process (boom)" in text
    assert "  Status: FAIL" in text


def test_format_console_fail_status(tmp_path):
    report = _spec_example_report(tmp_path)
    report["status"] = "FAIL"
    text = perfreport.format_console(report)
    assert "  Status: FAIL" in text


# --------------------------------------------------------------------------------
# C. Metadata collectors
# --------------------------------------------------------------------------------


class _FakeCompleted:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_collect_git_info_parses_injected_output():
    calls = []

    def fake_run(argv):
        calls.append(argv)
        if argv[-1] == "--show-toplevel":
            return _FakeCompleted(stdout="/repo\n")
        if argv[-2:] == ["rev-parse", "HEAD"]:
            return _FakeCompleted(stdout="a" * 40 + "\n")
        if argv[-2:] == ["--abbrev-ref", "HEAD"]:
            return _FakeCompleted(stdout="main\n")
        if argv[-1] == "--porcelain":
            return _FakeCompleted(stdout="")
        raise AssertionError(f"unexpected git invocation: {argv}")

    info = perfreport.collect_git_info("/repo/some/test/dir", run=fake_run)

    assert info == {"repository_root": "/repo", "commit": "a" * 40, "branch": "main",
                     "dirty": False, "unavailable_reason": None}
    assert all(argv[:3] == ["git", "-C", "/repo/some/test/dir"] for argv in calls)


def test_collect_git_info_dirty_when_porcelain_nonempty():
    def fake_run(argv):
        if argv[-1] == "--porcelain":
            return _FakeCompleted(stdout=" M some/file.py\n")
        return _FakeCompleted(stdout="x\n")

    info = perfreport.collect_git_info("/repo", run=fake_run)
    assert info["dirty"] is True


def test_collect_git_info_missing_git_binary():
    def fake_run(argv):
        raise FileNotFoundError("no git")

    info = perfreport.collect_git_info("/repo", run=fake_run)
    assert info["repository_root"] is None
    assert info["commit"] is None
    assert info["branch"] is None
    assert info["dirty"] is None
    assert "no git" in info["unavailable_reason"]


def test_collect_git_info_not_a_work_tree():
    def fake_run(argv):
        return _FakeCompleted(stdout="", stderr="fatal: not a git repository", returncode=128)

    info = perfreport.collect_git_info("/repo", run=fake_run)
    assert info["repository_root"] is None
    assert info["dirty"] is None
    assert "not a git repository" in info["unavailable_reason"]


@pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git not on PATH")
def test_collect_git_info_real_git_smoke():
    info = perfreport.collect_git_info(Path(__file__).resolve().parent)
    assert info["commit"] is not None
    assert __import__("re").match(r"^[0-9a-f]{40}$", info["commit"])
    assert Path(info["repository_root"]).is_dir()
    assert isinstance(info["dirty"], bool)


def _jar_with_fingerprint(path, version="5.4.0.6C", series="5.4", java="17"):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/MANIFEST.MF", (
            "Manifest-Version: 1.0\n"
            f"Striim-Build-Version: {version}\n"
            f"Striim-Build-Series: {series}\n"
            f"Striim-Build-Java: {java}\n"
        ))


def test_collect_build_info_with_fingerprint(tmp_path):
    jar = tmp_path / "FakeOp-5.4.jar"
    _jar_with_fingerprint(jar)
    artifact = opartifacts.BuiltArtifact(path=jar, name=jar.name, op_name="FakeOp")
    release = {"STRIIM_VERSION": "5.4.0.6C", "STRIIM_SERIES": "5.4", "JAVA_RELEASE": "17"}

    info = perfreport.collect_build_info(
        artifact=artifact, release=release, rebuild_reason=None, striim_home="/opt/striim")

    assert info["manifest_fingerprint"] == {"STRIIM_VERSION": "5.4.0.6C", "STRIIM_SERIES": "5.4", "JAVA_RELEASE": "17"}
    assert info["manifest_fingerprint_unavailable_reason"] is None
    assert info["op_jar"]["name"] == "FakeOp-5.4.jar"
    assert info["op_jar"]["size_bytes"] == jar.stat().st_size
    assert info["rebuilt"] is False
    assert info["rebuild_reason"] is None


def test_collect_build_info_no_fingerprint_has_reason(tmp_path):
    jar = tmp_path / "plain.jar"
    with zipfile.ZipFile(jar, "w") as z:
        z.writestr("some/File.class", b"x")
    artifact = opartifacts.BuiltArtifact(path=jar, name=jar.name, op_name="Plain")

    info = perfreport.collect_build_info(
        artifact=artifact, release={}, rebuild_reason="source changed", striim_home=None)

    assert info["manifest_fingerprint"] is None
    assert info["manifest_fingerprint_unavailable_reason"] == "jar has no complete Striim-Build-* manifest stamp"
    assert info["rebuilt"] is True
    assert info["rebuild_reason"] == "source changed"


def test_collect_build_info_fingerprint_read_exception_does_not_raise(tmp_path, monkeypatch):
    jar = tmp_path / "op.jar"
    jar.write_bytes(b"not a real jar at all")
    artifact = opartifacts.BuiltArtifact(path=jar, name=jar.name, op_name="Op")

    def boom(_jar):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(opartifacts, "_read_manifest_fingerprint", boom)
    info = perfreport.collect_build_info(artifact=artifact, release={}, rebuild_reason=None, striim_home=None)

    assert info["manifest_fingerprint"] is None
    assert "kaboom" in info["manifest_fingerprint_unavailable_reason"]


def test_collect_build_info_missing_jar_stat_fails_gracefully(tmp_path):
    artifact = opartifacts.BuiltArtifact(path=tmp_path / "missing.jar", name="missing.jar", op_name="Missing")
    info = perfreport.collect_build_info(artifact=artifact, release={}, rebuild_reason=None, striim_home=None)
    assert info["op_jar"]["size_bytes"] is None
    assert info["op_jar"]["modified_utc"] is None


def test_collect_environment_java_version_from_injected_run():
    def fake_run(argv):
        return _FakeCompleted(stderr='openjdk version "17.0.1" 2023-10-17\n')

    env = perfreport.collect_environment(
        java_bin="/opt/jdk/bin/java", timestamp=datetime(2026, 7, 30, tzinfo=timezone.utc), run=fake_run)

    assert env["java_version"] == 'openjdk version "17.0.1" 2023-10-17'
    assert env["java_version_unavailable_reason"] is None
    assert env["java_path"] == "/opt/jdk/bin/java"
    assert env["timestamp_utc"] == "2026-07-30T00:00:00+00:00"


def test_collect_environment_java_missing_binary():
    def fake_run(argv):
        raise FileNotFoundError("no java")

    env = perfreport.collect_environment(java_bin="java", timestamp=datetime.now(timezone.utc), run=fake_run)
    assert env["java_version"] is None
    assert "no java" in env["java_version_unavailable_reason"]


def test_collect_environment_no_java_bin_resolved():
    env = perfreport.collect_environment(java_bin=None, timestamp=datetime.now(timezone.utc), run=lambda a: _FakeCompleted())
    assert env["java_version"] is None
    assert env["java_version_unavailable_reason"] == "no java binary resolved"


def test_collect_environment_real_fields():
    env = perfreport.collect_environment(java_bin=None, timestamp=datetime.now(timezone.utc))
    assert env["cpu_count"] >= 1
    assert env["memory_total_bytes"] > 0
    from inttest import __version__ as pkg_version
    assert env["framework_version"] == pkg_version
    assert isinstance(env["python_version"], str)


def test_collect_metadata_bundles_all_three(tmp_path):
    jar = tmp_path / "op.jar"
    jar.write_bytes(b"x")
    artifact = opartifacts.BuiltArtifact(path=jar, name=jar.name, op_name="Op")

    metadata = perfreport.collect_metadata(
        test_dir=tmp_path, artifact=artifact, release={}, rebuild_reason=None,
        striim_home=None, java_bin=None, timestamp=datetime.now(timezone.utc),
    )
    assert set(metadata.keys()) == {"git", "build", "environment"}


# --------------------------------------------------------------------------------
# D. Report paths and writing
# --------------------------------------------------------------------------------


def test_report_key_for_distinguishes_same_case_name_across_ops(tmp_path):
    # The exact scenario the report-filename collision fix exists for: two
    # different operators each have a case directory with the SAME last path
    # segment. report_key_for must produce DIFFERENT keys for the two, or one
    # test's report would silently overwrite the other's at the same path.
    perf_root = tmp_path / "perf"
    case_a = perf_root / "op" / "a" / "baseline"
    case_b = perf_root / "op" / "b" / "baseline"
    case_a.mkdir(parents=True)
    case_b.mkdir(parents=True)

    key_a = perfreport.report_key_for(case_a, perf_root)
    key_b = perfreport.report_key_for(case_b, perf_root)

    assert key_a != key_b
    assert key_a == "op-a-baseline"
    assert key_b == "op-b-baseline"


def test_default_report_path_shape(tmp_path):
    ts = datetime(2026, 7, 30, 0, 45, 12, tzinfo=timezone.utc)
    path = perfreport.default_report_path(tmp_path, "transform-regex-strip-non-ascii", ts)

    assert path.parent == tmp_path
    assert path.name.endswith("-transform-regex-strip-non-ascii.json")
    assert ":" not in path.name
    assert path.name.startswith("20260730T004512Z")


def test_default_report_path_sanitizes_unsafe_test_name(tmp_path):
    # Slashes (the only real path-traversal vector in a single filename component)
    # are replaced; the result never escapes results_dir regardless of what a test
    # name contains.
    path = perfreport.default_report_path(tmp_path, "../weird name/../x", datetime.now(timezone.utc))
    assert path.resolve().is_relative_to(tmp_path.resolve())
    assert path.parent == tmp_path
    assert "/" not in path.name


def test_resolve_report_path_default(tmp_path):
    ts = datetime.now(timezone.utc)
    path = perfreport.resolve_report_path(None, tmp_path, "some-test", ts)
    assert path == perfreport.default_report_path(tmp_path, "some-test", ts)


def test_resolve_report_path_explicit_file_single_test(tmp_path):
    explicit = tmp_path / "custom.json"
    path = perfreport.resolve_report_path(str(explicit), tmp_path, "some-test", datetime.now(timezone.utc),
                                           multiple_tests=False)
    assert path == explicit


def test_resolve_report_path_explicit_dir_multi_test(tmp_path):
    explicit_dir = tmp_path / "reports"
    ts = datetime(2026, 7, 30, 0, 0, 0, tzinfo=timezone.utc)
    path = perfreport.resolve_report_path(str(explicit_dir), tmp_path, "some-test", ts, multiple_tests=True)
    assert path.parent == explicit_dir
    assert path.name == "20260730T000000Z-some-test.json"


def test_write_report_creates_parent_and_round_trips(tmp_path):
    report = {"schema_version": 1, "status": "PASS", "nested": {"a": [1, 2, None]}}
    path = tmp_path / "nested" / "dir" / "report.json"

    returned = perfreport.write_report(report, path)

    assert returned == path
    assert path.is_file()
    assert json.loads(path.read_text()) == report


# --------------------------------------------------------------------------------
# E. Plugin-level terminal emit (light -- see plugin.py's _terminal_emit)
# --------------------------------------------------------------------------------


def test_terminal_emit_falls_back_to_print_when_no_reporter(capsys):
    from inttest import plugin

    class _StubConfig:
        class pluginmanager:
            @staticmethod
            def getplugin(name):
                return None

    plugin._terminal_emit(_StubConfig(), "hello\nworld")
    out = capsys.readouterr().out
    assert "hello" in out
    assert "world" in out


# --- the `variant` field -- §85.1 axis 1 -----------------------------------------------


def test_report_carries_neither_axis_by_default(tmp_path):
    """A case using neither axis reports null/empty, not missing keys -- a consumer never
    has to distinguish "no variant" from "this report predates the field"."""
    report = _spec_example_report(tmp_path)
    assert report["test"]["variant"] is None
    assert report["test"]["permutation"] == {}
    assert perfreport.format_console(report).splitlines()[0] == (
        "Performance Test: transform-regex-strip-non-ascii")


def test_report_and_console_name_the_variant(tmp_path):
    """One test.yaml fans out to one report PER RUN. A throughput that does not say which
    engine produced it cannot be compared to anything."""
    report = _spec_example_report(tmp_path)
    report["test"]["variant"] = "spanner-googlesql"
    assert perfreport.format_console(report).splitlines()[0] == (
        "Performance Test: transform-regex-strip-non-ascii [spanner-googlesql]")


def test_report_and_console_name_both_axes(tmp_path):
    """The engine axis and the property axis compose, and the header names both -- the
    difference between two runs one property apart is what a perf case is FOR (§69.3)."""
    report = _spec_example_report(tmp_path)
    report["test"]["variant"] = "sqlserver"
    report["test"]["permutation"] = {"UseUpsert": "false", "CompactEvents": "true"}
    assert perfreport.format_console(report).splitlines()[0] == (
        "Performance Test: transform-regex-strip-non-ascii "
        "[sqlserver, UseUpsert=false, CompactEvents=true]")


def test_console_names_a_permutation_with_no_variant(tmp_path):
    """`matrix:` without `variants:` is the ordinary single-engine paired case."""
    report = _spec_example_report(tmp_path)
    report["test"]["permutation"] = {"UseUpsert": "false"}
    assert perfreport.format_console(report).splitlines()[0] == (
        "Performance Test: transform-regex-strip-non-ascii [UseUpsert=false]")


def test_report_key_separates_the_runs_of_one_case():
    """⚠ ONE perf_dir now produces one report PER RUN. Without the run id in the key, the
    only thing separating two of them in a filename is a SECOND-granularity timestamp, so
    two fast runs of the same case finishing inside one second overwrite each other and the
    loss looks like a report that was never written."""
    perf_root = Path("/repo/scripts/integration/perf")
    case = perf_root / "op" / "jdbcsink" / "upsert-every-engine"
    keys = {
        perfreport.report_key_for(case, perf_root, run_id)
        for run_id in ("[postgres]", "[oracle]", "[postgres-UseUpsert=false]")
    }
    assert len(keys) == 3
    assert perfreport.report_key_for(case, perf_root) == (
        "op-jdbcsink-upsert-every-engine")


def test_report_key_unchanged_for_a_case_with_no_runs_axis():
    """Every pre-existing perf case keeps the filename it had."""
    perf_root = Path("/repo/scripts/integration/perf")
    assert perfreport.report_key_for(perf_root / "op" / "transform" / "x", perf_root) == (
        "op-transform-x")


def test_report_key_is_not_claimed_collision_free_beyond_the_run():
    """The run id distinguishes a case's own runs. It does NOT rescue the pre-existing
    path-flattening collision, and the docstring no longer claims it does."""
    perf_root = Path("/repo/scripts/integration/perf")
    assert perfreport.report_key_for(perf_root / "op" / "x" / "case", perf_root, "[postgres]") == (
        "op-x-case[postgres]")
    # Documented, not fixed: flattening makes these two equal, and _slug then equalises them.
    assert (perfreport.report_key_for(perf_root / "op" / "x" / "case-postgres", perf_root)
            != perfreport.report_key_for(perf_root / "op" / "x" / "case", perf_root, "[postgres]"))


def test_run_id_is_injective_over_values_containing_its_separators():
    """⚠ Node-id selection, `--deselect` and the report filename all build on `_run_id`, so a
    value containing its `-`/`=` separators must not be able to forge another run's id. The
    first version could: `matrix: {A: ['x','x-B=y'], B: ['z','y-B=z']}` rendered two distinct
    permutations as the same `[A=x-B=y-B=z]`."""
    from inttest.plugin import _run_id

    a = _run_id(None, {"A": "x", "B": "y-B=z"})
    b = _run_id(None, {"A": "x-B=y", "B": "z"})
    assert a != b
    # The ordinary cases stay readable -- escaping must not tax the common path.
    assert _run_id(None, {}) == ""
    assert _run_id(None, {"UseUpsert": "false"}) == "[UseUpsert=false]"


# ---- one metric family switched off (capture_cpu / capture_memory, PERF_SPEC.md §7) ----------

def _family_off(cpu=True, memory=True):
    m = _metrics()
    return perf.AttributedMetrics(
        cpu_percent_avg=m.cpu_percent_avg if cpu else None,
        cpu_percent_peak=m.cpu_percent_peak if cpu else None,
        cpu_percent_avg_normalized=m.cpu_percent_avg_normalized if cpu else None,
        cpu_percent_peak_normalized=m.cpu_percent_peak_normalized if cpu else None,
        cpu_count=m.cpu_count, memory_rss_peak_bytes=m.memory_rss_peak_bytes if memory else None,
        sample_count=m.sample_count, insufficient_samples=False,
    )


def test_run_metrics_summary_with_cpu_off_reports_memory_and_null_cpu():
    # Used to raise TypeError (statistics.mean over None), swallowed by the plugin, losing the report.
    its = [_iteration(metrics=_family_off(cpu=False)) for _ in range(3)]
    s = perfreport._run_metrics_summary(its)
    assert s["avg_percent"] is None and s["peak_percent"] is None
    assert s["memory_rss_peak_bytes"] == 100 * 1024 * 1024


def test_run_metrics_summary_with_memory_off_reports_cpu_and_null_memory():
    # Used to raise TypeError (max over None) once two or more iterations were usable.
    its = [_iteration(metrics=_family_off(memory=False)) for _ in range(3)]
    s = perfreport._run_metrics_summary(its)
    assert s["memory_rss_peak_bytes"] is None
    assert s["avg_percent"] == 50.0 and s["peak_percent"] == 80.0
