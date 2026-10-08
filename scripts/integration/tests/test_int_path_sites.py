"""Every engine path site takes its value from inttest.paths (spec section 2).
One row per site; fresh interpreter per row. Services and state roots resolve at call time
(through inttest.resources), so those rows call the accessor rather than read a constant."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from inttest.paths import read_dotenv

INT = Path(__file__).resolve().parents[1]
REPO = INT.parents[1]


def _run(expr, extra, cwd):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SLT_", "STRIIM_"))}
    env.update(extra)
    env["PYTHONPATH"] = str(INT)
    r = subprocess.run([sys.executable, "-c", f"import importlib; print({expr})"],
                       cwd=cwd, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return Path(r.stdout.strip().splitlines()[-1])


def _mod(name, attr):
    return f"getattr(importlib.import_module('{name}'), '{attr}')"


def _fw_int(tmp):      # a framework-home-shaped target for shared settings discovery
    fw = tmp / "fw" / "scripts"
    (fw / "integration" / "inttest").mkdir(parents=True)
    (fw / "live" / "livetest").mkdir(parents=True)
    return fw


# (expression, key, default, expected(target), target factory)
SITES = [
    # PR-5
    ("importlib.import_module('inttest.opartifacts')._root()", "SLT_PROJECT_ROOT", REPO, lambda t: t, None),
    ("importlib.import_module('inttest.opartifacts')._common_dir()", "SLT_PROJECT_ROOT",
     REPO / "java" / "OpenProcessors" / "OpenProcessorCommon",
     lambda t: t / "java" / "OpenProcessors" / "OpenProcessorCommon", None),
    (_mod("inttest.harness", "_JAVA_DIR"), "SLT_FRAMEWORK_HOME", INT / "java",
     lambda t: t / "integration" / "java", _fw_int),   # _fw_int makes integration/inttest
    # PR-6
    ("importlib.import_module('inttest.plugin')._state_root()", "SLT_STATE_DIR", INT,
     lambda t: t, None),
    ("importlib.import_module('inttest.plugin')._service_file('x', 'service.yaml').parent.parent",
     "SLT_INT_SERVICES_DIR", INT / "services", lambda t: t, None),
    # plugin._PERF_DIR (key SLT_INT_CASES) is pinned in test_int_state_paths.py instead:
    # before the framework move a moved case root is refused, so it cannot "follow" the key.
    (_mod("inttest.plugin", "_PERF_RESULTS_DIR"), "SLT_STATE_DIR", INT / ".perf-results",
     lambda t: t / ".perf-results", None),
    ("importlib.import_module('inttest.plugin')._compose_lock_path()", "SLT_STATE_DIR",
     INT / ".int-compose.lock", lambda t: t / ".int-compose.lock", None),
    # cli lists services through inttest.resources.list_profiles (the row below covers that seam)
    ("importlib.import_module('inttest.resources').services_roots()[0]", "SLT_INT_SERVICES_DIR",
     INT / "services", lambda t: t, None),
    ("importlib.import_module('inttest.cli')._lock_file()", "SLT_STATE_DIR",
     INT / ".int-compose.lock", lambda t: t / ".int-compose.lock", None),
    ("importlib.import_module('inttest.tokens')._services_root('x', None)",
     "SLT_INT_SERVICES_DIR", INT / "services", lambda t: t, None),
    ("importlib.import_module('inttest.services')._state_root()", "SLT_STATE_DIR", INT,
     lambda t: t, None),
    ("importlib.import_module('inttest.services')._compose_file('x').parent.parent",
     "SLT_INT_SERVICES_DIR", INT / "services", lambda t: t, None),
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
