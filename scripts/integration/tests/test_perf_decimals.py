"""The perf correctness check compares decimals exactly, as assert.data does."""
from __future__ import annotations

import json

from inttest import perf, waevent

RECORD = '{"metadata": {"TableName": "T"}, "data": {"values": [12345678901234567890.12], "present": [true]}}'


def _result_text() -> str:
    return ('{"measuredInputEvents": 1, "measuredOutputEvents": 1, "perReplayOutputEventsStable": true,'
            ' "comparedReplayOutputEvents": 1, "throughputEventsPerSecond": 1234.5,'
            f' "comparedRecords": [{RECORD}], "errorMessage": null, "firstUnstableReplayIndex": null}}')


def test_identical_high_precision_decimals_pass_the_perf_check():
    result = perf._read_result(_result_text())
    outcome = perf.check_correctness(result, events_in_file=1, run_size=1, correctness_mode="full",
                                     sample_indices=None, expected_events=waevent.load(f"[{RECORD}]"))
    assert outcome.passed, outcome.reason


def test_metrics_stay_float_for_the_report():
    assert isinstance(perf._read_result(_result_text())["throughputEventsPerSecond"], float)


def test_a_result_without_records_is_read_unchanged():
    assert perf._read_result(json.dumps({"errorMessage": "boom"})) == {"errorMessage": "boom"}
