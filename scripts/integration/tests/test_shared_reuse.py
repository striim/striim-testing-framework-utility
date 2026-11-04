"""Under SLT_INFRA_OWNERSHIP=shared a running shared service is reused, never recreated.

A lane worktree resolves its own copy of a service's compose.yaml; `compose up` from there sees
different bind-mount paths and recreates the shared container (int-teradata, 2026-11-03). Docker
is mocked throughout: no container is inspected, started or recreated for real.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from inttest import services

COMPOSE = ("name: int-teradata\nservices:\n  int-teradata:\n"
           "    container_name: ${INT_STACK_PREFIX:+${INT_STACK_PREFIX}-}int-teradata\n")
SIDECAR = COMPOSE + "  int-teradata-side:\n    container_name: int-teradata-side\n"
MAIN = "/checkouts/main/services/integration/teradata"


@pytest.fixture
def lane(tmp_path, monkeypatch):
    """A lane's services root holding its own teradata compose file, ownership shared."""
    root = tmp_path / "services"
    (root / "teradata").mkdir(parents=True)
    (root / "teradata" / "compose.yaml").write_text(COMPOSE)
    monkeypatch.setenv("SLT_INT_SERVICES_DIR", str(root))
    monkeypatch.setenv("SLT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SLT_INFRA_OWNERSHIP", "shared")
    return root / "teradata"


def _info(running=True, health="healthy", working_dir=MAIN):
    state = {"Running": running, "Status": "running" if running else "exited"}
    if health:
        state["Health"] = {"Status": health}
    return {"State": state,
            "Config": {"Labels": {"com.docker.compose.project.working_dir": working_dir}}}


def _docker(containers, calls=None):
    """A fake subprocess.run: `docker container inspect` answers from `containers`; every
    command is recorded."""
    calls = [] if calls is None else calls

    def run(argv, **kw):
        calls.append(list(argv))
        if argv[:3] == ["docker", "container", "inspect"]:
            info = containers.get(argv[3])
            if info is None:
                return SimpleNamespace(returncode=1, stdout="", stderr="No such container")
            return SimpleNamespace(returncode=0, stdout=json.dumps([info]), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return run, calls


def test_not_shared_never_inspects(lane, monkeypatch):
    monkeypatch.setenv("SLT_INFRA_OWNERSHIP", "exclusive")
    run, calls = _docker({"int-teradata": _info()})
    assert services.shared_reuse("teradata", run=run) is False
    assert calls == []


def test_shared_read_from_dotenv(monkeypatch):
    from inttest import paths
    monkeypatch.delenv("SLT_INFRA_OWNERSHIP", raising=False)
    monkeypatch.setattr(paths, "dotenv_values", lambda env=None: {"SLT_INFRA_OWNERSHIP": "shared"})
    assert services.shared_ownership() is True


def test_running_healthy_container_from_another_checkout_is_reused(lane):
    run, _ = _docker({"int-teradata": _info(working_dir=MAIN)})
    assert services.shared_reuse("teradata", run=run) is True


def test_running_container_without_healthcheck_is_reused(lane):
    run, _ = _docker({"int-teradata": _info(health="")})
    assert services.shared_reuse("teradata", run=run) is True


def test_compose_up_runs_no_compose_command_for_a_reused_service(lane, monkeypatch):
    run, calls = _docker({"int-teradata": _info()})
    monkeypatch.setattr(services.subprocess, "run", run)
    services.compose_up("teradata", MagicMock())
    assert [c for c in calls if c[:2] == ["docker", "compose"]] == []
    assert calls == [["docker", "container", "inspect", "int-teradata"]]


class _Clock:
    """A fake monotonic clock that sleep() advances."""
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def _hc(interval=10, timeout=30, retries=180, start=0):
    return {"Test": ["CMD", "true"], "Interval": int(interval * 1e9), "Timeout": int(timeout * 1e9),
            "Retries": retries, "StartPeriod": int(start * 1e9)}


def test_starting_container_is_waited_on_not_recreated(lane):
    states = iter([_info(health="starting"), _info(health="starting"), _info()])
    clock = _Clock()

    def staged(argv, **kw):
        return SimpleNamespace(returncode=0, stdout=json.dumps([next(states)]), stderr="")
    assert services.shared_reuse("teradata", run=staged, sleep=clock.sleep, clock=clock) is True
    assert len(clock.sleeps) == 2


def test_health_budget_comes_from_the_containers_healthcheck():
    # teradata: 180 x (10 s + 30 s) = 7200 s, longer than any fixed cap would allow.
    assert services.health_budget_s({"Config": {"Healthcheck": _hc()}}) == 7200
    assert services.health_budget_s({"Config": {"Healthcheck": _hc(5, 5, 3, start=60)}}) == \
        services.SHARED_HEALTH_FLOOR_S
    # docker's defaults, then the floor
    assert services.health_budget_s({"Config": {}}) == services.SHARED_HEALTH_FLOOR_S


def test_starting_container_is_waited_on_for_its_healthchecks_budget(lane):
    info = _info(health="starting")
    info["Config"]["Healthcheck"] = _hc()
    run, _ = _docker({"int-teradata": info})
    clock = _Clock()
    with pytest.raises(services.SharedServiceError, match="not healthy after 7200s"):
        services.shared_reuse("teradata", run=run, sleep=clock.sleep, clock=clock)
    assert clock.t == 7200


def test_the_wait_counts_wall_clock_time_not_sleeps(lane):
    # a slow inspect uses the budget too
    info = _info(health="starting")
    clock = _Clock()

    def slow(argv, **kw):
        clock.t += 400
        return SimpleNamespace(returncode=0, stdout=json.dumps([info]), stderr="")
    with pytest.raises(services.SharedServiceError, match="not healthy after"):
        services.shared_reuse("teradata", run=slow, sleep=clock.sleep, clock=clock)
    assert len(clock.sleeps) == 1          # 400 s, sleep 10, 810 s >= 600 s floor


def test_unhealthy_container_is_refused_naming_the_owner_and_the_fix(lane, monkeypatch):
    run, calls = _docker({"int-teradata": _info(health="unhealthy")})
    monkeypatch.setattr(services.subprocess, "run", run)
    with pytest.raises(services.SharedServiceError, match="unhealthy") as e:
        services.compose_up("teradata", MagicMock())
    assert f"Owned by {MAIN}" in str(e.value)
    assert "python -m inttest.cli stop teradata" in str(e.value)
    assert [c for c in calls if c[:2] == ["docker", "compose"]] == []


@pytest.mark.parametrize("flag", ["Restarting", "Paused"])
def test_restarting_or_paused_container_is_not_reused(lane, flag):
    info = _info()                       # State.Running is true for both
    info["State"][flag] = True
    run, _ = _docker({"int-teradata": info})
    with pytest.raises(services.SharedServiceError, match=f"{flag.lower()}") as e:
        services.shared_reuse("teradata", run=run)
    assert "python -m inttest.cli stop teradata" in str(e.value)


def test_stopped_container_of_another_checkout_is_refused(lane):
    run, _ = _docker({"int-teradata": _info(running=False, health="", working_dir=MAIN)})
    with pytest.raises(services.SharedServiceError, match="another checkout") as e:
        services.shared_reuse("teradata", run=run)
    assert f"Owned by {MAIN}" in str(e.value)


def test_stopped_container_of_this_checkout_is_brought_up(lane):
    run, _ = _docker({"int-teradata": _info(running=False, health="", working_dir=str(lane))})
    assert services.shared_reuse("teradata", run=run) is False


def test_absent_container_is_brought_up(lane):
    run, _ = _docker({})
    assert services.shared_reuse("teradata", run=run) is False


def test_partly_running_service_of_another_checkout_is_refused(lane):
    (lane / "compose.yaml").write_text(SIDECAR)
    run, _ = _docker({"int-teradata": _info()})          # the sidecar is gone
    with pytest.raises(services.SharedServiceError, match="int-teradata"):
        services.shared_reuse("teradata", run=run)


def test_stack_prefix_names_the_inspected_container(lane, monkeypatch):
    monkeypatch.setenv("INT_STACK_PREFIX", "ec")
    run, calls = _docker({"ec-int-teradata": _info()})
    assert services.shared_reuse("teradata", run=run) is True
    assert calls == [["docker", "container", "inspect", "ec-int-teradata"]]


def test_stack_prefix_set_only_in_dotenv_names_the_inspected_container(lane, monkeypatch):
    from inttest import paths
    monkeypatch.delenv("INT_STACK_PREFIX", raising=False)
    monkeypatch.setattr(paths, "dotenv_values", lambda env=None: {"INT_STACK_PREFIX": "ec"})
    run, calls = _docker({"ec-int-teradata": _info()})
    assert services.shared_reuse("teradata", run=run) is True
    assert calls == [["docker", "container", "inspect", "ec-int-teradata"]]


# --- the YAML requires: path -------------------------------------------------------------

def _mock_docker(reuse):
    docker = MagicMock()
    docker.SUPPORTED_SERVICES = {"teradata", "postgres"}
    docker.SharedServiceError = services.SharedServiceError
    if isinstance(reuse, Exception):
        docker.shared_reuse.side_effect = reuse
    else:
        docker.shared_reuse.side_effect = lambda svc: reuse.get(svc, False)
    docker.unavailable.return_value = None
    docker.host_unavailable.return_value = None
    docker._service_spec.return_value = {}
    return docker


def test_provision_requires_skips_hook_check_and_up_for_a_reused_service(monkeypatch):
    from inttest import plugin
    docker = _mock_docker({"teradata": True})
    monkeypatch.setattr(plugin, "_docker_mod", docker)
    monkeypatch.setattr(plugin, "_require_service_opt_in", lambda svc: None)
    monkeypatch.setattr(plugin, "_load_service_yaml", lambda svc: {"compose": "compose.yaml"})
    plugin._provision_requires("case-x", ["teradata", "postgres"], lock=MagicMock())
    assert [c.args[0] for c in docker.run_pre_up.call_args_list] == ["postgres"]
    assert [c.args[0] for c in docker.unavailable.call_args_list] == ["postgres"]
    assert [c.args[0] for c in docker.ensure_up.call_args_list] == ["postgres"]


def test_provision_requires_fails_on_a_shared_service_it_may_not_recreate(monkeypatch):
    from inttest import plugin
    docker = _mock_docker(services.SharedServiceError("teradata: unhealthy"))
    monkeypatch.setattr(plugin, "_docker_mod", docker)
    monkeypatch.setattr(plugin, "_require_service_opt_in", lambda svc: None)
    with pytest.raises(pytest.fail.Exception, match="case-x: teradata: unhealthy"):
        plugin._provision_requires("case-x", ["teradata"], lock=MagicMock())
    docker.ensure_up.assert_not_called()


@pytest.mark.parametrize("policy", ["fail", "skip"])
def test_a_reused_service_still_needs_its_python_module_here(monkeypatch, policy):
    from inttest import plugin
    docker = _mock_docker({"teradata": True})
    docker.host_unavailable.return_value = "python module 'teradatasql' is not installed"
    docker._service_spec.return_value = {"unavailable_policy": policy}
    monkeypatch.setattr(plugin, "_docker_mod", docker)
    monkeypatch.setattr(plugin, "_require_service_opt_in", lambda svc: None)
    raised = pytest.fail.Exception if policy == "fail" else pytest.skip.Exception
    with pytest.raises(raised, match="teradatasql"):
        plugin._provision_requires("case-x", ["teradata"], lock=MagicMock())
    docker.run_pre_up.assert_not_called()
    docker.unavailable.assert_not_called()           # the required-files check is skipped
    docker.ensure_up.assert_not_called()


def test_host_unavailable_reports_only_the_python_module(monkeypatch):
    from livetest import registry
    monkeypatch.setattr(services, "_service_spec", lambda svc: {"python_module": "teradatasql"})
    monkeypatch.setattr(registry, "missing_python_module", lambda mod, hint: f"no {mod}")
    monkeypatch.setattr(services, "required_files", lambda svc: pytest.fail("required files read"))
    assert services.host_unavailable("teradata") == "no teradatasql"


def test_fixture_path_fails_on_a_shared_service_it_may_not_recreate(monkeypatch, tmp_path):
    from inttest import plugin, resources
    docker = MagicMock()
    docker.SharedServiceError = services.SharedServiceError
    docker.is_up.return_value = True
    docker.compose_up.side_effect = services.SharedServiceError("teradata: unhealthy; Owned by X")
    compose = tmp_path / "compose.yaml"
    compose.write_text("services: {}\n")
    monkeypatch.setattr(plugin, "_docker_mod", docker)
    monkeypatch.setattr(plugin, "_service_file", lambda name, f: compose)
    monkeypatch.setattr(resources, "select_profile", lambda name: None)
    monkeypatch.setattr(plugin, "_read_started", lambda: pytest.fail("registry read"))
    with pytest.raises(pytest.fail.Exception, match="teradata: unhealthy; Owned by X"):
        plugin._ensure_up("teradata", MagicMock())
