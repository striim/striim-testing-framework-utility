"""A `target:` case driven end to end through the real IntYamlItem.runtest and _run_target_case.

pytester runs a child session with `harness.drive`, the database routes, service bring-up, the op
build and the tokens faked in its conftest. Everything between them -- the property and input
rendering, the drive's error handling, the run-report checks (ack marker, restarts, replayed,
monitor, acked, exception store), expect_log, the target-row comparison and the failure text -- is
the plugin's own code. Outcomes are read from the child's JUnit XML.
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
    def reset_schemas(self): log("reset")
    def reset_test_objects(self, tid): log("reset")


def ensure_harness_jar():
    if "jar_error" in SC:
        raise H.HarnessError(SC["jar_error"])
    return Path("harness.jar")


def drive(op_jar, props, events, **kwargs):
    config = props.get("ConfigFile")
    log("drive", props=props, events=events,
        target=kwargs.get("target") is not None,
        has_log_sink=kwargs.get("log_sink") is not None,
        config_text=(Path(config).read_text() if config else None))
    for ordinal in SC.get("mid_run", []):
        kwargs["on_mid_run"](ordinal)
    if kwargs.get("log_sink") is not None:
        kwargs["log_sink"].append(SC.get("logged", ""))
    errors = SC.get("errors", {})
    key = props.get("Mode", "")
    if key in errors:
        raise H.HarnessError(errors[key])
    return SC["report"]


P._provision_requires = lambda *a, **k: None
P._build_operator_jar = lambda name, ref, report=None: O.BuiltArtifact(
    path=Path("op.jar"), name="op-5.4.jar", op_name="op")
P._build_tokens = lambda test_dir, requires, parallel=False: {
    "TID": "", "TID_ORACLE": "", "TEST_DIR": str(test_dir), **SC.get("tokens", {})}
P._pg_admin = lambda *a, **k: _Pg()
P._drop_oracle_test_prefix = lambda prefix: None
H.ensure_harness_jar = ensure_harness_jar
H.drive = drive


def run_sql_file(db, path, tok):
    log("sql", file=Path(path).name, db=db)
    if Path(path).name in SC.get("sql_fails", []):
        raise RuntimeError(f"relation already exists in {Path(path).name}")


D.run_sql_file = run_sql_file
D.run_sql_text = lambda db, sql, tok: log("mid_run", db=db, sql=sql)
D.query_rows = lambda route, sql, tok: (log("query", route=route, sql=sql), SC.get("rows", [[1]]))[1]


@pytest.fixture(autouse=True)
def cleanup_postgres():
    yield
'''

EVENT = {"metadata": {"TableName": "T"}, "data": {"values": [1], "present": [True]}}
REPORT = {"ackedEvents": 1, "acknowledgeable": True}


def case(asserts: str = "  acked: 1", *, extra: str = "", target: str = "target: {input: in.json}",
         properties: str = "properties: {Url: '${PG_URL}'}") -> str:
    return (f"name: write\npurpose: p\nop: {{jar: java/OpenProcessors/X}}\nrequires: [postgres]\n"
            f"{properties}\n{target}\nddl: [ddl.sql]\nseed: [seed.sql]\n{extra}"
            f"assert:\n  target:\n    - query: SELECT ID FROM T ORDER BY ID\n      match: rows.json\n"
            f"{asserts}\n")


def run_case(pytester, monkeypatch, tmp_path, text: str, scenario: dict | None = None,
             files: dict | None = None, env: dict | None = None):
    """Run one case in a child session. Returns (result, outcome, message, log events)."""
    d = pytester.mkdir("case")
    (d / "test.yaml").write_text(text)
    base = {"in.json": json.dumps([EVENT]), "rows.json": json.dumps([[1]]),
            "ddl.sql": "CREATE TABLE T (ID INT);", "seed.sql": "SELECT 1;"}
    for name, content in {**base, **(files or {})}.items():
        (d / name).write_text(content)
    pytester.makeconftest(CONFTEST)
    log = tmp_path / "log.jsonl"
    log.write_text("")
    sc = {"report": REPORT, "tokens": {"PG_URL": "jdbc:postgresql://db/x"}, **(scenario or {})}
    monkeypatch.setenv("XINT_SCENARIO", json.dumps(sc))
    monkeypatch.setenv("XINT_LOG", str(log))
    (tmp_path / "state").mkdir()
    monkeypatch.setenv("SLT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("INT_VARIANT_ITEMS", "0")
    monkeypatch.delenv("INT_VARIANT_WORKERS", raising=False)
    monkeypatch.delenv("INT_ALLOW_ALL_SKIPPED", raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    junit = tmp_path / "junit.xml"
    result = pytester.runpytest_subprocess("-p", "inttest.plugin", "-p", "no:cacheprovider",
                                           f"--junitxml={junit}", "case")
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert junit.exists(), result.stdout.str() + result.stderr.str()
    cases = ET.parse(junit).findall(".//testcase")
    assert len(cases) == 1, result.stdout.str()
    el = cases[0]
    assert el.find("error") is None, result.stdout.str()
    for outcome in ("failure", "skipped"):
        found = el.find(outcome)
        if found is not None:
            return result, outcome, found.get("message", "") + (found.text or ""), events
    return result, "passed", "", events


def drives(events) -> list:
    return [e for e in events if e["event"] == "drive"]


# --- outcomes of the drive itself -----------------------------------------------------------


def test_a_matching_target_case_passes_and_drives_the_writer_with_rendered_inputs(
        pytester, monkeypatch, tmp_path):
    text = case()
    inp = [{"metadata": {"TableName": "${TID_X}T"}, "data": {"values": [1], "present": [True]}}]
    _, outcome, message, events = run_case(
        pytester, monkeypatch, tmp_path, text,
        {"tokens": {"PG_URL": "jdbc:postgresql://db/x", "TID_X": "t9_"}},
        {"in.json": json.dumps(inp)})
    assert outcome == "passed", message
    [d] = drives(events)
    assert d["target"] is True and d["has_log_sink"] is True
    assert d["props"] == {"Url": "jdbc:postgresql://db/x"}
    assert d["events"][0]["metadata"]["TableName"] == "t9_T"
    kinds = [e["event"] for e in events]
    # setup reset, ddl, seed, the drive, the read-back, teardown reset (the case passed)
    assert kinds == ["reset", "sql", "sql", "drive", "query", "reset"]


def test_a_target_row_mismatch_fails_and_quotes_the_writer_report(pytester, monkeypatch, tmp_path):
    _, outcome, message, events = run_case(pytester, monkeypatch, tmp_path, case(),
                                           {"rows": [[2]]})
    assert outcome == "failure"
    assert "assert.target mismatch" in message and "actual:   [[2]]" in message
    assert "writer report:" in message and '"ackedEvents": 1' in message
    # A failed case keeps its resources: no teardown reset.
    assert [e["event"] for e in events].count("reset") == 1


def test_a_drive_error_fails_the_case_naming_it(pytester, monkeypatch, tmp_path):
    _, outcome, message, events = run_case(
        pytester, monkeypatch, tmp_path, case(),
        {"errors": {"": "IntegrationProcessor exited 1 driving op.jar\nboom"}})
    assert outcome == "failure", message
    assert "write: IntegrationProcessor exited 1 driving op.jar" in message
    assert "query" not in [e["event"] for e in events]


def test_a_drive_error_in_one_permutation_names_that_permutation(pytester, monkeypatch, tmp_path):
    text = case(extra="matrix:\n  Mode: ['a', 'b']\n")
    _, outcome, message, events = run_case(pytester, monkeypatch, tmp_path, text,
                                           {"errors": {"b": "exited 1 in b"}})
    assert outcome == "failure"
    assert "write [Mode=b]: exited 1 in b" in message
    assert [d["props"] for d in drives(events)] == [
        {"Url": "jdbc:postgresql://db/x", "Mode": "a"}, {"Url": "jdbc:postgresql://db/x", "Mode": "b"}]
    # The second permutation re-ran ddl and seed before its drive.
    kinds = [e["event"] for e in events]
    second = [i for i, k in enumerate(kinds) if k == "drive"][1]
    assert kinds[second - 2:second] == ["sql", "sql"]


def test_no_java_skips_the_case_as_an_environment_gap(pytester, monkeypatch, tmp_path):
    result, outcome, message, _ = run_case(
        pytester, monkeypatch, tmp_path, case(),
        {"errors": {"": "no `java` executable found on PATH or under JAVA_HOME"}})
    assert outcome == "skipped", message
    assert "no `java` executable found" in message
    out = result.stdout.str()
    # An EnvSkip with cause "env": the all-skipped guard fails the run and offers the opt-out.
    assert "PROVED NOTHING" in out and "INT_ALLOW_ALL_SKIPPED=1" in out
    assert result.ret == 1


def test_a_harness_jar_that_cannot_be_built_skips_as_a_build_gap(pytester, monkeypatch, tmp_path):
    result, outcome, message, events = run_case(pytester, monkeypatch, tmp_path, case(),
                                                {"jar_error": "mvn package failed"})
    assert outcome == "skipped", message
    assert "write: cannot build the inttest harness jar: mvn package failed" in message
    assert not drives(events)
    out = result.stdout.str()
    # Cause "build": the guard fails the run and offers no opt-out.
    assert "PROVED NOTHING" in out and "INT_ALLOW_ALL_SKIPPED=1" not in out
    assert result.ret == 1
    # A skip is not a failure: the teardown still reset what setup made.
    assert [e["event"] for e in events].count("reset") == 2


@pytest.mark.parametrize("which,what", [("ddl.sql", "ddl"), ("seed.sql", "seed")])
def test_a_setup_sql_failure_fails_the_case_before_the_drive(pytester, monkeypatch, tmp_path,
                                                            which, what):
    _, outcome, message, events = run_case(pytester, monkeypatch, tmp_path, case(),
                                           {"sql_fails": [which]})
    assert outcome == "failure"
    assert f"write: {what} {which!r} against 'postgres-source' failed: relation already exists" in message
    assert not drives(events)


# --- the run report -------------------------------------------------------------------------


def test_a_writer_without_the_ack_marker_fails(pytester, monkeypatch, tmp_path):
    _, outcome, message, _ = run_case(pytester, monkeypatch, tmp_path, case(),
                                      {"report": {"ackedEvents": 1, "acknowledgeable": False}})
    assert outcome == "failure"
    assert "does not implement com.webaction.recovery.Acknowledgeable" in message


@pytest.mark.parametrize("acked,passes", [(1, True), (0, False), (2, False)])
def test_the_ack_count_decides(pytester, monkeypatch, tmp_path, acked, passes):
    _, outcome, message, _ = run_case(pytester, monkeypatch, tmp_path, case(),
                                      {"report": {"ackedEvents": acked, "acknowledgeable": True}})
    assert (outcome == "passed") is passes, message
    if not passes:
        assert f"assert.acked expected 1 acknowledged event(s) but the writer acked {acked}" in message


@pytest.mark.parametrize("report,passes,named", [
    ({"restarts": 1, "eventsReplayed": 2}, True, None),
    ({"restarts": 0, "eventsReplayed": 2}, False, "assert.restarts expected 1 restart(s) but the run reported 0"),
    ({"restarts": 1, "eventsReplayed": 3}, False, "assert.replayed expected 2 replayed event(s) but the run reported 3"),
    ({"eventsReplayed": 2}, False, "assert.restarts expected 1 restart(s) but the run reported None"),
])
def test_the_restart_accounting_decides_before_the_ack_count(pytester, monkeypatch, tmp_path,
                                                             report, passes, named):
    # ackedEvents is wrong too: the restart message must win, it is checked first.
    full = {"acknowledgeable": True, "ackedEvents": 1 if passes else 99, **report}
    _, outcome, message, _ = run_case(pytester, monkeypatch, tmp_path,
                                      case("  acked: 1\n  restarts: 1\n  replayed: 2"),
                                      {"report": full})
    assert (outcome == "passed") is passes, message
    if named:
        assert named in message and "assert.acked" not in message


@pytest.mark.parametrize("store,passes", [([{"events": [2, 3]}, {"events": [5]}], True),
                                          ([{"events": [2]}, {"events": [5]}], False),
                                          ([], False)])
def test_the_exception_store_decides(pytester, monkeypatch, tmp_path, store, passes):
    _, outcome, message, _ = run_case(
        pytester, monkeypatch, tmp_path, case("  acked: 1\n  exception_store: [[2, 3], [5]]"),
        {"report": {**REPORT, "exceptionStore": store}})
    assert (outcome == "passed") is passes, message
    if not passes:
        assert "assert.exception_store expected the writer to hand [[2, 3], [5]]" in message


# --- assert.monitor -------------------------------------------------------------------------


@pytest.mark.parametrize("published,passes", [("orders:3", True), ("orders:4", False), (None, False)])
def test_an_exact_monitor_metric_is_rendered_and_compared(pytester, monkeypatch, tmp_path,
                                                          published, passes):
    monitor = {} if published is None else {"TABLE_INFO": published}
    _, outcome, message, _ = run_case(
        pytester, monkeypatch, tmp_path, case("  acked: 1\n  monitor: {TABLE_INFO: '${TBL}:3'}"),
        {"report": {**REPORT, "monitor": monitor},
         "tokens": {"PG_URL": "u", "TBL": "orders"}})
    assert (outcome == "passed") is passes, message
    if not passes:
        assert f"assert.monitor.TABLE_INFO expected 'orders:3' but the writer published {published!r}" in message


@pytest.mark.parametrize("published,passes", [
    ({"orders": 12}, True),
    ({"orders": 0}, True),
    ({"orders": 12, "other": 1}, False),
    ({}, False),
    ({"orders": -1}, False),
    ({"orders": True}, False),
    ({"orders": 1.5}, False),
    ("not json", False),
    (None, False),
])
def test_a_shape_monitor_metric_checks_keys_and_non_negative_ints(pytester, monkeypatch, tmp_path,
                                                                  published, passes):
    if published is None:
        monitor = {}
    elif isinstance(published, str):
        monitor = {"COMMIT_LAG": published}
    else:
        monitor = {"COMMIT_LAG": json.dumps(published)}
    _, outcome, message, _ = run_case(
        pytester, monkeypatch, tmp_path, case("  acked: 1\n  monitor: {COMMIT_LAG: ['${TBL}']}"),
        {"report": {**REPORT, "monitor": monitor}, "tokens": {"PG_URL": "u", "TBL": "orders"}})
    assert (outcome == "passed") is passes, message
    if not passes:
        assert "assert.monitor.COMMIT_LAG expected a lag for exactly ['orders']" in message


@pytest.mark.parametrize("published,passes", [
    ({"Windows": 2, "CommitMs": 17}, True),
    ({"Windows": 3, "CommitMs": 17}, False),
    ({"CommitMs": 17}, False),
    ("not json", False),
])
def test_a_subset_monitor_metric_compares_only_the_named_keys(pytester, monkeypatch, tmp_path,
                                                              published, passes):
    raw = published if isinstance(published, str) else json.dumps(published)
    _, outcome, message, _ = run_case(
        pytester, monkeypatch, tmp_path, case("  acked: 1\n  monitor: {ADAPTER_BASIC: {Windows: 2}}"),
        {"report": {**REPORT, "monitor": {"ADAPTER_BASIC": raw}}})
    assert (outcome == "passed") is passes, message
    if not passes:
        assert "assert.monitor.ADAPTER_BASIC differs on the keys the case names" in message


# --- expect_log -----------------------------------------------------------------------------


@pytest.mark.parametrize("logged,passes", [("WARN VendorConfiguration ignored\n", True),
                                           ("INFO started\n", False), ("", False)])
def test_expect_log_reads_what_the_writer_logged(pytester, monkeypatch, tmp_path, logged, passes):
    _, outcome, message, _ = run_case(
        pytester, monkeypatch, tmp_path, case("  acked: 1\n  expect_log: ['VendorConfiguration ignored']"),
        {"logged": logged})
    assert (outcome == "passed") is passes, message
    if not passes:
        assert "assert.expect_log did not find ['VendorConfiguration ignored']" in message
        assert (logged.strip() or "the operator logged nothing at all") in message


# --- what reaches the drive -----------------------------------------------------------------


def test_a_config_file_is_rendered_onto_a_copy(pytester, monkeypatch, tmp_path):
    text = case(properties="properties: {ConfigFile: '${TEST_DIR}/c.json'}")
    _, outcome, message, events = run_case(pytester, monkeypatch, tmp_path, text,
                                           {"tokens": {"TBL": "orders"}},
                                           {"c.json": '{"table": "${TBL}"}'})
    assert outcome == "passed", message
    [d] = drives(events)
    assert d["config_text"] == '{"table": "orders"}'
    assert not d["props"]["ConfigFile"].endswith("/case/c.json")


def test_mid_run_sql_is_wired_into_the_target_drive(pytester, monkeypatch, tmp_path):
    text = case(target="target: {input: in.json, mid_run: [{after: 1, file: mid.sql, db: postgres-target}]}")
    _, outcome, message, events = run_case(pytester, monkeypatch, tmp_path, text,
                                           {"mid_run": [1], "tokens": {"PG_URL": "u", "TBL": "orders"}},
                                           {"mid.sql": "UPDATE ${TBL} SET X = 1;"})
    assert outcome == "passed", message
    assert [(e["db"], e["sql"]) for e in events if e["event"] == "mid_run"] == [
        ("postgres-target", "UPDATE orders SET X = 1;")]


VARIANT_CASE = """name: write
purpose: p
op: {jar: java/OpenProcessors/X}
requires: [postgres]
properties: {Url: '${V_URL}'}
target: {input: in.json}
variants:
  pg:
    db: postgres-target
    ddl: [ddl.sql]
    tokens: {V_URL: '${PG_URL}/pg'}
  ora:
    db: oracle-target
    ddl: [ddl.sql]
    tokens: {V_URL: 'jdbc:oracle'}
assert:
  acked: 1
  target:
    - query: SELECT ID FROM T ORDER BY ID
      match: rows.json
"""


def test_each_variant_drives_with_its_tokens_and_reads_back_through_its_route(
        pytester, monkeypatch, tmp_path):
    _, outcome, message, events = run_case(pytester, monkeypatch, tmp_path, VARIANT_CASE)
    assert outcome == "passed", message
    assert [d["props"]["Url"] for d in drives(events)] == ["jdbc:postgresql://db/x/pg", "jdbc:oracle"]
    assert [e["route"] for e in events if e["event"] == "query"] == ["postgres-target", "oracle-target"]


def test_a_variant_failure_names_the_variant(pytester, monkeypatch, tmp_path):
    text = VARIANT_CASE.replace("tokens: {V_URL: 'jdbc:oracle'}", "tokens: {V_URL: 'jdbc:oracle', V_MODE: b}")
    text = text.replace("properties: {Url: '${V_URL}'}", "properties: {Url: '${V_URL}', Mode: '${V_MODE}'}")
    text = text.replace("tokens: {V_URL: '${PG_URL}/pg'}", "tokens: {V_URL: '${PG_URL}/pg', V_MODE: a}")
    _, outcome, message, _ = run_case(pytester, monkeypatch, tmp_path, text,
                                      {"errors": {"b": "exited 1 on oracle"}})
    assert outcome == "failure"
    assert "write [ora]: exited 1 on oracle" in message
