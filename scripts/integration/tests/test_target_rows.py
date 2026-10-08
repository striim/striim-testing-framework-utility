"""Unit tests for plugin._jsonable_rows -- the normalization every `assert.target:` comparison
goes through, and the reason a target fixture can state a value exactly.

These matter more than their size suggests. T2b's temporal cases compare the value READ BACK
against the value written, and its decimal cases assert exact digits; a lossy conversion here
would make both of those quietly unable to fail.
"""
from __future__ import annotations

import datetime as dt
import decimal

from inttest.plugin import _jsonable_rows


def test_primitives_pass_through():
    assert _jsonable_rows([[1, "a", None, True, 1.5]]) == [[1, "a", None, True, 1.5]]


def test_decimal_becomes_a_string_keeping_its_scale():
    # float(Decimal("1.10")) is 1.1 -- the trailing zero, and with it the column's scale, is gone.
    # This writer REFUSES a decimal that does not fit the column's scale (§43.25), so the exact
    # digits are the assertion.
    assert _jsonable_rows([[decimal.Decimal("1.10")]]) == [["1.10"]]
    assert _jsonable_rows([[decimal.Decimal("0.1234567890123456789012345")]]) == \
        [["0.1234567890123456789012345"]]


def test_naive_and_aware_timestamps_are_distinguishable():
    # The whole point of a non-UTC round trip: a dropped offset must not compare equal to the
    # same wall clock with one.
    naive = dt.datetime(2026, 8, 29, 9, 30)
    aware = dt.datetime(2026, 8, 29, 9, 30, tzinfo=dt.timezone(dt.timedelta(hours=9)))
    rows = _jsonable_rows([[naive, aware]])
    assert rows == [["2026-08-29T09:30:00", "2026-08-29T09:30:00+09:00"]]
    assert rows[0][0] != rows[0][1]


def test_date_and_time_keep_their_own_shapes():
    assert _jsonable_rows([[dt.date(2026, 8, 29), dt.time(23, 59, 59, 123000)]]) == \
        [["2026-08-29", "23:59:59.123000"]]


def test_milliseconds_survive_a_time_value():
    # Every TIME column lost its milliseconds once already (see the deferred-defect table).
    assert _jsonable_rows([[dt.time(1, 2, 3, 456000)]]) == [["01:02:03.456000"]]


def test_bytes_become_hex():
    assert _jsonable_rows([[b"\x00\xff", memoryview(b"\x01")]]) == [["00ff", "01"]]


def test_unknown_types_stringify_rather_than_raise():
    class Opaque:
        def __str__(self):
            return "opaque"

    assert _jsonable_rows([[Opaque()]]) == [["opaque"]]


def test_no_rows_is_an_empty_list_not_an_error():
    # The shape a case asserting "the writer wrote nothing" needs.
    assert _jsonable_rows([]) == []
