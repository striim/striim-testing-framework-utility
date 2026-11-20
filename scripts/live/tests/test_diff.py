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
    assert kwargs == {"timeout": 90, "poll": 0.25, "clock_start": None}, kwargs
    assert diff_kwargs(SimpleNamespace(timeout=90, diff_poll=0.25), 12.5)["clock_start"] == 12.5


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


# ---- the time a diff reports, and the count it checks first ----------------------------------

class _SlowCountingPg(FakePg):
    """A target whose full read is slow, as 20M rows are, and which counts cheaply."""
    diff_counts_rows = True

    def __init__(self, tables, *, read_s=0.0, behind_polls=0):
        super().__init__(tables)
        self.read_s, self.behind, self.reads, self.counts = read_s, behind_polls, [], 0

    def count_rows(self, table):
        self.counts += 1
        if self.behind > 0 and table != "s.src":
            self.behind -= 1
            return 0
        return len(self._t.get(table, []))

    def select_rows(self, table):
        import time as _t
        self.reads.append(table)
        if table != "s.src":
            _t.sleep(self.read_s)
        return super().select_rows(table)


def test_elapsed_is_when_the_passing_poll_started_not_when_its_read_ended():
    """Measured 2026-10-27: a 20M-row target took 552 s to read after it was complete, and the
    whole read landed in `elapsed_s`. The time reported is the start of the poll that passed."""
    rows = [{"id": "1"}]
    pg = _SlowCountingPg({"s.src": rows, "s.tgt": rows}, read_s=0.3)
    rec = assert_diff({"postgres-source": pg}, [{"source": "s.src", "target": "s.tgt", "exact": True}],
                      timeout=5, poll=0)[0]
    assert rec["elapsed_s"] < 0.1, rec["elapsed_s"]


def test_a_count_that_differs_skips_the_full_read():
    rows = [{"id": "1"}, {"id": "2"}]
    pg = _SlowCountingPg({"s.src": rows, "s.tgt": rows}, behind_polls=3)
    assert_diff({"postgres-source": pg}, [{"source": "s.src", "target": "s.tgt", "exact": True}],
                timeout=5, poll=0)
    assert pg.reads.count("s.tgt") == 1, pg.reads
    assert pg.counts == 5          # the source once, the target on each of four polls


def test_a_matching_count_still_needs_the_rows_to_match():
    pg = _SlowCountingPg({"s.src": [{"id": "1", "v": "a"}], "s.tgt": [{"id": "1", "v": "b"}]})
    with pytest.raises(AssertionError, match="does not hold exactly"):
        assert_diff({"postgres-source": pg}, [{"source": "s.src", "target": "s.tgt", "exact": True}],
                    timeout=0, poll=0)


def test_a_not_exact_diff_skips_only_a_count_below_the_distinct_source():
    """Without `exact`, duplicates in the target are allowed, so only a count BELOW the source's
    distinct rows proves the target is behind."""
    src = [{"id": "1"}]
    pg = _SlowCountingPg({"s.src": src, "s.tgt": [{"id": "1"}, {"id": "1"}]})
    assert_diff({"postgres-source": pg}, [{"source": "s.src", "target": "s.tgt"}], timeout=0, poll=0)
    assert pg.reads.count("s.tgt") == 1


def test_an_admin_without_the_opt_in_is_read_in_full_every_poll():
    """Kafka, GCS and BigQuery count differently from what they select, so they never skip."""
    class Unflagged(_SlowCountingPg):
        diff_counts_rows = False
    rows = [{"id": "1"}]
    pg = Unflagged({"s.src": rows, "s.tgt": rows}, behind_polls=5)
    assert_diff({"postgres-source": pg}, [{"source": "s.src", "target": "s.tgt", "exact": True}],
                timeout=5, poll=0)
    assert pg.counts == 0


def test_a_count_that_fails_falls_back_to_the_full_read():
    class Broken(_SlowCountingPg):
        def count_rows(self, table):
            raise RuntimeError("no count here")
    rows = [{"id": "1"}]
    pg = Broken({"s.src": rows, "s.tgt": rows})
    assert_diff({"postgres-source": pg}, [{"source": "s.src", "target": "s.tgt", "exact": True}],
                timeout=1, poll=0)
    assert pg.reads.count("s.tgt") == 1


def test_the_sql_admins_opt_in_to_the_count():
    from livetest.mssqladmin import MssqlAdmin
    from livetest.mysqlclient import MySQLAdmin
    from livetest.oraadmin import OraAdmin
    from livetest.pgclient import PgAdmin
    from livetest.spanneradmin import SpannerAdmin
    from livetest.teradataadmin import TeradataAdmin
    from livetest.verticaadmin import VerticaAdmin
    for cls in (MssqlAdmin, MySQLAdmin, OraAdmin, PgAdmin, SpannerAdmin, TeradataAdmin, VerticaAdmin):
        assert getattr(cls, "diff_counts_rows", False) is True, cls.__name__


def test_a_timeout_after_a_count_still_reports_the_rows():
    """The count skips the read while polling; the failure must still say what differs."""
    src = [{"id": "1"}, {"id": "2"}]
    pg = _SlowCountingPg({"s.src": src, "s.tgt": [{"id": "1"}, {"id": "1"}, {"id": "2"}]})
    with pytest.raises(AssertionFailed) as ei:
        assert_diff({"postgres-source": pg}, [{"source": "s.src", "target": "s.tgt", "exact": True}],
                    timeout=0, poll=0)
    assert "1 extra-or-duplicated" in str(ei.value)
    rec = ei.value.records[0]
    assert rec["actual"]["rows"], rec


def test_elapsed_with_several_specs_is_when_the_last_spec_was_read():
    """Spec 2's target is only read after spec 1's slow read; its time must not be spec 1's."""
    rows = [{"id": "1"}]
    pg = _SlowCountingPg({"s.src": rows, "s.t1": rows, "s.t2": rows}, read_s=0.3)
    recs = assert_diff({"postgres-source": pg},
                       [{"source": "s.src", "target": "s.t1", "exact": True},
                        {"source": "s.src", "target": "s.t2", "exact": True}], timeout=5, poll=0)
    assert 0.25 < recs[0]["elapsed_s"] < 0.45, recs[0]["elapsed_s"]


def test_elapsed_runs_from_the_clock_the_harness_passes():
    import time as _t
    rows = [{"id": "1"}]
    pg = _SlowCountingPg({"s.src": rows, "s.tgt": rows}, behind_polls=2)
    rec = assert_diff({"postgres-source": pg}, [{"source": "s.src", "target": "s.tgt", "exact": True}],
                      timeout=5, poll=0, clock_start=_t.monotonic() - 10)[0]
    assert 10 <= rec["elapsed_s"] < 11, rec
    assert "elapsed_upper_bound" not in rec


def test_a_target_equal_at_the_first_poll_is_only_an_upper_bound():
    """Measured 2026-10-27: a writer done before the first poll reported 0.14 s."""
    import time as _t
    rows = [{"id": "1"}]
    pg = _SlowCountingPg({"s.src": rows, "s.tgt": rows})
    rec = assert_diff({"postgres-source": pg}, [{"source": "s.src", "target": "s.tgt", "exact": True}],
                      timeout=5, poll=0, clock_start=_t.monotonic() - 3)[0]
    assert rec["elapsed_upper_bound"] is True and rec["elapsed_s"] >= 3, rec


def test_without_a_clock_nothing_is_called_an_upper_bound():
    rows = [{"id": "1"}]
    pg = _SlowCountingPg({"s.src": rows, "s.tgt": rows})
    rec = assert_diff({"postgres-source": pg}, [{"source": "s.src", "target": "s.tgt", "exact": True}],
                      timeout=5, poll=0)[0]
    assert "elapsed_upper_bound" not in rec


def test_an_exact_source_is_read_only_once_the_target_count_matches():
    """A 20M-row source took minutes to read before the first poll; counting it is enough until
    the target's count matches."""
    rows = [{"id": "1"}, {"id": "2"}]
    pg = _SlowCountingPg({"s.src": rows, "s.tgt": rows}, behind_polls=3)
    seen = []
    orig = pg.select_rows
    pg.select_rows = lambda t: (seen.append((t, pg.behind)), orig(t))[1]
    assert_diff({"postgres-source": pg}, [{"source": "s.src", "target": "s.tgt", "exact": True}],
                timeout=5, poll=0)
    assert seen[0] == ("s.src", 0), seen


def test_an_empty_source_is_refused_from_its_count():
    pg = _SlowCountingPg({"s.src": [], "s.tgt": []})
    with pytest.raises(AssertionError, match="is empty"):
        assert_diff({"postgres-source": pg}, [{"source": "s.src", "target": "s.tgt", "exact": True}],
                    timeout=100, poll=0)
    assert pg.reads == []


def test_the_upper_bound_flag_needs_its_time():
    from livetest.resultschema import SchemaError, build_assertion_result
    rec = build_assertion_result(type="diff", status="passed", spec={}, elapsed_s=1.0,
                                 elapsed_upper_bound=True)
    assert rec["elapsed_upper_bound"] is True
    assert "elapsed_upper_bound" not in build_assertion_result(type="diff", status="passed", spec={},
                                                               elapsed_s=1.0)
    with pytest.raises(SchemaError):
        build_assertion_result(type="diff", status="passed", spec={}, elapsed_upper_bound=True)


def test_another_specs_source_read_makes_the_time_an_upper_bound():
    """Spec A's source is read inside the passing poll, before spec B is observed: B's time is
    late by up to that read."""
    import time as _t
    rows = [{"id": "1"}]
    pg = _SlowCountingPg({"s.src": rows, "s.t1": rows, "s.t2": rows}, behind_polls=1)
    recs = assert_diff({"postgres-source": pg},
                       [{"source": "s.src", "target": "s.t1", "exact": True},
                        {"source": "s.src", "target": "s.t2", "exact": True}],
                       timeout=5, poll=0, clock_start=_t.monotonic())
    assert recs[0]["elapsed_upper_bound"] is True, recs[0]


def test_one_spec_read_in_its_passing_poll_is_still_a_measurement():
    """A single exact spec: the count matched when the poll began, so the time is measured."""
    import time as _t
    rows = [{"id": "1"}]
    pg = _SlowCountingPg({"s.src": rows, "s.tgt": rows}, behind_polls=1)
    rec = assert_diff({"postgres-source": pg}, [{"source": "s.src", "target": "s.tgt", "exact": True}],
                      timeout=5, poll=0, clock_start=_t.monotonic())[0]
    assert "elapsed_upper_bound" not in rec, rec


def test_a_poll_after_a_source_read_that_failed_is_an_upper_bound():
    """Counts matched before the content did: the source read delayed the next poll."""
    import time as _t
    pg = _SlowCountingPg({"s.src": [{"id": "1", "v": "a"}], "s.tgt": [{"id": "1", "v": "b"}]})
    orig = pg.select_rows
    calls = []

    def select(t):
        rows = orig(t)
        if t == "s.tgt":
            calls.append(t)
            if len(calls) >= 2:
                return [{"id": "1", "v": "a"}]
        return rows
    pg.select_rows = select
    rec = assert_diff({"postgres-source": pg}, [{"source": "s.src", "target": "s.tgt", "exact": True}],
                      timeout=5, poll=0, clock_start=_t.monotonic())[0]
    assert rec["elapsed_upper_bound"] is True, rec


def test_a_source_unreadable_at_the_timeout_keeps_every_record():
    from livetest.assertions import NotYetReadable
    rows = [{"id": "1"}, {"id": "2"}]

    class Unreadable(_SlowCountingPg):
        def select_rows(self, table):
            if table == "s.src":
                raise NotYetReadable("locked")
            return super().select_rows(table)
    pg = Unreadable({"s.src": rows, "s.tgt": rows[:1]})
    with pytest.raises(AssertionFailed) as ei:
        assert_diff({"postgres-source": pg}, [{"source": "s.src", "target": "s.tgt", "exact": True}],
                    timeout=0, poll=0)
    assert "diff not satisfied" in str(ei.value)
    assert len(ei.value.records) == 1


# ---- surplus data: a target AHEAD of its source is never a pass ------------------------------

@pytest.mark.parametrize("admin", [FakePg, _SlowCountingPg], ids=["full-read", "count-first"])
def test_a_target_with_a_row_the_source_lacks_never_passes(admin):
    """Without `exact:` the distinct row SETS must be equal, not the source a subset of the target:
    a writer that leaks a row the source never had has not converged. With the count opt-in the
    target's count (2) is above the source's distinct rows (1), so the full read decides."""
    pg = admin({"s.src": [{"id": "1"}], "s.tgt": [{"id": "1"}, {"id": "2"}]})
    with pytest.raises(AssertionFailed, match=r"\|src\|=1 \|tgt\|=2") as ei:
        assert_diff({"postgres-source": pg}, [{"source": "s.src", "target": "s.tgt"}], timeout=0, poll=0)
    assert [r["status"] for r in ei.value.records] == ["failed"]


def test_a_target_with_a_changed_row_never_passes():
    pg = FakePg({"s.src": [{"id": "1", "v": "a"}], "s.tgt": [{"id": "1", "v": "b"}]})
    with pytest.raises(AssertionFailed, match=r"\|src\|=1 \|tgt\|=1") as ei:
        assert_diff({"postgres-source": pg}, [{"source": "s.src", "target": "s.tgt"}], timeout=0, poll=0)
    assert [r["status"] for r in ei.value.records] == ["failed"]


# ---- stable_seconds: hold a converged diff for N seconds ----------------------------------------

class _TargetSequence(FakePg):
    """The source is static; each target read returns the next state (the last repeats). A state
    may be an exception instance, raised from the read."""
    def __init__(self, src, *states, counts=False):
        super().__init__({"s.src": src})
        self.states, self.reads = list(states), 0
        self.diff_counts_rows = counts

    def select_rows(self, table):
        if table == "s.src":
            return super().select_rows(table)
        state = self.states[min(self.reads, len(self.states) - 1)]
        self.reads += 1
        if isinstance(state, Exception):
            raise state
        return list(state)

    def count_rows(self, table):
        if table == "s.src":
            return len(self._t["s.src"])
        state = self.states[min(self.reads, len(self.states) - 1)]
        return 0 if isinstance(state, Exception) else len(state)


_ROWS = [{"id": "1"}, {"id": "2"}]


def test_parse_accepts_and_validates_stable_seconds():
    assert parse_diff_specs([{"source": "a", "target": "b", "stable_seconds": 3}])
    for bad in (-1, "3", True, None):
        with pytest.raises(DiffSpecError, match="stable_seconds"):
            parse_diff_specs([{"source": "a", "target": "b", "stable_seconds": bad}])


def test_without_stable_seconds_a_late_duplicate_is_never_read():
    pg = _TargetSequence(_ROWS, _ROWS, _ROWS + [_ROWS[1]])
    recs = assert_diff({"postgres-source": pg}, [{"source": "s.src", "target": "s.tgt", "exact": True}],
                       timeout=5, poll=0.01)
    assert recs[0]["status"] == "passed" and pg.reads == 1


def test_stable_seconds_fails_an_exact_diff_when_a_late_duplicate_lands():
    pg = _TargetSequence(_ROWS, _ROWS, _ROWS + [_ROWS[1]])
    with pytest.raises(AssertionFailed) as ei:
        assert_diff({"postgres-source": pg},
                    [{"source": "s.src", "target": "s.tgt", "exact": True, "stable_seconds": 5}],
                    timeout=5, poll=0.01)
    assert "stable_seconds" in str(ei.value) and "1 extra-or-duplicated" in str(ei.value)
    assert ei.value.records[0]["status"] == "failed"
    assert "elapsed_s" not in ei.value.records[0]


def test_stable_seconds_fails_a_default_diff_when_a_late_wrong_row_lands():
    pg = _TargetSequence(_ROWS, _ROWS, _ROWS + [{"id": "9"}])
    with pytest.raises(AssertionFailed, match="stable_seconds"):
        assert_diff({"postgres-source": pg}, [{"source": "s.src", "target": "s.tgt", "stable_seconds": 5}],
                    timeout=5, poll=0.01)


@pytest.mark.parametrize("late", [[_ROWS[0], {"id": "9"}], _ROWS + [_ROWS[1]]])
def test_stable_seconds_on_counting_admins_reads_the_target_in_full(late):
    # The window reads in full: a count that still matches proves nothing about a changed row, and
    # a count that differs must fail as a change, not be taken for an unreadable poll.
    pg = _TargetSequence(_ROWS, _ROWS, late, counts=True)
    with pytest.raises(AssertionFailed, match="passed, then failed within its stable_seconds window"):
        assert_diff({"postgres-source": pg},
                    [{"source": "s.src", "target": "s.tgt", "exact": True, "stable_seconds": 5}],
                    timeout=1, poll=0.01)


def test_stable_seconds_passes_an_unchanged_target_and_keeps_the_first_pass_elapsed():
    import time as _t
    pg = _TargetSequence(_ROWS, _ROWS)
    t0 = _t.monotonic()
    recs = assert_diff({"postgres-source": pg},
                       [{"source": "s.src", "target": "s.tgt", "exact": True, "stable_seconds": 0.3}],
                       timeout=5, poll=0.05)
    assert _t.monotonic() - t0 >= 0.3 and pg.reads > 2
    assert recs[0]["status"] == "passed" and recs[0]["elapsed_s"] < 0.3


def test_an_unreadable_target_in_the_window_neither_passes_nor_fails_it():
    from livetest.assertions import NotYetReadable
    pg = _TargetSequence(_ROWS, _ROWS, NotYetReadable("locked"), _ROWS)
    recs = assert_diff({"postgres-source": pg},
                       [{"source": "s.src", "target": "s.tgt", "stable_seconds": 0.2}], timeout=5, poll=0.05)
    assert recs[0]["status"] == "passed"


def test_a_diff_window_that_ends_unreadable_fails():
    from livetest.assertions import NotYetReadable
    pg = _TargetSequence(_ROWS, _ROWS, NotYetReadable("locked"))
    with pytest.raises(AssertionFailed, match="could not be read again"):
        assert_diff({"postgres-source": pg},
                    [{"source": "s.src", "target": "s.tgt", "stable_seconds": 0.2}], timeout=0.3, poll=0.05)
