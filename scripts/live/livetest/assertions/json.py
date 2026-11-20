from __future__ import annotations

import json
import re
import time
from pathlib import Path

from livetest.assertions import AssertionFailed, refuse_unknown_keys
from livetest.assertions.data import check_stable_seconds
from livetest.assertions.expected import load_expected_text
from livetest.resultschema import build_assertion_result


class JsonSpecError(Exception):
    pass


#: Every key an `assert.json` spec may carry: what assert_json reads, plus the plugin's routing
#: keys (`_spec_route` reads source_db and target_db before db).
JSON_SPEC_KEYS = frozenset({"target", "db", "key", "column", "match", "source_db", "target_db",
                            "unordered", "stable_seconds"})


def parse_json_specs(raw: list) -> list:
    if not isinstance(raw, list):
        raise JsonSpecError("'assert.json' must be a list of specs")
    for spec in raw:
        if not isinstance(spec, dict):
            raise JsonSpecError(f"json spec must be an object: {spec!r}")
        refuse_unknown_keys(spec, JSON_SPEC_KEYS, f"json spec {spec.get('target')!r}", JsonSpecError)
        for field in ("target", "db", "column", "match"):
            if not isinstance(spec.get(field), str) or not spec.get(field):
                raise JsonSpecError(f"json spec needs a string '{field}': {spec!r}")
        key = spec.get("key")
        if isinstance(key, str):
            spec["key"] = [key]
        elif isinstance(key, list) and key and all(isinstance(k, str) for k in key):
            pass
        else:
            raise JsonSpecError(
                f"json spec {spec.get('target')!r} needs a 'key' that is a string or non-empty list "
                f"of strings: {key!r}")
        if "unordered" in spec:
            _check_unordered(spec)
        if "stable_seconds" in spec:
            check_stable_seconds(spec["stable_seconds"], f"json spec {spec['target']!r}", JsonSpecError)
    return raw


# An `unordered:` path names arrays inside the column's value: `$` is the value itself, `.name`
# a member, `[]` every element of an array. A path may omit the leading `$.`.
_UNORDERED_PATH = re.compile(r"^(?:\$(?:\[\])*|[^.\[\]\s$]+(?:\[\])*)(?:\.[^.\[\]\s]+(?:\[\])*)*$")


def _check_unordered(spec: dict) -> None:
    paths = spec["unordered"]
    where = f"json spec {spec.get('target')!r}: 'unordered'"
    if not isinstance(paths, list) or not paths:
        raise JsonSpecError(f"{where} must be a non-empty list of array paths, got {paths!r}")
    for p in paths:
        if not isinstance(p, str) or not _UNORDERED_PATH.match(p):
            raise JsonSpecError(
                f"{where}: {p!r} is not an array path -- write `$` for the value itself, member "
                f"names joined by '.', and `[]` for every element (e.g. items[].subitems)")
    if len(set(paths)) != len(paths):
        raise JsonSpecError(f"{where} names a path twice: {paths!r}")


def _path_tokens(path: str) -> tuple:
    # "items[].subitems" -> ("items", "[]", "subitems"); "$" -> (); "$[]" -> ("[]",).
    out = []
    for i, seg in enumerate(path.split(".")):
        name = seg.rstrip("[]")
        if name and not (i == 0 and name == "$"):
            out.append(name)
        out.extend(["[]"] * ((len(seg) - len(name)) // 2))
    return tuple(out)


def _array_paths(v, path: tuple = (), out: set | None = None) -> set:
    out = set() if out is None else out
    if isinstance(v, dict):
        for k, x in v.items():
            _array_paths(x, path + (k,), out)
    elif isinstance(v, list):
        out.add(path)
        for x in v:
            _array_paths(x, path + ("[]",), out)
    return out


# --------------------------------------------------------------------------------------
# Pure comparator (no DB, no I/O) -- path-scoped semantic JSON diff.
# --------------------------------------------------------------------------------------


def _sort_key(v) -> str:
    # Typed: 1, 1.0, true and "1" serialize differently, so they never sort as one value.
    return json.dumps(v, sort_keys=True, ensure_ascii=False)


def _canon(v, unordered: frozenset = frozenset(), path: tuple = ()):
    # Sorts the arrays at the `unordered` paths (their elements canonicalized first); every
    # other array keeps its order, which is part of its value.
    if isinstance(v, dict):
        return {k: _canon(x, unordered, path + (k,)) for k, x in v.items()}
    if isinstance(v, list):
        items = [_canon(x, unordered, path + ("[]",)) for x in v]
        return sorted(items, key=_sort_key) if path in unordered else items
    return v


def _diff_path(actual, expected, path: str = ""):
    # Returns None if actual/expected are semantically equal, else a human string naming
    # the first differing location (dict key-set mismatch, list length mismatch, or a
    # scalar/type mismatch). Arrays are compared in order; json_equal sorts the ones a spec
    # lists as `unordered` before calling this.
    if isinstance(actual, dict) and isinstance(expected, dict):
        akeys, ekeys = set(actual.keys()), set(expected.keys())
        for k in ekeys - akeys:
            return f"{path or '<root>'}: missing key {k!r}"
        for k in akeys - ekeys:
            return f"{path or '<root>'}: unexpected key {k!r}"
        for k in ekeys:
            sub = f"{path}.{k}" if path else k
            d = _diff_path(actual[k], expected[k], sub)
            if d is not None:
                return d
        return None

    if isinstance(actual, list) and isinstance(expected, list):
        if len(actual) != len(expected):
            return f"{path or '<root>'}: array length {len(actual)} != {len(expected)}"
        for i, (a, e) in enumerate(zip(actual, expected)):
            d = _diff_path(a, e, f"{path}[{i}]")
            if d is not None:
                return d
        return None

    # Typed: bool is an int subclass (True == 1) and 1 == 1.0, so equality alone would pass both.
    if type(actual) is not type(expected) or actual != expected:
        return f"{path or '<root>'} (expected {expected!r}, got {actual!r})"
    return None


def json_equal(actual, expected, unordered=()):
    paths = frozenset(_path_tokens(p) for p in unordered)
    d = _diff_path(_canon(actual, paths), _canon(expected, paths))
    return (d is None, d)


def _key_label(spec: dict, key_tuple: tuple) -> str:
    return ", ".join(f"{c}={v!r}" for c, v in zip(spec["key"], key_tuple))


def _plain(v):
    # The read path may hand back a JsonObject-like value (google-cloud-spanner) rather
    # than a plain dict/list; also DatetimeWithNanoseconds for TIMESTAMP columns.
    # Use json.dumps with default=str to serialize non-JSON-serializable types like
    # DatetimeWithNanoseconds, then json.loads back to plain types.
    if isinstance(v, (dict, list)):
        try:
            return json.loads(json.dumps(v, default=str))
        except (TypeError, ValueError):
            return v
    try:
        # For scalar types, try json.dumps with default=str to handle DatetimeWithNanoseconds
        return json.loads(json.dumps(v, default=str))
    except (TypeError, ValueError):
        return v


# --------------------------------------------------------------------------------------
# Golden loading
# --------------------------------------------------------------------------------------


def _load_expected(spec: dict, basedir, tokens: dict | None = None) -> dict:
    path = Path(basedir) / spec["match"]
    try:
        text = load_expected_text(path, tokens)
    except FileNotFoundError as e:
        raise JsonSpecError(f"{spec['target']}: golden file not found: {path}") from e
    def members(pairs):
        # json.loads keeps the last of two equal member names; a golden that says a thing twice
        # is refused rather than read as its second half.
        obj = {}
        for k, v in pairs:
            if k in obj:
                raise JsonSpecError(
                    f"{spec['target']}: golden {spec['match']} has a duplicate member {k!r} in one "
                    f"object")
            obj[k] = v
        return obj

    try:
        rows = json.loads(text, object_pairs_hook=members)
    except ValueError as e:
        raise JsonSpecError(f"{spec['target']}: golden {spec['match']} is not valid JSON: {e}") from e
    if not isinstance(rows, list):
        raise JsonSpecError(f"{spec['target']}: golden {spec['match']} must be a JSON array of row objects")
    if not rows:
        raise JsonSpecError(f"{spec['target']}: golden {spec['match']} is empty; must contain at least one row")

    expected = {}
    for elem in rows:
        if not isinstance(elem, dict):
            raise JsonSpecError(f"{spec['target']}: golden {spec['match']} elements must be objects")
        try:
            key_tuple = tuple(str(elem[k]) for k in spec["key"])
        except KeyError as e:
            raise JsonSpecError(
                f"{spec['target']}: golden {spec['match']} element missing key field {e.args[0]!r}: {elem!r}") from e
        if spec["column"] not in elem:
            raise JsonSpecError(
                f"{spec['target']}: golden {spec['match']} element missing column field "
                f"{spec['column']!r}: {elem!r}")
        if key_tuple in expected:
            n = sum(1 for r in rows if isinstance(r, dict)
                    and tuple(str(r.get(k)) for k in spec["key"]) == key_tuple)
            raise JsonSpecError(
                f"{spec['target']}: golden {spec['match']} key {_key_label(spec, key_tuple)} appears "
                f"{n} times; each key names one row")
        expected[key_tuple] = elem[spec["column"]]
    if spec.get("unordered"):
        present = set()
        for value in expected.values():
            _array_paths(value, (), present)
        for p in spec["unordered"]:
            if _path_tokens(p) not in present:
                raise JsonSpecError(
                    f"{spec['target']}: unordered path {p!r} names no array in golden "
                    f"{spec['match']} (column {spec['column']!r})")
    return expected


# --------------------------------------------------------------------------------------
# Assertion
# --------------------------------------------------------------------------------------


def _evaluate(admin, spec: dict, expected: dict):
    target = spec["target"]
    rows = admin.select_json_rows(target, spec["key"], spec["column"])
    actual, seen = {}, {}
    for key_tuple, value in rows:
        seen[key_tuple] = seen.get(key_tuple, 0) + 1
        actual[key_tuple] = _plain(value)
    for k, n in seen.items():
        if n > 1:
            return (False, f"{target}: key {_key_label(spec, k)} appears {n} times in the read; "
                           f"each key must name one row", expected, actual)

    akeys, ekeys = set(actual.keys()), set(expected.keys())
    for k in ekeys - akeys:
        return False, f"{target}: missing key {'.'.join(spec['key'])}={k!r}", expected, actual
    for k in akeys - ekeys:
        return False, f"{target}: unexpected key {'.'.join(spec['key'])}={k!r}", expected, actual

    for k in ekeys:
        ok, diff = json_equal(actual[k], expected[k], spec.get("unordered", ()))
        if not ok:
            return False, f"{target} key={k}: {diff}", expected, actual

    return True, None, expected, actual


def _format_json_failure(results: list, expecteds: list, specs: list = None) -> str:
    """Format expected vs actual JSON data for test failure output."""
    lines = []
    for i, ((ok, d, exp, act), exp_dict) in enumerate(zip(results, expecteds)):
        if not ok:
            lines.append("")
            # Add column name if specs provided
            if specs and i < len(specs):
                col_info = f" [column: {specs[i]['column']}]"
            else:
                col_info = ""
            lines.append(d + col_info)  # main error message with column name
            lines.append("")
            lines.append("  EXPECTED:")
            for k, v in sorted(exp_dict.items()):
                lines.append(f"    {k}: {json.dumps(v, indent=6)}")
            lines.append("")
            lines.append("  ACTUAL:")
            for k, v in sorted(act.items()):
                lines.append(f"    {k}: {json.dumps(v, indent=6)}")
    return "\n".join(lines)


def assert_json(admin, specs: list, basedir, timeout: int, poll: float = 2.0,
                 status_probe=None, *, db: str | None = None, progress=None,
                 tokens: dict | None = None) -> list:
    db_label = db
    if not hasattr(admin, "select_json_rows"):
        raise JsonSpecError(f"assert.json db={db_label!r} is not a JSON-capable target (need a Spanner admin)")

    expecteds = [_load_expected(s, basedir, tokens) for s in specs]
    deadline = time.monotonic() + timeout
    # stable_seconds (as in assert.data and assert.file): after every spec first passes, keep
    # reading for the longest window in the group; a read that no longer passes fails the run, so
    # a value overwritten by a still-running writer is read instead of missed. A read that raises
    # ends the assertion as it does before the first pass: it neither passes nor holds the window.
    stable_for = max((float(s.get("stable_seconds", 0)) for s in specs), default=0.0)
    passed_at = None
    while True:
        if status_probe:
            status_probe()
        results = [_evaluate(admin, s, exp) for s, exp in zip(specs, expecteds)]
        detail = next((d for ok, d, _, _ in results if not ok), None)
        if detail is not None and passed_at is not None:
            records = [
                build_assertion_result(type="json", status="passed" if ok else "failed", spec=s,
                                        target=s["target"], db=db, detail="ok" if ok else d,
                                        expected=None, actual=None)
                for s, (ok, d, _, _) in zip(specs, results)
            ]
            raise AssertionFailed(
                "json assertion passed, then failed within its stable_seconds window (a late write"
                f" changed the result):{_format_json_failure(results, expecteds, specs)}", records)
        if detail is None and stable_for > 0:
            now = time.monotonic()
            if passed_at is None:
                passed_at = now
            if now - passed_at < stable_for:
                time.sleep(min(poll or 0.05, max(0.0, stable_for - (now - passed_at))))
                continue
        if detail is None:
            return [
                build_assertion_result(type="json", status="passed", spec=s, target=s["target"], db=db,
                                        detail="ok", expected=None, actual=None)
                for s in specs
            ]
        if time.monotonic() >= deadline:
            records = [
                build_assertion_result(type="json", status="passed" if ok else "failed", spec=s,
                                        target=s["target"], db=db, detail="ok" if ok else d,
                                        expected=None, actual=None)
                for s, (ok, d, _, _) in zip(specs, results)
            ]
            detailed_msg = _format_json_failure(results, expecteds, specs)
            # Print detailed message to console so it's visible in test output
            print(f"\n{'='*80}\nJSON ASSERTION FAILURE DETAILS:{detailed_msg}\n{'='*80}\n")
            raise AssertionFailed(f"json assertion not satisfied within {timeout}s:{detailed_msg}", records)
        if progress:
            try:
                progress(timeout - (deadline - time.monotonic()), float(timeout))
            except Exception:
                pass
        time.sleep(poll or 0.05)   # floor avoids a busy-wait when poll=0 with a positive timeout
