"""WAEvent-JSON fixture load/dump and semantic comparison (docs/INTEGRATION-TESTS.md).

`load`/`dump` are the thin (de)serialization half of the compact WAEvent-JSON
fixture format also implemented Java-side by `WAEventJsonFactory`
(scripts/integration/java/.../WAEventJsonFactory.java) -- this module never
constructs a real `WAEvent`; it works directly on the parsed JSON `dict`/`list`
shapes, which is all Python-side comparison needs.

`compare` uses the following comparison rules:

- event order is significant; a length mismatch fails immediately;
- per event, `metadata`/`userdata` compare as maps (key-order-independent);
  `data`/`before` compare as `{values[], present[]}` -- `present[]`
  element-wise AND `values[]` element-wise, null distinct from absent;
  a section present on only one side (e.g. one event has `before`, the
  other doesn't) is itself a diff;
- the FIRST differing path is reported: event index, section, and column
  index or map key, expected-vs-got.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path


class WAEventMismatch(AssertionError):
    """Raised by `compare` naming the first differing path between emitted and
    expected WAEvent arrays. Subclasses AssertionError so pytest renders it
    like any other assertion failure."""


def load(path_or_str) -> list[dict]:
    """Parse a WAEvent-JSON fixture (docs/INTEGRATION-TESTS.md) into a list of event dicts.

    Accepts a `Path`/`str` pointing at an existing file, or a raw JSON string
    (a `str` that is not an existing path is parsed directly)."""
    if isinstance(path_or_str, Path):
        text = path_or_str.read_text()
    elif isinstance(path_or_str, str):
        candidate = Path(path_or_str)
        try:
            text = candidate.read_text() if candidate.is_file() else path_or_str
        except (OSError, ValueError):
            # OSError: path string is too long to be a valid filename (e.g., JSON)
            # ValueError: path contains invalid characters
            text = path_or_str
    else:
        raise TypeError(f"load() expects a path or JSON string, got {type(path_or_str)!r}")

    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError(f"WAEvent JSON fixture must be a top-level array; got {type(data).__name__}")
    return data


def dump(events: list[dict]) -> str:
    """Serialize a list of event dicts back to the compact WAEvent-JSON array form."""
    return json.dumps(list(events))


def _describe(value) -> str:
    return "null" if value is None else repr(value)


def _compare_map(event_idx: int, section: str, emitted: dict | None, expected: dict | None,
                 *, ignored=frozenset(), projected=None) -> None:
    emitted = emitted or {}
    expected = expected or {}
    for key in sorted(set(emitted) | set(expected)):
        if key in ignored:
            continue
        if projected is not None and key not in projected:
            continue
        if key not in expected:
            raise WAEventMismatch(
                f"event {event_idx}: {section} has unexpected key {key!r} (got {_describe(emitted[key])}, "
                f"expected key to be absent)")
        if key not in emitted:
            raise WAEventMismatch(
                f"event {event_idx}: {section} is missing key {key!r} (expected {_describe(expected[key])})")
        if emitted[key] != expected[key]:
            raise WAEventMismatch(
                f"event {event_idx}: {section}[{key!r}]: expected {_describe(expected[key])}, "
                f"got {_describe(emitted[key])}")


def _presence_of(section: str) -> str:
    return "present" if section is not None else "absent"


def _compare_section(event_idx: int, section: str, emitted: dict | None, expected: dict | None,
                     *, ignored=frozenset(), projected=None) -> None:
    emitted_present = emitted is not None
    expected_present = expected is not None
    if emitted_present != expected_present:
        raise WAEventMismatch(
            f"event {event_idx}: section '{section}' is {_presence_of(emitted)} in the emitted event but "
            f"{_presence_of(expected)} in the expected event")
    if not expected_present:
        return  # both sides omit this section entirely -- nothing to compare

    e_values = emitted.get("values", [])
    e_presence = emitted.get("present", [])
    x_values = expected.get("values", [])
    x_presence = expected.get("present", [])

    if projected is None:
        if len(e_values) != len(x_values) or len(e_presence) != len(x_presence):
            raise WAEventMismatch(
                f"event {event_idx}: section '{section}' length mismatch: expected "
                f"{len(x_values)} values / {len(x_presence)} present, got "
                f"{len(e_values)} values / {len(e_presence)} present")
    else:
        # Under a projection the section's LENGTH is out of scope too -- otherwise "only these
        # columns are deterministic" could not be said about an operator whose other columns vary
        # in shape, which is the motivating case. What must still hold is that every projected
        # index exists on BOTH sides: a projection naming a column that isn't there is asserting
        # nothing, and silently.
        for i in sorted(projected):
            if i >= len(x_values) or i >= len(e_values):
                raise WAEventMismatch(
                    f"event {event_idx}: projected index {section}[{i}] is out of range "
                    f"(emitted has {len(e_values)} value(s), expected has {len(x_values)}) -- "
                    f"a projection must address something on both sides")

    for i in range(min(len(x_values), len(e_values)) if projected is not None else len(x_values)):
        # A non-projected index is out of scope ENTIRELY -- value and presence both. That is
        # what "compare only these fields" has to mean: an op whose other columns vary in
        # shape (a reader's, say) could not use `project:` at all if their presence were
        # still asserted. Deliberately NOT symmetric with `ignore_fields`, which suppresses
        # only the value and keeps presence -- see compare()'s docstring for why the two
        # differ.
        if projected is not None and i not in projected:
            continue
        if bool(e_presence[i]) != bool(x_presence[i]):
            raise WAEventMismatch(
                f"event {event_idx}: {section}.present[{i}]: expected {bool(x_presence[i])}, "
                f"got {bool(e_presence[i])}")
        if i in ignored:
            continue    # value suppressed, presence above still compared
        if e_values[i] != x_values[i]:
            raise WAEventMismatch(
                f"event {event_idx}: {section}.values[{i}]: expected {_describe(x_values[i])}, "
                f"got {_describe(e_values[i])}")


_MAP_SECTIONS = ("metadata", "userdata")
_ARRAY_SECTIONS = ("data", "before")

#: `metadata.<key>` / `userdata.<key>`, and `data[<i>]` / `before[<i>]`. Anchored so a
#: half-formed path (`data[1`, `metadata.`) is rejected rather than partially matched.
# \Z not $, because `$` also matches before a trailing newline -- "data[0]\n" would parse.
# [0-9] not \d, because Python's \d is Unicode-wide: "data[٣]" (Arabic-Indic 3) would parse as 3
# and then address a column nobody typed.
_MAP_PATH = re.compile(r"\A(metadata|userdata)\.(.+)\Z", re.DOTALL)
_ARRAY_PATH = re.compile(r"\A(data|before)\[([0-9]+)\]\Z")

PATH_GRAMMAR = ("metadata.<key>, userdata.<key>, data[<index>] or before[<index>] "
                "(no wildcards)")


def parse_field_path(path: str):
    """Parse one `ignore_fields:`/`project:` entry into `(section, key_or_index)`.

    Returns `(section, str_key)` for a map path and `(section, int_index)` for an array
    path. Raises `ValueError` naming the grammar for anything else -- callers surface that
    at manifest-load time, so a typo fails before a single container starts rather than
    quietly matching nothing.
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError(f"field path must be a non-empty string, got {path!r}; "
                         f"expected {PATH_GRAMMAR}")
    m = _MAP_PATH.match(path)
    if m:
        section, key = m.group(1), m.group(2)
        if not key:
            raise ValueError(f"field path {path!r} names no key; expected {PATH_GRAMMAR}")
        return section, key
    m = _ARRAY_PATH.match(path)
    if m:
        return m.group(1), int(m.group(2))
    raise ValueError(f"unparseable field path {path!r}; expected {PATH_GRAMMAR}")


def _matches_event(event: dict, section: str, key) -> bool:
    """True when this event actually carries the addressed field.

    Reachability, not equality: it answers "is this path real", which is what makes an
    unmatched path an error instead of a silent weakening of the assertion.
    """
    if section in _MAP_SECTIONS:
        return key in (event.get(section) or {})
    body = event.get(section)
    if body is None:
        return False
    return 0 <= key < len(body.get("values", []))


def _resolve_paths(kind: str, paths, emitted: list[dict], expected: list[dict]) -> dict:
    """Parse every path and prove each one addresses something IN THE EMITTED ARRAY.

    A path that matches no emitted event raises. That is the whole point of this machinery
    being strict: `ignore_fields:`/`project:` weaken an assertion by design, so a path that
    has silently stopped matching would leave a case green while asserting less than its
    author believes.

    Deliberately checked against the EMITTED side, not "either side". An earlier revision
    accepted a match in either array, which let the worst case through: when an operator
    STOPS emitting a field, a stale fixture still names it, so the path resolved against
    `expected`, and -- because an ignored key also suppresses the missing-key check -- the
    dropped field was reported as nothing at all. Emitted is the live data; a path that
    addresses nothing there is either stale or a typo, and both should fail.

    Matching at least ONE emitted event is the bar, so a fixture whose events differ in
    shape still works.
    """
    resolved: dict = {}
    for raw in paths:
        section, key = parse_field_path(raw)      # ValueError -> caller's error message
        if not any(_matches_event(ev, section, key) for ev in emitted):
            in_expected = any(_matches_event(ev, section, key) for ev in expected)
            detail = (" It DOES match the expected array, so the operator has stopped emitting "
                      "it -- which is a real change, not something to ignore."
                      if in_expected else
                      " It addresses nothing, so it is silently weakening this assertion.")
            raise WAEventMismatch(
                f"{kind} path {raw!r} matched no EMITTED event.{detail} "
                f"Fix the path or remove it.")
        resolved.setdefault(section, set()).add(key)
    return resolved


def _require_sort_key_present(which: str, events: list[dict], sort_by) -> None:
    """Every sort path must address something in `events`, or the sort is a no-op on that side."""
    for path in sort_by:
        if all(_sort_key_value(e, path) == ("", "") for e in events):
            raise WAEventMismatch(
                f"sort_by {path!r} matches nothing in the {which} events, so that side would keep "
                f"its fixture order while the other is sorted -- which silently restores the "
                f"positional comparison sort_by exists to escape")


def _require_discriminating(emitted: list[dict], expected: list[dict], sort_by) -> None:
    """A sort key that repeats cannot line events up, and must say so rather than mis-pair them.

    ⚠ Python's sort is stable, so events sharing a key keep their relative order -- and an earlier
    docstring called that a graceful "degrades to the old positional behaviour". For a set-output
    operator, positional behaviour is exactly what `sort_by` exists to escape, so the degradation
    is a flake, not a fallback: two events with the same key get compared against the wrong
    partners and the case fails on a VALUE diff that looks like an operator bug. Naming the real
    problem is cheap; debugging the value diff is not.
    """
    for which, events in (("emitted", emitted), ("expected", expected)):
        keys = [tuple(_sort_key_value(e, p) for p in sort_by) for e in events]
        if len(set(keys)) != len(keys):
            # Counter, not `keys.count(k)` in a comprehension -- that is O(n^2) over the same list.
            # It only ever runs on the failure path and no large-n caller exists today
            # (compare_indexed takes no sort_by, and the perf tier calls compare without one), so
            # this is cheapness rather than a fix.
            counts = Counter(keys)
            dupes = sorted(k for k, n in counts.items() if n > 1)
            raise WAEventMismatch(
                f"sort_by {list(sort_by)} does not discriminate the {which} events -- "
                f"{len(keys) - len(set(keys))} share a key (e.g. {dupes[0]}). Add a path that "
                f"makes each event unique; a repeated key pairs events by fixture order, which is "
                f"the comparison sort_by is meant to replace")


def _sort_key_value(event: dict, path: str):
    """One event's value at `path`, as a type-tagged tuple.

    The tag is what makes this safe: a column may hold an int in one event and a string in
    another (or be absent), and Python raises TypeError comparing those. Tagging by type name
    first gives a total order without pretending the values are comparable -- events group by
    type, then sort within it. `None` sorts first under the tag "", which keeps a missing value
    stable rather than crashing the sort.
    """
    section, key = parse_field_path(path)
    blob = event.get(section)
    value = None
    if isinstance(blob, dict):
        if section in _MAP_SECTIONS:
            value = blob.get(key)
        else:
            values = blob.get("values")
            if isinstance(values, list) and 0 <= key < len(values):
                value = values[key]
    if value is None:
        return ("", "")
    return (type(value).__name__, str(value))


def _sorted_events(events: list[dict], sort_by) -> list[dict]:
    """Both sides ordered by the same key tuple.

    A repeated key never reaches here -- `_require_discriminating` rejects it first, because
    stability would silently pair those events by fixture order."""
    return sorted(events, key=lambda e: tuple(_sort_key_value(e, p) for p in sort_by))


def compare(emitted: list[dict], expected: list[dict], *,
            ignore_fields=None, project=None, sort_by=None) -> None:
    """Semantically compare `emitted` against `expected` (docs/INTEGRATION-TESTS.md).

    `ignore_fields` drops the named fields from the comparison; `project` restricts the
    comparison to them. They are mutually exclusive (the manifest layer rejects both
    together, and this asserts it again for direct callers). Each is a list of paths in the
    grammar `parse_field_path` defines.

    The two are deliberately NOT mirror images, because they answer different questions:

    * `ignore_fields` says "this field is volatile". It suppresses the VALUE check only --
      `present[i]` is still compared for an ignored index, because a wall-clock column
      varies in value and never in presence, and presence is where this fleet's bitmap bugs
      live. The rest of the event is asserted exactly as normal.
    * `project` says "only these fields are deterministic". Everything else is out of scope
      entirely -- value AND presence -- because an operator whose other columns vary in
      shape could not use a projection at all if their presence were still asserted.

    Both are validated for reachability against the EMITTED array (see `_resolve_paths`).

    `sort_by` orders BOTH sides by the named paths before comparing, for an operator whose
    output is a SET rather than a sequence -- e.g. a reader's initial-load snapshot that reads
    with `SELECT *` and no `ORDER BY`, so its row order is unspecified. It is orthogonal to the
    other two: they choose which FIELDS are compared, this chooses how the EVENTS are lined up.

    Raises `WAEventMismatch` naming the first differing path. Returns `None`
    when the arrays are fully equal."""
    if ignore_fields and project:
        raise ValueError("ignore_fields and project are mutually exclusive")

    if len(emitted) != len(expected):
        raise WAEventMismatch(
            f"event count mismatch: expected {len(expected)} event(s), got {len(emitted)}")

    ignored = _resolve_paths("ignore_fields", ignore_fields or [], emitted, expected)
    projected = _resolve_paths("project", project or [], emitted, expected) if project else None

    if sort_by:
        # ⚠ Reachability in BOTH arrays, and this is stricter than `_resolve_paths` on purpose.
        # That helper raises only when NO EMITTED event matches -- `expected` feeds its error
        # text and nothing else. A key present in emitted and absent from every expected event
        # would therefore pass it, after which the expected side keys entirely on ("", "") and
        # keeps fixture order while the emitted side is genuinely sorted: precisely the
        # sorted-by-a-constant state the check is supposed to prevent. A review caught the
        # earlier version claiming that protection without providing it.
        _resolve_paths("sort_by", sort_by, emitted, expected)
        _require_sort_key_present("expected", expected, sort_by)
        _require_discriminating(emitted, expected, sort_by)
        emitted = _sorted_events(emitted, sort_by)
        expected = _sorted_events(expected, sort_by)

    for i, (e, x) in enumerate(zip(emitted, expected)):
        for section in _MAP_SECTIONS:
            _compare_map(i, section, e.get(section), x.get(section),
                         ignored=ignored.get(section, set()),
                         projected=None if projected is None else projected.get(section, set()))
        for section in _ARRAY_SECTIONS:
            if projected is not None and not projected.get(section):
                continue    # projection names nothing here: the section is out of scope whole
            _compare_section(i, section, e.get(section), x.get(section),
                             ignored=ignored.get(section, set()),
                             projected=None if projected is None else projected.get(section, set()))


def compare_indexed(pairs: list[tuple[int, dict]], expected: list[dict]) -> None:
    """Semantically compare a sparse subset of records against `expected`
    (PERF_SPEC.md §6 'sampled' mode): each `(absolute_index, emitted_record)` pair is
    checked against `expected[absolute_index]`, using the same field-by-field
    semantics as `compare()`.

    Raises `WAEventMismatch` naming the mismatch at its ABSOLUTE index (never the
    pair's position within `pairs`) on the first mismatch -- per PERF_SPEC.md §6,
    "mismatches are reported at the original absolute index, never the sample
    ordinal." Returns `None` when every pair matches."""
    for absolute_index, emitted_record in pairs:
        if absolute_index < 0 or absolute_index >= len(expected):
            raise WAEventMismatch(
                f"event {absolute_index}: sampled index out of range for expected "
                f"(expected has {len(expected)} record(s))")
        x = expected[absolute_index]
        _compare_map(absolute_index, "metadata", emitted_record.get("metadata"), x.get("metadata"))
        _compare_map(absolute_index, "userdata", emitted_record.get("userdata"), x.get("userdata"))
        _compare_section(absolute_index, "data", emitted_record.get("data"), x.get("data"))
        _compare_section(absolute_index, "before", emitted_record.get("before"), x.get("before"))
