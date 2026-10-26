"""The ownership ledger's refusal paths: each leaves the resource in place and says why."""

import json
from types import SimpleNamespace

import pytest

from livetest import lifecycle, ownership
from livetest.ownership import Ledger, OwnershipError, Unsupported

DOCKER = SimpleNamespace(mode="docker")


@pytest.fixture
def removed(monkeypatch):
    calls = []
    monkeypatch.setattr(ownership, "docker_rm", lambda path, **k: calls.append(("docker", path, k)))
    monkeypatch.setattr(ownership, "local_rm", lambda path: calls.append(("local", path)))
    return calls


def _ledger(tmp_path, attempt="a2", entries=(), bindings=None):
    ident = SimpleNamespace(attempt=attempt, tid="t0123456789_", per_test="t0123456789", ns="SLT_x_t0123456789")
    doc = {"ledgerVersion": ownership.LEDGER_VERSION, "entries": [dict(e) for e in entries],
           "bindings": bindings or {}}
    return Ledger(tmp_path / "ledger.json", ident, doc, env={})


def _delete(ledger, entry, ctx=DOCKER):
    ledger._delete({"attempt": "a2", "db": None, **entry}, client=None, admins={}, ctx=ctx, tokens={})


@pytest.mark.parametrize("name", ["/opt/striim/../etc/x", "a b", "/opt/striim/x;rm -rf /"])
def test_unsafe_file_path_is_refused_before_any_rm(tmp_path, removed, name):
    for ctx in (DOCKER, SimpleNamespace(mode=None)):
        with pytest.raises(OwnershipError, match="unsafe server path"):
            _delete(_ledger(tmp_path), {"kind": "file-output", "name": name}, ctx)
    assert removed == []


@pytest.mark.parametrize("name", ["/opt/striim/UploadedFiles", "/opt/striim/slt-runs/../UploadedFiles"])
def test_owned_dir_outside_the_runs_root_is_never_removed(tmp_path, removed, name):
    with pytest.raises((RuntimeError, OwnershipError)):
        _delete(_ledger(tmp_path), {"kind": "owned-dir", "name": name})
    assert removed == []


def test_unknown_kind_is_refused(tmp_path, removed):
    with pytest.raises(RuntimeError, match="unknown ledger kind"):
        _delete(_ledger(tmp_path), {"kind": "volume", "name": "x"})


def test_foreign_deployment_group_is_refused(tmp_path):
    client = SimpleNamespace(deploy_tql=lambda tql: pytest.fail("dropped a foreign group"))
    with pytest.raises(OwnershipError, match="foreign group"):
        _ledger(tmp_path)._delete({"kind": "deployment-group", "name": "OtherRunDG", "attempt": "a2"},
                                  client=client, admins={}, ctx=DOCKER, tokens={})


@pytest.mark.parametrize("kind", ownership.UNSUPPORTED_KINDS)
def test_unsupported_kinds_are_recorded_not_deleted(tmp_path, removed, kind):
    ledger = _ledger(tmp_path, entries=[{"kind": kind, "name": "n", "db": None, "state": "confirmed",
                                         "attempt": "a2"}])
    with pytest.raises(Unsupported):
        _delete(ledger, {"kind": kind, "name": "n"})
    result = ownership.CleanupResult("ok")
    ledger._delete_all(ledger.entries, result, lambda e: {"client": None, "admins": {}, "ctx": DOCKER}, {})
    assert ledger.entries[0]["state"] == "not-deleted" and result.failures == []
    assert any("never deleted" in g for g in ledger.gaps) and removed == []


def test_prior_attempt_kafka_topic_is_not_deleted_on_reset(tmp_path):
    routes = {"routes": {"kafka": {"host": "k", "port": 9092}}}
    deleted = []
    admin = SimpleNamespace(delete_topic=lambda n: deleted.append(n))
    ledger = _ledger(tmp_path, attempt="a2", bindings={"a1": routes},
                     entries=[{"kind": "kafka-topic", "name": "slt_t1_src", "db": "kafka", "state": "confirmed",
                               "attempt": "a1"}])
    ledger.current = routes             # identical routes: still not proof of the same backend
    assert ledger.reset(client=None, admins={"kafka": {"admin": admin}}, ctx=DOCKER) == []
    assert deleted == []
    assert any("backend incarnation unverified" in g for g in ledger.gaps)
    assert any("backend incarnation unverified" in f["note"] for f in ledger.foreign)


def test_target_problems_refuse_unbound_or_unrecorded_targets(tmp_path):
    ledger = _ledger(tmp_path, bindings={"a1": {"routes": {}, "docker": {"nodes": {}}}})
    pg = {"kind": "pg-table", "name": "t", "db": "postgres-source", "attempt": "a1"}
    assert "not bound on both sides" in ledger.target_problem(pg, {"routes": {}})
    assert "recorded no target binding" in ledger.target_problem({**pg, "attempt": "a0"}, {})
    f = {"kind": "file-output", "name": "/opt/striim/x", "attempt": "a1"}
    assert ledger.target_problem(f, {"docker": {"nodes": {"n": "id"}}}) == "no docker nodes were recorded"
    remote = _ledger(tmp_path, bindings={"a1": {"striim": {"mode": "native", "url": "http://striim.example:9080"}}})
    assert "native-remote-file" in remote._native_problem(f)


def test_cleanup_deadline_marks_the_rest_delete_failed(tmp_path, removed):
    ledger = _ledger(tmp_path, entries=[{"kind": "file-output", "name": "/opt/striim/x", "db": None,
                                         "state": "confirmed", "attempt": "a2"}])
    result = ownership.CleanupResult("ok")
    ledger._delete_all(ledger.entries, result, lambda e: {"client": None, "admins": {}, "ctx": DOCKER}, {},
                       deadline=lifecycle.Deadline(0))
    assert ledger.entries[0]["state"] == "delete-failed"
    assert result.failures == [{"kind": "file-output", "name": "/opt/striim/x",
                                "error": "cleanup-deadline: not attempted"}]
    assert removed == []


def test_unverifiable_delete_is_a_gap(tmp_path):
    ledger = _ledger(tmp_path, entries=[{"kind": "server-file", "name": "/opt/striim/x", "db": None,
                                         "state": "deleted", "attempt": "a2"}])
    result = ownership.CleanupResult("ok")
    ledger._verify_all(ledger.entries, result, lambda e: {"client": None, "admins": {},
                                                          "ctx": SimpleNamespace(mode="native")})
    assert ledger.entries[0]["state"] == "deleted"
    assert any("no catalog read" in g for g in ledger.gaps)


def test_replay_refuses_another_ledger_version(tmp_path):
    p = tmp_path / "l.json"
    p.write_text(json.dumps({"ledgerVersion": ownership.LEDGER_VERSION - 1, "entries": []}))
    with pytest.raises(OwnershipError, match="ledger-version-mismatch"):
        ownership.replay(p)


@pytest.mark.parametrize("status,rc", [("ok", 0), ("failed", 1), ("skipped", 1)])
def test_replay_cli_exit_code(monkeypatch, capsys, status, rc):
    monkeypatch.setattr(ownership, "replay", lambda path: ownership.CleanupResult(status, ledger_path=path))
    assert ownership.main(["replay", "/x/l.json"]) == rc
    assert json.loads(capsys.readouterr().out)
