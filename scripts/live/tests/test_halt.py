import pytest
from livetest.assertions import AssertionFailed
from livetest.assertions.halt import assert_halt, accept_expected_halt


class StubClient:
    """current_status returns a fixed value, or walks a scripted sequence (last repeats)."""
    def __init__(self, status="HALT", sequence=None):
        self._status = status
        self._seq = list(sequence) if sequence else None
    def current_status(self, app):
        if self._seq is None:
            return self._status
        return self._seq.pop(0) if len(self._seq) > 1 else self._seq[0]


def test_halt_passes_when_terminal():
    records = assert_halt(StubClient(status="HALT"), "NS.App", timeout=5, poll=0)
    assert len(records) == 1
    assert records[0]["status"] == "passed"
    assert records[0]["type"] == "halt"
    assert "HALT" in records[0]["detail"]

def test_halt_passes_after_transitioning_to_terminal():
    # RUNNING for a couple polls, then HALT -> pass.
    records = assert_halt(StubClient(sequence=["RUNNING", "RUNNING", "HALT"]),
                          "NS.App", timeout=5, poll=0)
    assert records[0]["status"] == "passed"

def test_halt_fails_when_app_stays_running():
    with pytest.raises(AssertionFailed, match="expected a terminal HALT") as exc:
        assert_halt(StubClient(status="RUNNING"), "NS.App", timeout=0, poll=0)
    recs = exc.value.records
    assert len(recs) == 1 and recs[0]["status"] == "failed" and recs[0]["type"] == "halt"
    assert "RUNNING" in recs[0]["detail"]

def test_halt_requires_all_apps_terminal():
    class TwoApp:
        def current_status(self, app):
            return "HALT" if app == "A" else "RUNNING"
    with pytest.raises(AssertionFailed, match="still non-terminal"):
        assert_halt(TwoApp(), ["A", "B"], timeout=0, poll=0)

def test_halt_status_read_error_fails_cleanly():
    class Boom:
        def current_status(self, app):
            raise RuntimeError("cannot read status")
    with pytest.raises(AssertionFailed, match="error reading application status"):
        assert_halt(Boom(), "NS.App", timeout=5, poll=0)


def test_accept_expected_halt_returns_none_when_expect_halt_false():
    assert accept_expected_halt(False, RuntimeError("boom")) is None

def test_accept_expected_halt_returns_passed_record_when_expect_halt_true():
    records = accept_expected_halt(True, RuntimeError("boom"))
    assert len(records) == 1
    assert records[0]["type"] == "halt"
    assert records[0]["status"] == "passed"
    assert "boom" in records[0]["detail"]


# --- expect_halt_contains -----------------------------------------------------------

def test_accept_expected_halt_passes_when_all_substrings_match():
    exc = RuntimeError("cache key conflict on USERS")
    records = accept_expected_halt(True, exc, contains=("cache", "conflict"))
    assert records[0]["status"] == "passed"
    assert "cache key conflict on USERS" in records[0]["detail"]
    assert "expect_halt_contains" in records[0]["detail"]

def test_accept_expected_halt_raises_naming_the_missing_substring():
    exc = RuntimeError("cache key conflict on USERS")
    with pytest.raises(AssertionFailed, match="TOMBSTONE") as excinfo:
        accept_expected_halt(True, exc, contains=("cache", "TOMBSTONE"))
    assert "cache key conflict on USERS" in str(excinfo.value)
    recs = excinfo.value.records
    assert len(recs) == 1 and recs[0]["status"] == "failed" and recs[0]["type"] == "halt"

def test_accept_expected_halt_and_semantics_not_or():
    # One substring matches, one doesn't -- must still raise (AND, not OR).
    exc = RuntimeError("cache key conflict on USERS")
    with pytest.raises(AssertionFailed, match="TOMBSTONE"):
        accept_expected_halt(True, exc, contains=("cache", "TOMBSTONE"))

def test_accept_expected_halt_contains_ignored_when_expect_halt_false():
    assert accept_expected_halt(False, RuntimeError("boom"), contains=("x",)) is None


def test_halt_contains_passes_when_log_tail_matches():
    # The reason sits in a log record that names this case's namespace (NS of NS.App).
    records = assert_halt(StubClient(status="HALT"), "NS.App", timeout=5, poll=0,
                          contains=("conflict",),
                          log_tail=lambda: "2026-11-14 10:00:00,001 -ERROR NS.App halted: conflict on USERS\n")
    assert records[0]["status"] == "passed"
    assert "expect_halt_contains" in records[0]["detail"]

def test_halt_contains_fails_when_log_tail_missing_substring():
    with pytest.raises(AssertionFailed, match="conflict") as excinfo:
        assert_halt(StubClient(status="HALT"), "NS.App", timeout=5, poll=0,
                   contains=("conflict",), log_tail=lambda: "...nothing relevant here...")
    recs = excinfo.value.records
    assert recs[0]["status"] == "failed" and recs[0]["type"] == "halt"

def test_halt_contains_passes_with_native_mode_caveat():
    records = assert_halt(StubClient(status="HALT"), "NS.App", timeout=5, poll=0,
                          mode="native", contains=("conflict",), log_tail=lambda: "")
    assert records[0]["status"] == "passed"
    assert "halt reason not checked (native mode)" in records[0]["detail"]

def test_halt_contains_passes_with_caveat_when_no_log_tail_callable():
    records = assert_halt(StubClient(status="HALT"), "NS.App", timeout=5, poll=0,
                          mode="native", contains=("conflict",), log_tail=None)
    assert records[0]["status"] == "passed"
    assert "halt reason not checked (native mode)" in records[0]["detail"]

def test_halt_does_not_call_log_tail_when_contains_unset():
    calls = []
    def _tail():
        calls.append(1)
        return "anything"
    records = assert_halt(StubClient(status="HALT"), "NS.App", timeout=5, poll=0,
                          contains=(), log_tail=_tail)
    assert records[0]["status"] == "passed"
    assert calls == []

def test_halt_contains_passes_with_caveat_when_log_tail_whitespace_only():
    # A docker exec that fails silently on every app node (bad container name, stopped
    # stack) returns "" per node with no exception -- "\n".join(["", ""]) == "\n", which
    # is truthy but carries no reason text. Must degrade to the caveat, not a hard
    # AssertionFailed naming every substring as "missing" from a log that was never read.
    records = assert_halt(StubClient(status="HALT"), "NS.App", timeout=5, poll=0,
                          mode="native", contains=("conflict",), log_tail=lambda: "\n")
    assert records[0]["status"] == "passed"
    assert "halt reason not checked (native mode)" in records[0]["detail"]

def test_halt_contains_and_semantics_not_or():
    # Two substrings, only one present in the tail -- AND must still fail (not OR).
    with pytest.raises(AssertionFailed, match="USERS") as excinfo:
        assert_halt(StubClient(status="HALT"), "NS.App", timeout=5, poll=0,
                   contains=("conflict", "USERS"), log_tail=lambda: "...conflict...")
    recs = excinfo.value.records
    assert recs[0]["status"] == "failed" and recs[0]["type"] == "halt"

def test_halt_contains_degrades_when_log_tail_raises():
    def _boom():
        raise RuntimeError("docker exec failed")
    records = assert_halt(StubClient(status="HALT"), "NS.App", timeout=5, poll=0,
                          mode="native", contains=("conflict",), log_tail=_boom)
    assert records[0]["status"] == "passed"
    assert "halt reason not checked (native mode)" in records[0]["detail"]
    assert "docker exec failed" in records[0]["detail"]


def test_a_timed_out_import_is_never_the_expected_halt():
    from livetest.assertions.halt import accept_expected_halt
    from livetest.striim import StriimTimeout
    assert accept_expected_halt(True, StriimTimeout("TQL import timed out")) is None


# --- a deploy/startup halt must not silently skip declared work ----------------------

def _startup_case(tmp_path, extra=""):
    import textwrap
    from livetest.manifest import load_manifest
    d = tmp_path / "halt-case"
    d.mkdir()
    for f in ("app.tql", "pre.sql", "dml.sql"):
        (d / f).write_text("-- x\n")
    (d / "test.yaml").write_text(textwrap.dedent("""
        name: halt-case
        tql: app.tql
        seed:
          - {db: postgres-source, file: pre.sql}
          - {db: postgres-source, file: dml.sql, when: post_start}
        expect_halt: true
    """) + textwrap.dedent(extra))
    return load_manifest(d / "test.yaml")

def test_skipped_by_startup_halt_names_post_start_seeds_and_asserts(tmp_path):
    from livetest.assertions.halt import skipped_by_startup_halt
    m = _startup_case(tmp_path, """
        assert:
          smoke: true
    """)
    assert skipped_by_startup_halt(m) == ["seed dml.sql (post_start)", "assert: smoke"]

def test_startup_failure_with_seed_and_asserts_and_no_contains_fails():
    # The app never started, so the post_start seed and the data assert never ran: not a pass.
    exc = RuntimeError("DatabaseJdbcWriter: unsupported CommitPolicy")
    skipped = ("seed dml.sql (post_start)", "assert: data")
    with pytest.raises(AssertionFailed, match="assert: data") as excinfo:
        accept_expected_halt(True, exc, skipped=skipped)
    assert "expect_halt_contains" in str(excinfo.value)
    assert "unsupported CommitPolicy" in str(excinfo.value)
    assert excinfo.value.records[0]["status"] == "failed"

def test_startup_failure_with_matching_contains_passes_and_names_what_was_skipped():
    exc = RuntimeError("JSON column bug_json declared both a root array and an object path")
    skipped = ("seed dml.sql (post_start)", "assert: data")
    records = accept_expected_halt(True, exc, contains=("root array",), skipped=skipped)
    assert records[0]["status"] == "passed"
    assert "skipped (startup refusal)" in records[0]["detail"]
    assert "assert: data" in records[0]["detail"]

def test_startup_failure_with_wrong_contains_still_fails_on_the_contains():
    exc = RuntimeError("DatabaseJdbcWriter: unsupported CommitPolicy")
    with pytest.raises(AssertionFailed, match="poisoned"):
        accept_expected_halt(True, exc, contains=("poisoned",), skipped=("assert: data",))

def test_startup_refusal_only_case_still_passes_without_contains(tmp_path):
    from livetest.assertions.halt import skipped_by_startup_halt
    import textwrap
    from livetest.manifest import load_manifest
    d = tmp_path / "refusal-only"
    d.mkdir()
    (d / "app.tql").write_text("-- x\n")
    (d / "test.yaml").write_text(textwrap.dedent("""
        name: refusal-only
        tql: app.tql
        expect_halt: true
    """))
    m = load_manifest(d / "test.yaml")
    assert skipped_by_startup_halt(m) == []
    records = accept_expected_halt(True, RuntimeError("boom"), skipped=())
    assert records[0]["status"] == "passed"
    assert "skipped" not in records[0]["detail"]

def test_running_phase_halt_is_unchanged_by_the_startup_rule():
    # assert_halt (the polled path, after seeding) takes no `skipped`: a halt after start
    # without expect_halt_contains passes as before.
    records = assert_halt(StubClient("HALT"), "app", timeout=5, poll=0)
    assert records[0]["status"] == "passed"


def test_startup_halt_record_names_every_skipped_transaction_step(tmp_path):
    from livetest.assertions.halt import skipped_by_startup_halt
    from livetest.manifest import TestManifest
    m = TestManifest(name='txn-halt', tql='app.tql', dir=tmp_path,
        lifecycle=object(), transaction_steps=[
            {'session':'setup', 'op':'begin', 'when':'pre_deploy'},
            {'session':'first', 'op':'begin', 'when':'post_start'},
            {'session':'first', 'op':'sql', 'file':'changes.sql', 'when':'post_start'},
            {'session':'first', 'op':'commit', 'when':'post_start'},
            {'session':'held', 'op':'commit', 'when':'post_recover'},
        ])
    skipped = skipped_by_startup_halt(m)
    assert skipped == [
        'transaction first begin (post_start)',
        'transaction first sql changes.sql (post_start)',
        'transaction first commit (post_start)',
        'transaction held commit (post_recover)',
        'lifecycle',
    ]
    record = accept_expected_halt(True, RuntimeError('startup failure'),
        contains=('startup failure',), skipped=tuple(skipped))[0]
    for item in skipped[:-1]:
        assert item in record['detail']
    assert 'transaction setup' not in record['detail']


# --- docker mode: a halt reason that cannot be read is a failure, not an unverified pass ------
# The node log exists in docker mode, so an unreadable or empty slice means the reason was never
# checked. Only native mode, where there is no log to read, keeps the "not checked" pass.

def _unverifiable(log_tail, mode="docker"):
    with pytest.raises(AssertionFailed) as exc:
        assert_halt(StubClient(status="HALT"), "NS.App", timeout=5, poll=0,
                    contains=("conflict",), mode=mode, log_tail=log_tail)
    # No record: an xfail covering `halt` must not absorb a log that could not be read.
    assert exc.value.records == []
    detail = str(exc.value)
    assert "NOT verified" not in detail and "native mode" not in detail
    return detail


def test_an_unverifiable_halt_is_not_absorbed_by_an_xfail_on_halt():
    from livetest.assertions import ExpectedAssertionFailure
    with pytest.raises(AssertionFailed) as exc:
        assert_halt(StubClient(status="HALT"), "NS.App", timeout=5, poll=0,
                    contains=("conflict",), mode="docker", log_tail=lambda: "")
    assert ExpectedAssertionFailure.if_covered(exc.value, ["halt"]) is exc.value


def test_docker_halt_contains_fails_when_the_node_log_cannot_be_read():
    def _boom():
        raise RuntimeError("docker exec failed")
    detail = _unverifiable(_boom)
    assert "node log could not be read" in detail and "docker exec failed" in detail
    assert "'conflict'" in detail and "HALT" in detail


@pytest.mark.parametrize("tail", ["", "\n", None])
def test_docker_halt_contains_fails_when_the_node_log_is_empty(tail):
    detail = _unverifiable(lambda: tail)
    assert "no text since the case deployed" in detail


def test_docker_halt_contains_fails_without_a_log_reader():
    assert "no node-log reader" in _unverifiable(None)


@pytest.mark.parametrize("mode", [None, "remote"])
def test_only_native_mode_keeps_the_unverified_pass(mode):
    _unverifiable(lambda: "", mode=mode)


def test_docker_halt_contains_still_matches_a_readable_log():
    records = assert_halt(StubClient(status="HALT"), "NS.App", timeout=5, poll=0, contains=("conflict",),
                          mode="docker", log_tail=lambda: "...NS.App: conflict on USERS...")
    assert records[0]["status"] == "passed" and "verified" in records[0]["detail"]
    with pytest.raises(AssertionFailed, match="missing"):
        assert_halt(StubClient(status="HALT"), "NS.App", timeout=5, poll=0, contains=("conflict",),
                    mode="docker", log_tail=lambda: "...nothing relevant...")


def test_docker_halt_without_contains_reads_no_log():
    def _tail():
        raise AssertionError("log read without expect_halt_contains")
    records = assert_halt(StubClient(status="HALT"), "NS.App", timeout=5, poll=0, mode="docker", log_tail=_tail)
    assert records[0]["status"] == "passed"


# --- FW-3: the reason must come from a log record that names this case's namespace -------------
# The node-log slice is shared by every app on the cluster (under SLT_PARALLEL, sibling cases
# write to it too), so a substring anywhere in it is not this app's halt reason.

def _rec(ts, text):
    return f"2026-11-14 10:00:{ts:02d},000 {text}\n"


def test_a_reason_on_another_namespaces_line_does_not_satisfy_contains():
    # The review's pin, inverted: ORA-00001 is on SLT_other's line; this app crashed with an NPE.
    tail = (_rec(1, "-ERROR SLT_other.otherApp halted: ORA-00001 unique constraint")
            + _rec(2, "-INFO SLT_mine.mineApp crashed: NullPointerException"))
    with pytest.raises(AssertionFailed) as ei:
        assert_halt(StubClient(status="HALT"), "SLT_mine.mineApp", timeout=1, poll=0,
                    contains=("ORA-00001",), log_tail=lambda: tail)
    msg = str(ei.value)
    assert "ORA-00001" in msg and "SLT_mine" in msg and "do not name" in msg
    assert ei.value.records[0]["status"] == "failed"


def test_a_reason_on_a_continuation_line_of_this_namespaces_record_satisfies_contains():
    # A stack trace or multi-line message continues the record its timestamped line started.
    tail = (_rec(1, "-ERROR SLT_mine.mineApp_Target exception in component:")
            + "com.example.WriterException: ORA-00001 unique constraint violated\n"
            + "\tat com.example.Writer.write(Writer.java:12)\n"
            + _rec(2, "-INFO SLT_other.otherApp running"))
    recs = assert_halt(StubClient(status="HALT"), "SLT_mine.mineApp", timeout=1, poll=0,
                       contains=("ORA-00001",), log_tail=lambda: tail)
    assert recs[0]["status"] == "passed" and "verified" in recs[0]["detail"]


def test_a_continuation_line_belongs_to_the_record_above_it_not_to_the_next():
    tail = (_rec(1, "-ERROR SLT_other.otherApp exception:")
            + "ORA-00001 unique constraint violated\n"
            + _rec(2, "-INFO SLT_mine.mineApp crashed"))
    with pytest.raises(AssertionFailed, match="do not name"):
        assert_halt(StubClient(status="HALT"), "SLT_mine.mineApp", timeout=1, poll=0,
                    contains=("ORA-00001",), log_tail=lambda: tail)


def test_every_substring_may_come_from_a_different_record_of_this_namespace():
    tail = (_rec(1, "-ERROR SLT_mine.mineApp: Cannot insert duplicate key")
            + _rec(2, "-WARN SLT_other.otherApp: unrelated")
            + _rec(3, "-ERROR SLT_mine.mineApp: The row(s) and their source events are in the server log"))
    recs = assert_halt(StubClient(status="HALT"), "SLT_mine.mineApp", timeout=1, poll=0,
                       contains=("Cannot insert duplicate key", "The row(s) and their source events"),
                       log_tail=lambda: tail)
    assert recs[0]["status"] == "passed"


def test_a_namespace_that_merely_starts_with_this_one_is_not_this_one():
    tail = _rec(1, "-ERROR SLT_mine2.app halted: ORA-00001")
    with pytest.raises(AssertionFailed, match="do not name"):
        assert_halt(StubClient(status="HALT"), "SLT_mine.mineApp", timeout=1, poll=0,
                    contains=("ORA-00001",), log_tail=lambda: tail)


def test_a_substring_absent_from_the_whole_slice_says_so():
    with pytest.raises(AssertionFailed, match="missing") as ei:
        assert_halt(StubClient(status="HALT"), "SLT_mine.mineApp", timeout=1, poll=0,
                    contains=("ORA-00001",), log_tail=lambda: _rec(1, "-INFO SLT_mine.mineApp halted"))
    assert "do not name" not in str(ei.value)


def test_any_of_the_cases_apps_names_a_namespace_it_owns():
    tail = _rec(1, "-ERROR SLT_b.second halted: ORA-00001")
    recs = assert_halt(StubClient(status="HALT"), ["SLT_a.first", "SLT_b.second"], timeout=1, poll=0,
                       contains=("ORA-00001",), log_tail=lambda: tail)
    assert recs[0]["status"] == "passed"


def test_the_record_start_is_one_documented_regex():
    from livetest.assertions import halt
    assert halt.LOG_RECORD_START.pattern == r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}"
    text = "lead-in\n" + _rec(1, "a") + "cont\n" + _rec(2, "b")
    assert halt.log_records(text) == ["lead-in\n", _rec(1, "a") + "cont\n", _rec(2, "b")]
