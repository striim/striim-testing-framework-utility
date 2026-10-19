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
    records = assert_halt(StubClient(status="HALT"), "NS.App", timeout=5, poll=0,
                          contains=("conflict",), log_tail=lambda: "...conflict on USERS...")
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
                          contains=("conflict",), log_tail=lambda: "")
    assert records[0]["status"] == "passed"
    assert "halt reason not checked (native mode)" in records[0]["detail"]

def test_halt_contains_passes_with_caveat_when_no_log_tail_callable():
    records = assert_halt(StubClient(status="HALT"), "NS.App", timeout=5, poll=0,
                          contains=("conflict",), log_tail=None)
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
                          contains=("conflict",), log_tail=lambda: "\n")
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
                          contains=("conflict",), log_tail=_boom)
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
