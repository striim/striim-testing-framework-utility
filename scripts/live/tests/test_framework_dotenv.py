"""SLT_FRAMEWORK_DOTENV names the clone .env: the framework checkout's own unless set, read from the process
environment only. Every clone read goes through paths.dotenv_path, so one setting covers them all."""
from pathlib import Path

import pytest

from livetest import paths

DOTENV_VALUES = paths.dotenv_values


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "dotenv_values", DOTENV_VALUES)      # the root conftest stubs it out
    project = tmp_path / "project"
    project.mkdir()
    clone = tmp_path / "clone.env"
    clone.write_text("STRIIM_USER=clone-user\n")
    return {"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "SLT_PROJECT_ROOT": str(project), "SLT_FRAMEWORK_DOTENV": str(clone)}


def test_dotenv_path_honours_the_setting_only_without_a_project_root(tmp_path):
    named = tmp_path / "named.env"
    assert paths.dotenv_path({}) == paths._default_project_root() / ".env"
    assert paths.dotenv_path({"SLT_FRAMEWORK_DOTENV": str(named)}) == named
    assert (paths.dotenv_path({"SLT_PROJECT_ROOT": str(tmp_path), "SLT_FRAMEWORK_DOTENV": str(named)})
            == tmp_path / ".env")


def test_the_named_file_is_the_clone_layer(env):
    assert paths.dotenv_values(env)["STRIIM_USER"] == "clone-user"


def test_a_missing_named_file_reads_as_empty(env, tmp_path):
    assert "STRIIM_USER" not in paths.dotenv_values({**env, "SLT_FRAMEWORK_DOTENV": str(tmp_path / "missing.env")})


def test_striim_test_reads_the_named_clone_layer(env):
    from striim_test import dispatch
    assert dispatch.striim_env({k: v for k, v in env.items()})["STRIIM_USER"] == "clone-user"


def test_the_setting_is_never_read_from_a_settings_file(env, tmp_path, monkeypatch):
    """Process environment only: a .env or the machine file naming it is ignored."""
    empty_checkout = tmp_path / "checkout"
    empty_checkout.mkdir()
    monkeypatch.setattr(paths, "_default_project_root", lambda: empty_checkout)   # not the real checkout's .env
    elsewhere = tmp_path / "elsewhere.env"
    elsewhere.write_text("STRIIM_USER=elsewhere\n")
    (Path(env["SLT_PROJECT_ROOT"]) / ".env").write_text(f"SLT_FRAMEWORK_DOTENV={elsewhere}\n")
    machine = tmp_path / "config" / "striim-test" / "machine.env"
    machine.parent.mkdir(parents=True)
    machine.write_text(f"SLT_FRAMEWORK_DOTENV={elsewhere}\n")
    machine.chmod(0o600)
    process = {k: v for k, v in env.items() if k != "SLT_FRAMEWORK_DOTENV"}
    assert paths.dotenv_values(process).get("STRIIM_USER") != "elsewhere"


def test_the_plugins_import_time_read_skips_a_hosts_clone_env(tmp_path):
    """livetest.plugin resolves its case roots (SLT_LIVE_CASES) when it is imported, before any conftest runs, so
    only an environment setting can keep a host's clone .env out of that read. A child points the engine's default
    checkout at a .env naming extra cases, imports the plugin and reports its root: with the test-child environment
    the .env is not read; without the SLT_FRAMEWORK_DOTENV redirect it is (the leak this guards)."""
    import json
    import subprocess
    import sys
    from tests import _hermetic_child

    live = Path(__file__).resolve().parents[1]
    checkout = tmp_path / "checkout"
    cases = tmp_path / "host-cases"
    cases.mkdir()
    checkout.mkdir()
    (checkout / ".env").write_text(f"SLT_LIVE_CASES={cases}\n")
    probe = ("import json, sys\nfrom pathlib import Path\nimport livetest.paths as p\n"
             f"p._default_project_root = lambda: Path({str(checkout)!r})\n"
             "import livetest.plugin as plugin\nprint(json.dumps(str(plugin._LIVE_CASES)))\n")

    def live_cases(env):
        r = subprocess.run([sys.executable, "-c", probe], cwd=tmp_path, env=env, capture_output=True, text=True,
                           timeout=120)
        assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
        return json.loads(r.stdout.strip().splitlines()[-1])

    env = _hermetic_child.child_env(tmp_path / "no-such-settings-file", PYTHONPATH=str(live))
    assert live_cases(env) == str((live / "regression").resolve())
    leaky = {k: v for k, v in env.items() if k != "SLT_FRAMEWORK_DOTENV"}
    assert live_cases(leaky) == str(cases)
