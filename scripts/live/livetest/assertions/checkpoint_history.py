from __future__ import annotations

import time

from livetest.assertions import AssertionFailed
from livetest.resultschema import build_assertion_result

# `assert.checkpoint_history:` asks the platform directly whether it has RECORDED a recovery
# checkpoint for the app -- `SHOW <app> CHECKPOINT HISTORY;` over the same Tungsten endpoint
# MON reads -- rather than inferring it from row counts surviving a stop/restart. The two are
# complementary, not redundant: the recovery-stop/-idle-stop/-quiesce cases already prove data
# arrives correctly across a restart, which only works if a checkpoint was both recorded and
# read back; this tier answers "was one recorded at all" for a single word, so a RECOVERY-clause
# app and a plain one are told apart by name instead of by inference.

_ALLOWED = frozenset({"nonempty", "empty"})

# The mapping form, `{predates: <seed file>}`, asks a different question: was the last
# checkpoint recorded before the interruption older than that seed? If so the restart MUST
# replay the seed's events, so a `recover:` case that passes has really exercised the replay.
# Without it a checkpoint that lands between the seed and a kill makes the case pass with
# nothing replayed.
_PREDATES = "predates"
# SHOW ... CHECKPOINT HISTORY numbers rows 1..N newest first on every read (serialNo is a rank,
# not an id) and keeps at most 10 per app, so a row is identified by its own content.
_ROW_KEY = ("checkpointRecordedTime", "checkpointType", "sourcePositionSummary",
            "targetPositionSummary")


class CheckpointHistorySpecError(Exception):
    pass


def parse_checkpoint_history_spec(raw):
    """`assert.checkpoint_history:` is the literal string `nonempty` (the platform must show
    at least one recorded checkpoint) or `empty` (it must show none), or the mapping
    `{predates: <seed file>}` (returned as a dict)."""
    if (isinstance(raw, dict) and set(raw) == {_PREDATES}
            and isinstance(raw[_PREDATES], str) and raw[_PREDATES]):
        return {_PREDATES: raw[_PREDATES]}
    if not isinstance(raw, str) or raw not in _ALLOWED:
        raise CheckpointHistorySpecError(
            f"'assert.checkpoint_history' must be one of {sorted(_ALLOWED)} or "
            f"{{{_PREDATES}: <seed file>}}, got {raw!r}")
    return raw


def check_predates_manifest(spec, recover, seed_files) -> None:
    """Refuse a `predates` spec the run could not evaluate. It needs one `recover:` cycle on the
    app (no `node:`) that brings the app back, and exactly one `post_start` seed of that file:
    the three reads it compares are taken just before that seed, just before the interruption
    and the moment the app is RUNNING again."""
    if not isinstance(spec, dict):
        return
    seed = spec[_PREDATES]
    if not recover:
        raise CheckpointHistorySpecError(
            "'assert.checkpoint_history.predates' needs 'recover:': it compares the checkpoints "
            "recorded before the interruption with the seed")
    if recover.get("mode") == "quiesce":
        raise CheckpointHistorySpecError(
            "'assert.checkpoint_history.predates' cannot use recover mode quiesce: it records a "
            "checkpoint as it stops, so the case always fails")
    if recover.get("times", 1) != 1 or "node" in recover or not recover.get("expect_running", True):
        raise CheckpointHistorySpecError(
            "'assert.checkpoint_history.predates' needs 'recover:' with times: 1, no 'node:' "
            "and expect_running: true")
    named = [f for _db, f, when, _a in seed_files if f == seed and when == "post_start"]
    if len(named) != 1:
        raise CheckpointHistorySpecError(
            f"'assert.checkpoint_history.predates: {seed}' must name exactly one 'seed:' entry "
            f"with 'when: post_start', found {len(named)}")


class CheckpointWitness:
    """The checkpoint history read at three points of a `predates` case: `seed` (just before the
    named seed runs), `interrupt` (just before the interruption command) and `restored` (the
    moment the app is RUNNING again, before any post-recover seed or settle). A read error is
    kept, not raised: the run goes on and the assertion fails naming it."""

    PHASES = ("seed", "interrupt", "restored")

    def __init__(self, client, app: str, now=None):
        from datetime import datetime, timezone
        self.client, self.app = client, app
        self._now = now or (lambda: datetime.now(timezone.utc).isoformat())
        self.reads: dict = {}

    def capture(self, phase: str) -> None:
        if phase not in self.PHASES:
            raise ValueError(f"unknown checkpoint witness phase {phase!r}")
        entry = {"phase": phase, "observedUtc": self._now(), "rows": None, "error": None}
        try:
            entry["rows"] = list(self.client.checkpoint_history(self.app))
        except Exception as e:      # noqa: BLE001 -- reported by assert_checkpoint_predates
            from livetest.striim import redact
            entry["error"] = redact(str(e))[:1024]
        self.reads[phase] = entry


def _row_key(row):
    if not isinstance(row, dict) or not row.get("checkpointRecordedTime"):
        raise ValueError(f"a checkpoint history row has no checkpointRecordedTime: {row!r}")
    return tuple(str(row.get(k)) for k in _ROW_KEY)


def assert_checkpoint_predates(witness: CheckpointWitness, spec: dict) -> list[dict]:
    """Pass when a checkpoint was recorded before the seed and none after it, up to the first
    read after the restore. Rows are compared with each other, never with the harness clock,
    so the server's time zone does not matter.

    The `interrupt` read is the last one before the command; a checkpoint recorded between it
    and the command shows up in the `restored` read instead, which is taken before the restored
    app's first checkpoint interval can elapse. A checkpoint the restored app records before
    that read also fails the case: a false failure, never a false pass."""
    seed = spec[_PREDATES]
    reads = witness.reads
    detail = None
    new = []
    try:
        for phase in CheckpointWitness.PHASES:
            if phase not in reads:
                raise ValueError(f"the '{phase}' read was never taken")
            if reads[phase]["error"] is not None:
                raise ValueError(f"the '{phase}' read failed: {reads[phase]['error']}")
        before = {_row_key(r) for r in reads["seed"]["rows"]}
        if not before:
            detail = (f"no checkpoint was recorded before seed {seed}: nothing shows where the "
                      f"restart resumed from")
        else:
            seen = set(before)
            for phase in ("interrupt", "restored"):
                for row in reads[phase]["rows"]:
                    if _row_key(row) not in seen:      # serialNo differs between reads
                        seen.add(_row_key(row))
                        new.append(row)
            if new:
                times = ", ".join(str(r.get("checkpointRecordedTime")) for r in new)
                detail = (f"a checkpoint was recorded after seed {seed} and before the app was "
                          f"RUNNING again (recorded {times}): the restart need not have "
                          f"replayed {seed}")
    except ValueError as e:
        detail = f"checkpoint history unreadable: {e}"
    ok = detail is None
    record = build_assertion_result(
        type="checkpoint_history", status="passed" if ok else "failed",
        spec={"checkpoint_history": dict(spec)}, target=witness.app, db=None,
        detail="ok" if ok else detail,
        expected={"kind": "rows", "rows": [{"newSinceSeed": 0, "beforeSeed": ">= 1"}]},
        actual={"kind": "rows", "rows": [
            {"phase": p, "observedUtc": reads.get(p, {}).get("observedUtc"),
             "error": reads.get(p, {}).get("error"), "history": reads.get(p, {}).get("rows")}
            for p in CheckpointWitness.PHASES] + [{"newSinceSeed": new}]})
    if not ok:
        raise AssertionFailed(f"{witness.app}: {detail}", [record])
    return [record]


def assert_checkpoint_history(client, app: str, expected: str, timeout: int,
                              poll: float = 2.0, status_probe=None, progress=None) -> list[dict]:
    """Polls `SHOW <app> CHECKPOINT HISTORY;`. `nonempty` passes on the first read that shows a
    checkpoint, or fails at the deadline -- a RECOVERY-clause app's first checkpoint lands on its
    own interval, not the instant the app reaches RUNNING. `empty` must hold for the whole
    `timeout`: it fails on the first read that shows a checkpoint and passes only at the
    deadline, since an early empty read cannot tell a plain app from one not yet checkpointed."""
    deadline = time.monotonic() + timeout
    rows: list = []
    while True:
        if status_probe:
            status_probe()
        rows = client.checkpoint_history(app)
        ok = bool(rows) if expected == "nonempty" else not rows
        if (ok if expected == "nonempty" else not ok) or time.monotonic() >= deadline:
            break
        if progress:
            try:
                progress(timeout - (deadline - time.monotonic()), float(timeout))
            except Exception:
                pass
        if poll:
            time.sleep(poll)
    shown = "nonempty" if rows else "empty"
    detail = None if ok else (
        f"SHOW {app} CHECKPOINT HISTORY shows {shown} within {timeout}s, expected {expected}"
        + (" for the whole timeout" if expected == "empty" else ""))
    record = build_assertion_result(
        type="checkpoint_history", status="passed" if ok else "failed",
        spec={"checkpoint_history": expected}, target=app, db=None,
        detail="ok" if ok else detail,
        expected={"kind": "rows", "rows": [{"expect": expected}]},
        actual={"kind": "rows", "rows": rows})
    if not ok:
        raise AssertionFailed(detail, [record])
    return [record]
