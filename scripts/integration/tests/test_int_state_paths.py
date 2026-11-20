import os
import subprocess
import sys
from pathlib import Path

from inttest import cli, paths, plugin
from tests.test_int_path_sites import INT, _fw_int, _mod, _run


def test_compose_lock_single_file(monkeypatch, tmp_path):
    # Review Focus 5: the CLI and the plugin must serialise on ONE compose lock, in the
    # machine-wide lock dir, resolved at call time.
    from livetest import stack
    monkeypatch.setattr(stack, "_LOCK_DIR", tmp_path / "locks")
    monkeypatch.setenv("SLT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("INT_STACK_PREFIX", raising=False)
    assert plugin._compose_lock_path() == cli._lock_file() == tmp_path / "locks" / ".int-compose.lock"


def test_session_and_compose_locks_are_per_stack_not_per_state_dir(monkeypatch, tmp_path):
    """Two checkouts (two state dirs) on one INT_STACK_PREFIX drive the same containers, so they take
    the same session and compose locks; the started registry stays each checkout's own record."""
    from livetest import stack
    monkeypatch.setattr(stack, "_LOCK_DIR", tmp_path / "locks")
    monkeypatch.delenv("INT_STACK_PREFIX", raising=False)
    seen = {}
    for name in ("checkout-a", "checkout-b"):
        (tmp_path / name).mkdir()
        monkeypatch.setenv("SLT_STATE_DIR", str(tmp_path / name))
        seen[name] = (plugin._session_lock_path(), plugin._compose_lock_path(), cli._lock_file(),
                      plugin._started_registry())
    a, b = seen["checkout-a"], seen["checkout-b"]
    assert a[0] == b[0] == tmp_path / "locks" / ".int-session.lock"
    assert a[1] == b[1] == a[2] == b[2] == tmp_path / "locks" / ".int-compose.lock"
    assert a[3] == tmp_path / "checkout-a" / ".int-services-up.json" != b[3]
    monkeypatch.setenv("INT_STACK_PREFIX", "alt")          # another stack: its own containers, its own locks
    assert plugin._session_lock_path() == tmp_path / "locks" / ".alt-int-session.lock"
    assert plugin._compose_lock_path() == cli._lock_file() == tmp_path / "locks" / ".alt-int-compose.lock"


def test_a_second_checkout_on_the_same_stack_is_refused_as_a_second_session(monkeypatch, tmp_path):
    import pytest
    from filelock import FileLock
    from types import SimpleNamespace
    from livetest import stack
    monkeypatch.setattr(stack, "_LOCK_DIR", tmp_path / "locks")
    for var in ("INT_STACK_PREFIX", "PYTEST_XDIST_WORKER", "INT_ALLOW_CONCURRENT_SESSIONS"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / "a").mkdir(), (tmp_path / "b").mkdir()
    monkeypatch.setenv("SLT_STATE_DIR", str(tmp_path / "a"))
    first = FileLock(str(plugin._session_lock_path()), timeout=0)
    first.acquire()                                        # checkout a's session
    monkeypatch.setattr(plugin, "_session_lock", None)
    try:
        monkeypatch.setenv("SLT_STATE_DIR", str(tmp_path / "b"))
        with pytest.raises(pytest.UsageError, match="another integration-test session is already running"):
            plugin._acquire_session_lock(SimpleNamespace(option=SimpleNamespace(collectonly=False)))
    finally:
        first.release()


def test_no_filelock_carries_a_wait():
    # A worker queued on the compose/provision lock must wait out a sibling's bring-up: at
    # timeout=120 the waiters failed with filelock.Timeout while Teradata was still booting.
    # timeout=0 (the session lock's non-blocking probe) is not a wait and is allowed. A named
    # lock bound is allowed too, and only while it outlasts a sibling's whole compose critical
    # section (a reset `down`, then `up -d --wait` and its post-up), each bounded on its own.
    import ast
    from livetest import dockercmd
    bounds = {"dockercmd.LOCK_WAIT_S": dockercmd.LOCK_WAIT_S}
    for name, value in bounds.items():
        assert value >= dockercmd.COMPOSE_DOWN_TIMEOUT_S + 2 * dockercmd.COMPOSE_UP_TIMEOUT_S, (name, value)
    root = Path(plugin.__file__).parent.parent
    waits = []
    for src in sorted([*root.glob("inttest/*.py"), *root.glob("tests/*.py")]):
        for node in ast.walk(ast.parse(src.read_text())):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id",
                    getattr(node.func, "attr", None)) == "FileLock"):
                continue
            timeout = next((k.value for k in node.keywords if k.arg == "timeout"),
                           node.args[1] if len(node.args) > 1 else None)
            if timeout is not None and not (isinstance(timeout, ast.Constant)
                                            and timeout.value in (0, -1)) \
                    and ast.unparse(timeout) not in bounds:
                waits.append(f"{src.relative_to(root)}:{node.lineno}")
    assert not waits, waits


def test_perf_detection_through_symlink(tmp_path):
    # Review Focus 4: SLT_INT_CASES through a symlink still classifies perf cases.
    real = tmp_path / "real"
    (real / "regression").mkdir(parents=True)
    (real / "perf" / "x").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)
    perf = paths.perf_dir({"SLT_INT_CASES": str(link / "regression")}, {})
    case = (real / "perf" / "x" / "test.yaml").resolve()
    assert case.is_relative_to(perf)


def test_spanner_ddl_lock_under_state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("SLT_STATE_DIR", str(tmp_path))
    from inttest.spanneradmin import SpannerAdmin
    admin = SpannerAdmin({"project": "p", "instance": "i", "database": "d",
                          "dialect": "postgresql", "emulator_host": "localhost:9010"},
                         database=object(), client=object())   # injected fakes: no google calls
    assert admin._ddl_lock_path().parent == tmp_path.resolve()
    assert admin._ddl_lock_path().name.endswith(".lock")


def _import_plugin(extra, cwd):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SLT_", "STRIIM_"))}
    env.update(extra)
    env["PYTHONPATH"] = str(INT)
    return subprocess.run([sys.executable, "-c", "import inttest.plugin"],
                          cwd=cwd, env=env, capture_output=True, text=True)


def test_perf_dir_default_unchanged(tmp_path):
    assert _run(_mod("inttest.plugin", "_PERF_DIR"), {}, tmp_path) == INT / "perf"


# striim-test collects the cases under SLT_INT_CASES, so the perf root follows the key: the
# framework repo wires collection to it (field refused a moved root until then).
def test_int_cases_elsewhere_moves_the_perf_root_with_it(tmp_path):
    (tmp_path / "regression").mkdir()
    assert _run(_mod("inttest.plugin", "_PERF_DIR"), {"SLT_INT_CASES": str(tmp_path / "regression")},
                tmp_path) == tmp_path.resolve() / "perf"


def test_int_cases_in_dotenv_moves_the_perf_root_with_it(tmp_path):
    (tmp_path / "regression").mkdir()
    (tmp_path / ".env").write_text(f"SLT_INT_CASES={tmp_path / 'regression'}\n")
    assert _run(_mod("inttest.plugin", "_PERF_DIR"), {"SLT_PROJECT_ROOT": str(tmp_path)},
                tmp_path) == tmp_path.resolve() / "perf"


def test_int_cases_that_does_not_exist_is_refused(tmp_path):
    r = _import_plugin({"SLT_INT_CASES": str(tmp_path / "missing")}, tmp_path)
    assert r.returncode != 0
    assert "SLT_INT_CASES" in r.stderr and "does not exist" in r.stderr


def test_perf_dir_stays_on_the_collected_tree_under_another_framework_home(tmp_path):
    # testpaths collects this checkout's perf/; SLT_FRAMEWORK_HOME moving _PERF_DIR elsewhere
    # would classify every perf test.yaml as a regression case.
    fw = _fw_int(tmp_path)
    # Settings discovery consults both tiers, even when only integration cases are collected.
    assert (fw / "integration" / "inttest").is_dir()
    assert (fw / "live" / "livetest").is_dir()
    assert _run(_mod("inttest.plugin", "_PERF_DIR"), {"SLT_FRAMEWORK_HOME": str(fw)},
                tmp_path) == INT / "perf"



def test_the_cli_and_a_session_share_the_compose_lock_under_a_stack_prefix(tmp_path, monkeypatch):
    # `start`/`stop` must serialise with a session's `compose up` on the same stack.
    from inttest import cli, plugin
    from livetest import stack
    monkeypatch.setattr(stack, "_LOCK_DIR", tmp_path / "locks")
    monkeypatch.setenv("SLT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("INT_STACK_PREFIX", "alt")
    assert cli._lock_file() == plugin._compose_lock_path()
    assert cli._lock_file().name == ".alt-int-compose.lock"
