"""Hermetic, no-Docker self-test for the INTEGRATION bigtable service definition.

Deliverable 8a / BIGTABLE_EMULATOR_SERVICE_PLAN.md §9.1 ("a bigtable service parses and
provides the expected BIGTABLE_* tokens on each tier"), the integration-half. Mirror of
scripts/live/tests/test_services_bigtable.py: parse services/bigtable/service.yaml through
inttest's own loader (inttest.tokens._load_service_def / service_tokens) and assert the
service publishes EXACTLY the BIGTABLE_* tokens the integration tier resolves -- no Docker.
Same guard shape scripts/integration/tests/test_services.py carries for spanner.
"""
from pathlib import Path

import yaml
from inttest import tokens as tokens_mod
from inttest import services as services_mod

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


def test_bigtable_service_def_declares_no_opt_in_env():
    # Unlike the LIVE service -- which gates on SLT_BIGTABLE_SERVICE because its Striim overlay sets
    # a JVM-global BIGTABLE_EMULATOR_HOST that would hijack real Bigtable in a reused slt-* cluster --
    # the INTEGRATION tier has no such overlay: a `db: bigtable-target` manifest reaches the
    # CBTEMULATOR only through the harness admin at its base port 19470, so nothing is redirected and
    # a non-bigtable test never sees a hijacked connection. The model (spanner, see
    # test_services.py + the live test_registry test_opt_in_env_declared_per_service) is ungated on
    # both tiers, so the gate must stay absent. This locks the decision so the divergence from the
    # live tier is not later "fixed" into an accidental opt-in.
    raw = tokens_mod._load_service_def("bigtable", _SERVICES_DIR)
    assert raw.get("opt_in_env") is None


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


# --- §9.2 compose rendering: name / container_name / published port (integration half) ---------
# Mirror of the live §9.2 block at scripts/live/tests/test_services_bigtable.py: this tier
# honours INT_STACK_PREFIX (not SLT_STACK_PREFIX), so it uses inttest.services' own
# declared_containers / compose_container_names, which read the prefix from the environment --
# the same idiom test_services.py::test_container_names_carry_the_stack_prefix uses. No Docker.

_INT_COMPOSE = _SERVICES_DIR / "bigtable" / "compose.yaml"
_INT_PREFIX_TPL = "${INT_STACK_PREFIX:+${INT_STACK_PREFIX}-}"


def test_integration_compose_container_name_renders_with_and_without_stack_prefix(monkeypatch):
    # container_name carries the canonical INT_STACK_PREFIX expression; the framework strips the
    # interpolation and re-derives via the prefix, so both the bare default and a prefixed stack
    # must render, and an override must not leak a literal prefix into the bare.
    monkeypatch.delenv("INT_STACK_PREFIX", raising=False)
    assert services_mod.declared_containers([_INT_COMPOSE]) == ["int-bigtable"]
    monkeypatch.setenv("INT_STACK_PREFIX", "ec")
    assert services_mod.declared_containers([_INT_COMPOSE]) == ["ec-int-bigtable"]


def test_integration_compose_project_and_container_names_carry_the_stack_prefix():
    # The compose `name:` (which scopes `docker compose down`) and the container_name must carry
    # the SAME prefix expression: a prefix that hits one but not the other orphaned the other on
    # teardown -- the silent-driver failure stack-scoping exists to prevent.
    data = yaml.safe_load(_INT_COMPOSE.read_text())
    assert data["name"] == f"{_INT_PREFIX_TPL}int-bigtable"
    assert data["services"]["int-bigtable"]["container_name"] == f"{_INT_PREFIX_TPL}int-bigtable"


def test_integration_compose_published_port_is_the_overridable_host_port():
    # The published host port IS the container port, both bound to the same overridable
    # INT_BIGTABLE_GRPC_HOST_PORT with the 19470 default -- same on a separate port so both
    # stacks can run at once (the CBTEMULATOR is one gRPC listener, no REST/admin split).
    data = yaml.safe_load(_INT_COMPOSE.read_text())
    ports = data["services"]["int-bigtable"]["ports"]
    assert ports == ["${INT_BIGTABLE_GRPC_HOST_PORT:-19470}:${INT_BIGTABLE_GRPC_HOST_PORT:-19470}"]


def test_bigtable_service_names_its_python_module(monkeypatch):
    # Without google-cloud-bigtable a bigtable test must skip with the install hint, before the
    # emulator is provisioned, rather than fail at the first admin call.
    from livetest import registry
    raw = tokens_mod._load_service_def("bigtable", _SERVICES_DIR)
    assert raw["python_module"] == "google.cloud.bigtable"
    assert "google-cloud-bigtable" in raw["python_module_hint"]
    real = registry.importlib.util.find_spec
    monkeypatch.setattr(registry.importlib.util, "find_spec",
                        lambda name, *a: None if name == raw["python_module"] else real(name, *a))
    assert services_mod.host_unavailable("bigtable") == (
        f"Python module google.cloud.bigtable is not installed: {raw['python_module_hint']}")


def _healthcheck_argv(env):
    # What Docker runs for the healthcheck, after compose interpolation: CMD-SHELL hands its one
    # string to /bin/sh -c; CMD runs its arguments directly.
    import re
    kind, *args = yaml.safe_load(_INT_COMPOSE.read_text())["services"]["int-bigtable"]["healthcheck"]["test"]
    expand = lambda s: re.sub(r"\$\{(\w+):-([^}]*)\}", lambda m: env.get(m[1], m[2]), s)
    return ["/bin/sh", "-c", expand(args[0])] if kind == "CMD-SHELL" else [expand(a) for a in args]


def test_integration_healthcheck_runs_its_tcp_probe_under_bash():
    # The emulator image is Debian-based, so its sh is dash, which has no /dev/tcp.
    argv = _healthcheck_argv({})
    assert Path(argv[0]).name == "bash"
    assert "sh -c" not in " ".join(argv[1:])


def test_integration_healthcheck_passes_on_a_listener_and_fails_without_one_when_sh_is_dash(tmp_path):
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
        argv = _healthcheck_argv({"INT_BIGTABLE_GRPC_HOST_PORT": str(listener.getsockname()[1])})
        argv[0] = str(shells / "sh") if argv[0] == "/bin/sh" else argv[0]
        assert subprocess.run(argv, env=env, timeout=10).returncode == 0
    assert subprocess.run(argv, env=env, timeout=10).returncode != 0
