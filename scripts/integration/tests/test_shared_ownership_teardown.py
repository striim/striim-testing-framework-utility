"""SLT_INFRA_OWNERSHIP=shared: reuse what is running, start what is missing, never tear it down.

The integration session used to honour `shared` at bring-up only: a service it found missing and started was
still `docker compose down -v`-ed at session end unless INT_KEEP_SERVICES was also set, volume included, while
another checkout could be using it. Under `shared` the session (and `ensure_up_for_test`) now leaves every
service running, clears the started-services record, and says which services it left."""
from __future__ import annotations

import json
import types

import pytest
from filelock import FileLock, Timeout

from inttest import plugin as P
from inttest import services as S


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("SLT_STATE_DIR", str(tmp_path))
    for var in ("SLT_INFRA_OWNERSHIP", "SLT_KEEP_SERVICES", "INT_KEEP_SERVICES", "PYTEST_XDIST_WORKER"):
        monkeypatch.delenv(var, raising=False)
    downs = []
    monkeypatch.setattr(P._docker_mod, "compose_down", lambda name, lock: downs.append(name))
    monkeypatch.setattr(S, "compose_down", lambda name, lock: downs.append(name))
    monkeypatch.setattr(S, "surviving_containers", lambda name: [])     # no docker in a hermetic test
    return downs


def _session():
    pm = types.SimpleNamespace(get_plugin=lambda name: None)
    return types.SimpleNamespace(exitstatus=0,
                                 config=types.SimpleNamespace(pluginmanager=pm, getoption=lambda *a, **k: ""))


def _shared(monkeypatch):
    monkeypatch.setenv("SLT_INFRA_OWNERSHIP", "shared")
    monkeypatch.setenv("SLT_KEEP_SERVICES", "1")
    assert S.shared_ownership()


def test_a_shared_session_leaves_the_services_it_started_running(monkeypatch, capsys, state):
    _shared(monkeypatch)
    P._write_started({"postgres", "oracle"})
    P.pytest_sessionfinish(_session(), 0)
    assert state == []                                    # no `docker compose down -v`
    assert not P._started_registry().exists()             # nothing claims them any more
    assert "left running (SLT_INFRA_OWNERSHIP=shared): oracle, postgres" in capsys.readouterr().out


def test_a_service_a_shared_session_brings_up_is_not_torn_down_at_session_end(monkeypatch, state):
    _shared(monkeypatch)
    monkeypatch.setattr(S, "_checked_compose_file", lambda name: None)
    monkeypatch.setattr(S, "_compose_file", lambda name: types.SimpleNamespace(exists=lambda: True))
    monkeypatch.setattr(S, "is_up", lambda name: False)
    monkeypatch.setattr(S, "shared_reuse", lambda name: False)
    monkeypatch.setattr(S, "compose_up", lambda name, lock: None)
    assert S.ensure_up("postgres", FileLock(str(P._compose_lock_path()))) is True
    P.pytest_sessionfinish(_session(), 0)
    assert state == [] and not P._started_registry().exists()


def test_ensure_up_for_test_leaves_a_shared_service_running(monkeypatch, state):
    _shared(monkeypatch)
    monkeypatch.setattr(S, "ensure_up", lambda name, lock: (P._write_started({name}), True)[1])
    lock = FileLock(str(P._compose_lock_path()))
    with S.ensure_up_for_test("postgres", lock):
        pass
    assert state == []
    assert "postgres" not in json.loads(P._started_registry().read_text())    # handed over, not claimed


def test_ensure_up_for_test_still_tears_down_an_exclusive_service(monkeypatch, state):
    monkeypatch.setattr(S, "ensure_up", lambda name, lock: (P._write_started({name}), True)[1])
    with S.ensure_up_for_test("postgres", FileLock(str(P._compose_lock_path()))):
        pass
    assert state == ["postgres"]


def test_an_unshared_session_still_tears_down_what_it_started(monkeypatch, state):
    P._write_started({"postgres"})
    P.pytest_sessionfinish(_session(), 0)
    assert state == ["postgres"] and not P._started_registry().exists()


def test_int_keep_services_still_keeps_them(monkeypatch, state):
    monkeypatch.setenv("INT_KEEP_SERVICES", "1")
    P._write_started({"postgres"})
    P.pytest_sessionfinish(_session(), 0)
    assert state == [] and not P._started_registry().exists()


# ---------------------------------------------------------------- the compose lock wait is bounded

def test_a_waiter_for_the_compose_lock_gives_up(monkeypatch, state):
    import threading
    from livetest import dockercmd
    monkeypatch.setattr(dockercmd, "LOCK_WAIT_S", 0.3)
    got, done = threading.Event(), threading.Event()

    def hold():                                            # a real contending holder, in another thread
        lock = FileLock(str(P._compose_lock_path()))
        lock.acquire()
        got.set()
        done.wait(60)
        lock.release()
    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert got.wait(10)
    box = {}

    def wait():
        try:
            with P._compose_filelock():
                box["acquired"] = True
        except Timeout as e:
            box["timeout"] = e
    waiter = threading.Thread(target=wait, daemon=True)
    waiter.start()
    waiter.join(10)
    done.set()
    holder.join(10)
    assert not waiter.is_alive() and "timeout" in box, box


def test_the_compose_lock_waits_the_named_bound(state):
    from livetest import dockercmd
    assert P._compose_filelock().timeout == dockercmd.LOCK_WAIT_S
