from pathlib import Path

import pytest

from livetest.assertions import AssertionFailed
from livetest.assertions.data import load_golden, distinct_set, parse_data_specs, assert_data, DataSpecError


def test_load_golden_reads_csv(tmp_path):
    p = tmp_path / "rows.csv"
    p.write_text("id,msg\n1,hello\n2,world\n")
    assert load_golden(p) == [{"id": "1", "msg": "hello"}, {"id": "2", "msg": "world"}]


def test_load_golden_renders_tokens(tmp_path):
    p = tmp_path / "rows.csv"
    p.write_text("id,msg\n${TID},hello\n")
    assert load_golden(p, tokens={"TID": "t1"}) == [{"id": "t1", "msg": "hello"}]


def test_load_golden_without_tokens_is_unchanged(tmp_path):
    p = tmp_path / "rows.csv"
    p.write_text("id,msg\n1,hello\n")
    assert load_golden(p) == [{"id": "1", "msg": "hello"}]   # tokens defaults to None


def test_distinct_set_ignores_order_and_dupes():
    a = [{"id": "1", "msg": "hello"}, {"id": "1", "msg": "hello"}]
    b = [{"msg": "hello", "id": "1"}]
    assert distinct_set(a) == distinct_set(b)


def test_distinct_set_normalizes_bool_to_json_lowercase():
    # A parsed JSON event carries real Python bools (True/False); a golden CSV is read by
    # csv.DictReader as plain strings, and authors naturally write JSON's own lowercase
    # true/false. str(True) == "True" would never match "true" without normalization.
    parsed_event_row = [{"flag": True}, {"flag": False}]
    golden_csv_row = [{"flag": "true"}, {"flag": "false"}]
    assert distinct_set(parsed_event_row) == distinct_set(golden_csv_row)


class FakePg:
    def __init__(self, count, rows): self._count = count; self._rows = rows
    def count_rows(self, t): return self._count
    def select_rows(self, t): return self._rows


class FakeKafkaRecords:
    def __init__(self, records): self.records = records
    def select_records(self, topic): return self.records


def _keyed_spec():
    return {"target": "slt_tgt", "db": "kafka", "project": "kafka_record", "rows": 2,
            "keys": ["key.ID", "value.data.ID"], "match": "keyed.csv"}


def test_kafka_record_assertion_compares_keys_with_their_own_values(tmp_path):
    (tmp_path / "keyed.csv").write_text("key.ID,value.data.ID\n1,1\n2,2\n")
    records = [{"key": {"ID": "1"}, "value": {"data": {"ID": 1}}},
               {"key": {"ID": "2"}, "value": {"data": {"ID": 2}}}]
    assert_data(FakeKafkaRecords(records), parse_data_specs([_keyed_spec()]), tmp_path, timeout=0)
    # The independent key and value sets are unchanged: only their association is wrong.
    records[0]["key"], records[1]["key"] = records[1]["key"], records[0]["key"]
    with pytest.raises(AssertionFailed, match="distinct row set"):
        assert_data(FakeKafkaRecords(records), [_keyed_spec()], tmp_path, timeout=0)


def test_kafka_record_count_catches_duplicate_deliveries(tmp_path):
    (tmp_path / "keyed.csv").write_text("key.ID,value.data.ID\n1,1\n2,2\n")
    records = [{"key": {"ID": "1"}, "value": {"data": {"ID": 1}}},
               {"key": {"ID": "2"}, "value": {"data": {"ID": 2}}}]
    with pytest.raises(AssertionFailed, match="rows=3, want exactly 2"):
        assert_data(FakeKafkaRecords(records + records[:1]), [_keyed_spec()], tmp_path, timeout=0)


def test_kafka_record_order_is_opt_in(tmp_path):
    (tmp_path / "keyed.csv").write_text("key.ID,value.data.ID\n1,1\n2,2\n")
    records = [{"key": {"ID": "2"}, "value": {"data": {"ID": 2}}},
               {"key": {"ID": "1"}, "value": {"data": {"ID": 1}}}]
    assert_data(FakeKafkaRecords(records), [_keyed_spec()], tmp_path, timeout=0)
    ordered = {**_keyed_spec(), "ordered": True}
    parse_data_specs([ordered])
    with pytest.raises(AssertionFailed, match="ordered records differ"):
        assert_data(FakeKafkaRecords(records), [ordered], tmp_path, timeout=0)
    assert_data(FakeKafkaRecords(records[::-1]), [ordered], tmp_path, timeout=0)


def test_kafka_record_ddl_key_string_null_is_not_json_null(tmp_path):
    (tmp_path / "keyed.csv").write_text("key.ID,value.data.ID\nnull,<null>\n1,1\n")
    records = [{"key": {"ID": "null"}, "value": {"data": {"DDLCommand": "CREATE"}}},
               {"key": {"ID": "1"}, "value": {"data": {"ID": 1}}}]
    assert_data(FakeKafkaRecords(records), [_keyed_spec()], tmp_path, timeout=0)
    records[0]["key"] = {"ID": None}
    with pytest.raises(AssertionFailed, match="distinct row set"):
        assert_data(FakeKafkaRecords(records), [_keyed_spec()], tmp_path, timeout=0)


@pytest.mark.parametrize("changes", [
    {"project": "data"}, {"db": "mssql-source"}, {"target_db": "postgres-target"},
    {"keys": []}, {"keys": "key.ID"}, {"keys": ["data.ID"]}, {"keys": ["value..ID"]},
    {"keys": [None]}, {"ordered": "true"}])
def test_kafka_record_projection_rejects_invalid_specs(changes):
    with pytest.raises(DataSpecError, match="project|Kafka|ordered"):
        parse_data_specs([{**_keyed_spec(), **changes}])


def test_kafka_record_projection_rejects_non_kafka_admin(tmp_path):
    (tmp_path / "keyed.csv").write_text("key.ID,value.data.ID\n1,1\n")
    with pytest.raises(DataSpecError, match="Kafka target"):
        assert_data(FakePg(1, []), [_keyed_spec()], tmp_path, timeout=0)


def test_assert_data_progress_heartbeat_fires_and_is_optional():
    """The optional `progress` callback ticks each poll while the assertion waits (the console's
    'still running' heartbeat), and omitting it must not break the poll loop."""
    specs = parse_data_specs([{"target": "s.t", "min_rows": 5}])
    ticks = []
    with pytest.raises(AssertionFailed):   # count stays 0 < 5 → polls until timeout
        assert_data(FakePg(count=0, rows=[]), specs, Path("."), timeout=0.15, poll=0.02,
                    progress=lambda elapsed, total, *a: ticks.append((elapsed, total)))
    assert ticks, "progress heartbeat should fire at least once during polling"
    assert ticks[-1][1] == pytest.approx(0.15)   # timeout is passed through as the total
    # omitting progress must still work (no crash)
    with pytest.raises(AssertionFailed):
        assert_data(FakePg(count=0, rows=[]),
                    parse_data_specs([{"target": "s.t", "min_rows": 5}]),
                    Path("."), timeout=0.05, poll=0.02)


def test_parse_requires_target_and_a_check():
    with pytest.raises(DataSpecError):
        parse_data_specs([{"min_rows": 1}])           # no target
    with pytest.raises(DataSpecError):
        parse_data_specs([{"target": "s.t"}])          # no check
    assert parse_data_specs([{"target": "s.t", "min_rows": 1}])


def test_parse_rejects_vacuous_min_rows():
    with pytest.raises(DataSpecError, match="min_rows"):
        parse_data_specs([{"target": "s.t", "min_rows": 0}])
    with pytest.raises(DataSpecError, match="min_rows"):
        parse_data_specs([{"target": "s.t", "min_rows": -1}])


def test_parse_rejects_negative_rows():
    with pytest.raises(DataSpecError, match="rows"):
        parse_data_specs([{"target": "s.t", "rows": -1}])
    assert parse_data_specs([{"target": "s.t", "rows": 0}])   # exactly-zero rows is legitimate


def test_parse_rejects_non_numeric_thresholds():
    with pytest.raises(DataSpecError):
        parse_data_specs([{"target": "s.t", "min_rows": "lots"}])
    with pytest.raises(DataSpecError):
        parse_data_specs([{"target": "s.t", "rows": "many"}])


def test_assert_data_passes_on_min_rows(tmp_path):
    assert_data(FakePg(3, []), [{"target": "s.t", "min_rows": 1}], tmp_path, timeout=1, poll=0)


def test_assert_data_times_out_when_count_short(tmp_path):
    with pytest.raises(AssertionError, match="min_rows"):
        assert_data(FakePg(0, []), [{"target": "s.t", "min_rows": 1}], tmp_path, timeout=0, poll=0)


def test_assert_data_match_golden(tmp_path):
    (tmp_path / "g.csv").write_text("id,msg\n1,hello\n")
    pg = FakePg(2, [{"id": "1", "msg": "hello"}, {"id": "1", "msg": "hello"}])  # dupes ok
    assert_data(pg, [{"target": "s.t", "min_rows": 1, "match": "g.csv"}], tmp_path, timeout=1, poll=0)


def test_assert_data_rejects_empty_golden(tmp_path):
    (tmp_path / "g.csv").write_text("id,msg\n")  # header-only, no rows
    with pytest.raises(DataSpecError, match="empty"):
        assert_data(FakePg(0, []), [{"target": "s.t", "match": "g.csv"}], tmp_path, timeout=0)


def test_assert_data_rejects_missing_golden(tmp_path):
    with pytest.raises(DataSpecError, match="not found"):
        assert_data(FakePg(0, []), [{"target": "s.t", "match": "missing.csv"}], tmp_path, timeout=0)


def test_assert_data_status_probe_fails_fast(tmp_path):
    # status_probe is checked at the TOP of each poll iteration -- it must fire before
    # the (never-satisfied) match check, so a terminal app status fails fast instead of
    # polling out a (here, deliberately huge) timeout.
    (tmp_path / "g.csv").write_text("id,msg\n1,hello\n")
    pg = FakePg(0, [])  # rows never match the golden

    def probe():
        raise AssertionError("terminal")

    with pytest.raises(AssertionError, match="terminal"):
        assert_data(pg, [{"target": "s.t", "match": "g.csv"}], tmp_path,
                    timeout=999999, poll=0, status_probe=probe)


# ---- structured per-assertion records -----------------------------------------------

def test_assert_data_success_returns_passed_records():
    spec = {"target": "s.t", "min_rows": 1}
    records = assert_data(FakePg(3, []), [spec], Path("."), timeout=1, poll=0, db="postgres")
    assert len(records) == 1
    rec = records[0]
    assert rec["status"] == "passed"
    assert rec["type"] == "data"
    assert rec["target"] == "s.t"
    assert rec["db"] == "postgres"
    assert rec["spec"] == spec
    assert rec["expected"] == {"kind": "count", "count": 1, "truncated": False}
    assert rec["actual"] == {"kind": "count", "count": 3, "truncated": False}


def test_assert_data_match_success_returns_rows_snapshot(tmp_path):
    (tmp_path / "g.csv").write_text("id,msg\n1,hello\n")
    pg = FakePg(1, [{"id": "1", "msg": "hello"}])
    spec = {"target": "s.t", "match": "g.csv"}
    records = assert_data(pg, [spec], tmp_path, timeout=1, poll=0)
    assert len(records) == 1
    assert records[0]["status"] == "passed"
    assert records[0]["expected"]["kind"] == "rows"
    assert records[0]["expected"]["rows"] == [{"id": "1", "msg": "hello"}]
    assert records[0]["actual"]["kind"] == "rows"
    assert records[0]["actual"]["rows"] == [{"id": "1", "msg": "hello"}]
    assert records[0]["db"] is None


def test_assert_data_failure_raises_assertion_failed_with_records(tmp_path):
    spec = {"target": "s.t", "min_rows": 5}
    with pytest.raises(AssertionFailed) as exc_info:
        assert_data(FakePg(2, []), [spec], tmp_path, timeout=0, poll=0, db="postgres")
    assert str(exc_info.value) == (
        "data assertion not satisfied within 0s:\n"
        "s.t: rows=2, want min_rows>=5\n"
        "\n"
        "  EXPECTED: 5 rows\n"
        "  ACTUAL: 2 rows"
    )
    records = exc_info.value.records
    assert len(records) == 1
    assert records[0]["status"] == "failed"
    assert records[0]["expected"] == {"kind": "count", "count": 5, "truncated": False}
    assert records[0]["actual"] == {"kind": "count", "count": 2, "truncated": False}
    assert records[0]["db"] == "postgres"


def test_assert_data_mixed_specs_report_both_statuses(tmp_path):
    ok_spec = {"target": "s.ok", "min_rows": 1}
    bad_spec = {"target": "s.bad", "min_rows": 5}

    class MultiPg:
        def count_rows(self, t):
            return 3 if t == "s.ok" else 1
        def select_rows(self, t):
            return []

    with pytest.raises(AssertionFailed) as exc_info:
        assert_data(MultiPg(), [ok_spec, bad_spec], tmp_path, timeout=0, poll=0)
    records = exc_info.value.records
    assert len(records) == 2
    by_target = {r["target"]: r for r in records}
    assert by_target["s.ok"]["status"] == "passed"
    assert by_target["s.bad"]["status"] == "failed"


def test_absent_marker_is_REFUSED_in_a_data_golden():
    # <absent> is a `file`-assertion feature: assert.data compares against SQL rows, where a
    # SELECT always returns every column it names. Left unchecked, such a golden fails as a
    # value mismatch AFTER the full assertion timeout, with a message that never names the
    # cause. It must be refused at load, naming the column and the file.
    import pathlib, tempfile, pytest as _pytest
    from livetest.assertions.data import load_golden, DataSpecError, ABSENT
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "golden.csv"
        p.write_text("ID,NAME\n1,%s\n" % ABSENT)
        with _pytest.raises(DataSpecError) as e:
            load_golden(p)
        msg = str(e.value)
        assert "NAME" in msg, "the message must name the offending column"
        assert "assert.file" in msg or "`file`" in msg, "and point at where the marker works"


def test_a_data_golden_without_the_marker_still_loads():
    # Negative control for the check above: it must refuse ONLY the marker.
    import pathlib, tempfile
    from livetest.assertions.data import load_golden
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "golden.csv"
        p.write_text("ID,NAME\n1,Widget\n2,\n")
        assert load_golden(p) == [{"ID": "1", "NAME": "Widget"}, {"ID": "2", "NAME": ""}]


# --- a failed "must be empty" assertion must show the rows -------------------------------
#
# A violation view (gviolations, gorphan_late) says WHICH row broke the rule and by how much.
# Reporting only "rows=2" throws that away, and the rows are gone by the next run, which drops
# the schema. That cost a full re-run of an hour-long fault arm to establish once.

def _empty_spec():
    return {"target": "slt_tgt", "db": "postgres-target", "rows": 0}


def test_a_failed_rows_zero_assertion_reports_the_offending_rows(tmp_path):
    rows = [{"kind": "line", "id": 1, "seconds_late": 138},
            {"kind": "pay", "id": 3001, "seconds_late": 141}]
    with pytest.raises(AssertionFailed) as e:
        assert_data(FakePg(2, rows), [_empty_spec()], tmp_path, timeout=0)
    msg = str(e.value)
    assert "rows=2, want exactly 0" in msg
    assert "the rows that should not exist" in msg
    # The VALUES are the point: 138 vs a marginal number is the whole diagnosis.
    assert "seconds_late=138" in msg and "kind='line'" in msg
    assert "id=3001" in msg


def test_a_nonzero_rows_mismatch_does_not_dump_rows(tmp_path):
    # Only the "should be empty" case gets this treatment. A count mismatch on a data table
    # would dump the table into the failure message, which buries the summary it is attached to.
    rows = [{"id": i} for i in range(5)]
    with pytest.raises(AssertionFailed) as e:
        assert_data(FakePg(5, rows), [{"target": "t", "db": "postgres-target", "rows": 4}],
                    tmp_path, timeout=0)
    assert "the rows that should not exist" not in str(e.value)


def test_the_row_dump_is_capped(tmp_path):
    rows = [{"id": i} for i in range(25)]
    with pytest.raises(AssertionFailed) as e:
        assert_data(FakePg(25, rows), [_empty_spec()], tmp_path, timeout=0)
    msg = str(e.value)
    assert "... and 15 more" in msg
    # The TRUNCATION, not just the tail line. Asserting only the tail let a mutation that kept
    # the tail and dropped the slice survive -- i.e. the cap's whole purpose (a wide view must
    # not bury the summary it is attached to) was unverified.
    assert msg.count("id=") == 10, f"expected 10 sampled rows, got {msg.count('id=')}"


def test_a_kafka_target_is_not_sampled(tmp_path):
    # Excluded deliberately: project: kafka_record would re-consume the topic, and a plain kafka
    # target's reader carries its own multi-second poll budget. A diagnostic must not cost that.
    # A plain kafka target counts through count_rows like any other, so it needs a client that
    # has one; only the db name marks it as kafka. The projected form uses the records client.
    plain = ({**_empty_spec(), "db": "kafka"}, FakePg(2, [{"id": 1}, {"id": 2}]))
    projected = ({"target": "t", "db": "kafka", "project": "kafka_record", "rows": 0,
                  "keys": ["key.ID"]},
                 FakeKafkaRecords([{"key": {"ID": "1"}, "value": {}}]))
    for spec, client in (plain, projected):
        with pytest.raises(AssertionFailed) as e:
            assert_data(client, [spec], tmp_path, timeout=0)
        assert "the rows that should not exist" not in str(e.value), spec


def test_a_nonzero_count_with_no_rows_says_so(tmp_path):
    # Counted non-zero, selected nothing. Worth reporting rather than printing an empty list.
    with pytest.raises(AssertionFailed) as e:
        assert_data(FakePg(2, []), [_empty_spec()], tmp_path, timeout=0)
    assert "count was non-zero but the select returned nothing" in str(e.value)


def test_a_non_dict_row_still_renders(tmp_path):
    # select_rows returns dicts today; a tuple-returning driver must not turn a failed assertion
    # into a TypeError inside its own diagnostic.
    with pytest.raises(AssertionFailed) as e:
        assert_data(FakePg(1, [(1, "a")]), [_empty_spec()], tmp_path, timeout=0)
    assert "(1, 'a')" in str(e.value)


def test_a_diagnostic_that_cannot_read_does_not_replace_the_real_failure(tmp_path):
    # This path is already failing. A raising diagnostic would hide the assertion it explains.
    class Unreadable(FakePg):
        def select_rows(self, t): raise RuntimeError("connection went away")

    with pytest.raises(AssertionFailed) as e:
        assert_data(Unreadable(2, []), [_empty_spec()], tmp_path, timeout=0)
    msg = str(e.value)
    assert "rows=2, want exactly 0" in msg
    assert "could not be read: connection went away" in msg



# ---- min_rows reads committed rows; short golden rows are refused --------------------------

class DirtyPg(FakePg):
    """mssql/teradata shape: count_rows sees executed rows, count_rows_committed committed ones."""
    def __init__(self, dirty, committed): super().__init__(dirty, []); self.committed = committed
    def count_rows_committed(self, t): return self.committed


def test_min_rows_is_not_satisfied_by_uncommitted_rows(tmp_path):
    with pytest.raises(AssertionFailed, match="not yet committed"):
        assert_data(DirtyPg(5, 0), [{"target": "dbo.x", "min_rows": 5}], tmp_path, timeout=0)


def test_min_rows_passes_once_enough_rows_are_committed(tmp_path):
    records = assert_data(DirtyPg(6, 5), [{"target": "dbo.x", "min_rows": 5}], tmp_path, timeout=0)
    assert records[0]["status"] == "passed"


def test_a_long_golden_row_is_refused(tmp_path):
    p = tmp_path / "rows.csv"
    p.write_text("id,name\n1,a,b\n")
    with pytest.raises(DataSpecError, match="data row 1 has more cells than the header"):
        load_golden(p)


def test_a_short_golden_rows_missing_trailing_cells_are_null(tmp_path):
    # Field goldens rely on this (spanner-google-allow-commit-timestamp: "DO NOT FIX THE SHORT ROW").
    p = tmp_path / "rows.csv"
    p.write_text("id,name,ts\n1,a\n")
    assert load_golden(p) == [{"id": "1", "name": "a", "ts": None}]


def test_a_count_that_is_not_yet_readable_keeps_polling_and_fails_at_the_deadline(tmp_path):
    from livetest.assertions import NotYetReadable

    class Unready(FakePg):
        def count_rows(self, t): raise NotYetReadable("topic t does not exist")

    with pytest.raises(AssertionFailed, match="not readable yet: topic t does not exist"):
        assert_data(Unready(0, []), [{"target": "t", "db": "kafka", "rows": 0}], tmp_path, timeout=0)


class LockedPg(DirtyPg):
    """A streaming writer that holds its lock on every committed read."""
    def count_rows_committed(self, t):
        from livetest.assertions import NotYetReadable
        raise NotYetReadable("t1 is locked by a writer")


def test_min_rows_against_a_writer_that_never_releases_its_lock_fails_at_the_deadline(tmp_path):
    # The dirty count of 9 satisfies min_rows: 5, but rows that are later rolled back were counted
    # in it. Reaching the deadline is no evidence that they committed.
    with pytest.raises(AssertionFailed) as ei:
        assert_data(LockedPg(9, 0), [{"target": "t1", "min_rows": 5}], tmp_path, timeout=0)
    assert "committed count never readable" in str(ei.value)
    assert "dirty count 9" in str(ei.value)
    rec = ei.value.records[0]
    assert rec["status"] == "failed" and "committed count never readable" in rec["detail"]


def test_min_rows_against_a_lock_that_is_released_before_the_deadline_passes(tmp_path):
    class ReleasesLater(DirtyPg):
        calls = 0

        def count_rows_committed(self, t):
            from livetest.assertions import NotYetReadable
            self.calls += 1
            if self.calls < 3:
                raise NotYetReadable("t1 is locked by a writer")
            return 9
    records = assert_data(ReleasesLater(9, 0), [{"target": "t1", "min_rows": 5}], tmp_path,
                          timeout=5, poll=0.01)
    assert records[0]["status"] == "passed" and records[0]["detail"] == "ok"


def test_exact_rows_never_falls_back_to_the_dirty_count(tmp_path):
    with pytest.raises(AssertionFailed, match="committed count not readable"):
        assert_data(LockedPg(5, 0), [{"target": "t1", "rows": 5}], tmp_path, timeout=0)


def test_a_committed_read_error_is_named_not_reported_as_uncommitted(tmp_path):
    class Denied(DirtyPg):
        def count_rows_committed(self, t): raise PermissionError("SELECT permission denied on t1")
    with pytest.raises(AssertionFailed, match="committed count could not be read: PermissionError"):
        assert_data(Denied(9, 0), [{"target": "t1", "min_rows": 5}], tmp_path, timeout=0)


# ---- stable_seconds: hold a passing spec for N seconds ------------------------------------------

class SequencePg:
    """Each poll reads the next state; the last one repeats. A state is a list of rows, or an
    exception instance to raise from both reads."""
    def __init__(self, *states):
        self.states, self.reads = list(states), 0

    def _next(self):
        state = self.states[min(self.reads, len(self.states) - 1)]
        self.reads += 1
        if isinstance(state, Exception):
            raise state
        return state

    def count_rows(self, t):
        return len(self._next())

    def select_rows(self, t):
        # assert_data counts before it selects, so this is the state the count just read.
        return list(self.states[min(self.reads - 1, len(self.states) - 1)])


def _golden(tmp_path, text="id\n1\n"):
    (tmp_path / "g.csv").write_text(text)


def test_parse_accepts_stable_seconds():
    assert parse_data_specs([{"target": "t", "rows": 1, "stable_seconds": 0}])
    assert parse_data_specs([{"target": "t", "rows": 1, "stable_seconds": 2.5}])


@pytest.mark.parametrize("bad", [-1, "5", True, None, [5]])
def test_parse_refuses_a_bad_stable_seconds(bad):
    with pytest.raises(DataSpecError, match="stable_seconds"):
        parse_data_specs([{"target": "t", "rows": 1, "stable_seconds": bad}])


def test_without_stable_seconds_a_late_row_is_never_read(tmp_path):
    # The default is unchanged: the first satisfying poll passes.
    _golden(tmp_path)
    pg = SequencePg([{"id": 1}], [{"id": 1}, {"id": 999}])
    records = assert_data(pg, [{"target": "t", "match": "g.csv"}], tmp_path, timeout=5, poll=0.01)
    assert records[0]["status"] == "passed"


def test_stable_seconds_fails_a_match_when_a_late_wrong_row_lands(tmp_path):
    _golden(tmp_path)
    pg = SequencePg([{"id": 1}], [{"id": 1}, {"id": 999}])
    with pytest.raises(AssertionFailed) as ei:
        assert_data(pg, [{"target": "t", "match": "g.csv", "stable_seconds": 5}], tmp_path,
                    timeout=5, poll=0.01)
    assert "stable_seconds" in str(ei.value) and "999" in str(ei.value)
    assert ei.value.records[0]["status"] == "failed"


def test_stable_seconds_fails_rows_zero_when_a_row_arrives_in_the_window(tmp_path):
    pg = SequencePg([], [], [{"id": 1}])
    with pytest.raises(AssertionFailed, match="stable_seconds"):
        assert_data(pg, [{"target": "t", "rows": 0, "stable_seconds": 5}], tmp_path, timeout=5, poll=0.01)


def test_stable_seconds_passes_an_unchanged_target_after_reading_it_through_the_window(tmp_path):
    import time
    pg = SequencePg([{"id": 1}])
    t0 = time.monotonic()
    records = assert_data(pg, [{"target": "t", "rows": 1, "stable_seconds": 0.3}], tmp_path,
                          timeout=5, poll=0.05)
    assert time.monotonic() - t0 >= 0.3
    assert pg.reads > 2
    assert records[0]["status"] == "passed"
    # elapsed_s is when the count was first reached, not when the window closed.
    assert records[0]["elapsed_s"] < 0.3


def test_stable_seconds_is_the_longest_of_the_group(tmp_path):
    import time
    t0 = time.monotonic()
    assert_data(SequencePg([{"id": 1}]), [{"target": "t", "rows": 1, "stable_seconds": 0.3},
                                          {"target": "t", "min_rows": 1}], tmp_path, timeout=5, poll=0.05)
    assert time.monotonic() - t0 >= 0.3


def test_stable_seconds_runs_past_the_timeout_when_it_must(tmp_path):
    # As in assert.file: the window starts at the first pass and is held in full.
    import time
    t0 = time.monotonic()
    assert_data(SequencePg([{"id": 1}]), [{"target": "t", "rows": 1, "stable_seconds": 0.3}], tmp_path,
                timeout=0, poll=0.05)
    assert time.monotonic() - t0 >= 0.3


def test_an_unreadable_poll_in_the_window_neither_passes_nor_fails_it(tmp_path):
    from livetest.assertions import NotYetReadable
    pg = SequencePg([{"id": 1}], NotYetReadable("locked"), [{"id": 1}])
    records = assert_data(pg, [{"target": "t", "rows": 1, "stable_seconds": 0.2}], tmp_path,
                          timeout=5, poll=0.05)
    assert records[0]["status"] == "passed"


def test_a_window_that_ends_unreadable_fails(tmp_path):
    from livetest.assertions import NotYetReadable
    pg = SequencePg([{"id": 1}], NotYetReadable("locked"))
    with pytest.raises(AssertionFailed, match="could not be read again"):
        assert_data(pg, [{"target": "t", "rows": 1, "stable_seconds": 0.2}], tmp_path, timeout=0.3, poll=0.05)


def test_a_committed_count_locked_in_the_window_is_not_a_change(tmp_path):
    class LockedLater(DirtyPg):
        calls = 0

        def count_rows_committed(self, t):
            from livetest.assertions import NotYetReadable
            self.calls += 1
            if self.calls == 2:
                raise NotYetReadable("locked")
            return 5
    records = assert_data(LockedLater(5, 0), [{"target": "t", "rows": 5, "stable_seconds": 0.2}], tmp_path,
                          timeout=5, poll=0.05)
    assert records[0]["status"] == "passed"
