"""waevent.compare is type-strict: bool is not int, an integer is not a decimal, decimals are exact."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pytest

from inttest import harness, waevent


def _one(value_json: str, section: str = "data") -> list[dict]:
    if section == "data":
        text = f'[{{"metadata": {{}}, "data": {{"values": [{value_json}], "present": [true]}}}}]'
    else:
        text = f'[{{"metadata": {{"K": {value_json}}}}}]'
    return waevent.load(text)


@pytest.mark.parametrize("section", ["data", "metadata"])
@pytest.mark.parametrize("emitted,expected", [
    ("true", "1"),
    ("1", "true"),
    ("false", "0"),
    ("5.0", "5"),
    ("5", "5.0"),
    ("12345678901234567890.12", "12345678901234567890.13"),
    ('"5"', "5"),
])
def test_a_different_type_or_digit_is_a_mismatch(section, emitted, expected):
    with pytest.raises(waevent.WAEventMismatch):
        waevent.compare(_one(emitted, section), _one(expected, section))


@pytest.mark.parametrize("emitted,expected", [
    ("true", "true"),
    ("5", "5"),
    ("12345678901234567890.12", "12345678901234567890.12"),
    ("1.5", "1.50"),
    ("1.0E10", "10000000000.0"),
    ('"x"', '"x"'),
    ("null", "null"),
])
def test_the_same_type_and_value_matches(emitted, expected):
    waevent.compare(_one(emitted), _one(expected))


def test_a_float_compares_exactly_against_a_decimal():
    # The perf tier's records are parsed as float; 0.1 must still equal a fixture's 0.1.
    emitted = [{"metadata": {}, "data": {"values": [0.1], "present": [True]}}]
    waevent.compare(emitted, _one("0.1"))
    with pytest.raises(waevent.WAEventMismatch):
        waevent.compare(emitted, _one("0.10000000000000001"))


def test_nested_userdata_is_compared_type_strictly():
    emitted = [{"metadata": {}, "userdata": {"k": [1, {"b": True}]}}]
    expected = [{"metadata": {}, "userdata": {"k": [1, {"b": 1}]}}]
    with pytest.raises(waevent.WAEventMismatch):
        waevent.compare(emitted, expected)


def test_load_keeps_decimals_and_dump_writes_them_back_digit_for_digit():
    text = '[{"metadata": {"n": 12345678901234567890.12}, "data": {"values": [1.10, 2], "present": [true, true]}}]'
    events = waevent.load(text)
    assert events[0]["metadata"]["n"] == Decimal("12345678901234567890.12")
    assert json.loads(waevent.dump(events), parse_float=Decimal) == events
    assert "12345678901234567890.12" in waevent.dump(events)
    assert "1.10" in waevent.dump(events)


def test_drive_sends_input_decimals_exactly_and_parses_output_as_decimal(tmp_path):
    op_jar = tmp_path / "op.jar"
    op_jar.write_text("")
    sent = {}

    def fake_run(argv, **kwargs):
        req = json.loads(Path(argv[-1]).read_text())
        sent["input"] = Path(req["inputFile"]).read_text()
        Path(req["outputFile"]).write_text(
            '[{"metadata": {}, "data": {"values": [12345678901234567890.13], "present": [true]}}]')
        return mock.MagicMock(returncode=0, stdout="", stderr="")

    with mock.patch("inttest.harness.ensure_harness_jar", return_value=tmp_path / "h.jar"), \
         mock.patch("inttest.harness._resolve_java", return_value="java"), \
         mock.patch("subprocess.run", side_effect=fake_run):
        emitted = harness.drive(op_jar, {}, _one("12345678901234567890.12"))

    assert "12345678901234567890.12" in sent["input"]
    assert emitted[0]["data"]["values"] == [Decimal("12345678901234567890.13")]
    with pytest.raises(waevent.WAEventMismatch):
        waevent.compare(emitted, _one("12345678901234567890.12"))
