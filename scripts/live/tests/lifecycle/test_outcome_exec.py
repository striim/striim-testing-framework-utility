"""The outcome a live case reports, executed through the real ``livetest.plugin`` and striim-test's guard
(tests/lifecycle/exec_harness.py): fail, skip, xfail or refuse, and what striim-test makes of it."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from .exec_harness import run_case  # noqa: F401 - run_case is a fixture

pytest_plugins = ["pytester"]

EVIDENCE_CASES = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "evidence" / "cases"
OP = lambda y: y.replace("requires: [postgres]\n", "requires: [postgres]\nop: {jar: java/OpenProcessors/StubOp}\n")
XFAIL_DATA = lambda y: y + "xfail: {reason: known defect, tiers: [data]}\n"


def _exit(run):
    """What striim-test reports for this tier, from the guard's own files."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts" / "cli"))
    from striim_test.dispatch import map_exit
    sel = json.loads((run.root / "live" / "selection.json").read_text())
    res = json.loads((run.root / "live" / "results.json").read_text())
    return map_exit(run.ret, sel, res, False), res


def test_op_build_failure_is_a_failure_not_a_skip(run_case):
    run = run_case("legacy", {"op_stub": True, "op_build": "failed"}, edit=OP, guard=True)
    status, _, xml = run.junit()
    assert status == "failed" and "mvn package failed in StubOp" in xml, run.text
    (code, reason), res = _exit(run)
    assert (run.ret, code, reason) == (1, 1, "tests-failed") and not res["skipped"]
    assert "deploy" not in run.names()


def test_rejected_prebuilt_jar_is_a_failure(run_case):
    run = run_case("legacy", {"op_stub": True, "op_build": "rejected"}, edit=OP, guard=True)
    status, _, xml = run.junit()
    assert status == "failed" and "prebuilt jar (SLT_PREBUILT_JAR)" in xml, run.text
    assert _exit(run)[0] == (1, "tests-failed")


def test_missing_build_tool_is_a_skip(run_case):
    run = run_case("legacy", {"op_stub": True, "op_build": "missing"}, edit=OP, guard=True)
    assert run.junit()[0] == "skipped", run.text
    (code, reason), _ = _exit(run)
    assert (code, reason) == (3, "selected-item-skipped")
    assert run.ret == 1                                       # the infra guard: a skip is not a pass


def test_unset_striim_home_with_no_platform_jar_is_a_skip(run_case):
    # The documented Docker-only setup leaves STRIIM_HOME unset; mvn would then fail on the pom's
    # default for a missing install, which is not a broken module.
    run = run_case("legacy", {"op_build": "no_striim_home"}, edit=OP, guard=True)
    status, _, xml = run.junit()
    assert status == "skipped" and "STRIIM_HOME is unset" in xml, run.text
    assert _exit(run)[0] == (3, "selected-item-skipped") and "mvn" not in run.names()


def test_no_supported_assertion_ran_fails(run_case):
    run = run_case("legacy", edit=lambda y: y.replace("smoke: true", "smoke: false"))
    status, _, xml = run.junit()
    assert status == "failed" and "no supported assertion ran" in xml, run.text
    assert run.ret == 1


def test_duplicate_manifest_name_is_refused(run_case):
    def twin(root, name):
        shutil.copytree(root / "cases" / "legacy", root / "cases" / "legacy-twin")
    run = run_case("legacy", prepare=twin)
    assert run.ret == 4 and "duplicate live-test name" in run.text, run.text
    assert "deploy" not in run.names()


def test_xfail_on_a_named_tier_is_xfailed(run_case):
    run = run_case("exact-wrong-golden", {"initial_load": True}, fixtures=EVIDENCE_CASES, edit=XFAIL_DATA,
                   guard=True)
    assert run.junit()[0] == "skipped" and run.ret == 0, run.text
    (code, reason), res = _exit(run)
    assert (code, reason) == (0, "ok") and len(res["xfailed"]) == 1 and not res["failed"]


def test_xfail_does_not_cover_a_deploy_error(run_case):
    run = run_case("legacy", {"deploy_fail_echo": True}, edit=XFAIL_DATA, guard=True)
    assert run.junit()[0] == "failed" and run.ret == 1, run.text
    assert _exit(run)[0] == (1, "tests-failed")


KEEP = {"SLT_KEEP_RESOURCES_ON_ERROR": "1"}


def _kept(run):
    cleanup = run.envelope()["lifecycle"]["cleanup"]
    return cleanup["status"] == "skipped" and "SLT_KEEP_RESOURCES_ON_ERROR is set" in cleanup["detail"]


def test_keep_on_error_keeps_a_real_failure(run_case):
    run = run_case("exact-wrong-golden", {"initial_load": True}, fixtures=EVIDENCE_CASES, env=KEEP)
    assert run.junit()[0] == "failed", run.text
    assert _kept(run) and run.dump()["tables"]


def test_keep_on_error_drops_an_expected_xfail(run_case):
    run = run_case("exact-wrong-golden", {"initial_load": True}, fixtures=EVIDENCE_CASES, edit=XFAIL_DATA, env=KEEP)
    assert run.junit()[0] == "skipped", run.text
    assert not _kept(run) and run.envelope()["lifecycle"]["cleanup"]["status"] == "ok"
    assert run.dump()["tables"] == {} and run.dump()["namespaces"] == []


def test_keep_on_error_drops_a_skip_after_deploy(run_case):
    run = run_case("legacy", env={**KEEP, "SLT_SKIP_VERIFY": "1"})
    assert run.junit()[0] == "skipped" and "deploy" in run.names(), run.text
    assert "SLT_KEEP_RESOURCES_ON_ERROR is set" not in run.text
    assert run.dump()["namespaces"] == []
