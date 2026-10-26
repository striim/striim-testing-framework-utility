"""The real IntYamlItem.runtest pass/fail decisions, run by pytester against fakes at the edges.

`harness.drive`, the database routes, service bring-up, the op build and the tokens are faked in
the child's conftest; everything between them -- expect_error, the target-row assertion, the
WAEvent comparison and the failure text -- is the plugin's own code.
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import pytest

CONFTEST = r'''
import json, os
from pathlib import Path

import pytest

from inttest import dbroutes as D, harness as H, opartifacts as O, plugin as P

SC = json.loads(os.environ["XINT_SCENARIO"])
LOG = os.environ["XINT_LOG"]


def log(event, **kw):
    with open(LOG, "a") as f:
        f.write(json.dumps({"event": event, **kw}) + "\n")


class _Pg:
    def ensure_setup(self): pass
    def reset_schemas(self): pass
    def reset_test_objects(self, tid): pass


def drive(*args, **kwargs):
    log("drive", target=kwargs.get("target") is not None)
    if "error" in SC:
        error = H.HarnessError(SC["error"])
        error.failure = SC.get("failure")   # what drive() reads back from the run's errorFile
        raise error
    return SC["returns"]


P._provision_requires = lambda *a, **k: None
P._build_operator_jar = lambda name, ref, report=None: O.BuiltArtifact(
    path=Path("op.jar"), name="op-5.4.jar", op_name="op")
P._build_tokens = lambda test_dir, requires, parallel=False: {
    "TID": "", "TID_ORACLE": "", "TEST_DIR": str(test_dir), **SC.get("tokens", {})}
P._pg_admin = lambda *a, **k: _Pg()
H.ensure_harness_jar = lambda: Path("harness.jar")
H.drive = drive
D.run_sql_file = lambda db, path, tok: log("sql", file=Path(path).name)
D.query_rows = lambda route, sql, tok: (log("query", route=route), SC.get("rows", []))[1]


@pytest.fixture(autouse=True)
def cleanup_postgres():
    yield
'''

TARGET_CASE = """
name: refuse
purpose: p
op: {jar: java/OpenProcessors/X}
requires: [postgres]
properties: {Password: '${POSTGRES_TARGET_PASSWORD}'}
password_properties: [Password]
target: {input: in.json}
assert:
  expect_error: does not fit scale 2
  target:
    - query: SELECT ID FROM T ORDER BY ID
      match: rows.json
"""

TARGET_ROWS_CASE = """
name: write
purpose: p
op: {jar: java/OpenProcessors/X}
requires: [postgres]
target: {input: in.json}
assert:
  target:
    - query: SELECT ID FROM T ORDER BY ID
      match: rows.json
"""

DATA_CASE = """
name: transform
purpose: p
op: {jar: java/OpenProcessors/X}
assert:
  data:
    - input: in.json
      match: out.json
"""

EVENT = {"metadata": {"TableName": "T"}, "data": {"values": [1], "present": [True]}}


def run_case(pytester, monkeypatch, tmp_path, case: str, scenario: dict, files: dict) -> tuple:
    d = pytester.mkdir("case")
    (d / "test.yaml").write_text(case)
    for name, content in files.items():
        (d / name).write_text(content)
    pytester.makeconftest(CONFTEST)
    log = tmp_path / "log.jsonl"
    log.write_text("")
    scenario.setdefault("tokens", {"POSTGRES_TARGET_PASSWORD": "unused-pw"})
    monkeypatch.setenv("XINT_SCENARIO", json.dumps(scenario))
    monkeypatch.setenv("XINT_LOG", str(log))
    (tmp_path / "state").mkdir()
    monkeypatch.setenv("SLT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("S3_PASSWORD", "s3cretpw-from-env")
    junit = tmp_path / "junit.xml"
    result = pytester.runpytest_subprocess("-p", "inttest.plugin", "-p", "no:cacheprovider",
                                           f"--junitxml={junit}", "case")
    case_el = ET.parse(junit).find(".//testcase")
    assert case_el is not None and case_el.find("error") is None, result.stdout.str()
    failure = case_el.find("failure")
    events = [json.loads(line) for line in log.read_text().splitlines()]
    return result, (None if failure is None else (failure.get("message", "") + (failure.text or ""))), events


TARGET_FILES = {"in.json": json.dumps([EVENT]), "rows.json": json.dumps([[1]])}


def test_expect_error_passes_on_the_operators_refusal_and_reads_the_rows(pytester, monkeypatch, tmp_path):
    result, failure, events = run_case(pytester, monkeypatch, tmp_path, TARGET_CASE, {
        "error": "IntegrationProcessor exited 1 driving op.jar\nstdout:\n\nstderr:\nboom",
        "failure": "java.lang.IllegalStateException: 1.234 does not fit scale 2\n",
        "rows": [[1]]}, TARGET_FILES)
    assert failure is None, failure
    assert [e["event"] for e in events if e["event"] != "sql"] == ["drive", "query"]


def test_expect_error_still_checks_the_rows_after_the_refusal(pytester, monkeypatch, tmp_path):
    _, failure, _ = run_case(pytester, monkeypatch, tmp_path, TARGET_CASE, {
        "error": "exited 1", "failure": "java.lang.IllegalStateException: does not fit scale 2\n",
        "rows": [[1], [2]]}, TARGET_FILES)
    assert failure is not None and "assert.target mismatch" in failure


def test_expect_error_ignores_the_text_when_only_the_operator_printed_it(pytester, monkeypatch, tmp_path):
    _, failure, events = run_case(pytester, monkeypatch, tmp_path, TARGET_CASE, {
        "error": "IntegrationProcessor exited 1 driving op.jar\n"
                 "stdout:\nWARN value does not fit scale 2, retrying\nstderr:\n",
        "failure": "java.sql.SQLException: Connection refused\n"}, TARGET_FILES)
    assert failure is not None and "not for the stated reason" in failure
    assert "query" not in [e["event"] for e in events]


def test_a_harness_timeout_never_satisfies_expect_error(pytester, monkeypatch, tmp_path):
    _, failure, _ = run_case(pytester, monkeypatch, tmp_path, TARGET_CASE, {
        "error": "IntegrationProcessor timed out after 120s driving op.jar\n"
                 "stdout:\nrefusing: does not fit scale 2\nstderr:\n"}, TARGET_FILES)
    assert failure is not None and "the drive ended without one" in failure


def test_expect_error_fails_when_the_drive_succeeds(pytester, monkeypatch, tmp_path):
    _, failure, _ = run_case(pytester, monkeypatch, tmp_path, TARGET_CASE,
                             {"returns": {"ackedEvents": 1}}, TARGET_FILES)
    assert failure is not None and "it SUCCEEDED" in failure


@pytest.mark.parametrize("rows,passes", [([[1]], True), ([[2]], False), ([], False)])
def test_the_target_rows_decide_a_target_case(pytester, monkeypatch, tmp_path, rows, passes):
    _, failure, _ = run_case(pytester, monkeypatch, tmp_path, TARGET_ROWS_CASE, {
        "returns": {"ackedEvents": 1, "acknowledgeable": True}, "rows": rows}, TARGET_FILES)
    assert (failure is None) is passes, failure


@pytest.mark.parametrize("emitted,passes", [(1, True), (True, False), (1.0, False)])
def test_the_waevent_comparison_decides_a_data_case(pytester, monkeypatch, tmp_path, emitted, passes):
    out = {"metadata": {"TableName": "T"}, "data": {"values": [emitted], "present": [True]}}
    _, failure, _ = run_case(pytester, monkeypatch, tmp_path, DATA_CASE, {"returns": [out]},
                             {"in.json": json.dumps([EVENT]), "out.json": json.dumps([EVENT])})
    assert (failure is None) is passes, failure


def test_failure_text_masks_known_secrets(pytester, monkeypatch, tmp_path):
    _, failure, _ = run_case(pytester, monkeypatch, tmp_path, TARGET_CASE, {
        "tokens": {"POSTGRES_TARGET_PASSWORD": "tgt-pw-42xyz"},
        "error": "IntegrationProcessor exited 1 driving op.jar\nstdout:\n"
                 "connecting jdbc:postgresql://qa:tgt-pw-42xyz@db:5432/x password=tgt-pw-42xyz\n"
                 "S3 key s3cretpw-from-env\nstderr:\n",
        "failure": "java.sql.SQLException: auth failed for tgt-pw-42xyz\n"}, TARGET_FILES)
    assert failure is not None and "not for the stated reason" in failure
    assert "tgt-pw-42xyz" not in failure
    assert "s3cretpw-from-env" not in failure
    assert "[redacted]" in failure
