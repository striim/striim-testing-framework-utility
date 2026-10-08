"""GOLD_TARGETS is activated outside striim-test too (console impact F1, F2).

A plain `pytest -p livetest.plugin` and `python -m livetest.preflight` see the manifest's
servicesRoots, as livetest.cli already did; striim_provision.striim_dir() is public.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from tests import _hermetic_child

LIVE = Path(__file__).resolve().parents[1]


def _consumer(tmp_path):
    root = tmp_path / "consumer"
    svc = root / "services" / "extdb"
    svc.mkdir(parents=True)
    (svc / "service.yaml").write_text("name: extdb\nisolation: none\ncompose: compose.yaml\n")
    (root / "tests" / "live").mkdir(parents=True)
    (root / "gold-targets.yaml").write_text(
        "schemaVersion: 1\ntargets: []\nsuites:\n  live: tests/live\nservicesRoots:\n  - services\n")
    return root


def test_a_plain_pytest_session_sees_the_manifests_services(tmp_path):
    root = _consumer(tmp_path)
    probe = tmp_path / "probe" / "test_probe.py"
    probe.parent.mkdir()
    probe.write_text("from livetest import registry\n\n"
                     "def test_extdb_is_registered():\n"
                     "    assert 'extdb' in registry.all_services()\n")
    base = {k: v for k, v in os.environ.items() if not k.startswith(("SLT_", "GOLD_", "PYTEST_"))}
    env = _hermetic_child.child_env(root / "no-such-settings-file", base=base,
                                    GOLD_TARGETS=str(root / "gold-targets.yaml"), PYTHONPATH=str(LIVE),
                                    SLT_PRE_UP="0")
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "livetest.plugin",
                        "-p", "no:cacheprovider", "--rootdir", str(probe.parent),
                        "-c", os.devnull, str(probe)],
                       cwd=str(probe.parent), env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]


def test_the_preflight_driver_activates_the_manifest(tmp_path, monkeypatch):
    from livetest import layout, preflight, project, registry
    root = _consumer(tmp_path)
    monkeypatch.setenv("GOLD_TARGETS", str(root / "gold-targets.yaml"))
    monkeypatch.setattr(preflight, "provision", lambda tests, env=None: 0)
    monkeypatch.setattr(project, "_ACTIVE", None)
    try:
        assert preflight.main([]) == 0
        assert "extdb" in registry.all_services()
    finally:
        monkeypatch.delenv("GOLD_TARGETS")
        layout._reset()
        project.load_and_activate()


def test_striim_dir_is_public_and_follows_the_services_dir(tmp_path, monkeypatch):
    from livetest import plugin, striim_provision
    monkeypatch.setenv("SLT_SERVICES_DIR", str(tmp_path))
    assert striim_provision.striim_dir() == tmp_path / "striim"
    assert plugin._striim_dir() == striim_provision.striim_dir()
