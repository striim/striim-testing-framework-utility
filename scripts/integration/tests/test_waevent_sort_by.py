"""`sort_by:` — comparing an operator whose output is a SET rather than a sequence.

Added for a change-data reader's initial-load tier: it snapshots with `SELECT *` and no
`ORDER BY`, so its row order is unspecified by contract, while `waevent.compare` is
positional. Before this, a multi-row snapshot could not be asserted at all.

The rejected alternative was a bare `unordered: true` multiset compare. It needs no key,
but it cannot say WHICH event differs -- only that the bags do -- and it would let a case
stay green while the operator's output order became genuinely arbitrary. Naming the key
keeps the diff positional and makes the case state what it believes.
"""
import pytest

from inttest import waevent


def ev(cid, name=None, meta=None):
    return {
        "kind": "waevent",
        "metadata": meta or {"TableName": "T"},
        "data": {"values": [cid, name], "present": [True, True]},
        "userdata": {},
    }


def test_sorts_both_sides_so_row_order_stops_mattering():
    emitted = [ev(2, "b"), ev(3, "c"), ev(1, "a")]
    expected = [ev(1, "a"), ev(2, "b"), ev(3, "c")]
    with pytest.raises(waevent.WAEventMismatch):
        waevent.compare(emitted, expected)          # positional: fails, as it always did
    waevent.compare(emitted, expected, sort_by=["data[0]"])   # keyed: passes


def test_a_real_difference_still_fails_after_sorting():
    # ⚠ The one that matters. Sorting must not become a way to pass -- it lines events up,
    # it does not soften the comparison.
    emitted = [ev(2, "b"), ev(1, "WRONG")]
    expected = [ev(1, "a"), ev(2, "b")]
    with pytest.raises(waevent.WAEventMismatch) as e:
        waevent.compare(emitted, expected, sort_by=["data[0]"])
    assert "data.values[1]" in str(e.value)


def test_sorts_by_a_metadata_key():
    emitted = [ev(1, "a", {"TableName": "B"}), ev(1, "a", {"TableName": "A"})]
    expected = [ev(1, "a", {"TableName": "A"}), ev(1, "a", {"TableName": "B"})]
    waevent.compare(emitted, expected, sort_by=["metadata.TableName"])


def test_mixed_value_types_do_not_crash_the_sort():
    # A column holding an int in one event and a string in another is not comparable in
    # Python; the key is type-tagged so this yields a total order instead of TypeError.
    emitted = [ev("x", "a"), ev(1, "b")]
    expected = [ev(1, "b"), ev("x", "a")]
    waevent.compare(emitted, expected, sort_by=["data[0]"])


def test_a_missing_value_sorts_stably_rather_than_crashing():
    emitted = [ev(1, "a"), {"kind": "waevent", "metadata": {}, "data": {"values": [], "present": []}, "userdata": {}}]
    expected = [{"kind": "waevent", "metadata": {}, "data": {"values": [], "present": []}, "userdata": {}}, ev(1, "a")]
    waevent.compare(emitted, expected, sort_by=["data[0]"])


def test_a_multi_key_sort_uses_the_keys_in_order():
    emitted = [ev(1, "b"), ev(1, "a")]
    expected = [ev(1, "a"), ev(1, "b")]
    waevent.compare(emitted, expected, sort_by=["data[0]", "data[1]"])


def test_count_mismatch_is_still_reported_before_sorting():
    with pytest.raises(waevent.WAEventMismatch) as e:
        waevent.compare([ev(1, "a")], [ev(1, "a"), ev(2, "b")], sort_by=["data[0]"])
    assert "event count mismatch" in str(e.value)


def test_an_unreachable_sort_key_is_rejected():
    # ⚠ Load-bearing. A key that has silently stopped matching would sort both sides by a
    # constant -- quietly restoring the positional comparison and passing for the wrong reason.
    with pytest.raises(Exception) as e:
        waevent.compare([ev(1, "a")], [ev(1, "a")], sort_by=["metadata.NoSuchKey"])
    assert "sort_by" in str(e.value)


# ---------------------------------------------------------------------------
# Guards added after review: the two ways sort_by could pass while asserting nothing
# ---------------------------------------------------------------------------

def test_a_key_missing_from_expected_is_rejected():
    """⚠ `_resolve_paths` alone does NOT catch this: it raises only when no EMITTED event matches,
    consulting `expected` purely for the error text. A key present in emitted and absent from
    expected leaves the expected side keyed on ("", "") -- in fixture order -- while the emitted
    side is genuinely sorted, which is the sorted-by-a-constant state the check claims to stop."""
    emitted = [ev(2, "b"), ev(1, "a")]
    expected = [{"kind": "waevent", "metadata": {"TableName": "T"},
                 "data": {"values": [], "present": []}, "userdata": {}} for _ in range(2)]
    with pytest.raises(waevent.WAEventMismatch) as e:
        waevent.compare(emitted, expected, sort_by=["data[0]"])
    assert "matches nothing in the expected events" in str(e.value)


def test_a_repeated_sort_key_names_itself_instead_of_mis_pairing():
    """A non-discriminating key pairs events by fixture order -- the comparison sort_by exists to
    replace -- and fails on a VALUE diff that reads like an operator bug. It must name the real
    problem instead."""
    emitted = [ev(1, "x"), ev(1, "y")]
    expected = [ev(1, "y"), ev(1, "x")]
    with pytest.raises(waevent.WAEventMismatch) as e:
        waevent.compare(emitted, expected, sort_by=["data[0]"])
    assert "does not discriminate" in str(e.value)


def test_a_composite_key_rescues_a_repeated_first_key():
    emitted = [ev(1, "x"), ev(1, "y")]
    expected = [ev(1, "y"), ev(1, "x")]
    waevent.compare(emitted, expected, sort_by=["data[0]", "data[1]"])
