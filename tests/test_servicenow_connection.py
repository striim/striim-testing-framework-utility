"""ServiceNow is a target the framework connects to, not a service it runs.

ServiceNow is hosted: no container runs an instance. Both tiers ship a connection-only
``servicenow`` definition (a service.yaml with no compose file and no container), so a test that
requires servicenow runs against the instance SLT_SERVICENOW_HOST / INT_SERVICENOW_HOST names. With
nothing set its tests skip, naming the setting. A consumer may load its own service beside it that
provides the same token names (a stand-in under another name, say); a case requires one or the
other, and gets that one's values.

Every docker and subprocess call is trapped: nothing here provisions or contacts anything.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
LIVE = REPO / "scripts" / "live" / "services" / "servicenow"
INT = REPO / "scripts" / "integration" / "services" / "servicenow"

TOKENS = {"SERVICENOW_URL", "SERVICENOW_HOST", "SERVICENOW_USER", "SERVICENOW_PASSWORD",
          "SERVICENOW_CLIENT_ID", "SERVICENOW_CLIENT_SECRET", "SERVICENOW_TOKEN_URL"}
SETTINGS = ("HOST", "SCHEME", "PORT", "USER", "PASSWORD", "CLIENT_ID", "CLIENT_SECRET")


@pytest.fixture
def no_side_effects(monkeypatch):
    """Any subprocess (docker, compose) fails the test."""
    def trap(*a, **k):
        raise AssertionError(f"unexpected subprocess: {a[0] if a else k}")
    monkeypatch.setattr(subprocess, "run", trap)
    monkeypatch.setattr(subprocess, "Popen", trap)
    for s in SETTINGS:
        monkeypatch.delenv(f"SLT_SERVICENOW_{s}", raising=False)
        monkeypatch.delenv(f"INT_SERVICENOW_{s}", raising=False)
    monkeypatch.delenv("SLT_SERVICENOW_VIEW_HOST", raising=False)


def _live_tokens(env):
    from livetest import registry, services
    from livetest.plugin import build_service_tokens
    defn = registry.load_service("servicenow")
    r = services.resolve("servicenow", env, set(), compose_up=None, post_up=None)
    assert r.mode == "live" and r.started is False
    return build_service_tokens(defn, r, schema="")


# --- what ships -------------------------------------------------------------------------------

def test_both_tiers_ship_a_connection_only_servicenow():
    for d, prefix in ((LIVE, "SLT"), (INT, "INT")):
        assert sorted(p.name for p in d.iterdir()) == ["service.yaml"], d
        spec = yaml.safe_load((d / "service.yaml").read_text())
        assert not spec.get("compose") and not spec.get("container"), d
        assert spec["live_override_env"] == f"{prefix}_SERVICENOW_HOST"
        assert set(spec["live_env"].values()) == {f"{prefix}_SERVICENOW_{s}" for s in SETTINGS}
        assert set(spec["provides"]) == TOKENS, d
        assert spec["docker_defaults"] == {"scheme": "https", "port": 443}, d


def test_every_setting_is_a_service_key_dotenv_passes_on():
    from livetest import paths as live_paths
    from inttest import paths as int_paths
    want = {f"{p}_SERVICENOW_{s}" for p in ("SLT", "INT") for s in SETTINGS} | {"SLT_SERVICENOW_VIEW_HOST"}
    for keys in (live_paths.SERVICE_KEYS, int_paths.SERVICE_KEYS):
        assert want <= set(keys), sorted(want - set(keys))


# --- live tier ----------------------------------------------------------------------------------

def test_the_live_definition_provides_the_seven_tokens(no_side_effects):
    from livetest import registry
    defn = registry.load_service("servicenow")
    assert defn.dir == LIVE and set(defn.provides) == TOKENS


def test_nothing_set_skips_with_the_setting_to_set(no_side_effects):
    from livetest import registry
    defn = registry.load_service("servicenow")
    assert registry.unavailable(defn, {}) == (
        "no container ships for servicenow: set SLT_SERVICENOW_HOST and its settings to your "
        "own instance, or add a servicenow service that has a compose file through servicesRoots")


def test_the_host_alone_gives_https_on_443(no_side_effects):
    tokens = _live_tokens({"SLT_SERVICENOW_HOST": "dev123.service-now.com"})
    assert tokens["SERVICENOW_URL"] == "https://dev123.service-now.com:443"
    assert tokens["SERVICENOW_HOST"] == "dev123.service-now.com"
    assert tokens["SERVICENOW_TOKEN_URL"] == "https://dev123.service-now.com:443/oauth_token.do"


def test_every_setting_reaches_its_token(no_side_effects):
    tokens = _live_tokens({
        "SLT_SERVICENOW_HOST": "sn.example.com", "SLT_SERVICENOW_SCHEME": "http",
        "SLT_SERVICENOW_PORT": "8080", "SLT_SERVICENOW_USER": "u1",
        "SLT_SERVICENOW_PASSWORD": "pw1", "SLT_SERVICENOW_CLIENT_ID": "cid",
        "SLT_SERVICENOW_CLIENT_SECRET": "csec", "SLT_SERVICENOW_VIEW_HOST": "proxy.example.com"})
    assert tokens == {
        "SERVICENOW_URL": "http://proxy.example.com:8080", "SERVICENOW_HOST": "proxy.example.com",
        "SERVICENOW_USER": "u1", "SERVICENOW_PASSWORD": "pw1", "SERVICENOW_CLIENT_ID": "cid",
        "SERVICENOW_CLIENT_SECRET": "csec",
        "SERVICENOW_TOKEN_URL": "http://proxy.example.com:8080/oauth_token.do"}


def test_an_unset_setting_with_no_default_renders_empty(no_side_effects):
    # A basic-auth test sets no OAuth client: its tokens are empty strings, not the text "None".
    tokens = _live_tokens({"SLT_SERVICENOW_HOST": "sn.example.com", "SLT_SERVICENOW_USER": "u1"})
    assert tokens["SERVICENOW_CLIENT_ID"] == "" and tokens["SERVICENOW_CLIENT_SECRET"] == ""
    assert tokens["SERVICENOW_PASSWORD"] == "" and tokens["SERVICENOW_USER"] == "u1"


def test_preflight_leaves_it_out_until_its_host_is_set(monkeypatch, no_side_effects):
    from livetest import preflight
    calls = []
    monkeypatch.setattr(preflight._slt_infra, "resolve_service",
                        lambda infra, svc, env, started, resolve, progress=None: calls.append(svc))
    monkeypatch.setattr(preflight, "ensure_provisioned_once", lambda key, fn: None)
    assert preflight._bring_up_services(["servicenow"], {}, infra=object()) == {}
    assert calls == []


# --- a consumer service beside it, providing the same token names --------------------------------

def _consumer_sim(root: Path) -> Path:
    d = root / "servicenow-stub"
    d.mkdir(parents=True)
    (d / "service.yaml").write_text(
        "name: servicenow-stub\ncompose: compose.yaml\ncontainer: slt-servicenow-stub\nisolation: none\n"
        "docker_defaults: {host: localhost, scheme: http, port: 5880, user: sim_user, password: sim_pw,\n"
        "                  client_id: sim-client, client_secret: sim-secret}\n"
        "docker_env: {port: STUB_HOST_PORT}\n"
        "provides:\n"
        "  SERVICENOW_URL: '{scheme}://{view_host}:{port}'\n"
        "  SERVICENOW_HOST: '{view_host}'\n"
        "  SERVICENOW_USER: '{user}'\n"
        "  SERVICENOW_PASSWORD: '{password}'\n"
        "  SERVICENOW_CLIENT_ID: '{client_id}'\n"
        "  SERVICENOW_CLIENT_SECRET: '{client_secret}'\n"
        "  SERVICENOW_TOKEN_URL: '{scheme}://{view_host}:{port}/oauth_token.do'\n")
    (d / "compose.yaml").write_text("services:\n  slt-servicenow-stub:\n    image: example/stub:1\n"
                                    "    container_name: slt-servicenow-stub\n")
    return d


def test_two_loaded_definitions_may_provide_the_same_token_names(tmp_path, monkeypatch,
                                                                  no_side_effects):
    from livetest import layout, registry, services
    from livetest.plugin import build_service_tokens
    root = tmp_path / "consumer-services"
    sim = _consumer_sim(root)
    monkeypatch.setattr(layout, "services_roots",
                        lambda: [root, REPO / "scripts" / "live" / "services"])
    assert {"servicenow", "servicenow-stub"} <= set(registry.all_services())
    real, fake = registry.load_service("servicenow"), registry.load_service("servicenow-stub")
    assert real.dir == LIVE and fake.dir == sim
    assert set(real.provides) == set(fake.provides) == TOKENS

    # a case requiring the simulator gets its values ...
    up = []
    r = services.resolve("servicenow-stub", {}, set(), compose_up=up.append, post_up=None)
    got = build_service_tokens(fake, r, schema="")
    assert up and got["SERVICENOW_URL"] == "http://localhost:5880" and got["SERVICENOW_USER"] == "sim_user"
    # ... and one requiring the real instance gets the instance's, with the simulator loaded too
    env = {"SLT_SERVICENOW_HOST": "sn.example.com", "SLT_SERVICENOW_USER": "u1"}
    r = services.resolve("servicenow", env, set(), compose_up=None, post_up=None)
    got = build_service_tokens(real, r, schema="")
    assert got["SERVICENOW_URL"] == "https://sn.example.com:443" and got["SERVICENOW_USER"] == "u1"


# --- doctor -------------------------------------------------------------------------------------

def doctor_status(name):
    from striim_test import doctor
    return {"WARN": doctor.WARN, "FAIL": doctor.FAIL}[name]


def _doctor_service_checks(env, tcp):
    from striim_test import doctor
    return doctor.check_services({"servicenow": ["tests/live/sn-case"]}, env, tcp=tcp,
                                 docker=lambda: (True, ""), running=lambda c: False)


def test_doctor_warns_a_connection_only_servicenow_is_skipped_until_its_host_is_set(no_side_effects):
    checks = _doctor_service_checks({}, tcp=lambda h, p: pytest.fail("nothing to probe"))
    (c,) = checks
    assert c.status == doctor_status("WARN") and "SLT_SERVICENOW_HOST" in c.message and "skip" in c.message


def test_doctor_names_the_host_when_it_does_not_answer(no_side_effects):
    seen = []

    def tcp(host, port):
        seen.append((host, port))
        return "connection refused"
    checks = _doctor_service_checks({"SLT_SERVICENOW_HOST": "sn.example.com"}, tcp)
    (c,) = checks
    assert seen == [("sn.example.com", 443)]
    assert c.status == doctor_status("FAIL") and "sn.example.com:443" in c.message and "connection refused" in c.message


# --- integration tier -----------------------------------------------------------------------------

def test_integration_external_host_resolves_tokens_and_brings_nothing_up(monkeypatch, no_side_effects):
    from inttest import plugin, tokens

    def never(*_a, **_k):
        raise AssertionError("an external servicenow must not be brought up")
    monkeypatch.setattr(plugin._docker_mod, "ensure_up", never)
    env = {"INT_SERVICENOW_HOST": "sn.example.com", "INT_SERVICENOW_USER": "u1"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    plugin._provision_requires("case", ["servicenow"], lock=None)
    got = tokens.service_tokens("servicenow", env)
    assert got["SERVICENOW_URL"] == "https://sn.example.com:443"
    assert got["SERVICENOW_USER"] == "u1"
    assert got["SERVICENOW_CLIENT_ID"] == "" and got["SERVICENOW_PASSWORD"] == ""


def test_integration_nothing_set_skips_naming_the_setting(monkeypatch, no_side_effects):
    from inttest import plugin
    monkeypatch.setattr(plugin._docker_mod, "ensure_up",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("brought up")))
    with pytest.raises(pytest.skip.Exception, match="set INT_SERVICENOW_HOST"):
        plugin._provision_requires("case", ["servicenow"], lock=None)


def test_integration_start_all_leaves_the_connection_only_service_out(no_side_effects):
    from inttest import services
    assert services.is_connection_only("servicenow")
    from inttest import cli
    assert "servicenow" not in cli._registered()
