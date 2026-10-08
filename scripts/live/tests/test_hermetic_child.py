"""tests/_hermetic_child.py: the child's environment is an allowlist plus the test's own values."""
from pathlib import Path

from tests import _hermetic_child as hermetic

HOST = {"PATH": "/usr/bin", "HOME": "/home/dev", "LC_ALL": "C", "COMPANY_NAME": "Striim", "STRIIM_PASS": "admin",
        "GOLD_TARGETS": "/home/dev/gold-targets.yaml", "STRIIM_HOME": "/opt/striim", "XDG_CONFIG_HOME": "/home/dev/.c",
        "SSH_AUTH_SOCK": "/tmp/agent", "PYTEST_XDIST_WORKER": "gw0", "SLT_STACK_PREFIX": "test-set",
        "XTR_LOG": "/tmp/log"}


def test_host_values_outside_the_allowlist_stay_out(tmp_path):
    env = hermetic.child_env(tmp_path / "missing", base=HOST)
    for gone in ("COMPANY_NAME", "STRIIM_PASS", "GOLD_TARGETS", "STRIIM_HOME", "XDG_CONFIG_HOME", "SSH_AUTH_SOCK",
                 "PYTEST_XDIST_WORKER", "XTR_LOG"):
        assert gone not in env, gone
    assert env["PATH"] == "/usr/bin" and env["HOME"] == "/home/dev" and env["LC_ALL"] == "C"


def test_the_tests_own_values_reach_the_child(tmp_path):
    env = hermetic.child_env(tmp_path / "missing", base=HOST, REVIEW_PASSWORD="decimal", XTR_LOG="/test/log")
    assert env["SLT_STACK_PREFIX"] == "test-set"
    assert env["REVIEW_PASSWORD"] == "decimal" and env["XTR_LOG"] == "/test/log"


def test_both_settings_files_point_at_a_missing_file_unless_overridden(tmp_path):
    missing = tmp_path / "missing"
    env = hermetic.child_env(missing, base={**HOST, "SLT_MACHINE_ENV": "/home/dev/m.env"})
    assert env["SLT_MACHINE_ENV"] == env["SLT_FRAMEWORK_DOTENV"] == str(missing)
    assert not Path(env["SLT_MACHINE_ENV"]).exists()
    assert hermetic.child_env(missing, base=HOST, SLT_MACHINE_ENV="/x")["SLT_MACHINE_ENV"] == "/x"


def test_apply_makes_os_environ_the_childs(monkeypatch, tmp_path):
    import os
    monkeypatch.setenv("COMPANY_NAME", "Striim")
    monkeypatch.setenv("SLT_PG_TARGET_PASSWORD", "test-set")
    hermetic.apply(monkeypatch, tmp_path)
    assert "COMPANY_NAME" not in os.environ
    assert os.environ["SLT_PG_TARGET_PASSWORD"] == "test-set"
    assert os.environ["SLT_FRAMEWORK_DOTENV"] == str(tmp_path / "no-such-settings-file")


def test_host_pytest_plugins_do_not_autoload_into_a_child(tmp_path):
    """A child pytest under child_env loads no plugin from an installed package's entry point; the same child without
    the setting does (this venv carries some), so the test can see a host plugin get in."""
    import subprocess
    import sys
    (tmp_path / "test_probe.py").write_text(
        "def test_probe(request):\n"
        "    import json, pathlib\n"
        "    dists = request.config.pluginmanager.list_plugin_distinfo()\n"
        "    pathlib.Path('loaded.json').write_text(json.dumps(sorted(d.project_name for _, d in dists)))\n")

    def loaded(env):
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_probe.py"],
                           cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
        import json
        return json.loads((tmp_path / "loaded.json").read_text())

    env = hermetic.child_env(tmp_path / "missing")
    assert loaded(env) == []
    assert loaded({k: v for k, v in env.items() if k != "PYTEST_DISABLE_PLUGIN_AUTOLOAD"}) != []
