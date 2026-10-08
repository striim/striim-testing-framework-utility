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


class CheckpointHistorySpecError(Exception):
    pass


def parse_checkpoint_history_spec(raw) -> str:
    """`assert.checkpoint_history:` is the literal string `nonempty` (the platform must show
    at least one recorded checkpoint) or `empty` (it must show none)."""
    if not isinstance(raw, str) or raw not in _ALLOWED:
        raise CheckpointHistorySpecError(
            f"'assert.checkpoint_history' must be one of {sorted(_ALLOWED)}, got {raw!r}")
    return raw


def assert_checkpoint_history(client, app: str, expected: str, timeout: int,
                              poll: float = 2.0, status_probe=None, progress=None) -> list[dict]:
    """Polls `SHOW <app> CHECKPOINT HISTORY;` until it shows what `expected` says, or the
    deadline expires. `nonempty` needs the wait -- a RECOVERY-clause app's first checkpoint
    lands on its own interval, not the instant the app reaches RUNNING. `empty` does not
    strictly need to poll, but does anyway so a case that races the two arms (a shared
    `recover:`/settle window) sees the same timeout budget on both sides."""
    deadline = time.monotonic() + timeout
    rows: list = []
    while True:
        if status_probe:
            status_probe()
        rows = client.checkpoint_history(app)
        ok = bool(rows) if expected == "nonempty" else not rows
        if ok or time.monotonic() >= deadline:
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
        f"SHOW {app} CHECKPOINT HISTORY shows {shown} within {timeout}s, expected {expected}")
    record = build_assertion_result(
        type="checkpoint_history", status="passed" if ok else "failed",
        spec={"checkpoint_history": expected}, target=app, db=None,
        detail="ok" if ok else detail,
        expected={"kind": "rows", "rows": [{"expect": expected}]},
        actual={"kind": "rows", "rows": rows})
    if not ok:
        raise AssertionFailed(detail, [record])
    return [record]
