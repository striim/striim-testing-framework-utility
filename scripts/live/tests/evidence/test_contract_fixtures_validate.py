"""The C4 case-envelope contract fixtures agree with the validator: the valid one is accepted, the invalid ones
are refused. (test_canon.py only checks their shape.)"""
import json
from pathlib import Path

import pytest

from livetest import evidence
from livetest.evidence import EvidenceError

CONTRACTS = Path(__file__).resolve().parents[1] / "fixtures" / "evidence" / "contracts"


def _doc(name):
    return json.loads((CONTRACTS / name).read_text())


def test_valid_case_fixture_validates():
    evidence.validate_case(_doc("evidence.case.valid.json"))


def test_partial_case_fixture_is_refused_as_partial():
    with pytest.raises(EvidenceError) as ei:
        evidence.validate_case(_doc("evidence.case.invalid-partial.json"))
    assert ei.value.code == "evidence-partial"


def test_reason_in_data_case_fixture_is_refused():
    # Refused today before its data rule is reached (the fixture predates runtime.striim.observed.image and
    # the resolved-release rule); the data-reason rule itself is covered with the envelope tests in 3.2.
    with pytest.raises(EvidenceError):
        evidence.validate_case(_doc("evidence.case.invalid-reason-in-data.json"))
