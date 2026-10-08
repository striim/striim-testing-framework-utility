"""The lifecycle executed through the real ``livetest.plugin``.

Each test runs a fresh pytest process (pytester) over a fixture case from ``tests/fixtures/lifecycle`` with
``-p livetest.plugin`` and ``--junitxml``. A conftest replaces only the infrastructure edges -- the Striim
resolver and client, the service adapter, ``PgAdmin``, the probe's psycopg2 connect and ``docker exec`` --
with an in-memory Postgres (tables, catalog, slots), a Striim namespace list and a server file store whose
source-to-target capture each scenario controls. ``LiveItem.runtest``, the manifest loader, the lifecycle
hooks, the ownership ledger, the report hook, the ``.slt.json`` sidecar and the junit writer are real, so
process exit, junit, the v1 sidecar and the v2 envelope are compared for the same run, and the in-memory
state left behind is read back at session end.
"""
from __future__ import annotations

import json

import pytest

from .exec_harness import CONFTEST, FIXTURES, RUN, Run, _agree, run_case  # noqa: F401 - run_case is a fixture

pytest_plugins = ["pytester"]   # run_case drives a fresh pytest process through pytester


def test_exec_invalid_lifecycle_fails_before_any_fake_provisioning_call(run_case):
    run = run_case("invalid-version")
    env = _agree(run, "failed")
    assert "lifecycle: version 2 is not supported" in run.junit()[2]
    assert not {"resolve_striim", "resolve", "create", "deploy"} & set(run.names())
    assert env["lifecycle"]["mode"] == "unknown" and env["lifecycle"]["cleanup"]["status"] == "skipped"


def test_exec_legacy_case_passes_with_qualifies_false(run_case):
    run = run_case("legacy")
    env = _agree(run, "passed")
    assert env["run"]["qualifiesReason"].startswith("legacy lifecycle")
    assert env["lifecycle"]["mode"] == "legacy" and env["lifecycle"]["ready"]["kind"] == "legacy"
    assert env["resources"]["infrastructure"]["ownership"] == "shared"
    assert [a["type"] for a in env["assertions"]] == ["smoke"]
    assert env["lifecycle"]["cleanup"]["status"] == "ok" and run.dump()["namespaces"] == []


def test_exec_op_poison_recovery_reacquires_a_fresh_in_use_lock(run_case):
    run = run_case(
        "legacy", {"op_stub": True, "op_poison": True}, env={"SLT_OPS_PRELOADED": "1"},
        edit=lambda y: y.replace(
            "requires: [postgres]\n", "requires: [postgres]\nop: {jar: java/OpenProcessors/StubOp}\n"))

    assert run.ret == 0, run.text
    lock_events = [(e["event"], e["kind"]) for e in run.events() if e["event"].startswith("lock_")]
    assert lock_events == [
        ("lock_enter", "shared"),
        ("lock_exit", "shared"),
        ("lock_enter", "exclusive"),
        ("lock_exit", "exclusive"),
        ("lock_enter", "shared"),
        ("lock_exit", "shared"),
    ]
    names = run.names()
    assert names.index("deploy_poison") < names.index("restart_app_nodes") < names.index("deploy_retry")


def test_exec_done_sentinel_never_arrives_empty_target_fails_with_deadline(run_case):
    run = run_case("cdc-sentinel", {"mirror": "live", "mirror_ops": 2})   # only the ready sentinel is captured
    env = _agree(run, "failed")
    lc = env["lifecycle"]
    assert lc["ready"]["reason"] == "satisfied" and lc["completion"]["reason"] == "deadline"
    assert lc["completion"]["kind"] == "sentinel"
    names = run.names()
    assert names.index("deploy") < names.index("insert")                 # the changes ran after readiness
    deletes = [e["id"] for e in run.events() if e["event"] == "delete"]
    assert len(deletes) == 1                                               # the done sentinel was never deleted


def test_exec_stale_prior_attempt_sentinel_row_present_does_not_satisfy(run_case):
    stale = 1999999999                          # a done-sentinel id an earlier attempt wrote, delivered late
    run = run_case("cdc-sentinel", {"mirror": "live", "mirror_ops": 2,              # the current done sentinel is withheld
                                    "deliver_on_deploy": {"qatarget.<TID>tgt": [stale]}})
    env = _agree(run, "failed")
    done = env["lifecycle"]["completion"]
    assert done["reason"] == "deadline"
    done_id = next(o["value"]["id"] for o in done["observations"]
                   if isinstance(o["value"], dict) and o["value"].get("step") == "insert")
    assert done_id != stale
    target = f"qatarget.{run.dump()['tid']}tgt"
    assert {"event": "delivered", "table": target, "ids": [stale]} in run.events()      # the target DDL made the table
    reads = [e for e in run.events() if e["event"] == "observe" and e["table"] == target and e["id"] == str(done_id)]
    assert reads and all(stale in e["rows"] and e["match"] == 0 for e in reads)      # the stale row was there at every read
    counts = [o["value"] for o in done["observations"] if isinstance(o["value"], dict) and o["value"].get("step") == "present"]
    assert counts and {c["count"] for c in counts} == {0}


def test_exec_source_count_zero_zero_fails(run_case):
    run = run_case("initial-load", {"initial_load": True},
                   edit=lambda y: y.replace("seed:\n  - file: seed.sql\n    db: postgres-source\n    when: pre_deploy\n", ""))
    env = _agree(run, "failed")
    assert env["lifecycle"]["ready"]["reason"] == "zero-count" and env["lifecycle"]["baseline"]["count"] == 0
    assert "deploy" not in run.names()


def test_exec_readiness_probe_hang_fails_within_bound(run_case):
    run = run_case("cdc-source-progress", {"mirror": "live", "hang_probe": True})
    env = _agree(run, "failed")
    assert env["lifecycle"]["ready"]["reason"] == "probe-cancelled"
    assert "probe_cancel" in run.names() and run.elapsed < 60


def test_exec_fresh_junit_v1_v2_triple_agree(run_case):
    run = run_case("initial-load", {"initial_load": True})
    env = _agree(run, "passed", qualifies=True)
    lc = env["lifecycle"]
    assert lc["mode"] == "initial-load" and lc["baseline"]["count"] == 3
    assert lc["ready"]["reason"] == "satisfied" and lc["completion"]["reason"] == "satisfied"
    assert lc["cleanup"] == {"status": "ok", "detail": None} and env["resources"]["cleanupVerified"] is True
    assert {(o["kind"], o["state"]) for o in env["resources"]["owned"]} == {("namespace", "verified-absent"),
                                                                           ("pg-table", "verified-absent")}
    assert env["assertions"] == run.sidecar()["assertions"]
    names = run.names()
    assert names.index("create") < names.index("insert") < names.index("deploy")
    tid = run.dump()["tid"]
    assert run.dump()["tables"] == {} and f"qasource.{tid}src" in {o["name"] for o in env["resources"]["owned"]}


def test_exec_undeclared_refused_rc4_before_provisioning(run_case):
    run = run_case("legacy", declared=False)
    assert run.ret == 4, run.text
    assert "SLT_INFRA_OWNERSHIP" in run.text
    assert "resolve_striim" not in run.names() and "resolve" not in run.names()


def test_exec_collect_only_needs_no_declaration(run_case):
    run = run_case("legacy", declared=False, args=("--collect-only",))
    assert run.ret == 0, run.text
    assert run.names() == []


def test_exec_failed_readiness_junit_v1_v2_agree(run_case):
    run = run_case("initial-load", {"initial_load": False})
    env = _agree(run, "failed")
    assert env["lifecycle"]["running"]["reason"] == "satisfied"
    assert env["lifecycle"]["ready"]["reason"] == "deadline" and env["lifecycle"]["completion"] is None
    assert "readiness baseline-landed failed: deadline" in env["run"]["failure"]
    assert env["lifecycle"]["cleanup"]["status"] == "ok" and run.dump()["tables"] == {}


def test_exec_failed_ddl_junit_v1_v2_agree(run_case):
    run = run_case("initial-load", {"ddl_fail": True})
    env = _agree(run, "failed")
    assert "already exists" in env["run"]["failure"]
    assert env["lifecycle"]["cleanup"]["status"] == "ok" and env["lifecycle"]["ready"] is None
    assert "teardown_namespace" in run.names()[run.names().index("resolve"):]


def test_exec_hung_probe_bounded_outcome_with_cleanup_and_evidence(run_case):
    run = run_case("cdc-source-progress", {"mirror": "live", "hang_probe": True})
    env = _agree(run, "failed")
    names = run.names()
    assert names.index("probe_cancel") < len(names) - 1 - names[::-1].index("teardown_namespace")
    assert env["lifecycle"]["cleanup"]["status"] == "ok" and run.dump()["tables"] == {}
    assert env["lifecycle"]["ready"]["deadlineS"] == 3.0


def test_exec_service_setup_failure_before_ledger_writes_error_envelope(run_case):
    run = run_case("initial-load", {"service_fail": True})
    env = _agree(run, "failed")
    assert "service setup failed" in env["run"]["failure"]
    assert env["lifecycle"]["mode"] == "initial-load" and env["lifecycle"]["ready"] is None
    assert "create" not in run.names() and env["lifecycle"]["identity"]["runId"] == RUN
    assert env["lifecycle"]["cleanup"]["status"] == "ok" and env["resources"]["owned"] == []


def test_exec_pass_then_injected_cleanup_fault_rc1_junit_v1_v2_agree(run_case):
    run = run_case("initial-load", {"initial_load": True}, env={"SLT_LIFECYCLE_FAULT": "cleanup:table"})
    env = _agree(run, "failed")
    assert "cleanup failed: pg-table" in run.junit()[2] and "injected-fault" in env["run"]["failure"]
    assert env["lifecycle"]["ready"]["reason"] == "satisfied" and env["lifecycle"]["completion"]["reason"] == "satisfied"
    assert all(a["status"] == "passed" for a in env["assertions"])          # the assertions themselves passed
    assert env["lifecycle"]["cleanup"]["status"] == "failed" and env["lifecycle"]["faultInjected"]["kind"] == "pg-table"
    tid = run.dump()["tid"]
    # tables drop in reverse creation order, so the injected (first) delete is the target table's
    assert list(run.dump()["tables"]) == [f"qatarget.{tid}tgt"]              # only the injected delete was left
    assert "qualifies" in env["run"] and env["run"]["qualifies"] is False


def test_exec_preexisting_foreign_table_refused_untouched(run_case):
    run = run_case("initial-load", {"initial_load": True, "tables": {"qasource.<TID>src": [99]}})
    env = _agree(run, "failed")
    tid = run.dump()["tid"]
    assert f"collision:pg-table:qasource.{tid}src" in env["run"]["failure"]
    assert run.dump()["tables"] == {f"qasource.{tid}src": [99]}
    assert "drop_table" not in run.names() and "deploy" not in run.names()


def test_exec_lifecycle_file_case_owned_dir_lifecycle(run_case):
    run = run_case("file-sink", {"mirror": "live", "slot_active": True})
    env = _agree(run, "passed", qualifies=True)
    dump = run.dump()
    owned_dir = next(o["name"] for o in env["resources"]["owned"] if o["kind"] == "owned-dir")
    assert owned_dir == f"/opt/striim/slt-runs/{env['lifecycle']['identity']['namespace']}"
    assert dump["dirs"] == [] and dump["files"] == []
    assert {"event": "rm", "flag": "-rf", "path": owned_dir} in run.events()
    assert env["lifecycle"]["completion"]["reason"] == "satisfied"


def test_exec_file_sink_initial_load_baseline_sees_the_owned_dir(run_case):
    # samples/live/03-file-output's shape. The pre-deploy seed, then the baseline, whose
    # readiness.path check needs the owned dir confirmed already; it used to be allocated after.
    run = run_case("file-initial-load", {"initial_load": True})
    env = _agree(run, "passed", qualifies=True)
    lc = env["lifecycle"]
    assert lc["baseline"]["count"] == 2 and lc["ready"]["reason"] == "satisfied", lc
    assert lc["completion"]["reason"] == "satisfied"
    owned_dir = next(o["name"] for o in env["resources"]["owned"] if o["kind"] == "owned-dir")
    names = run.names()
    assert names.index("insert") < names.index("mkdir") < names.index("deploy")
    assert {"event": "rm", "flag": "-rf", "path": owned_dir} in run.events() and run.dump()["dirs"] == []


def test_exec_keep_resources_gives_skipped_and_qualifies_false(run_case):
    run = run_case("initial-load", {"initial_load": True}, env={"SLT_KEEP_RESOURCES": "1"})
    env = _agree(run, "passed")
    assert env["lifecycle"]["cleanup"]["status"] == "skipped" and "SLT_KEEP_RESOURCES is set" in env["lifecycle"]["cleanup"]["detail"]
    assert "cleanup skipped" in env["run"]["qualifiesReason"]
    tid = run.dump()["tid"]
    assert set(run.dump()["tables"]) == {f"qasource.{tid}src", f"qatarget.{tid}tgt"}
    assert "python -m livetest.ownership replay" in env["lifecycle"]["cleanup"]["detail"]


def test_exec_ddl_second_file_raises_first_table_cleaned_original_error_reported(run_case):
    run = run_case("initial-load", {"ddl_fail_tgt": True})
    env = _agree(run, "failed")
    tid = run.dump()["tid"]
    assert f'relation "{tid}tgt" already exists' in env["run"]["failure"]
    assert run.dump()["tables"] == {} and {"event": "drop_table", "table": f"qasource.{tid}src"} in run.events()
    assert env["lifecycle"]["cleanup"]["status"] == "ok"


def test_exec_foreign_lookalike_table_and_fixed_name_checkpoint_survive(run_case):
    run = run_case("initial-load", {"initial_load": True,
                                    "tables": {"qasource.t9f8e7d6c_src": [1], "qasource.src": [2]},
                                    "files": {"/opt/striim/ExampleReaderOp.position.json": "{}"}})
    env = _agree(run, "passed", qualifies=True)
    dump = run.dump()
    assert dump["tables"] == {"qasource.t9f8e7d6c_src": [1], "qasource.src": [2]}
    assert dump["files"] == ["/opt/striim/ExampleReaderOp.position.json"]
    assert {f["name"] for f in env["resources"]["foreign"]} >= {"qasource.t9f8e7d6c_src", "qasource.src"}


def test_exec_shared_mode_never_calls_compose_down(run_case):
    run = run_case("legacy", {"provisioned": True, "compose": "compose.yaml"})
    _agree(run, "passed")
    assert "compose_down" not in run.names() and "cluster_down" not in run.names()


# ---------------------------------------------------------------- code review r1: R5, R9

def test_exec_witness_on_a_populated_foreign_target_fails_before_deploy(run_case):
    run = run_case("initial-load", {"initial_load": False, "tables": {"qatarget.previous_result": [1, 2, 3]}},
                   edit=lambda y: y.replace('target: {db: postgres-target, table: "${PG_TARGET_SCHEMA}.${TID}tgt"}',
                                            'target: {db: postgres-target, table: "qatarget.previous_result"}'))
    env = _agree(run, "failed")
    assert "witness-not-owned" in env["run"]["failure"] and env["lifecycle"]["ready"]["reason"] == "witness-not-owned"
    assert "deploy" not in run.names()
    assert run.dump()["tables"] == {"qatarget.previous_result": [1, 2, 3]}


def test_exec_persistence_failure_during_cleanup_keeps_the_original_error(run_case):
    run = run_case("initial-load", {"initial_load": False, "persist_fail_in_cleanup": True})
    env = _agree(run, "failed")
    assert "readiness baseline-landed failed: deadline" in env["run"]["failure"]
    assert env["lifecycle"]["cleanup"]["status"] == "failed"
    assert "No space left on device" in env["lifecycle"]["cleanup"]["detail"]


def test_exec_real_owned_dir_delete_failure_fails_the_run(run_case):
    run = run_case("file-sink", {"mirror": "live", "slot_active": True, "rm_fail": True})
    env = _agree(run, "failed")
    assert "cleanup failed: owned-dir" in run.junit()[2] and "injected-fault" not in run.junit()[2]
    assert env["lifecycle"]["cleanup"]["status"] == "failed" and env["lifecycle"]["faultInjected"] is None
    assert env["lifecycle"]["completion"]["reason"] == "satisfied"


def test_exec_exclusive_session_teardown_failure_fails_the_run(run_case):
    run = run_case("initial-load", {"initial_load": True, "provisioned": True, "compose": "compose.yaml",
                                    "compose_down_fail": True},
                   declared=False, env={"SLT_INFRA_OWNERSHIP": "exclusive"})
    env = _agree(run, "error")
    assert "compose_down" in run.names() and "cluster_down" in run.names()
    teardown = env["resources"]["infrastructure"]["teardown"]
    assert teardown["status"] == "failed" and "network slt-net has active endpoints" in teardown["failures"][0]["error"]
    assert env["resources"]["infrastructure"]["ownership"] == "exclusive"
    assert "infrastructure teardown failed" in env["run"]["qualifiesReason"]
    assert "infrastructure teardown failed" in run.junit()[2]
