import pytest
from livetest.assertions import AssertionFailed
from livetest.assertions.diff import parse_diff_specs, assert_diff, DiffSpecError

class FakePg:
    def __init__(self, tables): self._t = tables      # {table: list[dict]}
    def select_rows(self, table): return self._t.get(table, [])

def test_parse_requires_source_and_target():
    with pytest.raises(DiffSpecError):
        parse_diff_specs([{"source": "s.src"}])            # no target
    with pytest.raises(DiffSpecError):
        parse_diff_specs([{"target": "s.tgt"}])            # no source
    assert parse_diff_specs([{"source": "s.src", "target": "s.tgt"}])

def test_assert_diff_passes_when_target_matches_source():
    rows = [{"id": "1", "msg": "a"}, {"id": "2", "msg": "b"}]
    pg = FakePg({"s.src": rows, "s.tgt": list(reversed(rows))})   # order-insensitive
    assert_diff({"postgres-source": pg}, [{"source": "s.src", "target": "s.tgt"}], timeout=1, poll=0)

def test_assert_diff_times_out_when_target_behind():
    pg = FakePg({"s.src": [{"id": "1"}], "s.tgt": []})
    with pytest.raises(AssertionError, match="caught up|behind|!="):
        assert_diff({"postgres-source": pg}, [{"source": "s.src", "target": "s.tgt"}], timeout=0, poll=0)

def test_assert_diff_fails_on_empty_source():
    # Large timeout + real poll: if the guard weren't up-front, this would hang
    # for the full timeout and (once it did fail) match on "source" from the
    # timeout-path message too. The up-front scan must raise essentially
    # immediately, and "is empty" only matches the empty-source message.
    pg = FakePg({"s.src": [], "s.tgt": []})
    with pytest.raises(AssertionError, match="is empty"):
        assert_diff({"postgres-source": pg}, [{"source": "s.src", "target": "s.tgt"}], timeout=100, poll=0)

def test_assert_diff_empty_source_fails_regardless_of_order():
    # First spec is behind (would need to wait/time out), second spec's source
    # is empty. The up-front scan must catch the empty source without ever
    # waiting on the first spec to catch up.
    pg = FakePg({
        "s.src1": [{"id": "1"}], "s.tgt1": [],
        "s.src2": [], "s.tgt2": [],
    })
    specs = [
        {"source": "s.src1", "target": "s.tgt1"},
        {"source": "s.src2", "target": "s.tgt2"},
    ]
    with pytest.raises(AssertionError, match="is empty"):
        assert_diff({"postgres-source": pg}, specs, timeout=100, poll=0)

def test_assert_diff_routes_source_and_target_to_different_admins():
    # Postgres source, Spanner target: each endpoint reads from its own admin.
    pg = FakePg({"slt_x.src": [{"id": "1", "msg": "a"}]})
    sp = FakePg({"emp": [{"id": "1", "msg": "a"}]})
    spec = {"source": "slt_x.src", "source_db": "postgres-source",
            "target": "emp", "target_db": "spanner-google"}
    assert_diff({"postgres-source": pg, "spanner-google": sp}, [spec], timeout=1, poll=0)

def test_assert_diff_unknown_endpoint_db_errors():
    pg = FakePg({"s.src": [{"id": "1"}]})
    with pytest.raises(AssertionError, match="no admin"):
        assert_diff({"postgres-source": pg},
                    [{"source": "s.src", "target": "emp", "target_db": "spanner-google"}],
                    timeout=1, poll=0)


# ---- structured per-assertion records -----------------------------------------------

def test_assert_diff_success_returns_passed_record_with_rows_snapshot():
    rows = [{"id": "1", "msg": "a"}, {"id": "2", "msg": "b"}]
    pg = FakePg({"s.src": rows, "s.tgt": list(reversed(rows))})
    spec = {"source": "s.src", "target": "s.tgt"}
    records = assert_diff({"postgres-source": pg}, [spec], timeout=1, poll=0, db="postgres-source")
    assert len(records) == 1
    rec = records[0]
    assert rec["status"] == "passed"
    assert rec["type"] == "diff"
    assert rec["target"] == "s.tgt"
    assert rec["db"] == "postgres-source"
    assert rec["spec"] == spec
    assert rec["expected"]["kind"] == "rows"
    assert rec["expected"]["rows"] == rows
    assert rec["actual"]["kind"] == "rows"
    assert rec["actual"]["rows"] == list(reversed(rows))


def test_assert_diff_failure_raises_assertion_failed_with_records():
    pg = FakePg({"s.src": [{"id": "1"}], "s.tgt": []})
    spec = {"source": "s.src", "target": "s.tgt"}
    with pytest.raises(AssertionFailed) as exc_info:
        assert_diff({"postgres-source": pg}, [spec], timeout=0, poll=0)
    assert "caught up" in str(exc_info.value)
    records = exc_info.value.records
    assert len(records) == 1
    assert records[0]["status"] == "failed"
    assert records[0]["expected"] == {"kind": "rows", "rows": [{"id": "1"}], "count": 1, "truncated": False}
    assert records[0]["actual"] == {"kind": "rows", "rows": [], "count": 0, "truncated": False}


def test_assert_diff_mixed_specs_report_both_statuses():
    pg = FakePg({
        "s.src1": [{"id": "1"}], "s.tgt1": [{"id": "1"}],
        "s.src2": [{"id": "2"}], "s.tgt2": [],
    })
    specs = [
        {"source": "s.src1", "target": "s.tgt1"},
        {"source": "s.src2", "target": "s.tgt2"},
    ]
    with pytest.raises(AssertionFailed) as exc_info:
        assert_diff({"postgres-source": pg}, specs, timeout=0, poll=0)
    records = exc_info.value.records
    assert len(records) == 2
    by_target = {r["target"]: r for r in records}
    assert by_target["s.tgt1"]["status"] == "passed"
    assert by_target["s.tgt2"]["status"] == "failed"


# --- §85.3: the catch-up clock ------------------------------------------------------------


def test_converged_diff_reports_elapsed(monkeypatch):
    """A diff that converges carries the seconds it took, for §85.3's paired comparison."""
    pg = FakePg({"s.src": [{"id": "1", "msg": "a"}], "s.tgt": [{"id": "1", "msg": "a"}]})
    records = assert_diff({"postgres-source": pg}, [{"source": "s.src", "target": "s.tgt"}],
                          timeout=5, poll=0)
    assert "elapsed_s" in records[0], records[0]
    assert isinstance(records[0]["elapsed_s"], float)
    assert records[0]["elapsed_s"] >= 0.0


def test_timed_out_diff_carries_no_elapsed():
    """⚠ The failure path must NOT report a catch-up time.

    A run that never converged has no catch-up time, and reporting the TIMEOUT as one would make
    the slower writer look like the faster: whichever arm failed would come back with the same
    tidy number as a fast success.
    """
    pg = FakePg({"s.src": [{"id": "1", "msg": "a"}], "s.tgt": []})
    try:
        assert_diff({"postgres-source": pg}, [{"source": "s.src", "target": "s.tgt"}],
                    timeout=0, poll=0)
        raise AssertionError("expected the diff to fail")
    except AssertionFailed as e:
        for rec in e.records:
            assert "elapsed_s" not in rec, f"a timed-out diff must not carry elapsed: {rec}"


def test_elapsed_is_refused_on_a_non_diff_assertion():
    """The schema pins it to `diff` and to a data assertion (an exact `rows:` count has a finish
    line), so it cannot be quietly attached to an assertion type for which 'time to catch up' has
    no meaning."""
    import pytest

    from livetest.resultschema import SchemaError, build_assertion_result

    with pytest.raises(SchemaError, match="only meaningful on a diff"):
        build_assertion_result(type="file", status="passed", spec={}, elapsed_s=1.0)
    build_assertion_result(type="data", status="passed", spec={}, elapsed_s=1.0)


def test_exact_rows_waits_for_the_committed_count():
    """A dirty count that reaches N does not pass the assertion until a committed read agrees:
    the writer has executed its rows, not committed them."""
    from livetest.assertions.data import assert_data

    class _Admin:
        def __init__(self):
            self.committed_calls = 0

        def count_rows(self, table):
            return 5

        def count_rows_committed(self, table):
            self.committed_calls += 1
            return 5 if self.committed_calls >= 3 else 4

    admin = _Admin()
    exact, = assert_data(admin, [{"target": "t", "rows": 5}], ".", timeout=5, poll=0)
    assert exact["status"] == "passed"
    assert admin.committed_calls == 3, "two polls saw an uncommitted count and kept waiting"


def test_exact_rows_data_assertion_carries_elapsed_and_min_rows_does_not():
    """A replay measured by `rows: N` needs the same clock a diff has, or the paired driver has
    nothing to compare; `min_rows` returns as soon as the floor is met and times nothing."""
    from livetest.assertions.data import assert_data

    class _Admin:
        def count_rows(self, table):
            return 5

    exact, floor = assert_data(_Admin(), [{"target": "t", "rows": 5}, {"target": "t", "min_rows": 1}],
                               ".", timeout=1, poll=0)
    assert exact["elapsed_s"] >= 0
    assert "elapsed_s" not in floor


def test_elapsed_must_be_a_non_negative_number():
    import pytest

    from livetest.resultschema import SchemaError, build_assertion_result

    with pytest.raises(SchemaError, match="non-negative number"):
        build_assertion_result(type="diff", status="passed", spec={}, elapsed_s=-1.0)
    with pytest.raises(SchemaError, match="must be a number"):
        build_assertion_result(type="diff", status="passed", spec={}, elapsed_s=True)


# --- §85.3 / §127.5: the correctness gate, which distinct-set comparison is not ---------------


def test_default_diff_is_dedup_blind_and_that_is_deliberate():
    """The convergence signal SHOULD ignore duplicates; pinning it stops a silent tightening.

    `diff` answers "has the target caught up", and a target that is ahead by a duplicate has
    caught up. §85.3's gate is a different question and gets `exact:`.
    """
    src = [{"id": "1"}, {"id": "2"}]
    pg = FakePg({"s.src": src, "s.tgt": [{"id": "1"}, {"id": "1"}, {"id": "2"}]})
    assert_diff({"postgres-source": pg}, [{"source": "s.src", "target": "s.tgt"}],
                timeout=1, poll=0)


def test_exact_diff_catches_a_duplicated_row():
    """⚠ §127.5. The whole point: a writer that duplicates every row must NOT report a clean
    catch-up time in a comparison whose premise is that both arms wrote the same data."""
    src = [{"id": "1"}, {"id": "2"}]
    pg = FakePg({"s.src": src, "s.tgt": [{"id": "1"}, {"id": "1"}, {"id": "2"}]})
    with pytest.raises(AssertionError, match="extra-or-duplicated"):
        assert_diff({"postgres-source": pg},
                    [{"source": "s.src", "target": "s.tgt", "exact": True}], timeout=0, poll=0)


def test_exact_diff_catches_a_missing_row():
    src = [{"id": "1"}, {"id": "2"}]
    pg = FakePg({"s.src": src, "s.tgt": [{"id": "1"}]})
    with pytest.raises(AssertionError, match="missing"):
        assert_diff({"postgres-source": pg},
                    [{"source": "s.src", "target": "s.tgt", "exact": True}], timeout=0, poll=0)


def test_exact_diff_passes_on_identical_multisets_including_repeats():
    """A source that legitimately holds a row twice must be matched twice, not collapsed."""
    rows = [{"id": "1"}, {"id": "1"}, {"id": "2"}]
    pg = FakePg({"s.src": rows, "s.tgt": list(reversed(rows))})   # order-insensitive
    records = assert_diff({"postgres-source": pg},
                          [{"source": "s.src", "target": "s.tgt", "exact": True}],
                          timeout=1, poll=0)
    assert "elapsed_s" in records[0], "an exact diff still reports its catch-up time"


def test_exact_must_be_a_boolean():
    with pytest.raises(DiffSpecError, match="'exact' must be a boolean"):
        parse_diff_specs([{"source": "a", "target": "b", "exact": "yes"}])


def test_substitute_targets_preserves_the_exact_flag():
    """The yaml -> assertion path must not drop `exact:` on the way through.

    ⚠ `substitute_targets` renders tokens into `source`/`target`. It copies the spec with
    `dict(s)`, so extra keys survive — but a rewrite that enumerated known keys instead would
    silently turn every `exact:` gate back into a dedup-blind convergence check, and every paired
    §85.3 case would still PASS. Pinned here because the failure is invisible.
    """
    from livetest.plugin import substitute_targets

    out = substitute_targets(
        [{"source": "${S}.src", "target": "${T}.tgt", "exact": True}],
        {"S": "qasource", "T": "qatarget"})
    assert out[0]["source"] == "qasource.src"
    assert out[0]["target"] == "qatarget.tgt"
    assert out[0]["exact"] is True, f"the gate was dropped in transit: {out[0]}"


def test_diff_kwargs_carries_the_manifest_poll():
    """⚠ Pins the wiring, which is invisible when it works and silent when it breaks.

    Removing `poll` from the call site makes every measurement revert to the 2.0s default:
    `elapsed_s` becomes quantisation and §85.3's comparison reports numbers that look fine.
    Nothing else in the tier would fail.
    """
    from types import SimpleNamespace

    from livetest.plugin import diff_kwargs

    kwargs = diff_kwargs(SimpleNamespace(timeout=90, diff_poll=0.25))
    assert kwargs == {"timeout": 90, "poll": 0.25}, kwargs


def test_fold_names_compares_columns_case_insensitively():
    """A PostgreSQL source names its columns `id`; an Oracle target answers `ID`. Without the
    option the diff never converges (measured, 900s); with it the same rows are equal."""
    src = [{"id": "1", "name": "a"}, {"id": "2", "name": "b"}]
    tgt = [{"ID": "1", "NAME": "a"}, {"ID": "2", "NAME": "b"}]
    pg = FakePg({"s.src": src, "o.tgt": tgt})
    with pytest.raises(AssertionError):
        assert_diff({"postgres-source": pg},
                    [{"source": "s.src", "target": "o.tgt", "exact": True}], timeout=0, poll=0)
    assert_diff({"postgres-source": pg},
                [{"source": "s.src", "target": "o.tgt", "exact": True, "fold_names": True}],
                timeout=0, poll=0)


def test_fold_names_does_not_forgive_a_wrong_value():
    src = [{"id": "1", "name": "a"}]
    pg = FakePg({"s.src": src, "o.tgt": [{"ID": "1", "NAME": "b"}]})
    with pytest.raises(AssertionError):
        assert_diff({"postgres-source": pg},
                    [{"source": "s.src", "target": "o.tgt", "exact": True, "fold_names": True}],
                    timeout=0, poll=0)
