"""A per-case teardown that fails is visible: on the terminal, in a summary section at the end of
the run, and counted in the case record (``teardownLeaks``). Before, it was a captured print that
only ``-rP`` showed and no record carried. The case keeps its outcome."""
from __future__ import annotations

import json

from tests.test_plugin_exec import EVENT, run_case

PG_DATA = """
name: leaves-tables
purpose: p
op: {jar: java/OpenProcessors/X}
requires: [postgres]
assert:
  data:
    - input: in.json
      match: out.json
"""
FILES = {"in.json": json.dumps([EVENT]), "out.json": json.dumps([EVENT])}
SECTION = "integration teardown left objects behind"
LEAK = "failed to reset Postgres test objects for leaves-tables"


def _run(pytester, monkeypatch, tmp_path, fails):
    store = tmp_path / "records"
    monkeypatch.setenv("SLT_CASE_RECORDS", str(store))
    monkeypatch.setenv("SLT_PROJECT_ROOT", str(pytester.path))
    scenario = {"returns": [EVENT], "teardown_reset_fails": fails}
    result, failure, _ = run_case(pytester, monkeypatch, tmp_path, PG_DATA, scenario, FILES)
    [path] = store.glob("*/main.jsonl")
    item = next(r for r in map(json.loads, path.read_text().splitlines()) if r["kind"] == "item")
    return result, failure, item


def test_a_failed_teardown_is_shown_and_counted_and_the_case_keeps_its_outcome(pytester, monkeypatch, tmp_path):
    result, failure, item = _run(pytester, monkeypatch, tmp_path, True)
    assert failure is None
    result.assert_outcomes(passed=1)
    assert result.ret == 0
    out = result.stdout.str()
    assert SECTION in out, out[-1500:]
    shown, summary = out[:out.index(SECTION)], out[out.index(SECTION):]
    assert LEAK in shown, "on the terminal as it happens, not only in captured output"
    assert ("case/test.yaml::leaves-tables: failed to reset Postgres test objects: "
            "RuntimeError('connection to server lost during teardown')") in summary
    assert "1 case(s) left objects behind" in summary
    assert item["status"] == "passed"
    assert item["teardownLeaks"] == [{"step": "reset Postgres test objects",
                                      "error": "RuntimeError('connection to server lost during teardown')"}]


def test_a_clean_teardown_adds_no_section_and_records_no_leak(pytester, monkeypatch, tmp_path):
    result, failure, item = _run(pytester, monkeypatch, tmp_path, False)
    assert failure is None and result.ret == 0
    assert SECTION not in result.stdout.str()
    assert item["teardownLeaks"] == []
