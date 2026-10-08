"""Verify: with no project manifest striim-test runs this clone's own suites, located by the path keys;
a set key naming a missing path is exit 2 naming it; a
project manifest's locations reach the tier children as those same keys."""
import json
import os
import shutil

import pytest

from _clikit import REPO, clone_env as _env, framework_env, run_cli

HELLO = ["live:scripts/live/regression/hello/hello-4col::hello-4col",
         "live:scripts/live/regression/hello/hello-4col-ora::hello-4col-ora",
         "live:scripts/live/regression/hello/hello-cluster::hello-cluster",
         "live:scripts/live/regression/hello/hello-single::hello-single"]


def test_list_without_a_manifest_lists_the_clones_hello_cases(clone, elsewhere):
    r = run_cli(["list", "--suite", "scripts/live/regression/hello"], cwd=elsewhere, env=_env(clone))
    assert r.rc == 0, r.stderr
    assert r.ids() == HELLO
    assert r.run_dir.parent == clone / "scripts" / "live" / "runs"        # default state dir
    identity = json.loads((r.run_dir / "identity.json").read_text())
    assert identity["manifest"] is None and identity["consumerRoot"] == str(clone)


def test_default_project_has_only_the_tiers_that_exist(clone, elsewhere):
    # The framework ships live cases only (integration regression and perf stay in your test repo).
    r = run_cli(["list"], cwd=elsewhere, env=_env(clone))
    assert r.rc == 0, r.stderr
    assert r.ids() == HELLO
    assert sorted(p.name for p in r.run_dir.iterdir() if p.is_dir()) == ["live"]


def test_state_dir_key_moves_the_run_dir(clone, tmp_path, elsewhere):
    state = tmp_path / "state"
    state.mkdir()
    r = run_cli(["list"], cwd=elsewhere, env=_env(clone, SLT_STATE_DIR=state))
    assert r.rc == 0, r.stderr
    assert r.run_dir.parent == state / "runs"
    assert not (clone / "scripts" / "live" / "runs").exists()


def test_live_cases_key_selects_another_case_tree(clone, tmp_path, elsewhere):
    cases = tmp_path / "my-cases"
    shutil.copytree(REPO / "scripts/live/regression/hello/hello-single", cases / "mine")
    (cases / "mine" / "test.yaml").write_text(
        (cases / "mine" / "test.yaml").read_text().replace("name: hello-single", "name: mine"))
    r = run_cli(["list"], cwd=elsewhere, env=_env(clone, SLT_LIVE_CASES=cases, SLT_PROJECT_ROOT=tmp_path))
    assert r.rc == 0, r.stderr
    assert r.ids() == ["live:my-cases/mine::mine"]


@pytest.mark.parametrize("key", ["SLT_LIVE_CASES", "SLT_STATE_DIR", "SLT_PROJECT_ROOT"])
def test_set_but_missing_key_is_exit_2_naming_it(clone, tmp_path, elsewhere, key):
    r = run_cli(["list"], cwd=elsewhere, env=_env(clone, **{key: tmp_path / "nope"}))
    assert r.rc == 2 and r.run_dir is None
    assert key in r.stderr and "does not exist" in r.stderr


def test_gold_targets_naming_a_missing_file_is_exit_2(clone, tmp_path, elsewhere):
    r = run_cli(["list"], cwd=elsewhere, env=_env(clone, GOLD_TARGETS=tmp_path / "nope.yaml"))
    assert r.rc == 2 and r.run_dir is None and "GOLD_TARGETS" in r.stderr


_DUMP = ("import json, os\n"
         "def pytest_configure(config):\n"
         "    keys = ('SLT_PROJECT_ROOT', 'SLT_LIVE_CASES', 'SLT_INT_CASES', 'SLT_STATE_DIR',\n"
         "            'SLT_FRAMEWORK_HOME', 'SLT_FRAMEWORK_MODE', 'GOLD_TARGETS')\n"
         "    with open(os.environ['XTR_ENV_DUMP'], 'w') as f:\n"
         "        json.dump({k: os.environ.get(k) for k in keys}, f)\n")


def test_manifest_locations_reach_the_tier_child(project, tmp_path, elsewhere):
    consumer = project.parent
    (consumer / "cases" / "live" / "conftest.py").write_text(_DUMP)
    dump = tmp_path / "env.json"
    r = run_cli(["list", "--tier", "live", "--targets", project], cwd=elsewhere,
                env=framework_env(XTR_ENV_DUMP=dump))
    assert r.rc == 0, r.stderr
    seen = json.loads(dump.read_text())
    assert seen == {"SLT_PROJECT_ROOT": str(consumer.resolve()),
                    "SLT_LIVE_CASES": str((consumer / "cases/live").resolve()),
                    "SLT_INT_CASES": str((consumer / "cases/integration").resolve()),
                    "SLT_STATE_DIR": str((consumer / ".state").resolve()),
                    "SLT_FRAMEWORK_HOME": str(REPO), "SLT_FRAMEWORK_MODE": "clone",
                    "GOLD_TARGETS": str(project)}


def test_default_project_hands_no_location_keys_down(clone, tmp_path, elsewhere):
    (clone / "scripts/live/regression/hello/conftest.py").write_text(_DUMP)
    dump = tmp_path / "env.json"
    r = run_cli(["list"], cwd=elsewhere, env=_env(clone, XTR_ENV_DUMP=dump))
    assert r.rc == 0, r.stderr
    seen = json.loads(dump.read_text())
    assert {k: v for k, v in seen.items() if v is not None} == {
        "SLT_FRAMEWORK_HOME": str(clone), "SLT_FRAMEWORK_MODE": "clone"}


def _misplaced_perf(project):
    project.write_text(project.read_text().replace("perf: cases/perf", "perf: cases/elsewhere-perf"))
    (project.parent / "cases" / "elsewhere-perf").mkdir()


def test_perf_suite_away_from_the_integration_cases_is_refused_for_perf(project, elsewhere):
    _misplaced_perf(project)
    r = run_cli(["list", "--tier", "perf", "--targets", project], cwd=elsewhere)
    assert r.rc == 2 and "beside suites.integration" in r.stderr, r.stderr


def test_misplaced_perf_suite_does_not_block_other_tiers(project, elsewhere):
    _misplaced_perf(project)
    r = run_cli(["list", "--tier", "live", "--targets", project], cwd=elsewhere)
    assert r.rc == 0 and "live:cases/live/alpha::alpha" in r.ids(), r.stderr


def test_manifest_without_state_dir_uses_the_consumers_env_state(project, tmp_path, elsewhere):
    # The parent's run dir and the children's state come from the same .env: the consumer's.
    consumer = project.parent
    project.write_text(project.read_text().replace("stateDir: .state\n", ""))
    (consumer / "from-env").mkdir()
    (consumer / ".env").write_text("SLT_STATE_DIR=from-env\n")
    (consumer / "cases" / "live" / "conftest.py").write_text(_DUMP)
    dump = tmp_path / "env.json"
    r = run_cli(["list", "--tier", "live", "--targets", project], cwd=elsewhere,
                env=framework_env(XTR_ENV_DUMP=dump))
    assert r.rc == 0, r.stderr
    assert r.run_dir.parent == (consumer / "from-env").resolve() / "runs"
    assert json.loads(dump.read_text())["SLT_STATE_DIR"] == str((consumer / "from-env").resolve())


_INT_DUMP = ("import json, os\n"
             "def pytest_configure(config):\n"
             "    from inttest import plugin, resources, services\n"
             "    with open(os.environ['XTR_ENV_DUMP'], 'w') as f:\n"
             "        json.dump({'SLT_STATE_DIR': os.environ.get('SLT_STATE_DIR'),\n"
             "                   'state_root': str(resources.state_root()),\n"
             "                   'started': str(services.started_registry_path()),\n"
             "                   'session_lock': str(plugin._session_lock_path())}, f)\n")


def test_manifest_without_state_dir_keeps_the_integration_state_default(clone, project, tmp_path, elsewhere):
    # With no stateDir and SLT_STATE_DIR unset, the integration child keeps its own
    # default (scripts/integration, plan A7), where the CLI and a direct pytest look, not scripts/live
    consumer = project.parent
    project.write_text(project.read_text().replace("stateDir: .state\n", ""))
    (consumer / "cases" / "integration" / "conftest.py").write_text(_INT_DUMP)
    dump = tmp_path / "env.json"
    r = run_cli(["list", "--tier", "integration", "--targets", project], cwd=elsewhere,
                env=_env(clone, XTR_ENV_DUMP=dump))
    assert r.rc == 0, r.stderr
    seen = json.loads(dump.read_text())
    integ = clone / "scripts" / "integration"
    assert seen == {"SLT_STATE_DIR": None, "state_root": str(integ),
                    "started": str(integ / ".int-services-up.json"),
                    "session_lock": str(integ / ".int-session.lock")}
    assert r.run_dir.parent == clone / "scripts" / "live" / "runs"     # the parent's run dir is unchanged


@pytest.mark.parametrize("mode", ["wheel", "legacy"])
def test_retired_framework_mode_in_the_manifest_is_refused(project, elsewhere, mode):
    extra = "framework:\n  mode: wheel\n  wheel: w.whl\n  lock: l.lock\n" if mode == "wheel" \
        else "framework:\n  mode: legacy\n"
    project.write_text(project.read_text() + extra)
    r = run_cli(["list", "--tier", "live", "--targets", project], cwd=elsewhere)
    assert r.rc == 2 and r.run_dir is None
    assert f"framework.mode={mode}" in r.stderr and "retired" in r.stderr


def test_sibling_framework_mode_in_the_manifest_is_accepted(project, elsewhere):
    project.write_text(project.read_text() + "framework:\n  mode: sibling\n")
    r = run_cli(["list", "--tier", "live", "--targets", project], cwd=elsewhere)
    assert r.rc == 0, r.stderr


def _case(root, name):
    shutil.copytree(REPO / "scripts/live/regression/hello/hello-single", root / name)
    (root / name / "test.yaml").write_text(
        (root / name / "test.yaml").read_text().replace("name: hello-single", f"name: {name}"))


def test_live_cases_key_lists_several_case_roots(clone, tmp_path, elsewhere):
    # The primary root keeps project-relative ids; a root outside the project is named by its
    # directory name, so every id stays unambiguous and machine-independent.
    proj, extra = tmp_path / "proj", tmp_path / "shared" / "services"
    _case(proj / "cases", "mine"); _case(extra / "svc", "theirs")
    roots = f"{proj / 'cases'}{os.pathsep}{extra}"
    r = run_cli(["list", "--tier", "live"], cwd=elsewhere, env=_env(clone, SLT_LIVE_CASES=roots, SLT_PROJECT_ROOT=proj))
    assert r.rc == 0, r.stderr
    assert r.ids() == ["live:cases/mine::mine", "live:services/svc/theirs::theirs"]


def test_a_manifest_lists_several_live_case_roots(project, tmp_path, elsewhere):
    extra = tmp_path / "shared" / "services"
    _case(extra / "svc", "theirs")
    text = project.read_text().replace(
        "  live: cases/live\n", "  live:\n    - cases/live\n    - ${XTR_SHARED_CASES}/services\n")
    project.write_text(text)
    env = framework_env(GOLD_TARGETS=project, XTR_SHARED_CASES=tmp_path / "shared")
    r = run_cli(["list", "--tier", "live"], cwd=elsewhere, env=env)
    assert r.rc == 0, r.stderr
    assert "live:services/svc/theirs::theirs" in r.ids()
    assert any(i.startswith("live:cases/live/") for i in r.ids())
    # An extra root whose variable is unset is left out; the primary root still lists.
    r = run_cli(["list", "--tier", "live"], cwd=elsewhere, env=framework_env(GOLD_TARGETS=project))
    assert r.rc == 0, r.stderr
    assert not any("theirs" in i for i in r.ids()) and any(i.startswith("live:cases/live/") for i in r.ids())


def test_the_documented_framework_root_lists_without_slt_framework_home(project, elsewhere):
    # The "Several case roots" example names ${SLT_FRAMEWORK_HOME}; unset, it is this checkout.
    project.write_text(project.read_text().replace(
        "  live: cases/live\n",
        "  live:\n    - cases/live\n    - ${SLT_FRAMEWORK_HOME}/scripts/live/regression/services\n"))
    env = framework_env(GOLD_TARGETS=project)
    assert "SLT_FRAMEWORK_HOME" not in env
    r = run_cli(["list", "--tier", "live"], cwd=elsewhere, env=env)
    assert r.rc == 0, r.stderr
    services = sorted((REPO / "scripts/live/regression/services").glob("*/*/test.yaml"))
    assert services and all(any(i.endswith(f"::{t.parent.name}") for i in r.ids()) for t in services)
    assert any(i.startswith("live:cases/live/") for i in r.ids())


@pytest.mark.parametrize("value,listed", [("", False), ("  ", False), (str(REPO), True)])
def test_a_set_slt_framework_home_keeps_its_meaning(project, elsewhere, value, listed):
    # Only an absent variable defaults; an explicitly empty one leaves the entry out.
    project.write_text(project.read_text().replace(
        "  live: cases/live\n",
        "  live:\n    - cases/live\n    - ${SLT_FRAMEWORK_HOME}/scripts/live/regression/services\n"))
    r = run_cli(["list", "--tier", "live"], cwd=elsewhere,
                env=framework_env(GOLD_TARGETS=project, SLT_FRAMEWORK_HOME=value))
    assert r.rc == 0, r.stderr
    assert any(i.startswith("live:cases/live/") for i in r.ids())
    assert any("::postgres" in i or "services/" in i for i in r.ids()) == listed, r.ids()


def test_the_same_case_name_in_two_live_case_roots_is_refused(clone, tmp_path, elsewhere):
    proj, extra = tmp_path / "proj", tmp_path / "shared" / "services"
    _case(proj / "cases", "same"); _case(extra / "svc", "same")
    r = run_cli(["list", "--tier", "live"], cwd=elsewhere,
                env=_env(clone, SLT_LIVE_CASES=f"{proj / 'cases'}{os.pathsep}{extra}", SLT_PROJECT_ROOT=proj))
    assert r.rc != 0 and "same" in (r.stderr + r.stdout) and "duplicate" in (r.stderr + r.stdout).lower()


def test_an_external_case_id_does_not_change_with_the_selected_suite(project, tmp_path, elsewhere):
    extra = tmp_path / "shared" / "services"
    _case(extra / "svc", "theirs")
    project.write_text(project.read_text().replace(
        "  live: cases/live\n", f"  live:\n    - cases/live\n    - {extra}\n"))
    env = framework_env(GOLD_TARGETS=project)
    full = "live:services/svc/theirs::theirs"
    assert full in run_cli(["list", "--tier", "live"], cwd=elsewhere, env=env).ids()
    for suite in ("../shared/services/svc", "../shared/services/svc/theirs"):
        r = run_cli(["list", "--tier", "live", "--suite", suite], cwd=elsewhere, env=env)
        assert r.rc == 0 and r.ids() == [full], r.stdout + r.stderr
    r = run_cli(["run", "--dry-run", "--tier", "live", "--suite", "../shared/services/svc", "--case", full],
                cwd=elsewhere, env=env)
    assert r.rc == 0, r.stdout + r.stderr


def test_one_case_root_outside_the_project_keeps_its_absolute_ids(clone, tmp_path, elsewhere):
    cases = tmp_path / "outside" / "cases"
    _case(cases, "solo"); (tmp_path / "proj").mkdir()
    r = run_cli(["list", "--tier", "live"], cwd=elsewhere,
                env=_env(clone, SLT_LIVE_CASES=cases, SLT_PROJECT_ROOT=tmp_path / "proj"))
    assert r.rc == 0, r.stderr
    assert r.ids() == [f"live:{(cases / 'solo').resolve()}::solo"]
