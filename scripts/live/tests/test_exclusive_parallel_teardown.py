"""An exclusive --parallel run tears its stack down: workers report what they brought up and the
controller, which runs no case, tears it down at session end."""

from types import SimpleNamespace

import pytest

from livetest import plugin as P


@pytest.fixture
def calls(monkeypatch):
    seen = []
    monkeypatch.setattr(P._sp, "cluster_down", lambda *a, **k: seen.append(("cluster_down", a[1])))
    monkeypatch.setattr(P, "compose_down", lambda defn: seen.append(("compose_down", defn.container)))
    monkeypatch.setattr(P, "forget_provisioned", lambda *a, **k: None)
    monkeypatch.setattr(P._slt_evidence, "fold_evidence_errors", lambda *a, **k: None)
    monkeypatch.setattr(P._slt_evidence, "finalize_session", lambda *a, **k: None)
    monkeypatch.setattr(P, "_slt_write_sidecar", lambda c: None)
    monkeypatch.setattr(P, "_slt_infra_guard", lambda s, e: e)
    monkeypatch.setattr(P, "load_service", lambda name: _defn(name))
    for var in ("SLT_KEEP_SERVICES", "PYTEST_XDIST_WORKER", "SLT_KEEP_RESOURCES", "SLT_KEEP_RESOURCES_ON_ERROR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SLT_STACK_PREFIX", "xabc")
    return seen


def _defn(name):
    return SimpleNamespace(name=name, container=f"xabc-slt-{name}", compose="compose.yaml")


def _session(cfg):
    return SimpleNamespace(config=cfg, exitstatus=0)


def _controller(ownership="exclusive"):
    return SimpleNamespace(_slt_infra=SimpleNamespace(ownership=ownership, teardown=None))


def _worker(monkeypatch, calls, **extra):
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    cfg = SimpleNamespace(workeroutput={}, _slt_striim_provisioned=True, _slt_started={"xabc-slt-postgres"},
                          _slt_defs={"postgres": _defn("postgres"), "kafka": _defn("kafka")},
                          _slt_release={"STRIIM_VERSION": "5.2.0"}, **extra)
    P.pytest_sessionfinish(_session(cfg), 0)
    monkeypatch.delenv("PYTEST_XDIST_WORKER")
    assert calls == []                  # a worker never tears down
    return cfg.workeroutput


def test_controller_tears_down_what_its_workers_brought_up(monkeypatch, calls):
    out = _worker(monkeypatch, calls)
    assert out["slt_teardown"]["services"] == ["postgres"]
    controller = _controller()
    monkeypatch.setattr(P.subprocess, "run", lambda *a, **k: pytest.fail("every worker reported; no prefix scan"))
    P.pytest_testnodeready(SimpleNamespace(config=controller))
    P.pytest_testnodedown(SimpleNamespace(config=controller, workeroutput=out), None)
    P.pytest_sessionfinish(_session(controller), 0)
    assert calls == [("compose_down", "xabc-slt-postgres"), ("cluster_down", {"STRIIM_VERSION": "5.2.0"})]


def test_worker_keep_request_keeps_the_stack(monkeypatch, calls):
    out = _worker(monkeypatch, calls, _slt_keep_resources=True)
    controller = _controller()
    P.pytest_testnodedown(SimpleNamespace(config=controller, workeroutput=out), None)
    P.pytest_sessionfinish(_session(controller), 0)
    assert calls == []


def test_shared_controller_adopts_nothing(monkeypatch, calls):
    out = _worker(monkeypatch, calls)
    controller = _controller("shared")
    P.pytest_testnodedown(SimpleNamespace(config=controller, workeroutput=out), None)
    P.pytest_sessionfinish(_session(controller), 0)
    assert calls == []


def test_crashed_worker_tears_down_by_stack_identity(monkeypatch, calls):
    import livetest.registry as registry
    monkeypatch.setattr(registry, "all_services", lambda: ["kafka", "postgres"])
    names = "xabc-slt-striim\nxabc-slt-kafka\n"
    monkeypatch.setattr(P.subprocess, "run", lambda argv, **k: SimpleNamespace(returncode=0, stdout=names, stderr=""))
    controller = _controller()
    P.pytest_testnodedown(SimpleNamespace(config=controller), RuntimeError("worker crashed"))
    P.pytest_sessionfinish(_session(controller), 0)
    assert calls == [("compose_down", "xabc-slt-kafka"), ("cluster_down", None)]


def test_crashed_worker_with_docker_unanswering_is_a_failed_teardown(monkeypatch, calls):
    monkeypatch.setattr(P.subprocess, "run", lambda argv, **k: SimpleNamespace(returncode=1, stdout="", stderr="no daemon"))
    controller = _controller()
    P.pytest_testnodedown(SimpleNamespace(config=controller), RuntimeError("worker crashed"))
    P.pytest_sessionfinish(_session(controller), 0)
    assert calls == []
    assert "python -m livetest.cli stop all" in controller._slt_teardown_failures[0]["error"]


@pytest.fixture
def stack_on_docker(monkeypatch):
    import livetest.registry as registry
    monkeypatch.setattr(registry, "all_services", lambda: ["kafka", "postgres"])
    names = "xabc-slt-striim\nxabc-slt-kafka\n"
    monkeypatch.setattr(P.subprocess, "run", lambda argv, **k: SimpleNamespace(returncode=0, stdout=names, stderr=""))


def test_interrupted_controller_tears_down_by_stack_identity(calls, stack_on_docker):
    # Ctrl-C: xdist stops before any worker report arrives, so no testnodedown ever fires.
    controller = _controller()
    for _ in range(2):
        P.pytest_testnodeready(SimpleNamespace(config=controller))
    P.pytest_sessionfinish(_session(controller), pytest.ExitCode.INTERRUPTED)
    assert calls == [("compose_down", "xabc-slt-kafka"), ("cluster_down", None)]


def test_a_worker_that_never_reported_triggers_the_identity_teardown(monkeypatch, calls, stack_on_docker):
    out = _worker(monkeypatch, calls)
    controller = _controller()
    for _ in range(2):
        P.pytest_testnodeready(SimpleNamespace(config=controller))
    P.pytest_testnodedown(SimpleNamespace(config=controller, workeroutput=out), None)
    P.pytest_sessionfinish(_session(controller), 1)
    assert ("compose_down", "xabc-slt-kafka") in calls and ("compose_down", "xabc-slt-postgres") in calls
    assert ("cluster_down", {"STRIIM_VERSION": "5.2.0"}) in calls


@pytest.mark.parametrize("keep", ["SLT_KEEP_RESOURCES", "SLT_KEEP_RESOURCES_ON_ERROR"])
@pytest.mark.parametrize("how", ["crashed", "interrupted"])
def test_keep_settings_keep_an_unaccounted_stack(monkeypatch, calls, stack_on_docker, capsys, keep, how):
    monkeypatch.setenv(keep, "1")
    controller = _controller()
    P.pytest_testnodeready(SimpleNamespace(config=controller))
    if how == "crashed":
        P.pytest_testnodedown(SimpleNamespace(config=controller), RuntimeError("worker crashed"))
    P.pytest_sessionfinish(_session(controller), pytest.ExitCode.INTERRUPTED if how == "interrupted" else 1)
    assert calls == []
    assert "SLT_STACK_PREFIX=xabc python -m livetest.cli stop all" in capsys.readouterr().out


def test_interrupted_shared_controller_adopts_nothing(calls, stack_on_docker):
    controller = _controller("shared")
    P.pytest_testnodeready(SimpleNamespace(config=controller))
    P.pytest_sessionfinish(_session(controller), pytest.ExitCode.INTERRUPTED)
    assert calls == []
