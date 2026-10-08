"""servicesRoots outside a test run.

`python -m livetest.cli start|stop` and `striim-test doctor` read the project manifest too, so
a consumer's own service is started, stopped and checked like a shipped one;
`stop all` no longer leaves it running.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from _clikit import REPO, TRIO

sys.path.insert(0, str(REPO / "scripts" / "cli"))
sys.path.insert(0, str(REPO / "scripts" / "live"))

from striim_test import doctor  # noqa: E402


def _consumer(tmp_path: Path) -> Path:
    svc = tmp_path / "consumer" / "services" / "consumeronly"
    svc.mkdir(parents=True)
    (svc / "service.yaml").write_text("name: consumeronly\nisolation: none\ncompose: compose.yaml\n")
    (svc / "compose.yaml").write_text("services:\n  consumeronly:\n    image: postgres:16\n")
    m = tmp_path / "consumer" / "gold-targets.yaml"
    m.write_text("schemaVersion: 1\ntargets: []\nsuites:\n  live: .\nservicesRoots:\n  - services\n")
    return m


def test_livetest_cli_sees_the_manifests_services(tmp_path):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("SLT_", "GOLD_", "PYTEST_")) and k != "PYTHONPATH"}
    env.update(PYTHONPATH=os.pathsep.join(map(str, TRIO)), GOLD_TARGETS=str(_consumer(tmp_path)))
    r = subprocess.run([sys.executable, "-m", "livetest.cli", "stop", "--help"], cwd=tmp_path,
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "consumeronly" in r.stdout and "postgres" in r.stdout, r.stdout


def test_doctor_sees_the_manifests_services(tmp_path):
    from livetest import layout, registry
    try:
        location, checks = doctor.project_location(str(_consumer(tmp_path)), {})
        assert checks == []
        assert "consumeronly" in registry.all_services()
        got = doctor.check_services({"consumeronly": ["case-x"]}, {}, running=lambda c: True)
        assert not [c for c in got if "unknown service" in c.line()], [c.line() for c in got]
    finally:
        layout._reset()
