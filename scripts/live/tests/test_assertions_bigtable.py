import pytest

from livetest.assertions import AssertionFailed
from livetest.assertions.bigtable import (
    BigtableSpecError,
    _check_spec,
    assert_bigtable,
    parse_bigtable_specs,
)


# A `read_all`-shaped BigtableAdmin stand-in: the assert tier only reads (`read_all` is the
# newest-cell-per-column, hex-for-bytes snapshot that `expected/*.json` records); it never mutates.
class FakeTableAdmin:
    def __init__(self, tables):
        self._tables = tables
        self.tables_read = []   # (table, cells_per_column_limit) pairs seen
        self.timeouts = []      # the timeout each read_all was given

    def read_all(self, table, cells_per_column_limit=1, timeout=None):
        self.tables_read.append((table, cells_per_column_limit))
        self.timeouts.append(timeout)
        return list(self._tables.get(table, []))

    # the inttest/CLI surface keeps these aliases; the assert tier only uses read_all, but exercise
    # them so a rename in the admin or here is caught here too.
    read_cells = read_all
    read_table = read_all


# The snapshot an int `setup_bigtable` + `assert_target` would record: two rows, newest cell per
# (family, qualifier), values hex.
EMP = [
    {"row": "01", "cells": {"emp:name": "6a6b6c", "emp:age": "1f"}},
    {"row": "02", "cells": {"emp:name": "6b6b"}},
]


# ---- parse ---------------------------------------------------------------------------

def test_parse_accepts_table_and_rowlist():
    specs = parse_bigtable_specs([{"table": "emp", "expected": EMP}])
    assert specs[0]["table"] == "emp"
    assert specs[0]["expected"] == EMP


def test_parse_accepts_an_empty_expected_rowlist():
    # an empty table is a legal assertion (a drop-all-rows case reads zero rows back)
    parse_bigtable_specs([{"table": "emp", "expected": []}])


@pytest.mark.parametrize("bad", [
    [{"object": "o"}],              # neither table nor expected
    [{"table": "emp"}],            # no expected at all
    [{"table": "emp", "expected": "x"}],     # expected must be a row list, not a scalar
    42,                             # not a list of specs
])
def test_parse_rejects_malformed(bad):
    with pytest.raises(BigtableSpecError):
        parse_bigtable_specs(bad)


# ---- the compare (the read-vs-expected comparison) --------------------------------------

def test_check_pass_when_the_table_matches_expected():
    a = FakeTableAdmin({"emp": EMP})
    assert _check_spec(a, {"table": "emp", "expected": EMP}) is None


def test_check_fail_when_one_row_is_short():
    a = FakeTableAdmin({"emp": [EMP[0]]})     # one row short → mismatch
    detail = _check_spec(a, {"table": "emp", "expected": EMP})
    assert detail is not None
    assert "expected 2" in detail


def test_check_fail_when_a_cell_value_differs():
    got = [dict(EMP[0], cells=dict(EMP[0]["cells"], **{"emp:age": "20"})), EMP[1]]
    a = FakeTableAdmin({"emp": got})          # age 0x20 vs expected 0x1f → mismatch
    detail = _check_spec(a, {"table": "emp", "expected": EMP})
    assert detail is not None


# read_all carries the newest-cell-per-column filter (the framework's own "latest version"
# semantics, num_cells=1); the assert trusts read_all to have applied it.
def test_check_reads_newest_cell_per_column_via_read_all():
    a = FakeTableAdmin({"emp": EMP})
    _check_spec(a, {"table": "emp", "expected": EMP})
    assert a.tables_read == [("emp", 1)]


# ---- assert_bigtable (polled; records; AssertionFailed on timeout) ---------------------

def test_assert_bigtable_success_returns_one_passed_record_per_spec():
    a = FakeTableAdmin({"emp": EMP})
    recs = assert_bigtable(a, [{"table": "emp", "expected": EMP}], timeout=1, poll=0,
                           db="bigtable-target")
    assert len(recs) == 1
    r = recs[0]
    assert r["status"] == "passed"
    assert r["type"] == "bigtable"
    assert r["target"] == "emp"
    assert r["db"] == "bigtable-target"
    assert r["spec"] == {"table": "emp", "expected": EMP}


def test_assert_bigtable_carries_the_read_snapshot_into_the_record_even_on_pass():
    a = FakeTableAdmin({"emp": EMP})
    recs = assert_bigtable(a, [{"table": "emp", "expected": EMP}], timeout=1, poll=0)
    # the sidecar records what was read, so a future diff is explainable from the record alone
    assert recs[0]["actual"] is not None
    assert recs[0]["actual"]["kind"] == "rows"
    assert recs[0]["actual"]["rows"] == EMP


class BudgetedTableAdmin(FakeTableAdmin):
    """Like BigtableAdmin: a read whose budget is already spent raises before it is sent."""

    def read_all(self, table, cells_per_column_limit=1, timeout=None):
        if timeout is not None and timeout <= 0:
            raise TimeoutError(f"Bigtable read_all {table} exceeded its {timeout:.4f}s budget")
        return super().read_all(table, cells_per_column_limit, timeout)


def test_assert_bigtable_timeout_keeps_the_last_mismatch_and_rows_read():
    a = BudgetedTableAdmin({"emp": [EMP[0]]})     # the table never reaches the 2 expected rows
    with pytest.raises(AssertionFailed, match="expected 2") as e:
        assert_bigtable(a, [{"table": "emp", "expected": EMP}], timeout=0.2, poll=0.05)
    (rec,) = e.value.records
    assert rec["status"] == "failed" and "expected 2" in rec["detail"]
    assert rec["actual"]["rows"] == [EMP[0]]
    assert all(t > 0 for t in a.timeouts)        # no read was sent with a spent budget


def test_assert_bigtable_on_a_missing_table_reports_it_at_the_deadline():
    from google.api_core.exceptions import NotFound

    class Missing(BudgetedTableAdmin):
        def read_all(self, table, cells_per_column_limit=1, timeout=None):
            if timeout is not None and timeout <= 0:
                return super().read_all(table, cells_per_column_limit, timeout)
            raise NotFound("table emp not found")

    with pytest.raises(AssertionFailed, match="table emp not found"):
        assert_bigtable(Missing({}), [{"table": "emp", "expected": EMP}], timeout=0.2, poll=0.05)


def test_assert_bigtable_polls_status_probe_each_pass_before_success():
    # a status_probe (the plugin's terminal-signal short-circuit) runs on every poll, not once
    probes = []
    a = FakeTableAdmin({"emp": EMP})
    assert_bigtable(a, [{"table": "emp", "expected": EMP}], timeout=1, poll=0,
                    status_probe=probes.append("t"))
    assert probes == ["t"]   # satisfied on the first poll → exactly one probe


# ---- assert_bigtable deadline: one budget, checked before a pass ------------------------

def test_assert_bigtable_hands_each_read_what_is_left_of_its_timeout():
    a = FakeTableAdmin({"emp": EMP, "dept": []})
    assert_bigtable(a, [{"table": "emp", "expected": EMP}, {"table": "dept", "expected": []}],
                    timeout=5, poll=0)
    assert len(a.timeouts) == 2 and all(0 < t <= 5 for t in a.timeouts)
    assert a.timeouts[1] <= a.timeouts[0]


def test_assert_bigtable_fails_a_read_that_matches_only_after_its_timeout():
    import time

    class Slow(FakeTableAdmin):
        def read_all(self, table, cells_per_column_limit=1, timeout=None):
            time.sleep(0.3)
            return super().read_all(table, cells_per_column_limit, timeout)

    with pytest.raises(AssertionFailed) as e:
        assert_bigtable(Slow({"emp": EMP}), [{"table": "emp", "expected": EMP}], timeout=0.1, poll=0)
    (record,) = e.value.records
    assert record["status"] == "failed"
    assert "after the 0.1s deadline" in record["detail"]
    assert record["actual"]["rows"] == EMP          # what was read is still recorded


def test_assert_bigtable_keeps_a_failed_record_when_a_read_expires():
    from google.api_core.exceptions import DeadlineExceeded

    class Expiring(FakeTableAdmin):
        def read_all(self, table, cells_per_column_limit=1, timeout=None):
            raise DeadlineExceeded("read rows timed out")

    with pytest.raises(AssertionFailed) as e:
        assert_bigtable(Expiring({}), [{"table": "emp", "expected": EMP}], timeout=1, poll=0)
    (record,) = e.value.records
    assert record["status"] == "failed"
    assert "did not finish within 1s" in record["detail"]
    assert record["expected"]["rows"] == EMP and record["actual"] is None


def test_assert_bigtable_lets_a_non_timeout_read_error_through():
    class Broken(FakeTableAdmin):
        def read_all(self, table, cells_per_column_limit=1, timeout=None):
            raise PermissionError("not a deadline")

    with pytest.raises(PermissionError):
        assert_bigtable(Broken({}), [{"table": "emp", "expected": EMP}], timeout=1, poll=0)


@pytest.mark.parametrize("error", ["NotFound", "ServiceUnavailable"])
def test_assert_bigtable_polls_through_a_missing_table_or_an_unavailable_emulator(error):
    # Before the writer creates the table, or while the emulator is briefly down, a read fails;
    # the assertion keeps polling and passes once the rows are there.
    from google.api_core import exceptions as gexc

    class Late(FakeTableAdmin):
        reads = 0

        def read_all(self, table, cells_per_column_limit=1, timeout=None):
            Late.reads += 1
            if Late.reads < 3:
                raise getattr(gexc, error)("not yet")
            return super().read_all(table)

    (record,) = assert_bigtable(Late({"emp": EMP}), [{"table": "emp", "expected": EMP}],
                                timeout=5, poll=0)
    assert record["status"] == "passed" and Late.reads == 3


def test_assert_bigtable_fails_at_its_deadline_when_the_table_never_appears():
    from google.api_core.exceptions import NotFound

    class Missing(FakeTableAdmin):
        def read_all(self, table, cells_per_column_limit=1, timeout=None):
            raise NotFound("table emp not found")

    with pytest.raises(AssertionFailed, match="table emp not found") as e:
        assert_bigtable(Missing({}), [{"table": "emp", "expected": EMP}], timeout=0.2, poll=0.05)
    (record,) = e.value.records
    assert record["status"] == "failed" and "deadline" in record["detail"]


def test_bigtable_is_not_a_manifest_assertion_tier_yet():
    # The service README and this module call the comparison groundwork; adding the tier means
    # updating them too.
    from livetest.manifest import VALID_ASSERT_TIERS
    assert "bigtable" not in VALID_ASSERT_TIERS
