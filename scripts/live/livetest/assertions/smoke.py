from __future__ import annotations
import time

from livetest.assertions import AssertionFailed
from livetest.resultschema import build_assertion_result


def assert_smoke(client, app: str | list[str], timeout: int, settle_seconds: float = 5.0,
                  *, db: str | None = None, progress=None) -> list[dict]:
    # No specs to evaluate -- smoke is a single pass/fail check with nothing to snapshot.
    # await_running/current_status raising, or an app not settling on RUNNING, are all
    # wrapped into one AssertionFailed carrying a single failed record, so the plugin
    # always has a record to write regardless of which step failed.
    # `app` is either one app name, or a list (e.g. producer+reader) that must ALL be
    # RUNNING -- a multi-app test only works if every sub-app is up.
    apps = [app] if isinstance(app, str) else list(app)
    try:
        for a in apps:
            client.await_running(a, timeout=timeout, progress=progress)   # raises StriimError if it never does
        if settle_seconds:
            time.sleep(settle_seconds)
        for a in apps:
            status = client.current_status(a)
            if status != "RUNNING":
                raise AssertionError(f"{a} did not stay RUNNING through the settle window: status={status}")
    except Exception as e:
        detail = str(e)
        record = build_assertion_result(type="smoke", status="failed", spec={}, target=None, db=db,
                                         detail=detail)
        raise AssertionFailed(detail, [record]) from e

    record = build_assertion_result(type="smoke", status="passed", spec={}, target=None, db=db,
                                     detail=f"RUNNING: {', '.join(apps)}")
    return [record]
