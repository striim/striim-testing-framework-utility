"""``live_defaults:``: what a setting falls back to when the existing instance is used.

A definition with a container and an existing-instance mode (``live_override_env``) used to fall
back to its ``docker_defaults`` for every setting left unset, so a real instance on another port or
scheme needed that setting too. ``live_defaults`` gives the existing-instance mode its own fallbacks:
with the override set and a setting unset, ``live_defaults`` wins over ``docker_defaults``; in Docker
mode it plays no part. Both tiers.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

SERVICE = """name: extapi
compose: compose.yaml
container: slt-extapi
isolation: none
live_override_env: SLT_EXTAPI_HOST
docker_defaults: {host: localhost, scheme: http, port: 5880, user: sim}
docker_env: {port: SLT_EXTAPI_HOST_PORT}
live_defaults: {scheme: https, port: 443}
live_env: {host: SLT_EXTAPI_HOST, scheme: SLT_EXTAPI_SCHEME, port: SLT_EXTAPI_PORT, user: SLT_EXTAPI_USER}
provides: {EXTAPI_URL: '{scheme}://{view_host}:{port}'}
"""


def _service(root: Path, text: str = SERVICE) -> Path:
    d = root / "extapi"
    d.mkdir(parents=True)
    (d / "service.yaml").write_text(text)
    (d / "compose.yaml").write_text("services:\n  slt-extapi:\n    image: example/extapi:1\n"
                                    "    container_name: slt-extapi\n")
    return root


@pytest.fixture
def live_root(tmp_path, monkeypatch):
    from livetest import layout
    root = _service(tmp_path / "services")
    monkeypatch.setattr(layout, "services_roots", lambda: [root, REPO / "scripts" / "live" / "services"])
    return root


def _url(env):
    from livetest import registry, services
    from livetest.plugin import build_service_tokens
    r = services.resolve("extapi", env, set(), compose_up=lambda d: None, post_up=None)
    return r.mode, build_service_tokens(registry.load_service("extapi"), r, schema="")["EXTAPI_URL"]


# --- live tier ----------------------------------------------------------------------------------

def test_the_existing_instance_falls_back_to_live_defaults(live_root):
    assert _url({"SLT_EXTAPI_HOST": "api.example.com"}) == ("live", "https://api.example.com:443")


def test_docker_mode_keeps_docker_defaults(live_root):
    assert _url({}) == ("docker", "http://localhost:5880")


def test_a_set_setting_still_wins_over_live_defaults(live_root):
    env = {"SLT_EXTAPI_HOST": "api.example.com", "SLT_EXTAPI_PORT": "8443"}
    assert _url(env) == ("live", "https://api.example.com:8443")


def test_a_key_live_defaults_does_not_name_falls_back_to_docker_defaults(live_root):
    from livetest import services
    r = services.resolve("extapi", {"SLT_EXTAPI_HOST": "api.example.com"}, set(),
                         compose_up=None, post_up=None)
    assert r.base["user"] == "sim"


def test_the_definition_carries_its_live_defaults(live_root):
    from livetest import registry
    assert registry.load_service("extapi").live_defaults == {"scheme": "https", "port": 443}


@pytest.mark.parametrize("bad,why", [
    ("live_defaults: [443]\n", "'live_defaults' must map live_env keys to values"),
    ("live_defaults: {password: x}\n", "'live_defaults' key password is not a live_env key"),
])
def test_live_defaults_must_map_live_env_keys(tmp_path, bad, why):
    from livetest import registry
    text = "\n".join(ln for ln in SERVICE.splitlines() if not ln.startswith("live_defaults")) + "\n" + bad
    root = _service(tmp_path / "s", text)
    with pytest.raises(registry.RegistryError, match=why):
        registry.load_service("extapi", services_dir=root)


def test_doctor_probes_the_live_defaults_port(live_root):
    from striim_test import doctor
    seen = []
    doctor.check_services({"extapi": ["tests/live/x"]}, {"SLT_EXTAPI_HOST": "api.example.com"},
                          tcp=lambda h, p: seen.append((h, p)), docker=lambda: (True, ""),
                          running=lambda c: False)
    assert seen == [("api.example.com", 443)]


# --- integration tier -----------------------------------------------------------------------------

INT_RAW = {"name": "extapi", "live_override_env": "INT_EXTAPI_HOST",
           "docker_defaults": {"host": "localhost", "scheme": "http", "port": 15880},
           "live_defaults": {"scheme": "https", "port": 443},
           "live_env": {"host": "INT_EXTAPI_HOST", "scheme": "INT_EXTAPI_SCHEME", "port": "INT_EXTAPI_PORT"}}


def test_integration_existing_instance_falls_back_to_live_defaults():
    from inttest.tokens import _resolve_service_base
    base = _resolve_service_base("extapi", INT_RAW, {"INT_EXTAPI_HOST": "api.example.com"})
    assert (base["host"], base["scheme"], base["port"]) == ("api.example.com", "https", 443)


def test_integration_docker_mode_keeps_docker_defaults():
    from inttest.tokens import _resolve_service_base
    base = _resolve_service_base("extapi", INT_RAW, {})
    assert (base["host"], base["scheme"], base["port"]) == ("localhost", "http", 15880)


def test_integration_a_set_setting_still_wins():
    from inttest.tokens import _resolve_service_base
    base = _resolve_service_base("extapi", INT_RAW, {"INT_EXTAPI_HOST": "a", "INT_EXTAPI_PORT": "8443"})
    assert base["port"] == "8443"


def test_integration_definition_is_validated_the_same_way(tmp_path):
    from inttest.tokens import ServiceConfigError, _load_service_def
    d = tmp_path / "extapi"
    d.mkdir()
    (d / "service.yaml").write_text("name: extapi\nisolation: none\nlive_override_env: INT_EXTAPI_HOST\n"
                                    "live_env: {host: INT_EXTAPI_HOST}\nlive_defaults: {port: 443}\n")
    with pytest.raises(ServiceConfigError, match="'live_defaults' key port is not a live_env key"):
        _load_service_def("extapi", tmp_path)


# --- a container's remapped port does not follow the switch to an existing instance -----------------

def test_live_a_remapped_container_port_does_not_reach_the_existing_instance(live_root):
    env = {"SLT_EXTAPI_HOST": "api.example.com", "SLT_EXTAPI_HOST_PORT": "15881"}
    assert _url(env) == ("live", "https://api.example.com:443")
    assert _url({**env, "SLT_EXTAPI_PORT": "8443"}) == ("live", "https://api.example.com:8443")


INT_MIXED = {**INT_RAW, "docker_env": {"port": "INT_EXTAPI_HOST_PORT"}}


def test_integration_a_remapped_container_port_does_not_reach_the_existing_instance():
    from inttest.tokens import _resolve_service_base
    env = {"INT_EXTAPI_HOST": "api.example.com", "INT_EXTAPI_HOST_PORT": "15881"}
    assert _resolve_service_base("extapi", INT_MIXED, env)["port"] == 443
    assert _resolve_service_base("extapi", INT_MIXED, {**env, "INT_EXTAPI_PORT": "8443"})["port"] == "8443"
    # in Docker mode the remapped port is the container's, as before
    assert _resolve_service_base("extapi", INT_MIXED, {"INT_EXTAPI_HOST_PORT": "15881"})["port"] == "15881"


def test_integration_without_live_defaults_resolution_is_unchanged():
    from inttest.tokens import _resolve_service_base
    raw = {k: v for k, v in INT_MIXED.items() if k != "live_defaults"}
    env = {"INT_EXTAPI_HOST": "api.example.com", "INT_EXTAPI_HOST_PORT": "15881"}
    assert _resolve_service_base("extapi", raw, env)["port"] == "15881"
