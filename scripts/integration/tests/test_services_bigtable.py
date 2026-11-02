"""Hermetic, no-Docker self-test for the INTEGRATION bigtable service definition.

Deliverable 8a / BIGTABLE_EMULATOR_SERVICE_PLAN.md §9.1 ("a bigtable service parses and
provides the expected BIGTABLE_* tokens on each tier"), the integration-half. Mirror of
scripts/live/tests/test_services_bigtable.py: parse services/bigtable/service.yaml through
inttest's own loader (inttest.tokens._load_service_def / service_tokens) and assert the
service publishes EXACTLY the BIGTABLE_* tokens the integration tier resolves -- no Docker.
Same guard shape scripts/integration/tests/test_services.py carries for spanner.
"""
from pathlib import Path

from inttest import tokens as tokens_mod

# Same origin the module-under-test reads service.yaml from (scripts/integration/services).
_SERVICES_DIR = Path(__file__).resolve().parents[1] / "services"

# The exact `provides:` block service.yaml advertises. On the integration tier the emulator
# lives on its own host port (19470, distinct from the live tier's 9470) so both stacks can
# run at once -- the CBTEMULATOR is one gRPC listener, no REST/admin split.
EXPECTED_PROVIDES = {
    "BIGTABLE_PROJECT": "{project}",
    "BIGTABLE_INSTANCE": "{instance}",
    "BIGTABLE_URL": "jdbc:bigtable:/projects/{project}/instances/{instance}?app_profile_id=default",
    "BIGTABLE_HOST": "{host}",
    "BIGTABLE_GRPC_PORT": "{port}",
    "BIGTABLE_EMULATOR_HOST": "{view_host}:{port}",
}


def test_bigtable_service_def_parses_and_provides_tokens():
    # (1) parse through inttest's own loader + (2) the shape the integration tier relies on.
    raw = tokens_mod._load_service_def("bigtable", _SERVICES_DIR)
    assert raw["name"] == "bigtable"
    assert raw["isolation"] == "none"
    assert raw["container"] == "int-bigtable"
    assert raw["live_override_env"] == "INT_BIGTABLE_HOST"
    assert raw["docker_env"] == {"port": "INT_BIGTABLE_GRPC_HOST_PORT"}
    assert raw["docker_defaults"]["port"] == 19470
    assert raw["docker_defaults"]["project"] == "test-project"
    assert raw["docker_defaults"]["instance"] == "test-instance"
    assert raw["provides"] == EXPECTED_PROVIDES


def test_bigtable_tokens_render_docker_defaults():
    # (3) the tokens exist AND resolve to the concrete values on the integration port.
    t = tokens_mod.service_tokens("bigtable", env={}, services_dir=_SERVICES_DIR)
    assert t["BIGTABLE_PROJECT"] == "test-project"
    assert t["BIGTABLE_INSTANCE"] == "test-instance"
    assert t["BIGTABLE_HOST"] == "localhost"
    assert t["BIGTABLE_GRPC_PORT"] == "19470"
    assert t["BIGTABLE_EMULATOR_HOST"] == "localhost:19470"
    assert t["BIGTABLE_URL"] == (
        "jdbc:bigtable:/projects/test-project/instances/test-instance?app_profile_id=default")


def test_bigtable_grpc_host_port_override_moves_the_client(monkeypatch):
    # INT_BIGTABLE_GRPC_HOST_PORT remaps a busy published port and, via service.yaml's
    # docker_env, must move the admin client that dials it -- the silent-driver hazard the
    # guard in test_services.py proves closes for every service that publishes an overridable
    # port (a bigtable remap is the same shape: container on 19110, client must follow).
    monkeypatch.setenv("INT_BIGTABLE_GRPC_HOST_PORT", "19110")
    t = tokens_mod.service_tokens("bigtable", services_dir=_SERVICES_DIR)
    assert t["BIGTABLE_GRPC_PORT"] == "19110"
    assert t["BIGTABLE_EMULATOR_HOST"] == "localhost:19110"
