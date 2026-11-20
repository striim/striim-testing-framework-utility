"""assert.monitor's SUBSET comparison (manifest._SUBSET_METRICS): a JSON object's named keys."""

from __future__ import annotations


def subset_mismatches(expected, actual, path: str = "") -> list:
    """Every way `actual` differs from `expected` on the keys `expected` names, recursively.

    A mapping in `expected` requires `actual` to be a mapping holding each of its keys, compared
    in turn; keys `expected` does not name are ignored. An EMPTY mapping requires an empty object
    (`IgnoredByCode: {}` reads as "nothing was ignored", so it must not pass against
    `{"23505": 7}`), and a key whose expected value is `null` must be absent. A list requires a
    list of the same length, compared element by element. Anything else must be equal (a number
    in the case equals the same number published; `True` is not `1`). Returns readable lines;
    empty when they agree.
    """
    where = path or "<root>"
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{where}: expected an object, published {actual!r}"]
        if not expected:
            return [] if not actual else [f"{where}: expected an empty object, published {actual!r}"]
        out = []
        for key, want in expected.items():
            sub = f"{path}.{key}" if path else str(key)
            if want is None:
                if key in actual:
                    out.append(f"{sub}: expected absent, published {actual[key]!r}")
            elif key not in actual:
                out.append(f"{sub}: missing (published keys: {sorted(actual)})")
            else:
                out.extend(subset_mismatches(want, actual[key], sub))
        return out
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            count = f"{len(actual)}: " if isinstance(actual, list) else ""
            return [f"{where}: expected {len(expected)} element(s), published {count}{actual!r}"]
        out = []
        for i, (want, got) in enumerate(zip(expected, actual)):
            out.extend(subset_mismatches(want, got, f"{path}[{i}]"))
        return out
    if isinstance(expected, bool) or isinstance(actual, bool):
        same = type(expected) is type(actual) and expected == actual
    elif isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        same = expected == actual
    else:
        same = expected == actual
    return [] if same else [f"{where}: expected {expected!r}, published {actual!r}"]


def render_strings(value, render):
    """`value` with `render` applied to every string leaf (tokens), keys left as written."""
    if isinstance(value, dict):
        return {k: render_strings(v, render) for k, v in value.items()}
    if isinstance(value, list):
        return [render_strings(v, render) for v in value]
    return render(value) if isinstance(value, str) else value
