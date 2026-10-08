"""(C5/C7.5, contract set 1.7.0): ``striim-test run`` hands its tier children
``SLT_RUN_EPOCH`` = the run directory basename (an operator value wins) and records it, with the
declared infrastructure ownership, in ``identity.json``."""
import json
import re

from _clikit import framework_env, run_cli


def _records(r):
    identity = json.loads((r.run_dir / "identity.json").read_text())
    command = json.loads((r.run_dir / "live" / "command.json").read_text())
    return identity, command


def test_run_sets_slt_run_epoch_to_run_dir_basename_and_records_it(project, elsewhere):
    argv = ["run", "--tier", "live", "--dry-run", "--targets", project]
    r = run_cli(argv, cwd=elsewhere, env=framework_env(SLT_RUN_EPOCH=None, SLT_INFRA_OWNERSHIP=None))
    assert r.rc == 0, r.stderr
    assert re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{8}", r.run_dir.name)
    identity, command = _records(r)
    assert identity["runEpoch"] == r.run_dir.name                      # the whole basename, not the suffix
    assert identity["infraOwnership"] is None
    assert command["env"]["SLT_RUN_EPOCH"] == r.run_dir.name

    op = run_cli(argv, cwd=elsewhere,
                 env=framework_env(SLT_RUN_EPOCH="operator-epoch-1", SLT_INFRA_OWNERSHIP="shared"))
    assert op.rc == 0, op.stderr                                        # --dry-run never declares ownership
    identity, command = _records(op)
    assert identity["runEpoch"] == "operator-epoch-1" and command["env"]["SLT_RUN_EPOCH"] == "operator-epoch-1"
    assert identity["infraOwnership"] == "shared"


# --- the ownership keys .env ships reach the tier child; a shell export wins -------

def _dispatch(monkeypatch):
    import sys
    from livetest import paths
    # the autouse hermetic fixture blanks .env; these tests read their own tmp .env
    monkeypatch.setattr(paths, "dotenv_values", lambda env=None: paths.read_dotenv(paths.dotenv_path(env)))
    from _clikit import REPO
    sys.path.insert(0, str(REPO / "scripts" / "cli"))
    from striim_test import dispatch
    return dispatch


def test_child_env_takes_ownership_from_dotenv_when_the_shell_leaves_it_unset(tmp_path, monkeypatch):
    from types import SimpleNamespace
    dispatch = _dispatch(monkeypatch)
    (tmp_path / ".env").write_text("SLT_INFRA_OWNERSHIP=shared\nSLT_KEEP_SERVICES=1\nSLT_PG_HOST=db.example\n")
    origins = SimpleNamespace(mode="checkout", home=tmp_path)
    env = dispatch.child_env(origins, {"SLT_PROJECT_ROOT": str(tmp_path)})
    assert env["SLT_INFRA_OWNERSHIP"] == "shared" and env["SLT_KEEP_SERVICES"] == "1"
    assert env["SLT_PG_HOST"] == "db.example"    # .env carries service settings too


def test_child_env_shell_export_wins_over_dotenv(tmp_path, monkeypatch):
    from types import SimpleNamespace
    dispatch = _dispatch(monkeypatch)
    (tmp_path / ".env").write_text("SLT_INFRA_OWNERSHIP=shared\nSLT_KEEP_SERVICES=1\n")
    origins = SimpleNamespace(mode="checkout", home=tmp_path)
    env = dispatch.child_env(origins, {"SLT_PROJECT_ROOT": str(tmp_path), "SLT_INFRA_OWNERSHIP": "exclusive",
                                       "SLT_KEEP_SERVICES": "0"})
    assert env["SLT_INFRA_OWNERSHIP"] == "exclusive" and env["SLT_KEEP_SERVICES"] == "0"
    assert dispatch.ownership_env({"SLT_PROJECT_ROOT": str(tmp_path), "SLT_INFRA_OWNERSHIP": "shared"}) \
        == {"SLT_KEEP_SERVICES": "1"}                   # shell shared, keep from .env


def test_shell_exclusive_does_not_inherit_the_dotenv_kept_stack(tmp_path, monkeypatch):
    # .env ships shared + SLT_KEEP_SERVICES=1; exclusive from the shell must not keep
    from types import SimpleNamespace
    dispatch = _dispatch(monkeypatch)
    (tmp_path / ".env").write_text("SLT_INFRA_OWNERSHIP=shared\nSLT_KEEP_SERVICES=1\n")
    root = {"SLT_PROJECT_ROOT": str(tmp_path)}
    for shell in ({"SLT_INFRA_OWNERSHIP": "exclusive"}, {"SLT_INFRA_OWNERSHIP": "exclusive", "SLT_KEEP_SERVICES": ""}):
        env = dispatch.child_env(SimpleNamespace(mode="checkout", home=tmp_path), {**root, **shell})
        assert env["SLT_INFRA_OWNERSHIP"] == "exclusive" and not env.get("SLT_KEEP_SERVICES")
    (tmp_path / ".env").write_text("SLT_INFRA_OWNERSHIP=exclusive\nSLT_KEEP_SERVICES=1\n")
    assert dispatch.ownership_env(root) == {"SLT_INFRA_OWNERSHIP": "exclusive"}


def test_ownership_comes_from_the_childs_project_root(tmp_path, monkeypatch):
    # With a project manifest the child's SLT_PROJECT_ROOT is the project's, so is its .env
    from types import SimpleNamespace
    dispatch = _dispatch(monkeypatch)
    clone, project = tmp_path / "clone", tmp_path / "project"
    clone.mkdir(); project.mkdir()
    (clone / ".env").write_text("SLT_INFRA_OWNERSHIP=exclusive\n")
    (project / ".env").write_text("SLT_INFRA_OWNERSHIP=shared\nSLT_KEEP_SERVICES=1\n")
    env = dispatch.child_env(SimpleNamespace(mode="checkout", home=clone), {"SLT_PROJECT_ROOT": str(clone)},
                             location={"SLT_PROJECT_ROOT": str(project)})
    assert env["SLT_INFRA_OWNERSHIP"] == "shared" and env["SLT_KEEP_SERVICES"] == "1"


def test_identity_records_the_projects_dotenv_ownership(project, elsewhere, tmp_path):
    # F8: identity.json reads the ownership the tier child gets (the project's .env), not the clone's
    import pathlib
    root = pathlib.Path(project).parent
    (root / ".env").write_text("SLT_INFRA_OWNERSHIP=shared\nSLT_KEEP_SERVICES=1\n")
    r = run_cli(["run", "--tier", "live", "--dry-run", "--targets", project], cwd=elsewhere,
                env=framework_env(SLT_INFRA_OWNERSHIP=None, SLT_KEEP_SERVICES=None))
    assert r.rc == 0, r.stderr
    assert _records(r)[0]["infraOwnership"] == "shared"



def test_service_settings_reach_the_child_shell_then_project_then_clone(tmp_path, monkeypatch):
    # .env may carry service settings; per key the shell wins, then the project .env, then the clone's
    from types import SimpleNamespace
    from livetest import paths
    dispatch = _dispatch(monkeypatch)
    clone, project = tmp_path / "clone", tmp_path / "project"
    clone.mkdir(); project.mkdir()
    monkeypatch.setattr(paths, "_default_project_root", lambda: clone)
    (clone / ".env").write_text("SLT_PG_HOST=clone-db\nSLT_PG_PORT=6432\nINT_PG_HOST=int-db\n"
                                "SLT_PG_DB=clonedb\nSLT_SERVICES_HOST=gw\n")
    (project / ".env").write_text("SLT_PG_HOST=project-db\nSLT_PG_DB=projdb\n")
    origins = SimpleNamespace(mode="checkout", home=clone)
    loc = {"SLT_PROJECT_ROOT": str(project)}
    env = dispatch.child_env(origins, {"SLT_PG_DB": "shelldb", "SLT_PG_PORT": " "}, location=loc)
    assert env["SLT_PG_HOST"] == "project-db"           # project .env over the clone's
    assert env["SLT_PG_DB"] == "shelldb"                # the shell over both
    assert env["SLT_PG_PORT"] == "6432"                 # empty in the shell counts as unset: the clone's
    assert env["INT_PG_HOST"] == "int-db"               # integration settings too
    assert "SLT_SERVICES_HOST" not in env               # not a key .env supplies
    assert dispatch.service_env({**loc, "SLT_PG_HOST": "shell-db"}) == {
        "SLT_PG_DB": "projdb", "SLT_PG_PORT": "6432", "INT_PG_HOST": "int-db"}
    # no manifest: the clone's .env alone, and nothing set anywhere adds nothing
    assert dispatch.child_env(origins, {})["SLT_PG_HOST"] == "clone-db"
    (clone / ".env").unlink(); (project / ".env").unlink()
    assert dispatch.service_env(loc) == {}


def test_a_key_the_project_dotenv_leaves_unset_falls_back_to_the_clone_dotenv(tmp_path, monkeypatch):
    # Shell > project .env > clone .env, per key
    from types import SimpleNamespace
    from livetest import paths
    dispatch = _dispatch(monkeypatch)
    clone, project = tmp_path / "clone", tmp_path / "project"
    clone.mkdir(); project.mkdir()
    monkeypatch.setattr(paths, "_default_project_root", lambda: clone)
    (clone / ".env").write_text("SLT_INFRA_OWNERSHIP=shared\nSLT_KEEP_SERVICES=1\n")
    (project / ".env").write_text("STRIIM_URL=http://striim.example:9080\n")        # no ownership keys
    loc = {"SLT_PROJECT_ROOT": str(project)}
    assert dispatch.ownership_env(loc) == {"SLT_INFRA_OWNERSHIP": "shared", "SLT_KEEP_SERVICES": "1"}
    env = dispatch.child_env(SimpleNamespace(mode="checkout", home=clone), {}, location=loc)
    assert env["SLT_INFRA_OWNERSHIP"] == "shared" and env["SLT_KEEP_SERVICES"] == "1"
    (project / ".env").unlink()                                                      # no project .env at all
    assert dispatch.ownership_env(loc) == {"SLT_INFRA_OWNERSHIP": "shared", "SLT_KEEP_SERVICES": "1"}
    (project / ".env").write_text("SLT_INFRA_OWNERSHIP=exclusive\n")                 # the project's own key wins
    assert dispatch.ownership_env(loc) == {"SLT_INFRA_OWNERSHIP": "exclusive"}
