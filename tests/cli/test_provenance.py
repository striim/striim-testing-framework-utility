"""Verify: striim-test runs from one framework clone. SLT_FRAMEWORK_HOME is optional and, when set,
must name that clone; an installed copy, a split origin, a PYTHONPATH decoy or a conftest that
injects one is refused (exit 2)."""
import os

import pytest

from _clikit import (FIXTURES, PACKAGES, REPO, clean_env, clone_copy, clone_env, framework_env,
                     run_cli, site_layout)


def test_framework_home_unset_defaults_to_the_clone(project, elsewhere):
    r = run_cli(["list", "--tier", "live", "--targets", project], cwd=elsewhere,
                env=framework_env(SLT_FRAMEWORK_HOME=None))
    assert r.rc == 0, r.stderr
    identity = r.run_dir / "identity.json"
    assert f'"frameworkHome": "{REPO}"' in identity.read_text()
    assert '"mode": "clone"' in identity.read_text()


@pytest.mark.parametrize("home", ["repo", "scripts"])
def test_framework_home_naming_this_clone_is_accepted(project, elsewhere, home):
    value = REPO if home == "repo" else REPO / "scripts"
    r = run_cli(["list", "--tier", "live", "--targets", project], cwd=elsewhere,
                env=framework_env(SLT_FRAMEWORK_HOME=value))
    assert r.rc == 0, r.stderr


def test_framework_home_naming_another_clone_is_refused(project, tmp_path, elsewhere):
    other = clone_copy(tmp_path / "other-clone")
    r = run_cli(["list", "--targets", project], cwd=elsewhere,
                env=framework_env(SLT_FRAMEWORK_HOME=other))
    assert r.rc == 2 and r.run_dir is None
    assert "SLT_FRAMEWORK_HOME=" in r.stderr and str(other) in r.stderr and str(REPO) in r.stderr


def test_framework_home_set_but_missing_names_the_key(project, tmp_path, elsewhere):
    r = run_cli(["list", "--targets", project], cwd=elsewhere,
                env=framework_env(SLT_FRAMEWORK_HOME=tmp_path / "nope"))
    assert r.rc == 2 and r.run_dir is None
    assert "SLT_FRAMEWORK_HOME" in r.stderr and "does not exist" in r.stderr


def test_installed_copy_is_refused(project, tmp_path, elsewhere):
    site = site_layout(tmp_path / "installed")
    r = run_cli(["list", "--targets", project], cwd=elsewhere, env=clean_env(PYTHONPATH=site))
    assert r.rc == 2 and r.run_dir is None
    assert "installed copy" in r.stderr and "pip install -e" in r.stderr


def test_packages_from_two_clones_are_refused(project, tmp_path, elsewhere):
    other = clone_copy(tmp_path / "other-clone")
    split = [other / PACKAGES[0][1]] + [REPO / parent for _, parent in PACKAGES[1:]]
    r = run_cli(["list", "--targets", project], cwd=elsewhere,
                env=clean_env(PYTHONPATH=os.pathsep.join(map(str, split))))
    assert r.rc == 2 and "more than one clone" in r.stderr


def test_pythonpath_decoy_rejected(project, elsewhere):
    env = framework_env()
    env["PYTHONPATH"] = os.pathsep.join([str(FIXTURES / "legacy-fake"), env["PYTHONPATH"]])
    r = run_cli(["list", "--targets", project], cwd=elsewhere, env=env)
    assert r.rc == 2 and r.run_dir is None
    assert str((FIXTURES / "legacy-fake" / "livetest").resolve()) in r.stderr
    assert not (project.parent / ".state").exists()


def test_conftest_sys_path_injection_rejected(isolation_project, elsewhere):
    r = run_cli(["list", "--tier", "live", "--suite", "cases/badconftest", "--targets",
                 isolation_project], cwd=elsewhere)
    assert r.rc == 2 and "provenance-violation" in r.stderr, r.stderr
    violations = r.part("live")["provenanceViolations"]
    assert any(".decoy" in v for v in violations), violations


@pytest.mark.parametrize("name", ["SLT_MODE", "SLT_FRAMEWORK"])
def test_retired_names_are_refused(project, elsewhere, name):
    r = run_cli(["list", "--targets", project], cwd=elsewhere, env=framework_env(**{name: "x"}))
    assert r.rc == 2 and f"{name} is retired and never read" in r.stderr


def test_framework_home_from_dotenv_naming_another_clone_is_refused(clone, tmp_path, elsewhere):
    other = clone_copy(tmp_path / "other-clone")
    (clone / ".env").write_text(f"SLT_FRAMEWORK_HOME={other}\n")
    r = run_cli(["list"], cwd=elsewhere, env=clone_env(clone))
    assert r.rc == 2 and r.run_dir is None
    assert str(clone / ".env") in r.stderr and str(other) in r.stderr


def test_framework_home_from_dotenv_naming_this_clone_is_accepted(clone, elsewhere):
    (clone / ".env").write_text("SLT_FRAMEWORK_HOME=.\n")            # relative to the .env's own dir
    r = run_cli(["list"], cwd=elsewhere, env=clone_env(clone))
    assert r.rc == 0, r.stderr
