"""Verify: ``striim-test run [PATH]`` selects the cases under PATH (a case dir, its test.yaml, or
a dir of cases), resolved against the working directory and located in the case root that holds
it. A PATH outside every case root, or missing, is exit 2 before any run directory."""
import pytest

from _clikit import clone_env, run_cli

ALPHA = "live:cases/live/alpha::alpha"


def dry(project, *argv, cwd=None):
    return run_cli(["run", *map(str, argv), "--dry-run", "--targets", project],
                   cwd=cwd or project.parent)


@pytest.mark.parametrize("path", ["cases/live/alpha", "cases/live/alpha/test.yaml",
                                  "cases/live/alpha/"], ids=["dir", "file", "trailing-slash"])
def test_path_selects_one_case(project, path):
    r = dry(project, path)
    assert r.rc == 0 and r.ids() == [ALPHA], r.stderr


def test_path_selects_a_folder_of_cases(project):
    whole = run_cli(["list", "--tier", "live", "--targets", project], cwd=project.parent)
    r = dry(project, "cases/live")
    assert r.rc == 0 and r.ids() == whole.ids() and ALPHA in r.ids(), r.stderr


def test_path_picks_its_tier(project):
    r = dry(project, "cases/integration/twin")
    assert r.rc == 0 and r.ids() and all(i.startswith("integration:cases/integration/twin::")
                                         for i in r.ids()), r.stderr


def test_absolute_path_from_unrelated_cwd(project, elsewhere):
    r = dry(project, project.parent / "cases/live/alpha", cwd=elsewhere)
    assert r.rc == 0 and r.ids() == [ALPHA], r.stderr


def test_path_with_case_narrows(project):
    r = dry(project, "cases/live", "--case", "alpha")
    assert r.rc == 0 and r.ids() == [ALPHA], r.stderr


def test_path_in_default_project(clone):
    """No manifest: the case roots are the path keys' (here the clone's regression/)."""
    r = run_cli(["run", "scripts/live/regression/hello/hello-4col", "--dry-run"], cwd=clone,
                env=clone_env(clone))
    assert r.rc == 0 and r.ids() == [
        "live:scripts/live/regression/hello/hello-4col::hello-4col"], r.stderr


def test_path_outside_every_case_root(project, tmp_path):
    stray = tmp_path / "stray"
    stray.mkdir()
    r = dry(project, stray)
    root = project.parent.resolve()
    assert r.rc == 2 and r.run_dir is None, r.stderr
    assert (f"striim-test: error: {stray} is outside every case root (live: {root}/cases/live, "
            f"integration: {root}/cases/integration, perf: {root}/cases/perf)") in r.stderr


def test_missing_path(project):
    r = dry(project, "cases/live/nope")
    assert r.rc == 2 and r.run_dir is None, r.stderr
    assert (f"striim-test: error: cases/live/nope does not exist "
            f"({project.parent.resolve()}/cases/live/nope)") in r.stderr


@pytest.mark.parametrize("extra,message", [
    (["--suite", "cases/live"], "PATH and --suite both select a folder; pass one of them"),
    (["--tier", "integration"], "cases/live/alpha is in the live case root, not integration"),
], ids=["suite", "tier"])
def test_path_conflicts(project, extra, message):
    r = dry(project, "cases/live/alpha", *extra)
    assert r.rc == 2 and r.run_dir is None and f"striim-test: error: {message}" in r.stderr, \
        r.stderr
