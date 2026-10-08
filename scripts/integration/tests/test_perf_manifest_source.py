"""Unit tests for the `source:` (reader) shape of a perf test.yaml.

A reader's replay is `source.max_ticks` ticks rather than one pass over
`performance.input`, so the perf loader has to drop the input requirement for exactly
these cases and reject the keys that would sit dead. Pure parsing/wire tests: no
Docker, no Striim, no pytest-plugin collection.

The Java half of the model is proven in `PerformanceProcessorSourceTest`, which shows
a replay-stable source benchmarking normally and a finite one being caught by the
emission-count stability check.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from inttest.manifest import SourceSpec
from inttest.perf import build_perf_request
from inttest.perfmanifest import PerfManifestError, load_perf_manifest


def _write(tmp_path, text: str):
    test_dir = tmp_path / "some-perf-case"
    test_dir.mkdir()
    manifest_path = test_dir / "test.yaml"
    manifest_path.write_text(text)
    return manifest_path, test_dir


READER = """
name: reader-perf-case
op:
  jar: java/OpenProcessors/ChangeReader
source:
  max_ticks: 50
performance:
  run_sizes: [1]
  correctness:
    mode: disabled
"""


def test_a_reader_perf_case_needs_no_input_fixture(tmp_path):
    path, _ = _write(tmp_path, READER)

    manifest = load_perf_manifest(path)

    assert manifest.test_manifest.source == SourceSpec(max_ticks=50, expect_events=None)
    assert manifest.performance.input is None
    assert manifest.performance.input_path is None


def test_an_input_fixture_on_a_reader_perf_case_is_rejected(tmp_path):
    path, _ = _write(tmp_path, READER + """
  input: input/events.json
""")
    with pytest.raises(PerfManifestError, match="has no meaning for a 'source:' case"):
        load_perf_manifest(path)


def test_an_in_stream_perf_case_still_requires_its_input(tmp_path):
    path, _ = _write(tmp_path, """
name: in-stream-perf-case
op:
  jar: java/OpenProcessors/ExampleTransformOp
performance:
  run_sizes: [1]
  correctness:
    mode: disabled
""")
    with pytest.raises(PerfManifestError, match="'performance.input' is required"):
        load_perf_manifest(path)


def test_expect_events_is_rejected_in_a_perf_case(tmp_path):
    # It stops ticking early, which would make replays different lengths -- and the Java side
    # ignores it here, so it would sit dead in the fixture rather than doing something wrong.
    path, _ = _write(tmp_path, """
name: reader-perf-case
op:
  jar: java/OpenProcessors/ChangeReader
source:
  max_ticks: 50
  expect_events: 10
performance:
  run_sizes: [1]
  correctness:
    mode: disabled
""")
    with pytest.raises(PerfManifestError, match="no meaning in a perf case"):
        load_perf_manifest(path)


def test_source_and_udf_cannot_both_be_present_in_a_perf_case(tmp_path):
    path, _ = _write(tmp_path, """
name: reader-perf-case
udf:
  jar: java/UserDefinedFunctions/ReferenceUdf
  class: com.example.ReferenceUdf
  pipeline:
    - function: trim
source:
  max_ticks: 50
performance:
  run_sizes: [1]
  correctness:
    mode: disabled
""")
    with pytest.raises(PerfManifestError, match="cannot both be present"):
        load_perf_manifest(path)


def test_a_bad_source_block_surfaces_as_a_perf_manifest_error(tmp_path):
    # The regression loader's normalizer is reused wholesale, so its messages must arrive as
    # this module's own error type rather than leaking a ManifestError to the caller.
    path, _ = _write(tmp_path, """
name: reader-perf-case
op:
  jar: java/OpenProcessors/ChangeReader
source:
  max_ticks: 0
performance:
  run_sizes: [1]
  correctness:
    mode: disabled
""")
    with pytest.raises(PerfManifestError, match="'source.max_ticks' is required"):
        load_perf_manifest(path)


def test_the_perf_request_carries_the_source_block_and_a_null_input_file(tmp_path):
    # The key spellings Java's PerfRequest deserializes; `inputFile` must be null rather than a
    # path to a file that was never written.
    request = build_perf_request(
        op_jar=Path("op.jar"), properties={}, input_file=None,
        result_file=tmp_path / "result.json", types=None, namespace=None, source_name=None,
        password_properties=None, run_size=1, warmup_runs=1,
        correctness_mode="disabled", sample_indices=None,
        source=SourceSpec(max_ticks=50, expect_events=None),
    )

    assert request["source"] == {"maxTicks": 50, "expectEvents": None,
                                 "seedWhen": "pre_start"}
    assert request["inputFile"] is None


def test_an_in_stream_perf_request_still_sends_a_null_source(tmp_path):
    request = build_perf_request(
        op_jar=Path("op.jar"), properties={}, input_file=tmp_path / "input.json",
        result_file=tmp_path / "result.json", types=None, namespace=None, source_name=None,
        password_properties=None, run_size=1, warmup_runs=1,
        correctness_mode="disabled", sample_indices=None,
    )

    assert request["source"] is None
    assert request["inputFile"].endswith("input.json")


def test_preflight_skips_the_input_fixture_entirely_for_a_reader(tmp_path):
    # The reader branch of preflight: nothing to find, render, or parse, and it must not
    # invent an empty-fixture failure out of the absence.
    from inttest.perf import preflight

    path, _ = _write(tmp_path, READER)
    manifest = load_perf_manifest(path)

    result = preflight(manifest.performance, source=manifest.test_manifest.source)

    assert result.input_events == []
    assert result.sample_indices is None
    # The replay unit for a reader is a TICK, so this carries max_ticks rather than 0 -- see
    # the mandatory-check test below for why that distinction is load-bearing.
    assert result.events_in_file == 50


def test_preflight_rejects_a_case_with_neither_an_input_nor_a_source(tmp_path):
    from inttest.perf import PerfPreflightError, preflight

    path, _ = _write(tmp_path, READER)
    performance = load_perf_manifest(path).performance

    # Reaching preflight without the source block means nothing would be replayed at all.
    with pytest.raises(PerfPreflightError, match="nothing to replay"):
        preflight(performance)


def test_the_mandatory_input_count_check_counts_ticks_for_a_reader():
    """The seam that unit-testing each half separately would miss.

    `check_correctness` verifies `measuredInputEvents == events_in_file x run_size`. Java reports
    ticks for a reader, so if `events_in_file` were the input fixture's length (zero) this would
    fail EVERY reader iteration with "expected 0, got N" -- a green Java suite and a green manifest
    suite either side of a broken join.
    """
    from inttest.perf import check_correctness

    result = {
        "measuredInputEvents": 150,          # 50 ticks x run_size 3, as Java reports it
        "perReplayOutputEventsStable": True,
        "comparedReplayOutputEvents": 100,
    }

    outcome = check_correctness(result, events_in_file=50, run_size=3,
                                correctness_mode="disabled", expected_events=None,
                                sample_indices=None, returncode=0)

    assert outcome.passed, outcome.reason


def test_an_unstable_reader_still_fails_the_mandatory_stability_check():
    # The finite-source case, seen from the Python side: PerformanceProcessorSourceTest proves
    # Java sets the flag; this proves the runner acts on it rather than reporting a number.
    from inttest.perf import check_correctness

    result = {
        "measuredInputEvents": 150,
        "perReplayOutputEventsStable": False,
        "firstUnstableReplayIndex": 1,
        "comparedReplayOutputEvents": 2,
    }

    outcome = check_correctness(result, events_in_file=50, run_size=3,
                                correctness_mode="disabled", expected_events=None,
                                sample_indices=None, returncode=0)

    assert not outcome.passed
    assert "1" in outcome.reason


# --- reporting -------------------------------------------------------------------
#
# A reader's replay unit is a tick, so the report must not describe it as a fixture:
# `performance.input` is None, and `events_in_file` counts ticks.

def _reader_run_size():
    from inttest.perfmanifest import RunSize
    return RunSize(authored="1", value=1)


def _reader_perf_manifest(tmp_path):
    return _reader_parts(tmp_path)[0]


def _reader_test_manifest(tmp_path):
    return _reader_parts(tmp_path)[1]


def _reader_parts(tmp_path):
    from inttest.manifest import AssertSpec, OpRef, TestManifest
    from inttest.perfmanifest import (
        PerfCorrectnessSpec, PerfJvmSpec, PerfManifest, PerfMetricsSpec, PerformanceSpec,
    )
    perf_dir = tmp_path / "perf-case"
    perf_dir.mkdir(exist_ok=True)
    performance = PerformanceSpec(
        input=None, input_path=None, run_sizes=[_reader_run_size()],
        jvm=PerfJvmSpec(), correctness=PerfCorrectnessSpec(mode="disabled"),
        metrics=PerfMetricsSpec(capture_cpu=False, capture_memory=False),
    )
    test_manifest = TestManifest(
        name="reader-perf-case", op=OpRef(jar="java/OpenProcessors/ErrorReader"),
        properties={}, assert_=AssertSpec(data=[], smoke=True), dir=perf_dir,
        requires=[], ddl=[], seed=[], types={}, password_properties=[],
        source=SourceSpec(max_ticks=1),
    )
    return PerfManifest(performance=performance, test_manifest=test_manifest,
                        dir=perf_dir, path=perf_dir / "test.yaml"), test_manifest


def _reader_report(tmp_path):
    from inttest import perf, perfreport
    from inttest.manifest import AssertSpec, OpRef, TestManifest
    from inttest.perfmanifest import (
        PerfCorrectnessSpec,
        PerfJvmSpec,
        PerfManifest,
        PerfMetricsSpec,
        PerformanceSpec,
        RunSize,
    )

    perf_dir = tmp_path / "perf-case"
    perf_dir.mkdir(exist_ok=True)
    performance = PerformanceSpec(
        input=None, input_path=None,
        run_sizes=[RunSize(authored="10", value=10)],
        jvm=PerfJvmSpec(), correctness=PerfCorrectnessSpec(mode="disabled"),
        metrics=PerfMetricsSpec(capture_cpu=False, capture_memory=False),
    )
    test_manifest = TestManifest(
        name="reader-perf-case", op=OpRef(jar="java/OpenProcessors/ChangeReader"),
        properties={}, assert_=AssertSpec(data=[], smoke=True), dir=perf_dir,
        requires=[], ddl=[], seed=[], types={}, password_properties=[],
        source=SourceSpec(max_ticks=50),
    )
    perf_manifest = PerfManifest(
        performance=performance, test_manifest=test_manifest,
        dir=perf_dir, path=perf_dir / "test.yaml",
    )
    preflight_result = perf.PreflightResult(
        events_in_file=50, input_events=[], expected_events=None, sample_indices=None)

    report = perfreport.build_report(
        perf_manifest=perf_manifest, test_manifest=test_manifest,
        preflight_result=preflight_result, run_size_reports=[],
        unrecreatable_error=None,
        metadata={"generated_at": "2026-08-20T00:00:00Z"},
    )
    return report, perfreport


def test_a_reader_report_says_ticks_and_carries_no_phantom_input_path(tmp_path):
    report, _ = _reader_report(tmp_path)

    assert report["input"]["replay_unit"] == "tick"
    assert report["input"]["events_in_file"] == 50
    assert report["input"]["path"] is None
    # str(None) would have put the literal string "None" here.
    assert report["input"]["resolved_path"] is None


def test_the_console_summary_calls_a_readers_replay_unit_ticks(tmp_path):
    report, perfreport = _reader_report(tmp_path)

    text = perfreport.format_console(report)

    # "Input: None (50 events)" is what this used to say.
    assert "Source: 50 ticks per replay" in text
    assert "Input: None" not in text


def test_a_readers_headline_throughput_is_labelled_ticks_not_events(tmp_path):
    """The mislabelling a real reader perf case exposed.

    Section 8 aggregates INPUT throughput, which for a reader is ticks per second. Printing that
    as "events/sec" understated a reader doing ~21,000 events/sec as "22 events/sec" -- off by
    three orders of magnitude, in the one number a benchmark exists to report.
    """
    from inttest import perf, perfreport

    report, _ = _reader_report(tmp_path)
    # One measured iteration: 1 tick in 0.046s that emitted 1,000 events -- the real shape of a
    # reader perf case, built through the same path the runner uses.
    iteration = perf.IterationResult(
        status="success", run_size_authored="1", run_size_value=1,
        duration_seconds=0.046, input_throughput=22.0, output_throughput=21739.0,
        measured_input_events=1, measured_output_events=1000,
        warmup_input_events=1, warmup_output_events=1000,
        metrics=None, reset_duration_seconds=0.0, failure_reason=None, raw_result=None,
        correctness=perf.CorrectnessOutcome(passed=True, reason=None, mode="disabled",
                                            records_compared=0),
    )
    report["run_sizes"] = perfreport.build_report(
        perf_manifest=_reader_perf_manifest(tmp_path),
        test_manifest=_reader_test_manifest(tmp_path),
        preflight_result=perf.PreflightResult(
            events_in_file=1, input_events=[], expected_events=None, sample_indices=None),
        run_size_reports=[{"run_size": _reader_run_size(), "iterations": [iteration],
                           "not_run": False}],
        unrecreatable_error=None, metadata={"generated_at": "2026-08-20T00:00:00Z"},
    )["run_sizes"]

    text = perfreport.format_console(report)

    assert "22 ticks/sec" in text, text
    assert "22 events/sec" not in text, "the tick rate must not be labelled events/sec"
    # The rate a reader benchmark actually cares about is shown, not left to be inferred.
    assert "21,739 events/sec" in text, text
