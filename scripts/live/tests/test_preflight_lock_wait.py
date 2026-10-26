"""A pre-flight waits out a bring-up elsewhere that holds the machine-wide provisioning lock, and
says busy when it cannot get it, instead of reporting the service unavailable after 5 s."""

import pytest
from filelock import Timeout

from livetest import service_healing as h, services


class _BusyLock:
    """Refuses the first `busy` acquires, as a lock held by a slow bring-up elsewhere would."""
    def __init__(self, busy, clock=None):
        self.busy, self.clock, self.held, self.timeouts = busy, clock, False, []

    def __call__(self, *a, timeout=-1, **k):
        self.timeout = timeout
        return self

    def acquire(self, timeout=None, **k):
        timeout = self.timeout if timeout is None else timeout
        self.timeouts.append(timeout)
        if self.busy:
            self.busy -= 1
            if self.clock is not None:
                self.clock.t += timeout
            raise Timeout("lock")
        self.held = True

    def release(self, **k):
        self.held = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *a):
        self.release()


class _Clock:
    t = 0.0

    def __call__(self):
        return self.t


def test_provisioning_waits_for_a_bring_up_elsewhere(monkeypatch, tmp_path):
    clock = _Clock()
    lock = _BusyLock(busy=4, clock=clock)            # held for ~20 s elsewhere
    monkeypatch.setattr(services, "FileLock", lock)
    ctx = h.RecoveryContext(tmp_path / "ev", "run", "op", "lane", clock=clock)
    ran = []
    with h.preflight_context(ctx):
        assert services.ensure_provisioned_once("slt-oracle", lambda: ran.append(1), state_dir=tmp_path)
    assert ran == [1] and not lock.held
    assert all(0 < t <= 5 for t in lock.timeouts)     # cancellation is still checked every 5 s


def test_lock_held_past_the_deadline_is_busy_not_unavailable(tmp_path):
    clock = _Clock()
    ctx = h.RecoveryContext(tmp_path / "ev", "run", "op", "lane", clock=clock)
    with pytest.raises(h.Refused, match="busy: provisioning lock x is still held by another bring-up"):
        ctx.acquire(_BusyLock(busy=10 ** 6, clock=clock)(), "provisioning lock x")


def test_cancellation_stops_the_wait(tmp_path):
    ctx = h.RecoveryContext(tmp_path / "ev", "run", "op", "lane", cancelled=lambda: True)
    with pytest.raises(h.Refused, match="cancelled"):
        ctx.acquire(_BusyLock(busy=1)(), "x")


def test_shared_service_lock_uses_the_waiting_acquire(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from livetest import infra
    monkeypatch.setattr(infra, "_lock_dir", lambda: tmp_path)
    acquired = []
    resolved = infra.resolve_service(
        SimpleNamespace(ownership="shared", services=[]), "pg", {}, set(), lambda *a, **k: "ok",
        load=lambda n: SimpleNamespace(container="slt-pg", live_override_env=None),
        container_running=lambda c: True, acquire_lock=lambda lock: acquired.append(lock) or lock.acquire())
    assert resolved == "ok" and len(acquired) == 1



def test_preflight_waits_out_a_bring_up_elsewhere_on_the_shared_lock(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from livetest import preflight
    clock = _Clock()
    lock = _BusyLock(busy=4, clock=clock)()        # the shared provisioning lock, held ~20 s elsewhere

    def resolve_service(infra, svc, env, started, resolve_fn, progress=None, **lock_options):
        assert "lock_timeout" not in lock_options
        lock_options["acquire_lock"](lock)
        return SimpleNamespace(base={})
    monkeypatch.setattr(preflight._slt_infra, "resolve_service", resolve_service)
    monkeypatch.setattr(preflight, "load_service", lambda svc: SimpleNamespace(name=svc))
    monkeypatch.setattr(preflight._prestart, "prepare_selected", lambda *a, **k: None)
    monkeypatch.setattr(preflight, "missing_port_overrides", lambda *a: [])
    ctx = h.RecoveryContext(tmp_path / "ev", "run", "op", "lane", clock=clock, log=lambda *a: None)
    outcomes = {}
    with h.preflight_context(ctx):
        preflight._bring_up_services(["kafka"], {}, apply_gate=False, infra=SimpleNamespace(ownership="shared"),
                                     outcomes=outcomes)
    assert outcomes == {"kafka": {"status": "ready", "reason": ""}}
    assert lock.held and len(lock.timeouts) == 5
