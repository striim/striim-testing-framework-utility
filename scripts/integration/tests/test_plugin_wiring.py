"""Unit tests for inttest.plugin's own pytest-hook wiring: the two-way
`pytest_collect_file` location dispatch (SPEC §3 / PERF_SPEC.md §14), the
`--perf` selection filtering + UsageError in `pytest_collection_modifyitems`
(PERF_SPEC.md §14), the `--perf`-under-xdist rejection in `pytest_configure`
(PERF_SPEC.md §9 "Sequencing"), and a malformed test.yaml's ManifestError/
PerfManifestError escaping to `pytest.fail` at collection time.

These call the hook functions directly with lightweight fakes/mocks rather than
running a real pytest-in-pytest session: `pytest_collect_file`/`*YamlFile.collect`
otherwise need a fully wired `pytest.Collector` tree (`from_parent` requires a
real `session`/`config`), which a `MagicMock(spec=...)` or a bare
`object.__new__(...)` sidesteps for exactly the piece of logic each test targets,
without the fragility of relocating `plugin._PERF_DIR`/`_BOOTSTRAP_DIR` (module-
level, hardcoded to the real repo tree) into an isolated `pytester` rootdir.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from inttest import manifest as manifest_mod
from inttest import perfmanifest as perfmanifest_mod
from inttest import plugin


# --- pytest_collect_file: two-way location dispatch (PERF_SPEC.md §14) --------

def test_pytest_collect_file_ignores_non_test_yaml_names():
    assert plugin.pytest_collect_file(parent=None, file_path=Path("/x/y/other.yaml")) is None


def test_pytest_collect_file_dispatches_perf_by_location(monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, "_PERF_DIR", tmp_path / "perf")
    file_path = tmp_path / "perf" / "op" / "transform" / "some-case" / "test.yaml"

    sentinel = object()
    from_parent = MagicMock(return_value=sentinel)
    monkeypatch.setattr(plugin.PerfYamlFile, "from_parent", from_parent)

    result = plugin.pytest_collect_file(parent="parent-marker", file_path=file_path)

    assert result is sentinel
    from_parent.assert_called_once_with("parent-marker", path=file_path)


def test_pytest_collect_file_dispatches_regression_by_location(monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, "_PERF_DIR", tmp_path / "perf")
    file_path = tmp_path / "regression" / "op" / "transform" / "some-case" / "test.yaml"

    sentinel = object()
    from_parent = MagicMock(return_value=sentinel)
    monkeypatch.setattr(plugin.IntYamlFile, "from_parent", from_parent)

    result = plugin.pytest_collect_file(parent="parent-marker", file_path=file_path)

    assert result is sentinel
    from_parent.assert_called_once_with("parent-marker", path=file_path)


# --- *YamlFile.collect(): a malformed test.yaml escapes as pytest.fail --------

def test_perf_yaml_file_collect_wraps_perf_manifest_error_as_pytest_fail(monkeypatch, tmp_path):
    bogus_path = tmp_path / "test.yaml"
    monkeypatch.setattr(
        perfmanifest_mod, "load_perf_manifest",
        MagicMock(side_effect=perfmanifest_mod.PerfManifestError("boom: bad shape")),
    )

    perf_file = object.__new__(plugin.PerfYamlFile)
    perf_file.path = bogus_path

    with pytest.raises(pytest.fail.Exception, match="boom: bad shape"):
        list(perf_file.collect())


def test_int_yaml_file_collect_wraps_manifest_error_as_pytest_fail(monkeypatch, tmp_path):
    bogus_path = tmp_path / "test.yaml"
    monkeypatch.setattr(
        manifest_mod, "load_manifest",
        MagicMock(side_effect=manifest_mod.ManifestError("boom: missing name")),
    )

    int_file = object.__new__(plugin.IntYamlFile)
    int_file.path = bogus_path

    with pytest.raises(pytest.fail.Exception, match="boom: missing name"):
        list(int_file.collect())


# --- pytest_collection_modifyitems: symmetric --perf selection (PERF_SPEC.md §14) --

class _FakeConfig:
    def __init__(self, perf: bool, reverse: bool = False):
        self._opts = {"--perf": perf, "--perf-reverse": reverse}
        self.hook = MagicMock()

    def getoption(self, name):
        # Still exhaustive, not permissive: an unexpected option name is a wiring change that
        # should fail here rather than silently read as False.
        assert name in self._opts, name
        return self._opts[name]


def _fake_perf_item():
    return MagicMock(spec=plugin.PerfYamlItem)


def _fake_int_item():
    return MagicMock(spec=plugin.IntYamlItem)


def test_modifyitems_perf_mode_selects_only_perf_items():
    perf_item = _fake_perf_item()
    int_item = _fake_int_item()
    items = [int_item, perf_item]
    config = _FakeConfig(perf=True)

    plugin.pytest_collection_modifyitems(config, items)

    assert items == [perf_item]
    config.hook.pytest_deselected.assert_called_once_with(items=[int_item])


def test_modifyitems_default_mode_deselects_perf_items():
    perf_item = _fake_perf_item()
    int_item = _fake_int_item()
    items = [int_item, perf_item]
    config = _FakeConfig(perf=False)

    plugin.pytest_collection_modifyitems(config, items)

    assert items == [int_item]
    config.hook.pytest_deselected.assert_called_once_with(items=[perf_item])


def test_modifyitems_default_mode_no_deselection_hook_when_nothing_to_deselect():
    int_item = _fake_int_item()
    items = [int_item]
    config = _FakeConfig(perf=False)

    plugin.pytest_collection_modifyitems(config, items)

    assert items == [int_item]
    config.hook.pytest_deselected.assert_not_called()


def test_modifyitems_perf_mode_raises_usage_error_when_none_selected():
    items = [_fake_int_item()]
    config = _FakeConfig(perf=True)

    with pytest.raises(pytest.UsageError, match="--perf selected no performance tests"):
        plugin.pytest_collection_modifyitems(config, items)


# --- pytest_configure: --perf runs serially, unconditionally (PERF_SPEC.md §9) ----


# --- §115.6: the ordering control ------------------------------------------------

def test_perf_reverse_reverses_the_measured_order():
    """§115.6. Adjacent runs differed 4-10% on ORDER ALONE, and the tier had no control."""
    a, b, c = _fake_perf_item(), _fake_perf_item(), _fake_perf_item()
    items = [a, b, c]

    plugin.pytest_collection_modifyitems(_FakeConfig(perf=True, reverse=True), items)

    assert items == [c, b, a]


def test_perf_reverse_reverses_across_cases_not_just_within_one():
    """⚠ §115.6 names TWO things, and one mechanism has to cover both.

    Adjacent `matrix:` permutations are contiguous within a file; §69.3's five hand-written
    pairs are separate cases that "run in sequence in one session and were never
    order-controlled". Reversing inside `runs()` would have covered only the first -- and
    doing it in BOTH places would reverse permutations twice, leaving them forward while the
    files moved, which looks like a control and is not one.
    """
    # Two "files" of two permutations each, in collection order.
    f1p1, f1p2, f2p1, f2p2 = (_fake_perf_item() for _ in range(4))
    items = [f1p1, f1p2, f2p1, f2p2]

    plugin.pytest_collection_modifyitems(_FakeConfig(perf=True, reverse=True), items)

    assert items == [f2p2, f2p1, f1p2, f1p1]
    assert items[0] is f2p2 and items[-1] is f1p1, "the whole sequence reverses, not each file"


def test_perf_forward_is_unchanged_by_the_option_existing():
    """The control must be OPT-IN: a default run measures exactly what it measured before."""
    a, b, c = _fake_perf_item(), _fake_perf_item(), _fake_perf_item()
    items = [a, b, c]

    plugin.pytest_collection_modifyitems(_FakeConfig(perf=True, reverse=False), items)

    assert items == [a, b, c]


def test_perf_reverse_without_perf_is_a_usage_error():
    """It reverses the PERF tier's order; silently doing nothing would be worse than refusing."""
    items = [_fake_int_item()]
    with pytest.raises(pytest.UsageError, match="does nothing without --perf"):
        plugin.pytest_collection_modifyitems(_FakeConfig(perf=False, reverse=True), items)


class _FakeOption:
    def __init__(self, numprocesses=None, dist=None, collectonly=False):
        self.numprocesses = numprocesses
        self.collectonly = collectonly
        self.dist = dist


class _FakeConfigureConfig:
    def __init__(self, *, perf: bool, numprocesses=None, dist=None, collectonly=False):
        self._perf = perf
        self.option = _FakeOption(numprocesses=numprocesses, dist=dist,
                                  collectonly=collectonly)

    def getoption(self, name):
        assert name == "--perf"
        return self._perf

    def addinivalue_line(self, *a, **kw):
        pass


def test_configure_rejects_xdist_numprocesses_under_perf():
    config = _FakeConfigureConfig(perf=True, numprocesses=4)

    with pytest.raises(pytest.UsageError, match="performance mode \\(--perf\\) runs SERIALLY"):
        plugin.pytest_configure(config)


def test_configure_rejects_xdist_dist_flag_under_perf():
    config = _FakeConfigureConfig(perf=True, dist="loadscope")

    with pytest.raises(pytest.UsageError, match="performance mode \\(--perf\\) runs SERIALLY"):
        plugin.pytest_configure(config)


def test_configure_allows_perf_with_no_xdist_options():
    config = _FakeConfigureConfig(perf=True)

    plugin.pytest_configure(config)  # must not raise


def test_configure_xdist_under_non_perf_needs_slt_parallel_not_the_perf_message(monkeypatch):
    monkeypatch.delenv("SLT_PARALLEL", raising=False)
    config = _FakeConfigureConfig(perf=False, numprocesses=4)

    with pytest.raises(pytest.UsageError, match="SLT_PARALLEL=1"):
        plugin.pytest_configure(config)


# --- _require_service_opt_in: Spanner's INT_SPANNER/INT_EMULATORS gate
# (mirrors scripts/live/livetest/plugin.py's SLT_SPANNER/SLT_EMULATORS gate) -------

def test_require_service_opt_in_skips_when_gate_unset(monkeypatch):
    monkeypatch.setattr(plugin, "_load_service_yaml", lambda name: {"opt_in_env": "INT_SPANNER"})
    monkeypatch.delenv("INT_SPANNER", raising=False)
    monkeypatch.delenv("INT_EMULATORS", raising=False)

    with pytest.raises(pytest.skip.Exception, match="INT_SPANNER=1"):
        plugin._require_service_opt_in("spanner")


def test_require_service_opt_in_allows_when_own_gate_set(monkeypatch):
    monkeypatch.setattr(plugin, "_load_service_yaml", lambda name: {"opt_in_env": "INT_SPANNER"})
    monkeypatch.setenv("INT_SPANNER", "1")
    monkeypatch.delenv("INT_EMULATORS", raising=False)

    plugin._require_service_opt_in("spanner")  # must not raise/skip


def test_require_service_opt_in_allows_via_emulators_umbrella(monkeypatch):
    monkeypatch.setattr(plugin, "_load_service_yaml", lambda name: {"opt_in_env": "INT_SPANNER"})
    monkeypatch.delenv("INT_SPANNER", raising=False)
    monkeypatch.setenv("INT_EMULATORS", "1")

    plugin._require_service_opt_in("spanner")  # must not raise/skip


def test_require_service_opt_in_no_gate_declared_never_skips(monkeypatch):
    # postgres/oracle declare no opt_in_env -- must stay unconditionally provisioned.
    monkeypatch.setattr(plugin, "_load_service_yaml", lambda name: {})
    monkeypatch.delenv("INT_EMULATORS", raising=False)

    plugin._require_service_opt_in("postgres")  # must not raise/skip


# --- _provision_requires: a service that WILL NOT start is a failure, not a skip

def test_provision_requires_fails_when_compose_up_fails(monkeypatch):
    import subprocess

    docker = MagicMock()
    docker.SUPPORTED_SERVICES = {"postgres"}
    docker.unavailable.return_value = None       # its required files are present
    docker.ensure_up.side_effect = subprocess.CalledProcessError(1, ["docker", "compose", "up"])
    monkeypatch.setattr(plugin, "_docker_mod", docker)
    monkeypatch.setattr(plugin, "_require_service_opt_in", lambda svc: None)

    with pytest.raises(pytest.fail.Exception, match="docker compose up.*failed for service 'postgres'"):
        plugin._provision_requires("case-x", ["postgres"], lock=MagicMock())


def test_provision_requires_still_skips_without_docker(monkeypatch):
    # No docker at all is "not applicable here", which stays a skip.
    docker = MagicMock()
    docker.SUPPORTED_SERVICES = {"postgres"}
    docker.unavailable.return_value = None       # its required files are present
    docker.ensure_up.side_effect = FileNotFoundError("docker")
    monkeypatch.setattr(plugin, "_docker_mod", docker)
    monkeypatch.setattr(plugin, "_require_service_opt_in", lambda svc: None)

    with pytest.raises(pytest.skip.Exception, match="cannot bring up service 'postgres'"):
        plugin._provision_requires("case-x", ["postgres"], lock=MagicMock())


# ---------------------------------------------------------------------------
# The concurrent-session guard.
#
# Two pytest sessions cannot share this checkout: in a serial run ${TID} is EMPTY, so both
# address the same fixed table names in the same schemas and drop, recreate and truncate
# each other's fixtures mid-test. Reproduced 2026-08-29; the failures landed in whichever
# module happened to be executing and named none of the real cause.
# ---------------------------------------------------------------------------


def _drop_session_lock():
    """Release whatever the surrounding real session holds, so a test can exercise the
    acquire path, and restore it afterwards."""
    held = plugin._session_lock
    plugin._session_lock = None
    return held


def test_session_guard_is_reentrant_within_one_process(monkeypatch):
    # One process is one session. Re-acquiring a second fd on the same file would deadlock
    # against ourselves and report it as a foreign session -- which is exactly what the
    # first cut of this guard did to the suite that tests it.
    monkeypatch.setattr(plugin, "_session_lock", object())
    plugin._acquire_session_lock(_FakeConfigureConfig(perf=False))  # must not raise


def test_session_guard_skips_xdist_workers(monkeypatch, tmp_path):
    # A worker shares the controller's session; blocking it would break every parallel run.
    held = _drop_session_lock()
    try:
        monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
        plugin._acquire_session_lock(_FakeConfigureConfig(perf=False))
        assert plugin._session_lock is None, "a worker must not take the session lock"
    finally:
        plugin._session_lock = held


def test_session_guard_honours_the_documented_bypass(monkeypatch):
    held = _drop_session_lock()
    try:
        monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
        monkeypatch.setenv("INT_ALLOW_CONCURRENT_SESSIONS", "1")
        plugin._acquire_session_lock(_FakeConfigureConfig(perf=False))
        assert plugin._session_lock is None
    finally:
        plugin._session_lock = held


def test_session_guard_refuses_a_second_session_and_names_the_cause(monkeypatch, tmp_path):
    from filelock import FileLock

    held = _drop_session_lock()
    try:
        monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
        monkeypatch.delenv("INT_ALLOW_CONCURRENT_SESSIONS", raising=False)
        monkeypatch.delenv("INT_SHARED_SERVICES", raising=False)
        # A lock file this test owns, so the assertion is about the guard rather than about
        # whatever the surrounding run happens to be holding.
        foreign = tmp_path / ".int-session.lock"
        monkeypatch.setattr(plugin, "_session_lock_path", lambda: foreign)
        other_session = FileLock(str(foreign), timeout=0)
        other_session.acquire()
        try:
            with pytest.raises(pytest.UsageError) as e:
                plugin._acquire_session_lock(_FakeConfigureConfig(perf=False))
            # The message has to name the mechanism: the whole point is that the symptom
            # (a unique-constraint violation, a missing table) names the wrong thing.
            assert "another integration-test session" in str(e.value)
            assert "${TID} is EMPTY" in str(e.value)
            assert "INT_STACK_PREFIX" in str(e.value)
        finally:
            other_session.release()
    finally:
        plugin._session_lock = held


def test_session_guard_lets_collect_only_through(monkeypatch, tmp_path):
    # --collect-only parses YAML and touches no service, so it must not be refused beside a
    # running suite -- blocking a read-only "what would run?" would be a regression in its
    # own right.
    from filelock import FileLock

    held = _drop_session_lock()
    try:
        monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
        monkeypatch.delenv("INT_ALLOW_CONCURRENT_SESSIONS", raising=False)
        foreign = tmp_path / ".int-session.lock"
        monkeypatch.setattr(plugin, "_session_lock_path", lambda: foreign)
        other_session = FileLock(str(foreign), timeout=0)
        other_session.acquire()
        try:
            plugin._acquire_session_lock(
                _FakeConfigureConfig(perf=False, collectonly=True))   # must not raise
            assert plugin._session_lock is None, "--collect-only must not take the lock"
        finally:
            other_session.release()
    finally:
        plugin._session_lock = held


def test_release_session_lock_is_idempotent(monkeypatch):
    held = _drop_session_lock()
    try:
        plugin._release_session_lock()
        plugin._release_session_lock()  # must not raise
        assert plugin._session_lock is None
    finally:
        plugin._session_lock = held


def test_session_guard_cannot_strand_future_runs_with_a_stale_file(tmp_path):
    """A dead session must never block the next one.

    THE PROPERTY THAT MATTERS: the marker is the OS lock, NOT the file's existence.
    `filelock` takes an `fcntl` lock, which the kernel drops when the holder dies -- so even
    a SIGKILL (no atexit, no finally, no `pytest_unconfigure`) frees it, leaving only an
    inert file behind.

    This test exists to stop a future "simplification" to `if path.exists(): refuse`, which
    would strand every run on this checkout after the first crash -- a far worse failure
    than the fixture corruption the guard is here to prevent.
    """
    import signal
    import subprocess
    import sys
    import time

    from filelock import FileLock, Timeout

    lockfile = tmp_path / ".int-session.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c",
         f"from filelock import FileLock\n"
         f"l = FileLock({str(lockfile)!r}, timeout=0); l.acquire()\n"
         f"print('HELD', flush=True)\n"
         f"import time; time.sleep(300)\n"],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "HELD"

        def acquires() -> bool:
            probe = FileLock(str(lockfile), timeout=0)
            try:
                probe.acquire()
            except Timeout:
                return False
            probe.release()
            return True

        assert not acquires(), "a live holder must block a second session"

        holder.send_signal(signal.SIGKILL)   # the harshest exit there is
        holder.wait(timeout=10)
        for _ in range(50):                  # the kernel drops it promptly, but not atomically
            if acquires():
                break
            time.sleep(0.1)
        else:
            raise AssertionError("SIGKILL left the lock held -- every future run is stranded")

        assert lockfile.exists(), (
            "the FILE is expected to survive; this test is only meaningful because it does")
        assert acquires(), "a leftover file with no holder must not block anything"
    finally:
        if holder.poll() is None:
            holder.kill()
        holder.wait(timeout=10)
        if holder.stdout:
            holder.stdout.close()

# --- assert.expect_log is honoured on BOTH drive paths -------------------------

def test_expect_log_is_wired_into_every_drive_path():
    """⚠ R2's defect shape: an assert key honoured on one path and inert on the other.

    `_assert_expect_log` is called from `_run_target_case` (a writer) and from the emitted-events
    path (an ordinary operator). A key evaluated on only one of them would be INVISIBLE, because a
    case whose log assertion never runs simply passes. This pins both call sites and the sink that
    feeds them, so deleting either is a test failure rather than a silent hole.
    """
    src = Path(plugin.__file__).read_text()
    assert src.count("self._assert_expect_log(m, at,") == 2, \
        "expect_log must be asserted on BOTH the target path and the emitted-events path"
    # Each call needs a sink actually passed to drive(), or it asserts against nothing.
    assert src.count("log_sink=") == 2, \
        "every drive() that feeds an expect_log assertion must pass a log_sink"


# --- _mid_run_callback: every step at the ordinal, in list order, on its own route -

def test_mid_run_callback_runs_steps_sharing_an_ordinal_in_list_order(monkeypatch, tmp_path):
    test_dir = tmp_path / "case"
    test_dir.mkdir()
    for name in ("wait.sql", "probe.sql", "later.sql"):
        (test_dir / name).write_text(f"-- {name}")
    (test_dir / "test.yaml").write_text("""
name: writer-case
op:
  jar: java/OpenProcessors/JdbcSink
requires: [postgres]
target:
  input: input/events.json
  mid_run:
    - {after: 2, file: wait.sql, db: postgres-source}
    - {after: 2, file: probe.sql}
    - {after: 3, file: later.sql}
assert:
  target:
    - query: SELECT ID FROM T ORDER BY ID
      match: expected/rows.json
""")
    m = manifest_mod.load_manifest(test_dir / "test.yaml")
    ran = []
    monkeypatch.setattr(plugin.dbroutes_mod, "run_sql_text",
                        lambda route, sql, tok: ran.append((route, sql)))
    variant = MagicMock(db="oracle-target")

    run = object.__new__(plugin.IntYamlItem)._mid_run_callback(m, {}, variant)
    run(2)

    # The explicit route wins; the other follows the variant. Ordinal 3 is untouched.
    assert ran == [("postgres-source", "-- wait.sql"), ("oracle-target", "-- probe.sql")]
