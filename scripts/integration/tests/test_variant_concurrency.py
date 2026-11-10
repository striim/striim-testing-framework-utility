"""INT_VARIANT_WORKERS (with INT_VARIANT_ITEMS=0): a case's variants drive concurrently, one worker per database.

Each fake drive sleeps and logs its start and end, so overlap and order are measured, not assumed.
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from inttest import plugin as P
from inttest.manifest import load_manifest

CONFTEST = r'''
import json, os, threading, time
from pathlib import Path

import pytest

from inttest import dbroutes as D, harness as H, opartifacts as O, plugin as P

SC = json.loads(os.environ["XINT_SCENARIO"])
LOG = os.environ["XINT_LOG"]
_LOCK = threading.Lock()


def log(event, **kw):
    with _LOCK, open(LOG, "a") as f:
        f.write(json.dumps({"event": event, "t": time.monotonic(), **kw}) + "\n")


class _Pg:
    def ensure_setup(self): pass
    def reset_schemas(self): log("reset")
    def reset_test_objects(self, tid): pass


def drive(op_jar, props, events, **kwargs):
    run = props["Run"] + "".join(f",{k}={props[k]}" for k in sorted(props) if k == "Mode")
    log("start", run=run)
    time.sleep(SC.get("sleep", 0.4))
    log("end", run=run)
    if props["Run"] in SC.get("fail", []):
        raise H.HarnessError(f"boom in {props['Run']}")
    if props["Run"] in SC.get("skip", []):
        pytest.skip(f"no java for {props['Run']}")
    if SC.get("refuse"):
        error = H.HarnessError("exited 1")
        error.failure = f"java.lang.IllegalStateException: refused {props['Run']}"
        raise error
    return {"ackedEvents": 1, "acknowledgeable": True}


P._provision_requires = lambda *a, **k: None
P._build_operator_jar = lambda name, ref, report=None: O.BuiltArtifact(
    path=Path("op.jar"), name="op-5.4.jar", op_name="op")
P._build_tokens = lambda test_dir, requires, parallel=False: {
    "TID": "", "TID_ORACLE": "", "TEST_DIR": str(test_dir), "ORACLE_URL": "jdbc:oracle"}
P._pg_admin = lambda *a, **k: _Pg()
P._ensure_spanner_databases = lambda tok: None
P._drop_oracle_test_prefix = lambda prefix: None
H.ensure_harness_jar = lambda: Path("harness.jar")
H.drive = drive
D.run_sql_file = lambda db, path, tok: log("sql", file=Path(path).name)


@pytest.fixture(autouse=True)
def cleanup_postgres():
    yield
'''


def case(variants: dict, matrix: str = "", asserts: str = "  acked: 1") -> str:
    lines = ["name: fan", "purpose: p", "op: {jar: java/OpenProcessors/X}",
             "requires: [postgres]", "properties: {Run: '${V_RUN}'}", "target: {input: in.json}"]
    if matrix:
        lines.append(matrix)
    lines.append("variants:")
    for name, db in variants.items():
        lines += [f"  {name}:", f"    db: {db}", f"    ddl: [{name}.sql]",
                  f"    tokens: {{V_RUN: {name}}}"]
    lines += ["assert:", asserts]
    return "\n".join(lines) + "\n"


def run_case(pytester, monkeypatch, tmp_path, yaml_text: str, workers, scenario=None):
    d = pytester.mkdir("case")
    (d / "test.yaml").write_text(yaml_text)
    (d / "in.json").write_text(json.dumps([{"metadata": {"TableName": "T"},
                                            "data": {"values": [1], "present": [True]}}]))
    for line in yaml_text.splitlines():
        if line.strip().startswith("ddl: ["):
            (d / line.strip()[6:-1]).write_text("SELECT 1;")
    pytester.makeconftest(CONFTEST)
    log = tmp_path / "log.jsonl"
    log.write_text("")
    monkeypatch.setenv("XINT_SCENARIO", json.dumps(scenario or {}))
    monkeypatch.setenv("XINT_LOG", str(log))
    (tmp_path / "state").mkdir()
    monkeypatch.setenv("SLT_STATE_DIR", str(tmp_path / "state"))
    # These tests drive one item per case; per-group items have their own tests.
    monkeypatch.setenv("INT_VARIANT_ITEMS", "0")
    if workers is None:
        monkeypatch.delenv("INT_VARIANT_WORKERS", raising=False)
    else:
        monkeypatch.setenv("INT_VARIANT_WORKERS", str(workers))
    junit = tmp_path / "junit.xml"
    result = pytester.runpytest_subprocess("-p", "inttest.plugin", "-p", "no:cacheprovider",
                                           f"--junitxml={junit}", "case")
    events = [json.loads(line) for line in log.read_text().splitlines()]
    if not junit.exists():
        return result, None, events
    el = ET.parse(junit).find(".//testcase")
    assert el is not None and el.find("error") is None, result.stdout.str()
    failure = el.find("failure")
    return result, (None if failure is None else failure.get("message", "") + (failure.text or "")), events


def intervals(events) -> dict:
    out = {}
    for e in events:
        if e["event"] in ("start", "end"):
            out.setdefault(e["run"], {})[e["event"]] = e["t"]
    return out


def peak(events, runs=None) -> int:
    """The most drives that were running at one moment."""
    level = best = 0
    for e in sorted((e for e in events if e["event"] in ("start", "end")
                     and (runs is None or e["run"].split(",")[0] in runs)),
                    key=lambda e: (e["t"], e["event"] == "start")):
        level += 1 if e["event"] == "start" else -1
        best = max(best, level)
    return best


THREE = {"pg": "postgres-target", "ora": "oracle-target", "mss": "sqlserver-target"}


def test_unset_drives_variants_one_after_another(pytester, monkeypatch, tmp_path):
    _, failure, events = run_case(pytester, monkeypatch, tmp_path, case(THREE), None)
    assert failure is None, failure
    assert peak(events) == 1
    assert [e["run"] for e in events if e["event"] == "start"] == ["pg", "ora", "mss"]


def test_workers_drive_variants_on_different_databases_at_once(pytester, monkeypatch, tmp_path):
    _, failure, events = run_case(pytester, monkeypatch, tmp_path, case(THREE), 3)
    assert failure is None, failure
    assert peak(events) == 3
    # Each variant laid down its own tables before its drive.
    for name in THREE:
        sql = next(e["t"] for e in events if e.get("file") == f"{name}.sql")
        assert sql <= intervals(events)[name]["start"]


def test_workers_cap_the_concurrency(pytester, monkeypatch, tmp_path):
    _, failure, events = run_case(pytester, monkeypatch, tmp_path, case(THREE), 2)
    assert failure is None, failure
    assert peak(events) == 2


def test_variants_of_one_service_and_their_permutations_stay_serial(pytester, monkeypatch, tmp_path):
    variants = {"pg": "postgres-target", "pg2": "postgres-source", "ora": "oracle-target"}
    _, failure, events = run_case(pytester, monkeypatch, tmp_path,
                                  case(variants, "matrix:\n  Mode: ['a', 'b']"), 4)
    assert failure is None, failure
    assert peak(events) == 2
    assert peak(events, {"pg", "pg2"}) == 1
    assert peak(events, {"ora"}) == 1
    starts = [e["run"] for e in events if e["event"] == "start"]
    assert [r for r in starts if r.startswith("pg")] == ["pg,Mode=a", "pg,Mode=b",
                                                          "pg2,Mode=a", "pg2,Mode=b"]
    assert [r for r in starts if r.startswith("ora")] == ["ora,Mode=a", "ora,Mode=b"]


def test_spanner_variants_run_alone_after_the_others(pytester, monkeypatch, tmp_path):
    variants = {"sg": "spanner-google", "pg": "postgres-target", "sp": "spanner-postgres",
                "ora": "oracle-target"}
    _, failure, events = run_case(pytester, monkeypatch, tmp_path, case(variants), 4)
    assert failure is None, failure
    iv = intervals(events)
    assert peak(events, {"pg", "ora"}) == 2
    assert iv["sg"]["start"] >= max(iv["pg"]["end"], iv["ora"]["end"])
    assert iv["sp"]["start"] >= iv["sg"]["end"]


def test_every_group_runs_and_each_failure_names_its_variant(pytester, monkeypatch, tmp_path):
    _, failure, events = run_case(pytester, monkeypatch, tmp_path, case(THREE), 3,
                                  {"fail": ["pg", "mss"]})
    assert sorted(e["run"] for e in events if e["event"] == "end") == ["mss", "ora", "pg"]
    assert failure is not None and "2 of 3 variant groups failed" in failure
    assert "[pg]" in failure and "boom in pg" in failure
    assert "[mss]" in failure and "boom in mss" in failure
    assert "[ora]" not in failure


def test_one_failure_is_raised_as_it_was(pytester, monkeypatch, tmp_path):
    _, failure, _ = run_case(pytester, monkeypatch, tmp_path, case(THREE), 3, {"fail": ["ora"]})
    assert failure is not None and "fan [ora]: boom in ora" in failure
    assert "variant groups failed" not in failure


def test_a_failure_outranks_a_skip(pytester, monkeypatch, tmp_path):
    _, failure, _ = run_case(pytester, monkeypatch, tmp_path, case(THREE), 3,
                             {"skip": ["pg"], "fail": ["ora"]})
    assert failure is not None and "boom in ora" in failure


def test_a_skip_without_a_failure_skips_the_case(pytester, monkeypatch, tmp_path):
    result, failure, _ = run_case(pytester, monkeypatch, tmp_path, case(THREE), 3, {"skip": ["mss"]})
    assert failure is None
    result.assert_outcomes(skipped=1)


def test_expect_error_variants_drive_at_once(pytester, monkeypatch, tmp_path):
    _, failure, events = run_case(pytester, monkeypatch, tmp_path,
                                  case(THREE, asserts="  expect_error: 'refused'"), 3, {"refuse": True})
    assert failure is None, failure
    assert peak(events) == 3


def test_teardown_runs_after_every_group_and_keeps_a_failed_case(pytester, monkeypatch, tmp_path):
    _, failure, events = run_case(pytester, monkeypatch, tmp_path, case(THREE), 3)
    assert failure is None, failure
    resets = [e["t"] for e in events if e["event"] == "reset"]
    assert len(resets) == 2, "setup and teardown"
    assert resets[-1] >= max(e["t"] for e in events if e["event"] == "end")


def test_a_failed_group_keeps_the_resources(pytester, monkeypatch, tmp_path):
    _, failure, events = run_case(pytester, monkeypatch, tmp_path, case(THREE), 3, {"fail": ["ora"]})
    assert failure is not None
    assert len([e for e in events if e["event"] == "reset"]) == 1, "setup only"


def test_a_bad_setting_is_a_usage_error(pytester, monkeypatch, tmp_path):
    result, _, events = run_case(pytester, monkeypatch, tmp_path, case(THREE), "0")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert "INT_VARIANT_WORKERS" in result.stderr.str()
    assert not events


# --- grouping, on the parsed manifest -------------------------------------------------------


def _groups(tmp_path, text):
    (tmp_path / "test.yaml").write_text(text)
    (tmp_path / "in.json").write_text("[]")
    for line in text.splitlines():
        if line.strip().startswith("ddl: ["):
            (tmp_path / line.strip()[6:-1]).write_text("SELECT 1;")
    for f in ("mid.sql",):
        (tmp_path / f).write_text("SELECT 1;")
    m = load_manifest(tmp_path / "test.yaml")
    return [sorted(dict.fromkeys(v.name for v, _ in runs)) for _, runs in P._variant_groups(m, m.runs())]


def test_a_variant_naming_another_services_tokens_joins_its_group(tmp_path):
    text = case(THREE).replace("tokens: {V_RUN: mss}", "tokens: {V_RUN: mss, V_X: '${ORACLE_URL}'}")
    assert _groups(tmp_path, text) == [["pg"], ["mss", "ora"]]


def test_a_service_token_in_shared_properties_joins_every_variant(tmp_path):
    text = case(THREE).replace("properties: {Run: '${V_RUN}'}",
                               "properties: {Run: '${V_RUN}', Ckpt: '${POSTGRES_URL}'}")
    assert _groups(tmp_path, text) == [["mss", "ora", "pg"]]


def test_a_requires_service_token_joins_its_variants(tmp_path):
    text = (case(THREE).replace("requires: [postgres]", "requires: [postgres, gcs]")
            .replace("tokens: {V_RUN: pg}", "tokens: {V_RUN: pg, V_B: '${GCS_BUCKET}'}")
            .replace("tokens: {V_RUN: mss}", "tokens: {V_RUN: mss, V_B: '${GCS_BUCKET}'}"))
    assert _groups(tmp_path, text) == [["mss", "pg"], ["ora"]]


def test_a_config_file_case_is_one_group(tmp_path):
    text = case(THREE).replace("properties: {Run: '${V_RUN}'}",
                               "properties: {Run: '${V_RUN}', ConfigFile: '${TEST_DIR}/c.json'}")
    assert _groups(tmp_path, text) == [["mss", "ora", "pg"]]


def test_an_explicit_mid_run_route_joins_every_variant(tmp_path):
    text = case(THREE).replace(
        "target: {input: in.json}",
        "target: {input: in.json, mid_run: [{after: 1, file: mid.sql, db: postgres-target}]}")
    assert _groups(tmp_path, text) == [["mss", "ora", "pg"]]


@pytest.mark.parametrize("raw,expected", [(None, 1), ("", 1), ("1", 1), (" 4 ", 4)])
def test_workers_setting(raw, expected):
    assert P._variant_workers({} if raw is None else {"INT_VARIANT_WORKERS": raw}) == expected


@pytest.mark.parametrize("raw", ["0", "-1", "two", "1.5"])
def test_workers_setting_refuses_nonsense(raw):
    with pytest.raises(ValueError, match="INT_VARIANT_WORKERS"):
        P._variant_workers({"INT_VARIANT_WORKERS": raw})
