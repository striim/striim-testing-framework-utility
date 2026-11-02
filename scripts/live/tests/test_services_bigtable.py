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
"""
from livetest.registry import load_service
from livetest.services import resolve
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
