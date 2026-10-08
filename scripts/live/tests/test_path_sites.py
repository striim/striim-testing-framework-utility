"""Every engine path site takes its value from livetest.paths (spec section 2).
One row per site; fresh interpreter per row because the sites resolve at import time."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from livetest.paths import read_dotenv

LIVE = Path(__file__).resolve().parents[1]
REPO = LIVE.parents[1]


def _run(expr, extra, cwd):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SLT_", "STRIIM_"))}
    env.update(extra)
    env["PYTHONPATH"] = str(LIVE)
    r = subprocess.run([sys.executable, "-c", f"import importlib; print({expr})"],
                       cwd=cwd, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return Path(r.stdout.strip().splitlines()[-1])


def _mod(name, attr):
    return f"getattr(importlib.import_module('{name}'), '{attr}')"


def _fw(tmp):          # both tiers for shared settings discovery, plus the API helper
    fw = tmp / "fw" / "scripts"
    (fw / "live" / "livetest").mkdir(parents=True)
    (fw / "integration" / "inttest").mkdir(parents=True)
    (tmp / "fw" / "tools" / "python").mkdir(parents=True)
    shutil.copy(REPO / "tools" / "python" / "striim_api.py", tmp / "fw" / "tools" / "python")
    return fw


# (expression, key, default, expected(target), target factory)
SITES = [
    # PR-2
    ("importlib.import_module('livetest.manifest')._root()", "SLT_PROJECT_ROOT", REPO, lambda t: t, None),
    ("importlib.import_module('livetest.opartifacts')._root()", "SLT_PROJECT_ROOT", REPO, lambda t: t, None),
    # the imported module's own file, not just the _TOOLS constant (review PR-2 MINOR 2)
    ("importlib.import_module('livetest.striim').striim_api.__file__", "SLT_FRAMEWORK_HOME",
     REPO / "tools" / "python" / "striim_api.py",
     lambda t: t.parent / "tools" / "python" / "striim_api.py", _fw),
    ("importlib.import_module('livetest.ggtrail.harness')._harness_dir()", "SLT_PROJECT_ROOT",
     REPO / "tools" / "ggtrail-harness", lambda t: t / "tools" / "ggtrail-harness", None),
    # PR-3
    (_mod("livetest.plugin", "_STRIIM_DIR"), "SLT_SERVICES_DIR", LIVE / "services" / "striim",
     lambda t: t / "striim", None),
    (_mod("livetest.plugin", "_STATE_DIR"), "SLT_STATE_DIR", LIVE, lambda t: t, None),
    (_mod("livetest.preflight", "_STATE_DIR"), "SLT_STATE_DIR", LIVE, lambda t: t, None),
    ("importlib.import_module('livetest.services')._state_dir()", "SLT_STATE_DIR", LIVE,
     lambda t: t, None),
    (_mod("livetest.registry", "_SERVICES_DIR"), "SLT_SERVICES_DIR", LIVE / "services",
     lambda t: t, None),
]


@pytest.mark.parametrize("expr,key,default,expected,factory", SITES)
def test_site_default_unchanged(expr, key, default, expected, factory, tmp_path):
    if key in read_dotenv(REPO / ".env"):
        pytest.skip(f"{key} is set in {REPO / '.env'}")
    assert _run(expr, {}, tmp_path) == default


@pytest.mark.parametrize("expr,key,default,expected,factory", SITES)
def test_site_follows_key(expr, key, default, expected, factory, tmp_path):
    target = factory(tmp_path) if factory else tmp_path / "target"
    target.mkdir(parents=True, exist_ok=True)
    assert _run(expr, {key: str(target)}, tmp_path) == expected(target.resolve())


# SLT_LIVE_CASES: striim-test collects the cases under it, so pre-flight must read the same root
# (the framework repo wires collection to the key; field refused it until then).
def _load(extra, cwd):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SLT_", "STRIIM_"))}
    env.update(extra)
    env["PYTHONPATH"] = str(LIVE)
    return subprocess.run([sys.executable, "-c", "import livetest.preflight as p; print(p._CASES)"],
                          cwd=cwd, env=env, capture_output=True, text=True)


def test_live_cases_elsewhere_is_what_preflight_reads(tmp_path):
    r = _load({"SLT_LIVE_CASES": str(tmp_path)}, tmp_path)
    assert r.returncode == 0, r.stderr
    assert Path(r.stdout.strip().splitlines()[-1]) == tmp_path.resolve()


def test_live_cases_in_dotenv_is_what_preflight_reads(tmp_path):
    proj = tmp_path / "proj"
    (proj / "cases").mkdir(parents=True)
    (proj / ".env").write_text("SLT_LIVE_CASES=cases\n")
    r = _load({"SLT_PROJECT_ROOT": str(proj)}, tmp_path)
    assert r.returncode == 0, r.stderr
    assert Path(r.stdout.strip().splitlines()[-1]) == (proj / "cases").resolve()


def test_live_cases_that_does_not_exist_is_refused(tmp_path):
    r = _load({"SLT_LIVE_CASES": str(tmp_path / "missing")}, tmp_path)
    assert r.returncode != 0
    assert "SLT_LIVE_CASES" in r.stderr and "does not exist" in r.stderr


def test_live_cases_at_the_default_is_accepted(tmp_path):
    r = _load({"SLT_LIVE_CASES": str(LIVE / "regression")}, tmp_path)
    assert r.returncode == 0, r.stderr
    assert Path(r.stdout.strip().splitlines()[-1]) == LIVE / "regression"


def test_plugin_has_no_stale_live_name():
    # A6/R4: a stale monkeypatch of plugin._LIVE must raise, not silently redirect nothing.
    import livetest.plugin as plugin
    assert not hasattr(plugin, "_LIVE")


def test_live_cases_follow_the_collected_tree_not_framework_home(tmp_path):
    # Collection reads this checkout's regression/ whatever SLT_FRAMEWORK_HOME says, so pre-flight
    # must too; and naming that very tree must be accepted (review WAVE2 m1).
    fw = _fw(tmp_path)
    assert (fw / "live" / "livetest").is_dir()
    assert (fw / "integration" / "inttest").is_dir()
    r = _load({"SLT_FRAMEWORK_HOME": str(fw)}, tmp_path)
    assert r.returncode == 0, r.stderr
    assert Path(r.stdout.strip().splitlines()[-1]) == LIVE / "regression"
    r = _load({"SLT_FRAMEWORK_HOME": str(fw), "SLT_LIVE_CASES": str(LIVE / "regression")}, tmp_path)
    assert r.returncode == 0, r.stderr
