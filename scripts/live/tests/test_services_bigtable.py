"""Hermetic, no-Docker self-test for the LIVE bigtable service definition.

Deliverable 8a / BIGTABLE_EMULATOR_SERVICE_PLAN.md §9.1 ("a bigtable service parses and
provides the expected BIGTABLE_* tokens on each tier"), the live-half. Mirrors
scripts/live/tests/test_spanner_service.py for the CBTEMULATOR service: it parses
services/bigtable/service.yaml off disk through the real loader the harness uses
(livetest.registry.load_service) and renders its `provides:` the way the tier resolves it
(livetest.services.resolve + livetest.plugin.build_service_tokens). No Docker, no running
CBTEMULATOR, and no auth key -- the emulator is reached via BIGTABLE_EMULATOR_HOST only.

The guard is the same shape spanner's carries: the service must (1) PARSE under the real
registry, (2) publish EXACTLY the BIGTABLE_* tokens the rest of the tier consumes, and (3)
render those templates to the expected concrete values -- including the gRPC-port override
the emulator and the admin client that dials it must follow together (the "the container
moves, the client does not" hazard service.yaml's docker_env is written to close).

§9.2 adds the compose-rendering half, in the same hermetic style: the bigtable compose.yaml
renders its name:, container_name: and published port correctly with and without
SLT_STACK_PREFIX, and the Striim `compose.bigtable-emulator.yaml` overlay injects
BIGTABLE_EMULATOR_HOST on slt-striim/slt-node/slt-agent at the published port -- while
staying out of the base compose so a plain `docker compose up` still targets real Bigtable.
"""
from pathlib import Path

import yaml
from livetest.registry import load_service
from livetest.services import declared_containers, resolve
from livetest.plugin import build_service_tokens

# The exact `provides:` block service.yaml advertises for the framework's own admin and for
# the Striim app. BIGTABLE_URL is the static JDBC URL: the emulator HOST is not in the URL,
# it comes from BIGTABLE_EMULATOR_HOST (no fake GCP key -- contrast Spanner's autoConfig).
EXPECTED_PROVIDES = {
    "BIGTABLE_PROJECT": "{project}",
    "BIGTABLE_INSTANCE": "{instance}",
    "BIGTABLE_URL": "jdbc:bigtable:/projects/{project}/instances/{instance}?app_profile_id=default",
    "BIGTABLE_HOST": "{host}",
    "BIGTABLE_GRPC_PORT": "{port}",
    "BIGTABLE_EMULATOR_HOST": "{view_host}:{port}",
}


def test_bigtable_service_def_loads():
    # (1) parse under the real registry, + the shape the live tier relies on for it.
    defn = load_service("bigtable")
    assert defn.isolation == "none"
    assert defn.container == "slt-bigtable"
    assert defn.live_override_env == "SLT_BIGTABLE_HOST"
    assert defn.opt_in_env == "SLT_BIGTABLE_SERVICE"
    assert defn.docker_defaults == {"host": "localhost", "port": 9470,
                                    "project": "test-project", "instance": "test-instance"}
    assert defn.docker_env == {"port": "SLT_BIGTABLE_GRPC_HOST_PORT"}
    assert defn.provides == EXPECTED_PROVIDES


def test_bigtable_provide_tokens_render_docker_defaults():
    # (2)+(3) the tokens exist AND resolve to the concrete values a test/strim app expects on
    # the emulator's default published port.
    defn = load_service("bigtable")
    r = resolve("bigtable", env={}, started=set(), compose_up=lambda defn: None)
    assert r.mode == "docker"
    tokens = build_service_tokens(defn, r, schema="")
    assert tokens["BIGTABLE_PROJECT"] == "test-project"
    assert tokens["BIGTABLE_INSTANCE"] == "test-instance"
    assert tokens["BIGTABLE_HOST"] == "localhost"
    assert tokens["BIGTABLE_GRPC_PORT"] == "9470"
    assert tokens["BIGTABLE_EMULATOR_HOST"] == "localhost:9470"
    assert tokens["BIGTABLE_URL"] == (
        "jdbc:bigtable:/projects/test-project/instances/test-instance?app_profile_id=default")


def test_bigtable_grpc_host_port_override_moves_the_emulator_and_client():
    # SLT_BIGTABLE_GRPC_HOST_PORT remaps a busy published host port AND must move the admin
    # client that dials it (service.yaml's docker_env) -- the very hazard test_spanner_service.py
    # guards; a remap that leaves the client on the old port is the silent-driver failure.
    env = {"SLT_BIGTABLE_GRPC_HOST_PORT": "9110"}
    defn = load_service("bigtable")
    r = resolve("bigtable", env=env, started=set(), compose_up=lambda defn: None)
    assert r.base["port"] == "9110"
    tokens = build_service_tokens(defn, r, schema="")
    assert tokens["BIGTABLE_GRPC_PORT"] == "9110"
    assert tokens["BIGTABLE_EMULATOR_HOST"] == "localhost:9110"


def test_bigtable_emulator_host_carries_the_containerised_view_host():
    # A containerised Striim reaches the emulator through the published host port on the host
    # gateway (host.docker.internal -- the SLT_STRIIM_VIEW_HOST path in resolve); the
    # BIGTABLE_EMULATOR_HOST token the cluster overlay sets must carry that view_host.
    defn = load_service("bigtable")
    r = resolve("bigtable", env={"SLT_STRIIM_VIEW_HOST": "host.docker.internal"},
                started=set(), compose_up=lambda defn: None)
    tokens = build_service_tokens(defn, r, schema="")
    assert tokens["BIGTABLE_EMULATOR_HOST"] == "host.docker.internal:9470"
    assert tokens["BIGTABLE_URL"].startswith(
        "jdbc:bigtable:/projects/test-project/instances/test-instance")


# --- §9.2 compose rendering: name / container_name / published port + the Striim overlay ------
# No Docker, no CBTEMULATOR: render the compose files' own text the way the harness does --
# via the framework's own declared_containers/stack (live reads SLT_STACK_PREFIX) and a YAML
# parse of the overlay. The claim in each test is the one §9.2 names: the rendered names/ports
# and the overlay's per-container injection are *correct*.

_LIVE_COMPOSE = Path(__file__).resolve().parents[1] / "services" / "bigtable" / "compose.yaml"
_LIVE_BASE_STRIIM = Path(__file__).resolve().parents[1] / "services" / "striim" / "compose.yaml"
_LIVE_OVERLAY = (Path(__file__).resolve().parents[1]
                 / "services" / "striim" / "compose.bigtable-emulator.yaml")

# The canonical stack-prefix expression every live compose names its project + container with.
_LIVE_PREFIX_TPL = "${SLT_STACK_PREFIX:+${SLT_STACK_PREFIX}-}"


def test_live_compose_container_name_renders_with_and_without_stack_prefix():
    # container_name carries the canonical SLT_STACK_PREFIX expression; the framework strips the
    # interpolation and re-derives the name via stack.prefixed, so both the bare default and a
    # prefixed stack must render -- and an override must not leak a literal prefix into the bare.
    assert declared_containers([_LIVE_COMPOSE], {}) == ["slt-bigtable"]
    assert declared_containers([_LIVE_COMPOSE], {"SLT_STACK_PREFIX": "ec"}) == ["ec-slt-bigtable"]


def test_live_compose_project_and_container_names_carry_the_stack_prefix():
    # The compose `name:` (which scopes `docker compose down`) and the container_name must carry
    # the SAME prefix expression: a prefix that hits one but not the other orphaned the other on
    # teardown -- the silent-driver failure stack-scoping exists to prevent.
    data = yaml.safe_load(_LIVE_COMPOSE.read_text())
    assert data["name"] == f"{_LIVE_PREFIX_TPL}slt-bigtable"
    assert data["services"]["slt-bigtable"]["container_name"] == f"{_LIVE_PREFIX_TPL}slt-bigtable"


def test_live_compose_published_port_is_the_overridable_host_port():
    # The published host port IS the container port, both bound to the same overridable
    # SLT_BIGTABLE_GRPC_HOST_PORT with the 9470 default -- so remapping a busy port moves where
    # the emulator publishes (the host BIGTABLE_EMULATOR_HOST then points at), not just source.
    data = yaml.safe_load(_LIVE_COMPOSE.read_text())
    ports = data["services"]["slt-bigtable"]["ports"]
    assert ports == ["${SLT_BIGTABLE_GRPC_HOST_PORT:-9470}:${SLT_BIGTABLE_GRPC_HOST_PORT:-9470}"]


def test_live_striim_overlay_injects_emulator_host_on_all_three_containers():
    # A BigtableWriter/reader in the cluster reaches the emulator only through the JVM-global
    # BIGTABLE_EMULATOR_HOST, so the overlay must reach slt-striim, slt-node AND slt-agent at the
    # published host port -- and must stay OUT of the base compose (a plain `up` then still
    # targets real Bigtable, never this empty emulator).
    overlay = yaml.safe_load(_LIVE_OVERLAY.read_text())
    expected = "host.docker.internal:${SLT_BIGTABLE_GRPC_HOST_PORT:-9470}"
    for svc in ("slt-striim", "slt-node", "slt-agent"):
        assert overlay["services"][svc]["environment"]["BIGTABLE_EMULATOR_HOST"] == expected
    assert "BIGTABLE_EMULATOR_HOST" not in _LIVE_BASE_STRIIM.read_text()


def test_bigtable_service_names_its_python_module(monkeypatch):
    # Without google-cloud-bigtable a bigtable test must skip with the install hint, before the
    # emulator is provisioned, rather than fail at the first admin call.
    from livetest import registry
    defn = load_service("bigtable")
    assert defn.python_module == "google.cloud.bigtable"
    assert "google-cloud-bigtable" in defn.python_module_hint
    real = registry.importlib.util.find_spec
    monkeypatch.setattr(registry.importlib.util, "find_spec",
                        lambda name, *a: None if name == defn.python_module else real(name, *a))
    assert registry.unavailable(defn, {}) == (
        f"Python module google.cloud.bigtable is not installed: {defn.python_module_hint}")


def _healthcheck_argv(env):
    # What Docker runs for the healthcheck, after compose interpolation: CMD-SHELL hands its one
    # string to /bin/sh -c; CMD runs its arguments directly.
    import re
    kind, *args = yaml.safe_load(_LIVE_COMPOSE.read_text())["services"]["slt-bigtable"]["healthcheck"]["test"]
    expand = lambda s: re.sub(r"\$\{(\w+):-([^}]*)\}", lambda m: env.get(m[1], m[2]), s)
    return ["/bin/sh", "-c", expand(args[0])] if kind == "CMD-SHELL" else [expand(a) for a in args]


def test_live_healthcheck_runs_its_tcp_probe_under_bash():
    # The emulator image is Debian-based, so its sh is dash, which has no /dev/tcp.
    argv = _healthcheck_argv({})
    assert Path(argv[0]).name == "bash"
    assert "sh -c" not in " ".join(argv[1:])


def test_live_healthcheck_passes_on_a_listener_and_fails_without_one_when_sh_is_dash(tmp_path):
    import os
    import shutil
    import socket
    import subprocess
    import pytest
    dash, bash = shutil.which("dash"), shutil.which("bash")
    if not (dash and bash):
        pytest.skip("needs dash and bash on this host")
    shells = tmp_path / "bin"
    shells.mkdir()
    (shells / "sh").symlink_to(dash)
    env = {"PATH": f"{shells}{os.pathsep}{os.environ['PATH']}"}
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        argv = _healthcheck_argv({"SLT_BIGTABLE_GRPC_HOST_PORT": str(listener.getsockname()[1])})
        argv[0] = str(shells / "sh") if argv[0] == "/bin/sh" else argv[0]
        assert subprocess.run(argv, env=env, timeout=10).returncode == 0
    assert subprocess.run(argv, env=env, timeout=10).returncode != 0
