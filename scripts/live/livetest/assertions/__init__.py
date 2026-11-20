from __future__ import annotations


def refuse_unknown_keys(spec: dict, allowed, what: str, error: type) -> None:
    """Raise ``error`` naming every key of ``spec`` outside ``allowed``.

    Each spec parser calls this with the keys its assertion code reads, so a misspelt key (a
    `multset:` that silently left the compare a distinct set) is refused, not ignored."""
    unknown = sorted(str(k) for k in spec if k not in allowed)
    if unknown:
        raise error(f"{what} has unknown key(s) {unknown} -- allowed: {sorted(allowed)}")


class NotYetReadable(Exception):
    """A committed read refused at once because a writer still holds the table (Teradata's
    NOWAIT read), or chosen as a deadlock victim against the writer (SQL Server 1205). The diff
    and data polls treat it as "not yet" and poll again, so their timeout can fire while the
    writer's batch stays open."""


class PartialRead(NotYetReadable):
    """A read whose budget ran out before every captured offset was read. ``rows`` is what was
    read: a lower bound, so enough for `min_rows`, never a count."""
    def __init__(self, detail, rows):
        super().__init__(detail)
        self.rows = rows


class AssertionFailed(AssertionError):
    """Raised by an assert_* function when its assertion fails. Carries the structured
    per-spec records built so far so the plugin can still write them to the sidecar,
    while remaining an AssertionError so pytest/JUnit report it as a failure unchanged."""
    def __init__(self, detail, records):
        super().__init__(detail)
        self.records = records  # list[dict], each from resultschema.build_assertion_result


class ExpectedAssertionFailure(AssertionFailed):
    """An AssertionFailed that a manifest's `xfail.tiers` covers: every failed record came from a
    tier the manifest named. The `pytest_runtest_call` hookwrapper in plugin.py re-raises a covered failure as this type, and the xfail
    marker is `raises=ExpectedAssertionFailure`, so anything else that fails the test -- a deploy
    error, a provisioning timeout, a smoke failure, an assertion on a tier the manifest did not
    name, an AssertionFailed with no records (a spec problem) -- is reported as a FAILURE rather
    than absorbed as the documented defect (design §10.1)."""

    @classmethod
    def if_covered(cls, failure: "AssertionFailed", tiers) -> "AssertionFailed":
        """Return `failure` re-typed as expected when `tiers` covers it, else `failure` unchanged."""
        failed = [r for r in (failure.records or []) if r.get("status") == "failed"]
        if not failed:
            return failure
        if all(r.get("type") in set(tiers) for r in failed):
            wrapped = cls(str(failure), failure.records)
            wrapped.__cause__ = failure
            return wrapped
        return failure


def poll_holding(evaluate, holds, timeout, poll, status_probe=None, progress=None):
    """Poll ``evaluate(indices)`` -- one result per index, ``result[0]`` true when that spec
    matched -- until every spec matches on one read, or the deadline passes.

    A spec whose ``holds[i]`` is true asserts only upper bounds, zeros or absences, which the
    first read can satisfy before anything has happened. Once every spec matches, those specs
    alone are read again until the deadline, and a read on which one misses ends the poll.
    ``holds`` is read on every poll, so ``evaluate`` may update an entry once it knows what the
    spec expects (a spec rendered from a value re-read each poll).
    Returns ``(results, outcome)``: ``"passed"``, ``"expired"`` (a spec never matched) or
    ``"broke"`` (a held spec matched, then missed)."""
    import time
    deadline = time.monotonic() + timeout
    results = [None] * len(holds)
    holding = False
    while True:
        if status_probe:
            status_probe()
        idx = [i for i, h in enumerate(holds) if h or not holding]
        for i, r in zip(idx, evaluate(idx)):
            results[i] = r
        if holding and not all(results[i][0] for i in idx):
            return results, "broke"
        matched = all(r[0] for r in results)
        expired = time.monotonic() >= deadline
        if matched and (expired or not any(holds)):
            return results, "passed"
        if expired:
            return results, "expired"
        holding = matched
        if progress:
            try:
                progress(timeout - (deadline - time.monotonic()), float(timeout))
            except Exception:
                pass
        step = poll or 0.05      # floor avoids a busy-wait when poll=0 with a positive timeout
        time.sleep(min(step, max(0.0, deadline - time.monotonic())) if holding else step)
