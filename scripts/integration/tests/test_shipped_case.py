"""The integration case the framework ships: the one docs/INTEGRATION-TESTS.md walks through, in
the tier's default case root, so `striim-test run --tier integration` in the clone runs it. The
doc's copy and the shipped files must stay the same case."""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from inttest.manifest import load_manifest

ROOT = Path(__file__).resolve().parents[3]
CASE = ROOT / "scripts/integration/regression/op/referenceop/referenceop-adds-processed"
DOC = ROOT / "docs/INTEGRATION-TESTS.md"


def _first_case_section() -> str:
    text = DOC.read_text()
    start = text.index("## Your first case")
    return text[start:text.index("\n## ", start + 1)]


def test_the_shipped_case_loads_and_names_the_sample_module():
    m = load_manifest(CASE / "test.yaml")
    assert m.name == "referenceop-adds-processed"
    assert (ROOT / "samples/code/op/java/OpenProcessors/ReferenceOp/pom.xml").is_file()
    assert sorted(p.relative_to(CASE).as_posix() for p in CASE.rglob("*") if p.is_file()) == [
        "expected/events.json", "input/events.json", "test.yaml"]


def test_the_doc_shows_the_shipped_case_verbatim():
    section = _first_case_section()
    blocks = re.findall(r"```(yaml|json)\n(.*?)```", section, re.S)
    [doc_yaml] = [b for kind, b in blocks if kind == "yaml"]
    doc_input, doc_expected = [json.loads(b) for kind, b in blocks if kind == "json"]
    assert yaml.safe_load(doc_yaml) == yaml.safe_load((CASE / "test.yaml").read_text())
    assert doc_input == json.loads((CASE / "input/events.json").read_text())
    assert doc_expected == json.loads((CASE / "expected/events.json").read_text())
    assert "scripts/integration/regression/op/referenceop/referenceop-adds-processed/" in section


def test_the_golden_is_the_input_with_the_stamp():
    """Derived by hand: ReferenceOp copies each event and sets userdata.processed to "true"."""
    [event] = json.loads((CASE / "input/events.json").read_text())
    [expected] = json.loads((CASE / "expected/events.json").read_text())
    assert expected == {**event, "userdata": {**event["userdata"], "processed": "true"}}


def test_agents_points_at_the_shipped_case():
    assert "`scripts/integration/regression/`" in (ROOT / "AGENTS.md").read_text()
