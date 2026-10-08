from __future__ import annotations

import json
import math
import re
import time

from livetest.assertions import AssertionFailed
from livetest.resultschema import build_assertion_result

# T4-MON. Assert what the platform's monitor SHOWS for a target -- `MON <target>;` over the
# same Tungsten endpoint the console reads -- rather than what a writer reports to the harness.
# A writer that writes correctly and publishes nothing looks dead on the monitor page, and
# these assertions detect that gap. Polls until
# every figure matches or the timeout expires: MON republishes once per monitoring snapshot,
# so a value that is right in the database is a few seconds behind on the monitor.

# Figures that measure the machine the case runs on, never the writer. Refused by name at
# load, for the reason the integration tier refuses its clocks: a case asserting one is a
# flake authored on purpose. They are still in the sidecar's `actual` for a human to read.
CLOCK_FIELDS = frozenset({
    "rate", "inputRate", "acceptedRate", "targetRate", "sourceRate",
    "lastCommitTime", "lastI/oTime", "lastCommitLatency", "externalI/oLatency",
    "lastEventWriteAge", "commitLag", "maxLeeFromAllSources", "latestActivity",
    "timestamp", "numberOfEventsSeenPerMonitorSnapshotInterval",
})


# String matchers: a mapping with one of these keys checks a figure's presence or form rather
# than its value -- what a position, whose value changes run to run, needs.
MATCHER_KEYS = frozenset({"present", "absent", "matches"})


class MonitorSpecError(Exception):
    pass


def parse_monitor_specs(raw: list) -> list[dict]:
    """`assert.monitor:` is a list of {target? | component?, metrics}. `target` is the TQL
    component name of a TARGET in the app; it may be omitted when the app has exactly one.
    `component` names any component (a source, a CQ, an OP). `metrics` maps a MON field name to
    the value it must show: a number or string compared exactly, {min, max} bounds on a number
    (either or both, inclusive), a matcher ({present: true}, {absent: true} or
    {matches: <regex>}), or a mapping compared key by key against a structured field (a subset
    match, so a case can pin `individualOperationCount: {Insert: 5}` without naming every op).
    A mapping with a `min` or `max` key is a bound, and one with a matcher key a matcher, at any
    depth. Strings and patterns may carry ${...} tokens, rendered when the tier runs."""
    if not isinstance(raw, list) or not raw:
        raise MonitorSpecError("'assert.monitor' must be a non-empty list of specs")
    for spec in raw:
        if not isinstance(spec, dict):
            raise MonitorSpecError(f"monitor spec must be a mapping: {spec!r}")
        unknown = set(spec) - {"target", "component", "metrics", "recapture"}
        if unknown:
            raise MonitorSpecError(
                f"monitor spec has unknown key(s) {sorted(unknown)}; the figures go under "
                f"'metrics:'")
        metrics = spec.get("metrics")
        if not isinstance(metrics, dict) or not metrics:
            raise MonitorSpecError(
                "monitor spec needs a non-empty 'metrics:' mapping of MON field -> expected value")
        for name, value in metrics.items():
            if name in CLOCK_FIELDS:
                raise MonitorSpecError(
                    f"'assert.monitor' metric {name!r} is a CLOCK or a RATE, and a case that "
                    f"asserts one is measuring the machine it runs on. It is still recorded for "
                    f"a human to read; assert a count instead")
            if isinstance(value, (list, bool)) or value is None:
                raise MonitorSpecError(
                    f"'assert.monitor' metric {name!r}: expected a number, a string or a "
                    f"mapping, got {value!r}")
            _check_bounds(name, value)
        if "target" in spec and (not isinstance(spec["target"], str) or not spec["target"]):
            raise MonitorSpecError("monitor spec 'target' must be a non-empty component name")
        if "component" in spec:
            if "target" in spec:
                raise MonitorSpecError(
                    "monitor spec takes 'target:' (a TARGET) or 'component:' (any component), "
                    "not both")
            if not isinstance(spec["component"], str) or not spec["component"]:
                raise MonitorSpecError("monitor spec 'component' must be a non-empty component name")
        if "recapture" in spec and (not isinstance(spec["recapture"], list) or not spec["recapture"]
                                    or not all(isinstance(c, dict) for c in spec["recapture"])):
            raise MonitorSpecError(
                "monitor spec 'recapture' must be a non-empty list of captures "
                "({token, describe|mon, field})")
    return raw


def _is_bound(v) -> bool:
    return isinstance(v, dict) and bool(set(v) & {"min", "max"})


def _is_matcher(v) -> bool:
    return isinstance(v, dict) and bool(set(v) & MATCHER_KEYS)


def _check_matcher(name, value):
    if len(value) != 1:
        raise MonitorSpecError(
            f"'assert.monitor' metric {name!r}: a matcher is exactly one of {{present: true}}, "
            f"{{absent: true}} or {{matches: <regex>}}, got {value!r}")
    (key, arg), = value.items()
    if key in ("present", "absent"):
        if arg is not True:
            raise MonitorSpecError(
                f"'assert.monitor' metric {name!r}: '{key}' takes true, got {arg!r} (write the "
                f"other matcher instead of false)")
        return
    if not isinstance(arg, str) or not arg:
        raise MonitorSpecError(
            f"'assert.monitor' metric {name!r}: 'matches' takes a non-empty regex, got {arg!r}")
    if "${" not in arg:      # a pattern with tokens is compiled once they are rendered
        try:
            re.compile(arg)
        except re.error as e:
            raise MonitorSpecError(
                f"'assert.monitor' metric {name!r}: 'matches' is not a valid regex: {e}") from None


def _check_bounds(name, value):
    if not isinstance(value, dict):
        return
    if _is_matcher(value):
        _check_matcher(name, value)
        return
    if not _is_bound(value):
        for v in value.values():
            _check_bounds(name, v)
        return
    if set(value) - {"min", "max"} or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
            for v in value.values()):
        raise MonitorSpecError(
            f"'assert.monitor' metric {name!r}: a bound is {{min, max}} with numeric values, "
            f"got {value!r}")
    if "min" in value and "max" in value and value["min"] > value["max"]:
        raise MonitorSpecError(f"'assert.monitor' metric {name!r}: min > max in {value!r}")


def _render(want) -> str:
    if _is_matcher(want):
        (key, arg), = want.items()
        return f"matches /{arg}/" if key == "matches" else key
    if _is_bound(want):
        return " and ".join(f"{'>=' if k == 'min' else '<='} {v}" for k, v in want.items())
    return str(want)


def _bound_miss(want: dict, actual) -> str | None:
    got = _num(actual)
    if got is None or math.isnan(got):
        return f"not a number, expected {_render(want)}"
    if "min" in want and got < want["min"]:
        return f"below min {want['min']}"
    if "max" in want and got > want["max"]:
        return f"above max {want['max']}"
    return None


def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _structured(v):
    # MON renders structured fields (per-op counts, table info) as JSON text or as a map.
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _matcher_miss(want: dict, actual) -> str | None:
    (key, arg), = want.items()
    shown = actual is not None and str(actual) != ""
    if key == "present":
        return None if shown else "absent, expected present"
    if key == "absent":
        return None if actual is None else "present, expected absent"
    if actual is None:
        return f"absent, expected to match /{arg}/"
    return None if re.search(arg, str(actual)) else f"does not match /{arg}/"


def _same(expected, actual) -> bool:
    if _is_matcher(expected):
        return _matcher_miss(expected, actual) is None
    if _is_bound(expected):
        return _bound_miss(expected, actual) is None
    if isinstance(expected, dict):
        got = _structured(actual)
        return got is not None and all(k in got and _same(want, got[k])
                                       for k, want in expected.items())
    if isinstance(expected, (int, float)):
        got = _num(actual)
        return got is not None and got == float(expected)
    return actual is not None and str(actual) == str(expected)


def find_targets(mon_app) -> list[str]:
    """Every component in `MON <app>`'s tree whose entityType is TARGET, by fullName."""
    found, stack = [], [mon_app]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("entityType") == "TARGET" and node.get("fullName"):
                found.append(node["fullName"])
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


def find_components(mon_app) -> list[str]:
    """Every component below the application in `MON <app>`'s tree, by fullName."""
    found, stack = [], [mon_app]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if (node.get("entityType") and node.get("entityType") != "APPLICATION"
                    and node.get("fullName")):
                found.append(node["fullName"])
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


def resolve_component(components: list[str], wanted: str, app: str) -> str:
    """`component:` against the app's tree. A name the tree does not show is qualified by the
    app's namespace and read directly: MON on a component that does not exist fails on its own."""
    if "." in wanted:
        return wanted
    hits = sorted({c for c in components if c == wanted or c.endswith("." + wanted)})
    if len(hits) > 1:
        raise AssertionFailed(
            f"monitor: {app} has component(s) {sorted(set(components))}; 'component: {wanted}' "
            f"matches {len(hits)} of them", [])
    if hits:
        return hits[0]
    return f"{app.split('.', 1)[0]}.{wanted}" if "." in app else wanted


def render_spec(spec: dict, tokens: dict | None) -> dict:
    """The spec with its strings rendered: names, expected values, and `matches` patterns, in which
    a token's value is escaped so a captured position (all brackets and braces) matches literally."""
    if tokens is None:
        return spec
    from livetest.substitute import _TOKEN, render

    def pattern(text):
        render(text, tokens)                      # names a missing token, as everywhere else
        return _TOKEN.sub(lambda m: re.escape(str(tokens[m.group(1)])), text)

    def walk(v):
        if _is_matcher(v) and "matches" in v:
            return {"matches": pattern(v["matches"])}
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        return render(v, tokens) if isinstance(v, str) else v

    out = dict(spec)
    for key in ("target", "component"):
        if key in out:
            out[key] = render(out[key], tokens)
    out["metrics"] = walk(spec["metrics"])
    return out


def resolve_target(targets: list[str], wanted: str | None, app: str) -> str:
    if wanted:
        hits = [t for t in targets if t == wanted or t.endswith("." + wanted)]
        if len(hits) != 1:
            raise AssertionFailed(
                f"monitor: {app} has target(s) {targets}; 'target: {wanted}' matches "
                f"{len(hits)} of them", [])
        return hits[0]
    if len(targets) != 1:
        raise AssertionFailed(
            f"monitor: {app} has {len(targets)} target(s) {targets}; name one with 'target:'",
            [])
    return targets[0]


def _evaluate(body: dict, spec: dict):
    misses = []
    for name, want in spec["metrics"].items():
        if _is_matcher(want):
            miss = _matcher_miss(want, body.get(name))
            if miss:
                misses.append(f"{name}: shows {body.get(name)!r}, {miss}")
        elif _is_bound(want):
            miss = _bound_miss(want, body.get(name))
            if miss:
                misses.append(f"{name}: shows {body.get(name)!r}, {miss}")
        elif not _same(want, body.get(name)):
            misses.append(f"{name}: shows {body.get(name)!r}, expected {want!r}")
    shown = {n: body.get(n) for n in spec["metrics"]}
    expected = {"kind": "rows",
                "rows": [{"metric": n, "value": _render(v)} for n, v in spec["metrics"].items()]}
    actual = {"kind": "rows", "rows": [{"metric": n, "value": str(v)} for n, v in shown.items()]}
    return (not misses), "; ".join(misses) or None, expected, actual


def _check_bounds_all(spec: dict) -> None:
    """Re-check a rendered spec: a `matches` pattern built from tokens is compiled only now."""
    for name, value in spec["metrics"].items():
        try:
            _check_bounds(name, value)
        except MonitorSpecError as e:
            raise AssertionFailed(f"monitor: {e}", []) from None


def _recaptured(client, spec: dict, app: str, tokens: dict):
    """Re-read every `recapture:` value, then render the spec against them: (spec, None), or
    (None, why) when a value is not shown this poll."""
    from livetest import appactions
    from livetest.manifest import _normalize_recapture
    ns = app.split(".", 1)[0]
    for j, raw in enumerate(spec["recapture"]):
        cap = _normalize_recapture(raw, f"assert.monitor recapture[{j}]", None)
        try:
            value, _tree, fqn = appactions.read_once(client, cap, ns, tokens)
        except appactions.CaptureError as e:
            return None, str(e)
        if value is None:
            return None, (f"recapture {cap['token']}: {cap['source'].upper()} {fqn} shows no value "
                          f"for {cap['field']!r}")
        tokens[cap["token"]] = value
    rendered = render_spec(spec, tokens)
    _check_bounds_all(rendered)
    return rendered, None


def assert_monitor(client, app: str, specs: list[dict], timeout: int, poll: float = 2.0,
                   status_probe=None, progress=None, tokens: dict | None = None) -> list[dict]:
    """Polls every spec until all match or `timeout`. A spec with `recapture:` re-reads those
    values on every poll, just before its MON read, and is rendered against that read: a value
    that keeps moving on a running app (a checkpoint advanced by idle heartbeats) is compared with
    its current reading, not with one taken once and outrun."""
    local = dict(tokens or {})
    fixed = {}
    for i, s in enumerate(specs):
        if not s.get("recapture"):
            fixed[i] = render_spec(s, tokens)
            _check_bounds_all(fixed[i])
    deadline = time.monotonic() + timeout
    resolved: dict[int, str] = {}
    shown_specs = list(specs)
    while True:
        if status_probe:
            status_probe()
        results = []
        for i, raw in enumerate(specs):
            spec = fixed.get(i)
            if spec is None:
                spec, why = _recaptured(client, raw, app, local)
                if spec is None:
                    empty = {"kind": "rows", "rows": []}
                    results.append((False, why, empty, empty))
                    continue
            shown_specs[i] = spec
            if i not in resolved:
                if spec.get("component"):
                    resolved[i] = resolve_component(
                        find_components(client.mon(app)), spec["component"], app)
                else:
                    resolved[i] = resolve_target(
                        find_targets(client.mon(app)), spec.get("target"), app)
            results.append(_evaluate(client.mon(resolved[i]), spec))
        detail = next((d for ok, d, _, _ in results if not ok), None)
        if detail is None or time.monotonic() >= deadline:
            records = [
                build_assertion_result(type="monitor", status="passed" if ok else "failed",
                                        spec=s, db=None, target=resolved.get(
                                            i, s.get("component") or s.get("target") or app),
                                        detail="ok" if ok else d, expected=exp, actual=act)
                for i, (s, (ok, d, exp, act)) in enumerate(zip(shown_specs, results))
            ]
            if detail is None:
                return records
            raise AssertionFailed(
                f"monitor assertion not satisfied within {timeout}s: {detail}. This is what a "
                f"person reads off the monitor page to decide whether a flow is progressing.",
                records)
        if progress:
            try:
                progress(timeout - (deadline - time.monotonic()), float(timeout))
            except Exception:
                pass
        time.sleep(poll or 0.05)
