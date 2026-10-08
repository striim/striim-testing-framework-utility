"""livetest.paths: precedence, .env filtering, set-but-missing, defaults (spec section 2)."""
import os
from pathlib import Path

import pytest

from livetest import paths

LIVE = Path(paths.__file__).resolve().parents[1]          # scripts/live
SCRIPTS = LIVE.parent
REPO = SCRIPTS.parent
PATH_KEYS = ("SLT_PROJECT_ROOT", "SLT_LIVE_CASES", "SLT_SERVICES_DIR", "SLT_STATE_DIR")
FN = {"SLT_PROJECT_ROOT": paths.project_root, "SLT_LIVE_CASES": paths.live_cases,
      "SLT_SERVICES_DIR": paths.services_dir, "SLT_STATE_DIR": paths.state_dir}


def test_defaults_equal_todays_derivation():
    # The literal expressions the call sites used before this effort.
    assert paths.project_root({}, {}) == REPO                       # manifest.py:603 parents[3]
    assert paths.framework_home({}, {}) == SCRIPTS
    assert paths.live_dir({}, {}) == LIVE                           # plugin.py:61 parents[1]
    assert paths.live_cases({}, {}) == LIVE / "regression"          # preflight.py:82
    assert paths.services_dir({}, {}) == LIVE / "services"          # registry.py:8
    assert paths.state_dir({}, {}) == LIVE                          # services.py:18-20
    assert paths.tools_python({}, {}) == REPO / "tools" / "python"  # striim.py:15-16


@pytest.mark.parametrize("key", PATH_KEYS)
def test_precedence_env_beats_dotenv_beats_default(key, tmp_path):
    env_dir, dot_dir = tmp_path / "env", tmp_path / "dot"
    env_dir.mkdir(); dot_dir.mkdir()
    assert FN[key]({key: str(env_dir)}, {key: str(dot_dir)}) == env_dir.resolve()
    assert FN[key]({}, {key: str(dot_dir)}) == dot_dir.resolve()
    assert FN[key]({}, {}) != dot_dir.resolve()


@pytest.mark.parametrize("key", PATH_KEYS)
def test_set_but_missing_names_the_key(key, tmp_path):
    missing = tmp_path / "nope"
    with pytest.raises(paths.PathConfigError, match=key) as exc:
        FN[key]({key: str(missing)}, {})
    assert "environment" in str(exc.value)
    with pytest.raises(paths.PathConfigError, match=key):
        FN[key]({}, {key: str(missing)})


def test_empty_value_is_unset(tmp_path):
    assert paths.project_root({"SLT_PROJECT_ROOT": "  "}, {}) == REPO
    dot = tmp_path / "d"; dot.mkdir()
    assert paths.project_root({"SLT_PROJECT_ROOT": ""}, {"SLT_PROJECT_ROOT": str(dot)}) == dot.resolve()


def test_missing_project_root_in_env_does_not_hide_dotenv(tmp_path):
    env = {"SLT_PROJECT_ROOT": str(tmp_path / "typo")}
    with pytest.raises(paths.PathConfigError, match="SLT_PROJECT_ROOT"):
        paths.dotenv_path(env)


def test_relative_values(tmp_path, monkeypatch):
    (tmp_path / "cases").mkdir()
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "cases").mkdir()
    (tmp_path / "proj" / ".env").write_text("SLT_LIVE_CASES=cases\n")
    monkeypatch.chdir(tmp_path)
    assert paths.live_cases({"SLT_LIVE_CASES": "cases"}, {}) == (tmp_path / "cases").resolve()
    env = {"SLT_PROJECT_ROOT": str(tmp_path / "proj")}
    dot = paths.read_dotenv(paths.dotenv_path(env))
    assert paths.live_cases(env, dot) == (tmp_path / "proj" / "cases").resolve()


def test_values_are_resolved(tmp_path):
    real = tmp_path / "real"; real.mkdir()
    link = tmp_path / "link"; link.symlink_to(real)
    assert paths.state_dir({"SLT_STATE_DIR": str(link) + "/"}, {}) == real.resolve()
    home = Path(os.path.expanduser("~"))
    assert paths.state_dir({"SLT_STATE_DIR": "~"}, {}) == home.resolve()


def test_dotenv_reads_only_listed_keys_and_exports_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("SLT_LIVE_CASES", raising=False)
    f = tmp_path / ".env"
    f.write_text("STRIIM_BUILD_5_4_2=/opt/striim\nFOO=bar\nSLT_LIVE_CASES=/x\nSLT_LOCK_DIR=/y\n")
    assert paths.read_dotenv(f) == {"SLT_LIVE_CASES": "/x"}
    assert "SLT_LIVE_CASES" not in os.environ


def test_dotenv_dialects(tmp_path):
    f = tmp_path / ".env"
    f.write_bytes("﻿export SLT_LIVE_CASES=\"/a b\"\r\n# c\r\n\r\n"
                  "SLT_STATE_DIR='/s' \r\nSTRIIM_URL=http://h:9080 # native\r\n"
                  "STRIIM_PASS=\"s3cret\" # prod\r\nexport\tSLT_INT_CASES='/i'  # x\r\n"
                  "STRIIM_USER=u\t# tab\r\n".encode())
    assert paths.read_dotenv(f) == {"SLT_LIVE_CASES": "/a b", "SLT_STATE_DIR": "/s",
                                    "STRIIM_URL": "http://h:9080", "STRIIM_PASS": "s3cret",
                                    "SLT_INT_CASES": "/i", "STRIIM_USER": "u"}


def test_missing_dotenv_is_empty(tmp_path):
    assert paths.read_dotenv(tmp_path / ".env") == {}


def test_framework_home_accepts_scripts_dir_or_repo_root(tmp_path):
    (tmp_path / "repo" / "scripts" / "live" / "livetest").mkdir(parents=True)
    root = tmp_path / "repo"
    assert paths.framework_home({"SLT_FRAMEWORK_HOME": str(root)}, {}) == (root / "scripts").resolve()
    assert paths.framework_home({"SLT_FRAMEWORK_HOME": str(root / "scripts")}, {}) == (root / "scripts").resolve()
    (tmp_path / "empty").mkdir()
    with pytest.raises(paths.PathConfigError, match="SLT_FRAMEWORK_HOME.*environment"):
        paths.framework_home({"SLT_FRAMEWORK_HOME": str(tmp_path / "empty")}, {})


def test_striim_password_alias_order():
    assert paths.setting("STRIIM_PASS", {"STRIIM_PASS": "a", "STRIIM_PASSWORD": "b"}, {}) == "a"
    assert paths.setting("STRIIM_PASS", {"STRIIM_PASSWORD": "b"}, {"STRIIM_PASS": "c"}) == "b"
    assert paths.setting("STRIIM_PASS", {}, {"STRIIM_PASSWORD": "d"}) == "d"
    assert paths.setting("STRIIM_PASS", {}, {}) is None


def test_effective_env_fills_only_unset():
    got = paths.effective_env({"STRIIM_URL": "http://env", "SLT_SERVICES_HOST": "h"},
                              {"STRIIM_URL": "http://dot", "STRIIM_USER": "u"})
    assert got["STRIIM_URL"] == "http://env" and got["STRIIM_USER"] == "u"
    assert got["SLT_SERVICES_HOST"] == "h"


def test_effective_env_follows_alias_and_dotenv_relative_rules(tmp_path):
    env = {"STRIIM_PASSWORD": "envpw", "SLT_PROJECT_ROOT": str(tmp_path)}
    got = paths.effective_env(env, {"STRIIM_PASS": "dotpw", "SLT_LIVE_CASES": "cases",
                                    "SLT_STATE_DIR": "/abs"})
    assert "STRIIM_PASS" not in got and got["STRIIM_PASSWORD"] == "envpw"
    assert got["SLT_LIVE_CASES"] == str(tmp_path / "cases")
    assert got["SLT_STATE_DIR"] == "/abs"


def test_hermetic_tests_see_no_dotenv(tmp_path, monkeypatch):
    # conftest._hermetic_stack_env disables the .env layer for non-live tests. A real .env is
    # planted so that this fails if the guard is removed.
    (tmp_path / ".env").write_text("SLT_LIVE_CASES=/x\n")
    monkeypatch.setenv("SLT_PROJECT_ROOT", str(tmp_path))
    assert paths.read_dotenv(tmp_path / ".env") == {"SLT_LIVE_CASES": "/x"}
    assert paths.dotenv_values() == {}


@pytest.mark.parametrize("key", ["STRIIM_URL", "STRIIM_USER", "SLT_INFRA_OWNERSHIP", "SLT_KEEP_SERVICES",
                                 "SLT_PG_HOST", "INT_PG_HOST"])
def test_setting_shell_export_wins_over_dotenv(key):
    # A value exported in the shell always wins over the same key in .env.
    assert paths.setting(key, {key: "from-shell"}, {key: "from-dotenv"}) == "from-shell"
    assert paths.setting(key, {}, {key: "from-dotenv"}) == "from-dotenv"
    assert paths.setting(key, {key: "  "}, {key: "from-dotenv"}) == "from-dotenv"   # empty counts as unset


def test_dotenv_reads_the_service_settings(tmp_path):
    # .env may carry service settings (both tiers); other keys are still dropped
    (tmp_path / ".env").write_text("SLT_PG_HOST=db.example\nSLT_PG_PORT=6432\nINT_ORA_HOST=ora\n"
                                   "SLT_POSTGRES_VIEW_HOST=vpn\nSLT_SERVICES_HOST=gw\nSLT_KEEP_RESOURCES=1\n")
    assert paths.read_dotenv(tmp_path / ".env") == {
        "SLT_PG_HOST": "db.example", "SLT_PG_PORT": "6432", "INT_ORA_HOST": "ora",
        "SLT_POSTGRES_VIEW_HOST": "vpn"}
    got = paths.effective_env({"SLT_PROJECT_ROOT": str(tmp_path), "SLT_PG_PORT": "7000"},
                              paths.read_dotenv(tmp_path / ".env"))
    assert got["SLT_PG_HOST"] == "db.example" and got["SLT_PG_PORT"] == "7000"   # not made a path


def test_service_keys_are_every_override_the_service_definitions_name():
    # The list .env is read for is the set the engines read: each service.yaml's live_override_env,
    # live_env and docker_env in both tiers, plus SLT_<SERVICE>_VIEW_HOST and SLT_STRIIM_VIEW_HOST.
    # A connection-only definition (teradata: no compose file) names its external-target keys the
    # same way, so they are here, and nothing that only built or booted a container is.
    import yaml
    want = {"SLT_STRIIM_VIEW_HOST"} | set(paths._SERVICE_EXTRA_KEYS)   # the pre_up off switch
    for tier in ("live", "integration"):
        for f in sorted((SCRIPTS / tier / "services").glob("*/service.yaml")):
            d = yaml.safe_load(f.read_text()) or {}
            want |= {d["live_override_env"]} if d.get("live_override_env") else set()
            for section in ("live_env", "docker_env"):
                want |= set((d.get(section) or {}).values())
            if tier == "live":
                want.add(f"SLT_{f.parent.name.upper()}_VIEW_HOST")
    assert set(paths.SERVICE_KEYS) == want, (want - set(paths.SERVICE_KEYS), set(paths.SERVICE_KEYS) - want)
    assert len(paths.SERVICE_KEYS) == len(want) and not set(paths.SERVICE_KEYS) & set(paths.KEYS)


def test_effective_env_makes_only_path_keys_absolute(tmp_path):
    # The ownership keys are values, not locations
    dot = {"SLT_INFRA_OWNERSHIP": "shared", "SLT_KEEP_SERVICES": "1", "SLT_STATE_DIR": "state"}
    (tmp_path / "state").mkdir()
    got = paths.effective_env({"SLT_PROJECT_ROOT": str(tmp_path)}, dot)
    assert got["SLT_INFRA_OWNERSHIP"] == "shared" and got["SLT_KEEP_SERVICES"] == "1"
    assert got["SLT_STATE_DIR"] == str(tmp_path / "state")
    assert set(paths._PATH_KEYS) == {k for k in paths.KEYS if k.startswith("SLT_")} - {
        "SLT_INFRA_OWNERSHIP", "SLT_KEEP_SERVICES"}


# SLT_LIVE_CASES may name several case roots, an os.pathsep list; the first is the primary root.
def test_one_live_case_root_is_unchanged(tmp_path):
    (tmp_path / "cases").mkdir()
    env = {"SLT_LIVE_CASES": str(tmp_path / "cases")}
    assert paths.live_case_roots(env, {}) == [paths.live_cases(env, {})] == [(tmp_path / "cases").resolve()]
    assert paths.live_case_roots({}, {}) == [LIVE / "regression"]


def test_several_live_case_roots_resolve_in_order(tmp_path, monkeypatch):
    for d in ("a", "b", "proj/c"):
        (tmp_path / d).mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    env = {"SLT_LIVE_CASES": os.pathsep.join([str(tmp_path / "a"), "b", ""])}
    assert paths.live_case_roots(env, {}) == [(tmp_path / "a").resolve(), (tmp_path / "b").resolve()]
    assert paths.live_cases(env, {}) == (tmp_path / "a").resolve()          # the primary root
    # A relative entry from .env is relative to the .env's directory, as for one root.
    (tmp_path / "proj" / ".env").write_text(f"SLT_LIVE_CASES=c{os.pathsep}{tmp_path / 'a'}\n")
    env = {"SLT_PROJECT_ROOT": str(tmp_path / "proj")}
    dot = paths.read_dotenv(paths.dotenv_path(env))
    assert paths.live_case_roots(env, dot) == [(tmp_path / "proj/c").resolve(), (tmp_path / "a").resolve()]
    assert paths.effective_env(env, dot)["SLT_LIVE_CASES"] == os.pathsep.join(
        [str(tmp_path / "proj" / "c"), str(tmp_path / "a")])


@pytest.mark.parametrize("layout, needle", [
    (("a", "missing"), "does not exist"),
    (("a", "a/inner"), "inside another"),
    (("x/same", "y/same"), "same name"),
])
def test_ambiguous_or_missing_live_case_roots_are_refused(tmp_path, layout, needle):
    for d in layout:
        if d != "missing":
            (tmp_path / d).mkdir(parents=True, exist_ok=True)
    env = {"SLT_LIVE_CASES": os.pathsep.join(str(tmp_path / d) for d in layout)}
    with pytest.raises(paths.PathConfigError, match=needle) as e:
        paths.live_case_roots(env, {})
    assert "SLT_LIVE_CASES" in str(e.value)


def test_an_empty_path_list_entry_is_ignored_even_beside_one_root(tmp_path):
    (tmp_path / "a").mkdir()
    for value in (f"{tmp_path / 'a'}{os.pathsep}", f"{os.pathsep}{tmp_path / 'a'}"):
        assert paths.live_case_roots({"SLT_LIVE_CASES": value}, {}) == [(tmp_path / "a").resolve()]
        assert paths.live_cases({"SLT_LIVE_CASES": value}, {}) == (tmp_path / "a").resolve()
