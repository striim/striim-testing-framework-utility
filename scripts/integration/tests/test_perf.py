"""Fast, pure-Python tests for inttest.perf (PERF_SPEC.md §6, §8).

No Java, no subprocess, no Docker -- these run everywhere, always.
"""
from __future__ import annotations

import pytest

from inttest import perf


# --- compute_sample_indices (§6 'sampled' mode) ------------------------------

def test_small_n_returns_everything():
    assert perf.compute_sample_indices(0) == []
    assert perf.compute_sample_indices(1) == [0]
    assert perf.compute_sample_indices(1000) == list(range(1000))


def test_large_n_returns_head_middle_tail():
    n = 5000
    result = perf.compute_sample_indices(n)

    assert result == sorted(set(result)), "must be sorted, deduplicated"
    assert result[:100] == list(range(100)), "first 100 indices always included"
    assert result[-100:] == list(range(n - 100, n)), "last 100 indices always included"
    assert len(result) <= 1000
    # m = min(800, n-200) = 800 for n=5000; |S| == 200 + 800 == 1000 exactly
    # (head/tail/middle are disjoint at this scale: middle only touches [100, n-100)).
    assert len(result) == 1000


@pytest.mark.parametrize("n", [1001, 1200, 5000, 100_000, 1_000_000])
def test_result_size_never_exceeds_1000(n):
    result = perf.compute_sample_indices(n)
    m = min(800, n - 200)
    assert len(result) == min(n, 200 + m)
    assert len(result) <= 1000


def test_indices_stay_within_bounds():
    n = 10_000
    result = perf.compute_sample_indices(n)
    assert all(0 <= i < n for i in result)


def test_deterministic_across_calls():
    assert perf.compute_sample_indices(7777) == perf.compute_sample_indices(7777)


def test_boundary_just_above_threshold():
    # n = 1001 is the smallest n taking the head/middle/tail branch.
    result = perf.compute_sample_indices(1001)
    assert result[0] == 0
    assert result[-1] == 1000
    assert len(result) <= 1000


# --- aggregate_metric / aggregate_run_size (§8) ------------------------------

def test_aggregate_metric_empty_is_all_none():
    agg = perf.aggregate_metric([])
    assert agg.min is None
    assert agg.max is None
    assert agg.mean is None
    assert agg.median is None
    assert agg.stdev is None


def test_aggregate_metric_single_value_stdev_is_none():
    agg = perf.aggregate_metric([42.0])
    assert agg.min == 42.0
    assert agg.max == 42.0
    assert agg.mean == 42.0
    assert agg.median == 42.0
    assert agg.stdev is None  # sample stdev undefined for n<2


def test_aggregate_metric_multiple_values():
    values = [10.0, 20.0, 30.0]
    agg = perf.aggregate_metric(values)
    assert agg.min == 10.0
    assert agg.max == 30.0
    assert agg.mean == 20.0
    assert agg.median == 20.0
    assert agg.stdev == pytest.approx(10.0)  # sample stdev of [10,20,30], ddof=1


def test_aggregate_metric_median_even_count_is_mean_of_middle_two():
    agg = perf.aggregate_metric([10.0, 20.0, 30.0, 40.0])
    assert agg.median == 25.0


def test_aggregate_run_size_pairs_duration_and_throughput():
    result = perf.aggregate_run_size([1.0, 2.0, 3.0], [100.0, 200.0, 300.0])
    assert result.duration_seconds.median == 2.0
    assert result.input_throughput.median == 200.0


def test_aggregate_run_size_mismatched_lengths_raises():
    with pytest.raises(ValueError):
        perf.aggregate_run_size([1.0, 2.0], [100.0])


def test_aggregate_run_size_empty_lists_all_none():
    result = perf.aggregate_run_size([], [])
    assert result.duration_seconds.mean is None
    assert result.input_throughput.mean is None


