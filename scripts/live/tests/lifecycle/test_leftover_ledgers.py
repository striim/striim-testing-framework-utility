"""Review of 3.1, F2: a run killed or timed out before its cleanup leaves a ledger nothing reclaims (a new
run's identity never matches it). Doctor and the live session start name each one with its replay command."""
from __future__ import annotations

import json

from livetest import ownership

from .exec_harness import run_case  # noqa: F401 - run_case is a fixture

pytest_plugins = ["pytester"]


def _ledger(state, name, run, states, keep=None):
    d = state / "lifecycle" / "ledgers"
    d.mkdir(parents=True, exist_ok=True)
    doc = {"ledgerVersion": ownership.LEDGER_VERSION, "identity": {"runId": run, "case": name},
           "entries": [{"kind": "namespace", "name": f"SLT_{name}", "state": s} for s in states], "keep": keep}
    (d / f"{name}.json").write_text(json.dumps(doc))
    return d / f"{name}.json"


def test_only_ledgers_with_objects_left_are_named(tmp_path):
    killed = _ledger(tmp_path, "killed", "r1", ["confirmed", "intended"])
    failed = _ledger(tmp_path, "failed", "r1", ["delete-failed"])
    kept = _ledger(tmp_path, "kept", "r1", ["confirmed"], keep={"reason": "SLT_KEEP_RESOURCES is set"})
    _ledger(tmp_path, "clean", "r1", ["verified-absent", "deleted"])
    _ledger(tmp_path, "intent-only", "r1", ["intended"])      # never created, so never deletion authority
    (tmp_path / "lifecycle" / "ledgers" / "junk.json").write_text("{not json")
    left = dict(ownership.leftover_ledgers(tmp_path))
    assert set(left) == {killed, failed, kept}
    assert "not cleaned up (killed, timed out or a failed cleanup)" in left[killed]
    assert "kept (SLT_KEEP_RESOURCES is set)" in left[kept] and "1 object(s) left (namespace)" in left[kept]


def test_the_current_runs_own_ledgers_are_left_out(tmp_path):
    _ledger(tmp_path, "mine", "now", ["confirmed"])
    old = _ledger(tmp_path, "old", "before", ["confirmed"])
    assert [p for p, _ in ownership.leftover_ledgers(tmp_path, current_run="now")] == [old]
    assert ownership.leftover_ledgers(tmp_path / "absent") == []


def test_replay_command_is_the_ownership_cli():
    assert ownership.replay_command("/s/l.json") == "python -m livetest.ownership replay /s/l.json"


def test_exec_live_session_start_names_a_leftover_ledger(run_case):
    seen = {}

    def leave_one(root, name):
        seen["path"] = _ledger(root / "state", "earlier-case", "an-earlier-run", ["confirmed"])
    run = run_case("legacy", prepare=leave_one)
    assert run.ret == 0, run.text
    assert (f"[slt] leftover ownership ledger: case 'earlier-case' run 'an-earlier-run': 1 object(s) left "
            f"(namespace), not cleaned up (killed, timed out or a failed cleanup); reclaim it with "
            f"python -m livetest.ownership replay {seen['path']}") in run.text


# ---- R3 N1: a ledger whose writer is alive is a run in flight, not a leftover -----------------------

def _with_writer(path, pid, host=None):
    import socket
    doc = json.loads(path.read_text())
    doc["writer"] = {"pid": pid, "host": host or socket.gethostname()}
    path.write_text(json.dumps(doc))
    return path


def test_a_live_writers_ledger_is_in_progress_and_gets_no_replay_command(tmp_path):
    import os
    live = _with_writer(_ledger(tmp_path, "running", "other-run", ["confirmed"]), os.getppid())   # a live process
    (path, why), = ownership.leftover_ledgers(tmp_path)
    assert path == live and why.endswith(f"in progress (writer pid {os.getppid()} is alive), do not replay")
    assert "replay" not in ownership.leftover_notice(path, why).replace("do not replay", "")


def test_a_dead_writers_ledger_is_a_leftover_with_its_replay_command(tmp_path):
    dead = _with_writer(_ledger(tmp_path, "killed", "old-run", ["confirmed"]), 12345)
    (path, why), = ownership.leftover_ledgers(tmp_path, pid_alive=lambda pid: False)
    assert "not cleaned up" in why
    assert ownership.leftover_notice(path, why).endswith(f"reclaim it with python -m livetest.ownership replay {dead}")


def test_a_writer_on_another_host_is_not_taken_as_alive(tmp_path):
    import os
    _with_writer(_ledger(tmp_path, "elsewhere", "r", ["confirmed"]), os.getppid(), host="some-other-host")
    (_, why), = ownership.leftover_ledgers(tmp_path)
    assert "not cleaned up" in why


def test_replay_refuses_a_ledger_whose_writer_is_alive(tmp_path):
    import os
    import pytest
    live = _with_writer(_ledger(tmp_path, "running", "other-run", ["confirmed"]), os.getppid())
    with pytest.raises(ownership.OwnershipError, match="ledger-in-progress"):
        ownership.replay(live, admins={}, client=None, ctx=None, env={})


def test_ledger_open_records_its_writer(tmp_path):
    import os
    import socket
    from livetest import runident
    ledger = ownership.Ledger.open(runident.derive("w", {"SLT_RUN_EPOCH": "r1"}), state_dir=tmp_path)
    assert json.loads(ledger.path.read_text())["writer"] == {"pid": os.getpid(), "host": socket.gethostname()}
