"""Verify: ``list`` and ``run --dry-run`` start no Docker, Maven or Striim process and open no socket;
the only processes are the CLI's own pytest tier children (ported from the legacy framework repo)."""
import pytest

from _clikit import clone_env, framework_env, run_cli

OFFLINE = [
    ("list", ["list"], 0, {"pytest"}),
    ("run-dry-run-integration", ["run", "--dry-run", "--tier", "integration"], 0, {"pytest"}),
    ("run-dry-run-live", ["run", "--dry-run", "--tier", "live"], 0, {"pytest"}),
]


def _module(argv):
    return argv[argv.index("-m") + 1] if "-m" in argv[:-1] else None


@pytest.mark.parametrize("key,argv,expected,modules", OFFLINE, ids=[k for k, *_ in OFFLINE])
def test_offline_commands_trap_clean(project, elsewhere, trap, key, argv, expected, modules):
    r = run_cli(argv + ["--targets", project], cwd=elsewhere,
                env=trap.env(framework_env(GOLD_TARGETS=project)))
    assert r.rc == expected, (r.stdout, r.stderr)
    assert trap.calls() == []
    events = trap.audit()
    assert not [e for e in events if e["event"] in ("socket.connect", "os.system")]
    spawns = [e for e in events if e["event"] == "subprocess.Popen"]
    # Exactly the CLI's own tier interpreters, and nothing they spawn in turn.
    assert {_module(e["argv"]) for e in spawns} == modules, spawns


def test_audit_reaches_descendants(project, elsewhere, trap):
    # Positive control for the instrument: the audit hook survives into the pytest children and
    # records their socket events.
    env = trap.env(framework_env(GOLD_TARGETS=project, XTR_AUDIT_PROBE="1"))
    r = run_cli(["list", "--tier", "integration", "--targets", project], cwd=elsewhere, env=env)
    assert r.rc == 0, r.stderr
    events = trap.audit()
    hooks = {e["pid"]: e["argv"] for e in events if e["event"] == "hook"}
    connects = {e["pid"] for e in events if e["event"] == "socket.connect"}
    pids = {pid for pid, argv in hooks.items() if "pytest" in argv}
    assert pids and pids <= connects, (hooks, connects)
    assert trap.calls() == []


@pytest.mark.parametrize("argv", [["list"], ["run", "--dry-run"]], ids=["list", "run-dry-run"])
def test_nothing_set_commands_trap_clean(clone, elsewhere, trap, argv):
    # No manifest, no path keys: this clone's own suites (the path 2.7 adds).
    r = run_cli(argv, cwd=elsewhere, env=trap.env(clone_env(clone)))
    assert r.rc == 0, (r.stdout, r.stderr)
    assert trap.calls() == []
    events = trap.audit()
    assert not [e for e in events if e["event"] in ("socket.connect", "os.system")]
    assert {_module(e["argv"]) for e in events if e["event"] == "subprocess.Popen"} == {"pytest"}
