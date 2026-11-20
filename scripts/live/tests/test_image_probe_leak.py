"""A probe container leaked by an interrupted run is cleared by the next one.

``image_inputs_match`` names its probe ``slt-image-probe-<pid>``. The comment said the next run
clears a leaked probe, but the pre-clean removed only the current pid's name, so a probe left by a
run interrupted between ``create`` and ``rm`` kept the 21 GB image pinned until removed by hand.
Every listed probe whose pid is no longer alive is now removed before the new one is created.
"""
from __future__ import annotations

import types

from livetest import striim_provision as sp


def _docker(listed: str, ps_rc: int = 0):
    calls = []

    def run(argv):
        calls.append(argv)
        if argv[:3] == ["docker", "images", "-q"]:
            return types.SimpleNamespace(stdout="abc123\n", returncode=0)
        if argv[:3] == ["docker", "ps", "-a"]:
            return types.SimpleNamespace(stdout=listed, returncode=ps_rc)
        if argv[:2] == ["docker", "create"]:
            return types.SimpleNamespace(stdout="cid\n", returncode=0)
        return types.SimpleNamespace(stdout="", returncode=0)

    return run, calls


def _removed(calls):
    return [a[3] for a in calls if a[:3] == ["docker", "rm", "-f"]]


def _checkout(tmp_path):
    root = tmp_path / "images" / "striim"
    (root / "files").mkdir(parents=True)
    (root / "Dockerfile").write_bytes(b"FROM scratch\n")
    return tmp_path


def test_a_probe_of_a_dead_pid_is_removed_before_the_new_probe_is_created(tmp_path, monkeypatch):
    monkeypatch.setattr(sp.os, "getpid", lambda: 222)
    monkeypatch.setattr(sp, "_pid_alive", lambda pid: pid == 333)
    run, calls = _docker("slt-image-probe-111\nslt-image-probe-333\nslt-image-probe-222\nslt-image-probe-x\n")
    sp.image_inputs_match("5.4.2", _checkout(tmp_path), run=run)
    removed = _removed(calls)
    create = next(i for i, a in enumerate(calls) if a[:2] == ["docker", "create"])
    assert "slt-image-probe-111" in removed                      # its run is gone: the leak is cleared
    assert calls.index(["docker", "rm", "-f", "slt-image-probe-111"]) < create
    assert "slt-image-probe-333" not in removed                  # a live run's probe is left alone
    assert "slt-image-probe-x" not in removed                    # not a name this code makes
    assert set(removed) == {"slt-image-probe-111", "slt-image-probe-222"}


def test_the_listing_asks_only_for_probe_names(tmp_path, monkeypatch):
    run, calls = _docker("")
    sp.image_inputs_match("5.4.2", _checkout(tmp_path), run=run)
    ps = [a for a in calls if a[:3] == ["docker", "ps", "-a"]]
    assert ps and "name=^slt-image-probe-" in ps[0] and "{{.Names}}" in ps[0]


def test_a_failed_listing_still_checks_the_image(tmp_path, monkeypatch):
    monkeypatch.setattr(sp.os, "getpid", lambda: 222)
    monkeypatch.setattr(sp, "_pid_alive", lambda pid: False)
    run, calls = _docker("slt-image-probe-111\n", ps_rc=1)
    sp.image_inputs_match("5.4.2", _checkout(tmp_path), run=run)
    assert "slt-image-probe-111" not in _removed(calls)          # an unreadable listing removes nothing
    assert any(a[:2] == ["docker", "create"] for a in calls)


def test_pid_alive_reads_a_live_and_a_dead_pid():
    import os
    import subprocess
    import sys
    assert sp._pid_alive(os.getpid()) is True
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait(timeout=30)
    assert sp._pid_alive(p.pid) is False
