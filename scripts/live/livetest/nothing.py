"""A bounded absence check, gated by a completed delivery sentinel."""
import math
from decimal import Decimal

from livetest import lifecycle
from livetest.assertions import AssertionFailed
from livetest.resultschema import build_assertion_result
from livetest.substitute import render


def get_path(row, path):
    for key in path.split("."):
        if not isinstance(row, dict) or key not in row:
            return False, None
        row = row[key]
    return True, row


def matches(row, selector):
    for key, expected in selector.items():
        present, actual = get_path(row, key)
        if not present or isinstance(actual, bool) != isinstance(expected, bool) or actual != expected:
            return False
    return True


def strict_matches(row, selector):
    """An output whose identity cannot be checked is a failure, not a non-match."""
    for key, expected in selector.items():
        present, actual = get_path(row, key)
        if not present:
            raise ValueError(f"selector field {key} is missing from output")
        if expected is not None:
            if isinstance(expected, bool):
                valid = isinstance(actual, bool)
            elif isinstance(expected, (int, float, Decimal)):
                valid = isinstance(actual, (int, float, Decimal)) and not isinstance(actual, bool)
                if isinstance(actual, (float, Decimal)):
                    valid = valid and (actual.is_finite() if isinstance(actual, Decimal) else math.isfinite(actual))
            else:
                valid = isinstance(actual, str)
            if not valid:
                raise ValueError(f"selector field {key} has an incompatible token type")
        if actual != expected:
            return False
    return True


def validate(raw, path, lc):
    for kind in ("data", "file"):
        for spec in (raw.get("assert") or {}).get(kind) or []:
            if not isinstance(spec, dict) or "nothing" not in spec:
                continue
            def refuse(message):
                raise ValueError(f"invalid-nothing-spec: {path}: {message}")
            allowed = {"path", "nothing"} if kind == "file" else {"target", "db", "target_db", "nothing", "payload"}
            if set(spec) - allowed:
                refuse("nothing cannot be combined with content-match keys")
            check = spec["nothing"]
            if not isinstance(check, dict) or set(check) != {"window", "where"}:
                refuse("nothing needs exactly window and where")
            try:
                seconds = lifecycle.parse_duration(check["window"], "nothing.window", path)
            except ValueError as e:
                refuse(str(e))
            if seconds <= 0 or not math.isfinite(seconds):
                refuse("window must be positive and finite")
            if not isinstance(check["where"], dict) or not check["where"] or any(
                    not isinstance(k, str) or not k or any(not s for s in k.split("."))
                    for k in check["where"]):
                refuse("where needs a non-empty mapping of field paths to scalar values")
            if any(isinstance(v, (dict, list)) for v in check["where"].values()):
                refuse("where values must be scalars")
            if any(isinstance(v, float) and not math.isfinite(v) for v in check["where"].values()):
                refuse("where values must be finite")
            if lc is None or lc.completion["kind"] != "sentinel":
                refuse("nothing requires lifecycle completion kind sentinel")
            observe = lc.sentinel["observe"]
            if kind == "file":
                same = spec.get("path") == observe.get("path")
            else:
                route = spec.get("target_db", spec.get("db", "postgres-source"))
                same = route == observe.get("db") and spec.get("target") == observe.get("table", observe.get("topic"))
            if not same:
                refuse("nothing must observe the same sink as the completion sentinel")


def assert_nothing(specs, *, kind, tokens, completion, read, clock=None, status_probe=None):
    clock = clock or lifecycle.Clock
    records = []
    for spec in specs:
        target = render(spec.get("target", spec.get("path")), tokens)
        ok, detail, actual = False, "nothing-sentinel-unproven: completion sentinel did not succeed", []
        if completion and completion.get("kind") == "sentinel" and completion.get("reason") == "satisfied":
            window = lifecycle.parse_duration(spec["nothing"]["window"], "nothing.window", target)
            deadline = clock.monotonic() + window
            reads = 0
            try:
                while True:
                    if status_probe:
                        status_probe()
                    read_started = clock.monotonic()
                    actual = [r for r in read(spec, max(0.05, deadline - clock.monotonic()))
                              if strict_matches(r, spec["nothing"]["where"])]
                    reads += 1
                    if actual:
                        detail = "nothing-arrived: forbidden input produced output during the observation window"
                        break
                    if read_started >= deadline:
                        ok, detail = True, f"nothing arrived for {window}s after completion sentinel; {reads} reads"
                        break
                    clock.sleep(max(0, min(0.2, deadline - clock.monotonic())))
            except Exception as e:
                detail = f"nothing-read-error: {e}"
        records.append(build_assertion_result(type=kind, status="passed" if ok else "failed", spec=spec,
            target=target, db=spec.get("target_db", spec.get("db")), detail=detail,
            expected={"kind": "count", "count": 0}, actual={"kind": "count", "count": len(actual)}))
    if any(r["status"] == "failed" for r in records):
        raise AssertionFailed("; ".join(r["detail"] for r in records if r["status"] == "failed"), records)
    return records
