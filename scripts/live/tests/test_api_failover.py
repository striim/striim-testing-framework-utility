from types import SimpleNamespace
import pytest
import requests
from livetest.striim import StriimClient, StriimError


def test_read_only_status_falls_back_to_survivor():
    class Dead:
        url_base = "primary"
        def status_application(self, app, timeout=None): raise requests.ConnectionError("down")
    class Survivor:
        url_base = "secondary"
        def status_application(self, app, timeout=None): return "RUNNING"
    calls = []
    client = StriimClient(Dead())
    client.configure_failover(["secondary"], app="N.app",
        factory=lambda url, left: calls.append((url, left)) or Survivor())
    assert client.current_status("N.app", timeout=1) == "RUNNING"
    assert client.api.url_base == "secondary" and len(calls) == 1


def test_state_changing_command_is_never_retried_on_other_node():
    class Dead:
        url_base = "primary"
        def start_application(self, app): raise requests.ConnectionError("uncertain write")
    calls = []
    client = StriimClient(Dead())
    client.configure_failover(["secondary"], factory=lambda *a: calls.append(a))
    with pytest.raises(StriimError): client.start_app("N.app")
    assert calls == []


def test_application_not_found_is_not_endpoint_failover():
    class Api:
        url_base = "primary"
        def status_application(self, app, timeout=None): raise RuntimeError("status -> HTTP 404: missing app")
    calls = []
    client = StriimClient(Api())
    client.configure_failover(["secondary"], factory=lambda *a: calls.append(a))
    with pytest.raises(StriimError, match="404"): client.current_status("N.app", timeout=1)
    assert calls == []


def test_monitor_probe_reauthenticates_on_surviving_api():
    class Dead:
        url_base = "primary"
        def status_application(self, app, timeout=None): raise RuntimeError("status -> HTTP 401: old token")
        def post_tungsten_line(self, *args, **kw): pytest.fail("metadata read with dead token")
    class Survivor:
        url_base = "secondary"
        def status_application(self, app, timeout=None): return "RUNNING"
        def post_tungsten_line(self, line, timeout=None):
            return [{"executionStatus": "Success", "output": {"status": "RUNNING"}}]
    client = StriimClient(Dead())
    client.configure_failover(["secondary"], app="N.app", factory=lambda *args: Survivor())
    assert client.mon("N.flow", timeout=1) == {"status": "RUNNING"}


def test_slow_dead_endpoint_leaves_time_for_survivor(monkeypatch):
    from livetest import striim
    clock = [0.0]
    monkeypatch.setattr(striim.time, "monotonic", lambda: clock[0])
    class Dead:
        url_base = "primary"
        def status_application(self, app, timeout=None):
            clock[0] += sum(timeout)
            raise requests.Timeout("dead endpoint consumed its allocation")
    class Survivor:
        url_base = "secondary"
        def status_application(self, app, timeout=None): return "RUNNING"
    client = StriimClient(Dead())
    client.configure_failover(["secondary"], factory=lambda *args: Survivor())
    assert client.current_status("N.app", timeout=10) == "RUNNING"
    assert clock[0] < 10
