"""The Docker-backed files carry the `docker` marker, so `pytest -m "not docker"` runs the rest
of this suite without a Docker daemon (5.4 M1 FS4). The framework gate still --ignores them."""
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

INT = Path(__file__).resolve().parents[1]


def _load_hermetic_child():
    """The live suite's test-child environment (scripts/live/tests/_hermetic_child.py), loaded by path once: this
    suite's own `tests` package shadows the live one."""
    name = "_slt_hermetic_child"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, INT.parent / "live" / "tests" / "_hermetic_child.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)              # cached only once it has loaded: a failure is not left behind
        sys.modules[name] = module
    return sys.modules[name]


HERMETIC_CHILD = _load_hermetic_child()

DOCKER_FILES = ["test_db_cleanup.py", "test_pgclient.py", "test_services_live.py",
                "test_spanner_live.py", "test_services_gcs.py"]


def _collect(*args):
    # The child is a second pytest session. On the shared state dir its pytest_sessionfinish reads
    # THIS session's started-services registry and tears those containers down, so a later Docker
    # test finds its Postgres gone. Its own state dir leaves it nothing to tear down.
    with tempfile.TemporaryDirectory() as state:
        r = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
                            *args, *[f"tests/{f}" for f in DOCKER_FILES]],
                           cwd=INT, capture_output=True, text=True,
                           env=HERMETIC_CHILD.child_env(
                               Path(state) / "no-such-settings-file",
                               base={k: v for k, v in os.environ.items() if not k.startswith("SLT_")},
                               SLT_STATE_DIR=state,
                               SLT_PRE_UP="0"))   # tests/conftest.py's guard: no service pre_up hook runs here
    return r.returncode, r.stdout + r.stderr


def test_not_docker_deselects_every_docker_backed_file():
    rc, out = _collect("-m", "not docker")
    assert rc == 5, out[-1500:]                      # 5: no tests collected (all deselected)
    assert "deselected" in out


def test_each_docker_backed_file_has_items_under_the_marker():
    rc, out = _collect("-m", "docker")
    assert rc == 0, out[-1500:]
    for f in DOCKER_FILES:
        assert f"tests/{f}::" in out, f"{f} collects nothing under -m docker"
