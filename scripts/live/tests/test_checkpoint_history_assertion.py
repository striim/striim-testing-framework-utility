import pytest
from livetest.assertions import AssertionFailed
from livetest.assertions.checkpoint_history import (
    CheckpointHistorySpecError, assert_checkpoint_history, parse_checkpoint_history_spec,
)
from livetest.resultschema import validate, SCHEMA_VERSION


class FakeStriim:
    """`checkpoint_history(app)`: one list per poll, consumed in order (the last one repeats
    once exhausted) so a test can script the platform recording its first checkpoint late."""

    def __init__(self, histories):
        self.histories = list(histories)
        self.polls = 0

    def checkpoint_history(self, app):
        self.polls += 1
        return self.histories.pop(0) if len(self.histories) > 1 else self.histories[0]


# ---- spec parsing --------------------------------------------------------------------

def test_parse_ok():
    assert parse_checkpoint_history_spec("nonempty") == "nonempty"
    assert parse_checkpoint_history_spec("empty") == "empty"


def test_parse_refuses_anything_else():
    for bad in ("Nonempty", "", None, True, ["nonempty"], {"expect": "nonempty"}):
        with pytest.raises(CheckpointHistorySpecError, match="must be one of"):
            parse_checkpoint_history_spec(bad)


# ---- the assertion ---------------------------------------------------------------------

def test_nonempty_polls_until_the_platform_records_the_first_checkpoint():
    fake = FakeStriim([[], [], [{"serialNo": 1, "applicationName": "NS.App"}]])
    records = assert_checkpoint_history(fake, "NS.App", "nonempty", timeout=10, poll=0)
    assert fake.polls == 3
    assert len(records) == 1 and records[0]["status"] == "passed"
    assert records[0]["type"] == "checkpoint_history" and records[0]["target"] == "NS.App"
    validate({"schema_version": SCHEMA_VERSION, "tests": [{
        "name": "t", "nodeid": "t", "status": "passed", "topology": "single", "services": [],
        "duration": 0.0, "skip_reason": None, "assertions": records}]})


def test_nonempty_fails_at_the_deadline_when_nothing_was_ever_recorded():
    fake = FakeStriim([[]])
    with pytest.raises(AssertionFailed) as e:
        assert_checkpoint_history(fake, "NS.App", "nonempty", timeout=0, poll=0)
    assert "shows empty within 0s, expected nonempty" in str(e.value)
    assert e.value.records[0]["status"] == "failed"


def test_empty_passes_when_the_platform_has_recorded_nothing():
    fake = FakeStriim([[]])
    records = assert_checkpoint_history(fake, "NS.App", "empty", timeout=0, poll=0)
    assert records[0]["status"] == "passed"


def test_empty_fails_once_a_checkpoint_shows_up():
    fake = FakeStriim([[{"serialNo": 1}]])
    with pytest.raises(AssertionFailed) as e:
        assert_checkpoint_history(fake, "NS.App", "empty", timeout=0, poll=0)
    assert "shows nonempty within 0s, expected empty" in str(e.value)


def test_status_probe_runs_every_poll():
    calls = []
    fake = FakeStriim([[{"serialNo": 1}]])
    assert_checkpoint_history(fake, "NS.App", "nonempty", timeout=10, poll=0,
                              status_probe=lambda: calls.append(1))
    assert calls == [1]


# ---- `predates`: the last checkpoint before the interruption predates a seed (STATUS 33) ----

from livetest.assertions.checkpoint_history import (  # noqa: E402
    CheckpointWitness, assert_checkpoint_predates, check_predates_manifest,
)


def _cp(at, kind="PERIODIC", src="binlog:161456"):
    return {"serialNo": 1, "applicationName": "NS.App", "checkpointType": kind,
            "sourcePositionSummary": src, "targetPositionSummary": None,
            "checkpointRecordedTime": at}


A = _cp("2026-10-19 10:00:50.123")          # after seed A, before seed B
B = _cp("2026-10-19 10:01:50.456", src="binlog:179507")   # between seed B and the kill
R = _cp("2026-10-19 10:03:10.000", src="binlog:190000")   # the restored app's first


class _Platform:
    """checkpoint_history(app) answers from what has been recorded so far."""

    def __init__(self):
        self.recorded = []

    def checkpoint_history(self, app):
        if isinstance(self.recorded, Exception):
            raise self.recorded
        # Newest first and renumbered on every read, as SHOW ... CHECKPOINT HISTORY does.
        return [{**r, "serialNo": i + 1} for i, r in enumerate(reversed(self.recorded))]


def _run(seed, interrupt, restored):
    """Witness a case where `seed`, `interrupt` and `restored` are what the platform has
    recorded at each of the three reads."""
    p = _Platform()
    w = CheckpointWitness(p, "NS.App", now=lambda: "t")
    for phase, recorded in (("seed", seed), ("interrupt", interrupt), ("restored", restored)):
        p.recorded = recorded
        w.capture(phase)
    return w


SPEC = {"predates": "seed_b.sql"}


def test_nonempty_cannot_see_a_checkpoint_between_the_seed_and_the_kill():
    # The gap STATUS 33 records: B's checkpoint makes the restart replay nothing, and the
    # existing tier still passes.
    fake = FakeStriim([[A, B]])
    assert assert_checkpoint_history(fake, "NS.App", "nonempty", timeout=0, poll=0)[0]["status"] == "passed"


def test_predates_fails_when_a_checkpoint_lands_between_the_seed_and_the_kill():
    w = _run([A], [A, B], [A, B])
    with pytest.raises(AssertionFailed) as e:
        assert_checkpoint_predates(w, SPEC)
    assert "after seed seed_b.sql" in str(e.value) and "10:01:50.456" in str(e.value)
    assert e.value.records[0]["status"] == "failed"


def test_predates_passes_when_the_last_checkpoint_before_the_kill_is_older_than_the_seed():
    w = _run([A], [A], [A])
    records = assert_checkpoint_predates(w, SPEC)
    assert records[0]["status"] == "passed" and records[0]["type"] == "checkpoint_history"
    validate({"schema_version": SCHEMA_VERSION, "tests": [{
        "name": "t", "nodeid": "t", "status": "passed", "topology": "single", "services": [],
        "duration": 0.0, "skip_reason": None, "assertions": records}]})


def test_predates_catches_a_checkpoint_between_the_last_read_and_the_kill():
    # The interruption read is a moment before the command; the RUNNING read closes the gap.
    w = _run([A], [A], [A, B])
    with pytest.raises(AssertionFailed, match="10:01:50.456"):
        assert_checkpoint_predates(w, SPEC)


def test_predates_names_each_new_checkpoint_once():
    # B is serialNo 1 in the interrupt read and 2 once R lands: still one checkpoint.
    w = _run([A], [A, B], [A, B, R])
    with pytest.raises(AssertionFailed) as e:
        assert_checkpoint_predates(w, SPEC)
    assert str(e.value).count("10:01:50.456") == 1 and "10:03:10.000" in str(e.value)


def test_predates_fails_when_the_restored_app_checkpoints_before_the_running_read():
    # A false failure, never a false pass.
    w = _run([A], [A], [A, R])
    with pytest.raises(AssertionFailed, match="10:03:10.000"):
        assert_checkpoint_predates(w, SPEC)


def test_predates_fails_without_a_checkpoint_before_the_seed():
    w = _run([], [], [])
    with pytest.raises(AssertionFailed, match="no checkpoint was recorded before seed seed_b.sql"):
        assert_checkpoint_predates(w, SPEC)


def test_predates_keys_rows_by_content_not_by_serial_number():
    # serialNo is a newest-first rank: A is 1 before B lands and 2 after. Ten rows are kept,
    # the oldest overwritten, so a dropped old row is not a new one.
    old = [_cp(f"2026-10-19 09:{m:02d}:00.000") for m in range(9)]
    w = _run(old + [A], old[1:] + [A], old[1:] + [A])
    assert assert_checkpoint_predates(w, SPEC)[0]["status"] == "passed"


def test_predates_fails_on_a_read_error_or_a_missing_read():
    p = _Platform()
    w = CheckpointWitness(p, "NS.App", now=lambda: "t")
    p.recorded = [A]
    w.capture("seed")
    p.recorded = RuntimeError("connection reset")
    w.capture("interrupt")        # kept, not raised: the run goes on
    with pytest.raises(AssertionFailed, match="'interrupt' read failed: connection reset"):
        assert_checkpoint_predates(w, SPEC)
    w2 = CheckpointWitness(p, "NS.App", now=lambda: "t")
    with pytest.raises(AssertionFailed, match="'seed' read was never taken"):
        assert_checkpoint_predates(w2, SPEC)


def test_predates_refuses_a_row_without_a_recorded_time():
    w = _run([{"serialNo": 1}], [{"serialNo": 1}], [{"serialNo": 1}])
    with pytest.raises(AssertionFailed, match="no checkpointRecordedTime"):
        assert_checkpoint_predates(w, SPEC)


def test_parse_accepts_the_predates_mapping_only():
    assert parse_checkpoint_history_spec({"predates": "seed_b.sql"}) == {"predates": "seed_b.sql"}
    for bad in ({}, {"predates": ""}, {"predates": 1}, {"predates": "b.sql", "x": 1}, {"before": "b.sql"}):
        with pytest.raises(CheckpointHistorySpecError, match="must be one of"):
            parse_checkpoint_history_spec(bad)


_SEEDS = [("mysql-source", "seed_a.sql", "post_start", 5.0),
          ("mysql-source", "seed_b.sql", "post_start", 80.0),
          ("mysql-source", "seed_c.sql", "post_recover", 10.0)]
_RECOVER = {"mode": "kill", "after": 10.0, "settle": 60.0, "expect_running": True,
            "times": 1, "every": 0.0}


def test_predates_manifest_checks():
    check_predates_manifest(SPEC, _RECOVER, _SEEDS)
    check_predates_manifest("nonempty", None, [])          # the string forms need nothing
    bad = [
        (None, _SEEDS, "needs 'recover:'"),
        ({**_RECOVER, "times": 2}, _SEEDS, "times: 1"),
        ({**_RECOVER, "node": "secondary"}, _SEEDS, "no 'node:'"),
        ({**_RECOVER, "expect_running": False}, _SEEDS, "expect_running: true"),
        ({**_RECOVER, "mode": "quiesce"}, _SEEDS, "quiesce"),
        (_RECOVER, _SEEDS[:1], "found 0"),
        (_RECOVER, _SEEDS + [_SEEDS[1]], "found 2"),
        (_RECOVER, [("mysql-source", "seed_b.sql", "post_recover", 0.0)], "found 0"),
    ]
    for recover, seeds, match in bad:
        with pytest.raises(CheckpointHistorySpecError, match=match):
            check_predates_manifest(SPEC, recover, seeds)


def test_manifest_loader_refuses_a_predates_seed_that_does_not_exist(tmp_path):
    import textwrap
    from livetest.manifest import ManifestError, load_manifest
    d = tmp_path / "c"
    d.mkdir()
    (d / "app.tql").write_text("-- x\n")
    body = textwrap.dedent("""\
        name: c
        purpose: p
        tql: app.tql
        seed:
          - {file: seed_b.sql, db: postgres-source, when: post_start}
        recover: {mode: kill}
        assert:
          smoke: true
          checkpoint_history: {predates: %s}
        """)
    (d / "test.yaml").write_text(body % "seed_b.sql")
    assert load_manifest(d / "test.yaml").assert_["checkpoint_history"] == {"predates": "seed_b.sql"}
    (d / "test.yaml").write_text(body % "seed_x.sql")
    with pytest.raises(ManifestError, match="seed_x.sql.*found 0"):
        load_manifest(d / "test.yaml")
