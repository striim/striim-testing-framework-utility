"""Hermetic, no-Docker self-test for the LIVE bigtable service definition (mirrors
test_spanner_service.py): it parses services/bigtable/service.yaml through the real loader
(livetest.registry.load_service), renders its `provides:` as the tier resolves them, and checks the
compose file and the Striim overlay that sets BIGTABLE_EMULATOR_HOST on the cluster."""
from pathlib import Path

import yaml
from livetest.registry import load_service
from livetest.services import declared_containers, resolve
from livetest.plugin import build_service_tokens

# The tokens for the Striim app, as live Spanner provides SPANNER_PROJECT/INSTANCE and its URLs.
# BIGTABLE_URL carries no host: Striim reaches the emulator through the overlay's
# BIGTABLE_EMULATOR_HOST.
EXPECTED_PROVIDES = {
    "BIGTABLE_PROJECT": "{project}",
    "BIGTABLE_INSTANCE": "{instance}",
    "BIGTABLE_URL": "jdbc:bigtable:/projects/{project}/instances/{instance}?app_profile_id=default",
}


def test_bigtable_service_def_loads():
    # (1) parse under the real registry, + the shape the live tier relies on for it.
    defn = load_service("bigtable")
    assert defn.isolation == "none"
    assert defn.container == "slt-bigtable"
    assert defn.live_override_env == "SLT_BIGTABLE_HOST"
    assert defn.opt_in_env is None                       # always available, as Spanner is
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
    assert tokens["BIGTABLE_URL"] == (
        "jdbc:bigtable:/projects/test-project/instances/test-instance?app_profile_id=default")


def test_bigtable_grpc_host_port_override_moves_the_admin_endpoint():
    # SLT_BIGTABLE_GRPC_HOST_PORT remaps the published host port; docker_env moves the base port
    # the framework's admin dials (plugin._bigtable_admins) the same way.
    r = resolve("bigtable", env={"SLT_BIGTABLE_GRPC_HOST_PORT": "9110"}, started=set(),
                compose_up=lambda defn: None)
    assert (r.base["host"], r.base["port"]) == ("localhost", "9110")


def test_bigtable_resolve_live_override():
    r = resolve("bigtable", env={"SLT_BIGTABLE_HOST": "emu.example", "SLT_BIGTABLE_PORT": "8086"},
                started=set(), compose_up=lambda defn: None)
    assert r.mode == "live"
    assert (r.base["host"], str(r.base["port"])) == ("emu.example", "8086")


# --- compose rendering: name / container_name / published port + the Striim overlay ------
# No Docker, no CBTEMULATOR: render the compose files' own text the way the harness does --
# via the framework's own declared_containers/stack (live reads SLT_STACK_PREFIX) and a YAML
# parse of the overlay. The claim in each test: the rendered names/ports
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


def test_live_compose_remaps_only_the_host_port():
    # Spanner's shape: the emulator listens on a fixed 9470 inside the container, and
    # SLT_BIGTABLE_GRPC_HOST_PORT moves only the published host side.
    svc = yaml.safe_load(_LIVE_COMPOSE.read_text())["services"]["slt-bigtable"]
    assert svc["ports"] == ["${SLT_BIGTABLE_GRPC_HOST_PORT:-9470}:9470"]
    assert svc["command"][-1].endswith("--host-port=0.0.0.0:9470")
    assert "SLT_BIGTABLE_GRPC_HOST_PORT" not in str(svc["command"]) + str(svc["healthcheck"])
    assert "$" not in svc["image"]                       # hard-coded, as Spanner's is


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


def _healthcheck_argv(port=9470):
    # What Docker runs for the healthcheck: CMD-SHELL hands its one string to /bin/sh -c; CMD runs
    # its arguments directly. `port` stands in for the container's fixed 9470 on this host.
    kind, *args = yaml.safe_load(_LIVE_COMPOSE.read_text())["services"]["slt-bigtable"]["healthcheck"]["test"]
    args = [a.replace("/9470", f"/{port}") for a in args]
    return ["/bin/sh", "-c", args[0]] if kind == "CMD-SHELL" else args


def test_live_healthcheck_runs_its_tcp_probe_under_bash():
    # The emulator image is Debian-based, so its sh is dash, which has no /dev/tcp.
    argv = _healthcheck_argv()
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
        argv = _healthcheck_argv(listener.getsockname()[1])
        argv[0] = str(shells / "sh") if argv[0] == "/bin/sh" else argv[0]
        assert subprocess.run(argv, env=env, timeout=10).returncode == 0
    assert subprocess.run(argv, env=env, timeout=10).returncode != 0


# --- the admins a test that requires bigtable gets (plugin._bigtable_admins) ----------------

class _RecordingAdmin:
    def __init__(self, dsn):
        self.dsn, self.ensured = dsn, 0

    def ensure(self):
        self.ensured += 1


def test_the_plugin_gives_one_admin_on_the_base_endpoint_under_both_routes(monkeypatch):
    from livetest import plugin
    monkeypatch.setattr(plugin, "BigtableAdmin", _RecordingAdmin)
    monkeypatch.setattr(plugin, "_parallel", lambda env: False)     # a serial run
    base = resolve("bigtable", env={"SLT_BIGTABLE_GRPC_HOST_PORT": "9110"}, started=set(),
                   compose_up=lambda defn: None).base
    pairs = plugin._bigtable_admins(base)
    assert [key for _, key in pairs] == ["bigtable-target", "bigtable-source"]
    admin = pairs[0][0]
    assert pairs[1][0] is admin and admin.ensured == 1
    assert admin.dsn == {"project": "test-project", "instance": "test-instance",
                         "emulator_host": "localhost:9110"}


def test_the_plugin_runs_the_readiness_gate_once_across_xdist_workers(monkeypatch):
    from livetest import plugin
    monkeypatch.setattr(plugin, "BigtableAdmin", _RecordingAdmin)
    monkeypatch.setattr(plugin, "_parallel", lambda env: True)
    once = []
    monkeypatch.setattr(plugin, "ensure_provisioned_once", lambda key, fn: once.append(key))
    base = {"host": "localhost", "port": 9470, "project": "test-project", "instance": "test-instance"}
    (admin, _), _ = plugin._bigtable_admins(base)
    assert once == ["bigtable-setup"] and admin.ensured == 0
