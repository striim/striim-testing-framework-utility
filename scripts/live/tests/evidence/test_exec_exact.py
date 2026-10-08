"""Exact data assertions executed through the real ``livetest.plugin`` (pytester, the
tests/lifecycle/exec_harness.py fakes at the infrastructure edges only). The manifest loader, input snapshot,
lifecycle, ownership ledger, ``exactdata`` readers over the real ``PgProbe``, ``canon``, the report hook, the
v1 sidecar, junit and the envelope are real; ``run.data()`` is the comparison records ``runtest`` collected."""
from __future__ import annotations

import hashlib
from pathlib import Path

pytest_plugins = ["pytester"]   # run_case drives a fresh pytest process through pytester

CASES = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "evidence" / "cases"


def _agree(run, status, qualifies=False):
    from _slt_exec_harness import _agree as agree
    return agree(run, status, qualifies)


def test_exec_exact_pass_collects_comparison_records(run_case):
    run = run_case("exact-db-any", {"initial_load": True}, fixtures=CASES)
    _agree(run, "passed", qualifies=True)
    [comp] = run.data()
    assert comp["profile"] == "slt-canon/1" and comp["equal"] is True and comp["actual"]["owned"] is True
    assert comp["expected"]["source"] == "expected/tgt.csv" and comp["actual"]["rowCount"] == 3
    data = [a for a in run.sidecar()["assertions"] if a["type"] == "data"]
    assert len(data) == 1 and data[0]["status"] == "passed" and comp["actual"]["sha256"] in data[0]["detail"]
    names = run.names()
    assert names.index("deploy") < names.index("select")
    selects = [e for e in run.events() if e["event"] == "select"]
    assert len(selects) == 1 and selects[0]["table"] == f"qatarget.{run.dump()['tid']}tgt" and selects[0]["limit"] == 101


def test_exec_wrong_golden_fails_golden_unchanged(run_case):
    source = CASES / "exact-wrong-golden" / "expected" / "tgt.csv"
    run = run_case("exact-wrong-golden", {"initial_load": True}, fixtures=CASES)
    env = _agree(run, "failed")
    golden = run.root / "cases" / "exact-wrong-golden" / "expected" / "tgt.csv"
    assert hashlib.sha256(golden.read_bytes()).digest() == hashlib.sha256(source.read_bytes()).digest()
    assert golden.stat().st_mtime_ns == source.stat().st_mtime_ns            # copied with its mtime, never rewritten
    [comp] = run.data()
    assert comp["expected"]["sha256"] != comp["actual"]["sha256"]
    assert comp["samples"]["missing"] == [{"row": [["id", "integer", "4"]], "count": 1}]
    assert "exact data assertion failed" in env["run"]["failure"] or "!= actual" in run.junit()[2]


def test_exec_duplicate_delivery_fails(run_case):
    run = run_case("exact-db-any", {"initial_load": True, "rows": {"qatarget.<TID>tgt": [{"id": 1}, {"id": 1}, {"id": 2}]}},
                   fixtures=CASES)
    _agree(run, "failed")
    [comp] = run.data()
    assert comp["samples"] == {"missing": [{"row": [["id", "integer", "3"]], "count": 1}],
                               "extra": [{"row": [["id", "integer", "1"]], "count": 1}]}


def test_exec_order_sequence_permutation_fails(run_case):
    run = run_case("exact-db-sequence", {"initial_load": True}, fixtures=CASES,
                   edit=lambda y: y.replace("expected/tgt.csv", "expected/permuted.csv"))
    _agree(run, "failed")
    [comp] = run.data()
    assert comp["order"] == "sequence" and comp["samples"]["firstMismatch"]["index"] == 0
    assert [e["order_by"] for e in run.events() if e["event"] == "select"] == ['"id"']


def test_exec_exact_manifest_error_before_any_fake_provisioning_call(run_case):
    run = run_case("exact-db-any", {"initial_load": True}, fixtures=CASES,
                   edit=lambda y: y.replace("exact: {version: 1, max_rows: 100}", "exact: {version: 2}"))
    env = _agree(run, "failed")
    assert "exact.version 2 is not supported" in run.junit()[2]
    assert not {"resolve_striim", "resolve", "create", "deploy", "select"} & set(run.names())
    assert env["lifecycle"]["mode"] == "unknown"


def test_exec_foreign_target_exact_fails_before_read(run_case):
    run = run_case("exact-db-any", {"initial_load": True, "tables": {"qatarget.foreigntgt": [1, 2, 3]}}, fixtures=CASES,
                   edit=lambda y: y.replace('    - target: "${PG_TARGET_SCHEMA}.${TID}tgt"\n      target_db: postgres-target\n      match',
                                            '    - target: "${PG_TARGET_SCHEMA}.foreigntgt"\n      target_db: postgres-target\n      match'))
    _agree(run, "failed")
    assert "exact-target-not-owned" in run.junit()[2]
    assert "select" not in run.names()
    assert run.dump()["tables"] == {"qatarget.foreigntgt": [1, 2, 3]}
    assert run.data()[0]["error"] == "exact-target-not-owned"
