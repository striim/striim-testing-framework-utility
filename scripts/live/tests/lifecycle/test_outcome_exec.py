"""The outcome a live case reports, executed through the real ``livetest.plugin`` and striim-test's guard
(tests/lifecycle/exec_harness.py): fail, skip, xfail or refuse, and what striim-test makes of it."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from .exec_harness import FIXTURES as FIXTURES_LIFECYCLE, run_case  # noqa: F401 - run_case is a fixture

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


def _with_op_twin(root, name):
    """A second case beside ``legacy`` that loads an OP module; ``legacy`` itself loads none."""
    twin = root / "cases" / "legacy-op"
    shutil.copytree(root / "cases" / "legacy", twin)
    manifest = twin / "test.yaml"
    manifest.write_text(OP(manifest.read_text()).replace(f"name: {name}\n", f"name: {name}-op\n"))


def test_a_module_that_could_not_be_built_fails_the_run_beside_a_passing_case(run_case):
    # The skipped case is never tested, so the case that passed cannot vouch for the run.
    run = run_case("legacy", {"op_build": "missing"}, prepare=_with_op_twin)
    run.result.assert_outcomes(passed=1, skipped=1)
    assert run.ret == 1, run.text
    assert "could not be built" in run.text and "lc-legacy-op" in run.text


def test_a_missing_service_jar_skips_with_a_cause_and_an_all_skipped_run_fails(run_case):
    run = run_case("legacy", {"service_jar_missing": "no JDBC driver jar in deps/"})
    status, _, xml = run.junit()
    assert status == "skipped" and "service postgres unavailable: no JDBC driver jar" in xml, run.text
    assert run.ret == 1, run.text
    assert "PROVED NOTHING" in run.text and "deploy" not in run.names()


def test_a_service_that_cannot_be_prepared_skips_with_a_cause_and_an_all_skipped_run_fails(run_case):
    run = run_case("legacy", {"service_unavailable": "set SLT_PG_DATA_DIR"})
    status, _, xml = run.junit()
    assert status == "skipped" and "service postgres unavailable: set SLT_PG_DATA_DIR" in xml, run.text
    assert run.ret == 1, run.text


def test_a_missing_service_jar_beside_a_passing_case_keeps_the_run_green(run_case):
    # Absent infrastructure in a partial run is an ordinary skip, as for Docker: only a run in
    # which nothing executed fails.
    def twin(root, name):
        _with_op_twin(root, name)
        manifest = root / "cases" / "legacy-op" / "test.yaml"
        manifest.write_text(manifest.read_text().replace("requires: [postgres]", "requires: [mysql]"))
    run = run_case("legacy", {"op_stub": True, "service_jar_missing": {"mysql": "no JDBC driver jar"}}, prepare=twin)
    run.result.assert_outcomes(passed=1, skipped=1)
    assert run.ret == 0, run.text


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


def test_a_collected_case_outside_the_suite_root_is_deselected(run_case):
    """pytest is handed a directory outside the selected suite: what it collects there never runs."""
    def outside(root, name):
        dest = root / "elsewhere" / "outsider"
        shutil.copytree(root / "cases" / "legacy", dest)
        manifest = dest / "test.yaml"
        manifest.write_text(manifest.read_text().replace(f"name: {name}\n", "name: lc-outsider\n"))
    run = run_case("legacy", args=("elsewhere",), prepare=outside, guard=True)
    assert run.ret == 0 and run.junit()[0] == "passed", run.text
    sel = json.loads((run.root / "live" / "selection.json").read_text())
    assert [(e["manifestName"], e["reason"]) for e in sel["deselected"]] == [("lc-outsider", "outside-suite")]
    assert [e["manifestName"] for e in sel["selected"]] == ["lc-legacy"]
    assert run.names().count("deploy") == 1


# A partial run: one case is executed and reports an expected-failure outcome, the other could not
# run because Docker was unavailable for its service. The infra guard keeps the exit status of a
# partial run, so the executed XPASS/XFAIL must count as executed.

_DOCKERLESS = r'''

import os as _os
from livetest.services import DockerUnavailable as _DockerUnavailable

_resolve_before_block = I.resolve_service


def _resolve_blocked(infra, svc, env, started, resolve_fn, progress=None):
    if "zz-blocked" in _os.environ.get("PYTEST_CURRENT_TEST", ""):
        raise _DockerUnavailable("Cannot connect to the Docker daemon")
    return _resolve_before_block(infra, svc, env, started, resolve_fn, progress=progress)


I.resolve_service = _resolve_blocked
'''


def _with_dockerless_sibling(root, name):
    blocked = root / "cases" / "zz-blocked"
    shutil.copytree(FIXTURES_LIFECYCLE / "legacy", blocked)
    y = blocked / "test.yaml"
    y.write_text(y.read_text().replace("name: lc-legacy", "name: zz-blocked"))
    with (root / "conftest.py").open("a") as f:
        f.write(_DOCKERLESS)


def test_an_xpass_beside_a_docker_skip_is_a_partial_run(run_case):
    run = run_case("legacy", edit=lambda y: y + "xfail: {reason: known defect, tiers: [data], strict: false}\n",
                   prepare=_with_dockerless_sibling, args=("-rs",))
    assert "no Docker): Cannot connect to the Docker daemon" in run.text and "XPASS" in run.text, run.text
    run.result.assert_outcomes(skipped=1, xpassed=1)
    assert "PROVED NOTHING" not in run.text and run.ret == 0, run.text


def test_an_xfail_beside_a_docker_skip_is_a_partial_run(run_case):
    run = run_case("exact-wrong-golden", {"initial_load": True}, fixtures=EVIDENCE_CASES, edit=XFAIL_DATA,
                   prepare=_with_dockerless_sibling, args=("-rs",))
    assert "no Docker): Cannot connect to the Docker daemon" in run.text, run.text
    run.result.assert_outcomes(skipped=1, xfailed=1)
    assert "PROVED NOTHING" not in run.text and run.ret == 0, run.text


def test_an_all_docker_skipped_run_still_proves_nothing(run_case):
    # The control: the same blocked sibling alone still fails the run.
    def only_blocked(root, name):
        _with_dockerless_sibling(root, name)
        shutil.rmtree(root / "cases" / "legacy")
    run = run_case("legacy", prepare=only_blocked)
    run.result.assert_outcomes(skipped=1)
    assert "PROVED NOTHING" in run.text and run.ret == 1, run.text
