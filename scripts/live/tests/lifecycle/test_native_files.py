"""A native Striim's file outputs are deleted the way native mode wrote them.

Native mode reads and places server files on the local filesystem (``striimfile``), so the ledger claims,
deletes and verifies a native file-output there too, when the server is on this host. There is no stack
container in native mode: every ``docker`` call fails as it did on a Linux test host. A remote native server's file
is a named gap, never a failed cleanup.
"""
from __future__ import annotations

import json
import socket
from types import SimpleNamespace

import pytest

from livetest import infra, ownership

from .test_ownership import Client, Pg, _admins, _ledger

NATIVE = SimpleNamespace(mode="native", url="http://localhost:49080", user="admin")
REMOTE = SimpleNamespace(mode="native", url="http://striim-remote.example:9080", user="admin")


@pytest.fixture
def no_container(monkeypatch):
    calls = []

    def docker(argv):
        calls.append(argv)
        return SimpleNamespace(returncode=1, stdout="", stderr="Error response from daemon: No such container: lift-slt-striim")
    monkeypatch.setattr(ownership, "_docker", docker)
    monkeypatch.setattr(ownership, "_nodes", lambda: ("lift-slt-striim",))
    monkeypatch.setattr(ownership, "make_catalog", lambda admin: admin.pg.probe())
    return SimpleNamespace(calls=calls, pg=Pg())


def _run(led, world, ctx):
    admins = _admins(world.pg)
    led.bind(admins, ctx)
    return admins


def _cleanup(led, world, ctx, admins):
    return led.cleanup(client=Client(), admins=admins, ctx=ctx, tokens={"TID": led.ident.tid})


def test_native_local_file_output_is_removed_locally_and_verified(tmp_path, no_container):
    led = _ledger(tmp_path / "state")
    out = str(tmp_path / f"{led.ident.tid}out")
    admins = _run(led, no_container, NATIVE)
    entry = led.claim_exact_file(NATIVE, out, "file-output")
    assert entry["state"] == "confirmed"
    open(out, "w").write('{"processed": true}\n')                       # the FileWriter's output
    result = _cleanup(led, no_container, NATIVE, admins)
    assert result.status == "ok", result.detail
    assert not (tmp_path / f"{led.ident.tid}out").exists()
    assert [(o["kind"], o["state"]) for o in result.owned] == [("file-output", "verified-absent")]
    assert result.verified is True and no_container.calls == []                 # never docker in native mode
    assert entry.get("host") == socket.gethostname()


def test_native_local_preexisting_exact_path_is_foreign_and_preserved(tmp_path, no_container):
    led = _ledger(tmp_path / "state")
    out = tmp_path / f"{led.ident.tid}out"
    out.write_text("someone else's\n")
    (tmp_path / f"{led.ident.tid}out.00").write_text("a rolled part\n")
    _run(led, no_container, NATIVE)
    assert led.claim_exact_file(NATIVE, str(out), "file-output") is None
    assert out.read_text() == "someone else's\n"
    assert {f["name"] for f in led.foreign} == {str(out), str(out) + ".00"}


def test_native_remote_file_output_is_a_named_gap_not_a_failure(tmp_path, no_container):
    led = _ledger(tmp_path / "state")
    admins = _run(led, no_container, REMOTE)
    entry = led.claim_exact_file(REMOTE, "/tmp/remote_out", "file-output")
    assert entry["state"] == "not-deleted"
    result = _cleanup(led, no_container, REMOTE, admins)
    assert result.status == "ok" and result.failures == []
    assert any(g.startswith("native-remote-file: file-output /tmp/remote_out") for g in result.gaps)
    assert result.verified is False and no_container.calls == []
    assert ownership.leftover_ledgers(tmp_path / "state") == []                # nothing left to reclaim


def test_replay_reclaims_a_native_ledger_written_before_the_fix(tmp_path, no_container):
    # the shape of a real leftover ledger: a confirmed native file-output, no host on the entry, a binding
    # with no docker nodes, and a cleanup that failed on `docker exec`
    led = _ledger(tmp_path / "state")
    out = tmp_path / f"{led.ident.tid}out"
    _run(led, no_container, NATIVE)
    led.add("file-output", str(out), None, "delete-failed", "rm -f on lift-slt-striim failed: No such container")
    out.write_text("left\n")
    assert len(ownership.leftover_ledgers(tmp_path / "state")) == 1
    result = ownership.replay(led.path, admins={}, client=Client(), env={})
    assert result.status == "ok", (result.detail, result.gaps)
    assert not out.exists() and no_container.calls == []
    assert [e["state"] for e in json.loads(led.path.read_text())["entries"]] == ["verified-absent"]
    assert ownership.leftover_ledgers(tmp_path / "state") == []


def test_replay_on_another_host_refuses_a_native_local_file(tmp_path, no_container, monkeypatch):
    led = _ledger(tmp_path / "state")
    out = tmp_path / f"{led.ident.tid}out"
    _run(led, no_container, NATIVE)
    led.claim_exact_file(NATIVE, str(out), "file-output")
    out.write_text("left\n")
    monkeypatch.setattr(socket, "gethostname", lambda: "some-other-host")
    result = ownership.replay(led.path, admins={}, client=Client(), env={})
    assert out.exists() and any("not this host" in g for g in result.gaps)


def test_shared_native_striim_is_recorded_external_without_a_container(tmp_path):
    cfg = SimpleNamespace(_slt_infra=infra.Infra("shared", 49080, tmp_path))
    assert infra.bind_striim(cfg, NATIVE, False, "lift-slt-striim", run=lambda a: pytest.fail("no docker")) is None
    assert cfg._slt_infra.striim == {"status": "external", "container": None, "urlPort": 49080,
                                     "boundToAllocated": False}


def test_replay_of_a_file_only_native_ledger_needs_no_striim(tmp_path, no_container, monkeypatch):
    # Replay built its Striim client up front, and StriimApi authenticates in its
    # constructor, so a ledger holding only files could not be replayed while the server was stopped
    from livetest import striim

    def refused(*a, **k):
        raise ConnectionError("HTTPConnectionPool(host='localhost', port=49080): Connection refused")
    monkeypatch.setattr(striim.StriimClient, "from_url", refused)
    led = _ledger(tmp_path / "state")
    out = tmp_path / f"{led.ident.tid}out"
    _run(led, no_container, NATIVE)
    led.add("file-output", str(out), None, "delete-failed", "rm -f on lift-slt-striim failed: No such container")
    out.write_text("left\n")
    result = ownership.replay(led.path, admins={}, env={})
    assert result.status == "ok", (result.detail, result.gaps)
    assert not out.exists() and ownership.leftover_ledgers(tmp_path / "state") == []


def test_replay_builds_the_striim_client_once_for_a_namespace_entry(tmp_path, no_container, monkeypatch):
    from livetest import striim
    made = []
    client = Client(namespaces={"SLT_lc_ns"})
    monkeypatch.setattr(striim.StriimClient, "from_url", lambda url, user, pw: made.append((url, user)) or client)
    led = _ledger(tmp_path / "state")
    _run(led, no_container, NATIVE)
    led.add("namespace", "SLT_lc_ns", None, "confirmed")
    result = ownership.replay(led.path, admins={}, env={})
    assert result.status == "ok", (result.detail, result.gaps)
    assert made == [("http://localhost:49080", "admin")] and client.dropped == ["SLT_lc_ns"]
