"""Teradata is a target the framework connects to, not a service it runs.

The Teradata VM image, its disks and their download left the framework.
What stays is the connection: both tiers ship a connection-only ``teradata`` definition (a
service.yaml with no compose file and no container), so a test that requires teradata runs
against the instance SLT_TERADATA_HOST / INT_TERADATA_HOST names, the way SLT_PG_HOST points at
your own Postgres. With nothing set its tests skip, naming the setting. A consumer service named
teradata (servicesRoots) that does ship a container replaces the definition.

Every docker and subprocess call is trapped: nothing here provisions or downloads anything.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
LIVE = REPO / "scripts" / "live" / "services" / "teradata"
INT = REPO / "scripts" / "integration" / "services" / "teradata"

EXTERNAL = {
    "SLT_TERADATA_HOST": "td.example.com", "SLT_TERADATA_PORT": "1026",
    "SLT_TERADATA_USER": "admin1", "SLT_TERADATA_PASSWORD": "pw1",
    "SLT_TERADATA_SOURCE_USER": "src_u", "SLT_TERADATA_SOURCE_PASSWORD": "pw2",
    "SLT_TERADATA_SOURCE_SCHEMA": "src_db",
    "SLT_TERADATA_TARGET_USER": "tgt_u", "SLT_TERADATA_TARGET_PASSWORD": "pw3",
    "SLT_TERADATA_TARGET_SCHEMA": "tgt_db",
}


@pytest.fixture
def no_side_effects(monkeypatch):
    """Any subprocess (docker, compose, a download script) fails the test. The optional driver
    counts as installed: these tests are about the connection settings, and its absence is
    scripts/live/tests/test_teradata_optional_driver.py's."""
    def trap(*a, **k):
        raise AssertionError(f"unexpected subprocess: {a[0] if a else k}")
    monkeypatch.setattr(subprocess, "run", trap)
    monkeypatch.setattr(subprocess, "Popen", trap)
    from livetest import registry
    monkeypatch.setattr(registry, "missing_python_module", lambda *a, **k: None)


# --- what ships -------------------------------------------------------------------------------

def test_both_tiers_ship_a_connection_only_teradata():
    for d in (LIVE, INT):
        assert sorted(p.name for p in d.iterdir()) == ["service.yaml"], d
        spec = yaml.safe_load((d / "service.yaml").read_text())
        assert not spec.get("compose") and not spec.get("container"), d
        assert spec["live_override_env"] in spec["live_env"].values()


def test_no_disk_download_or_boot_code_remains():
    from livetest import teradataadmin
    for gone in ("ensure_deps", "ensure_deps_locked", "_deps_dir", "unavailable"):
        assert not hasattr(teradataadmin, gone), gone
    from livetest import paths
    for key in ("SLT_TERADATA_AUTO_DOWNLOAD", "INT_TERADATA_DEPS_DIR", "SLT_TERADATA_HOST_PORT",
                "SLT_TERADATA_SSH_HOST_PORT", "INT_TERADATA_HOST_PORT"):
        assert key not in paths.SERVICE_KEYS, key
    # the connection settings stay, and .env passes them on like SLT_PG_*
    assert set(EXTERNAL) | {"SLT_TERADATA_VIEW_HOST", "INT_TERADATA_HOST"} <= set(paths.SERVICE_KEYS)


# --- live tier ----------------------------------------------------------------------------------

def test_nothing_set_skips_with_the_setting_to_set(no_side_effects):
    from livetest import registry
    defn = registry.load_service("teradata")
    assert defn.dir == LIVE
    why = registry.unavailable(defn, {})
    assert why == ("no container ships for teradata: set SLT_TERADATA_HOST and its settings to your "
                   "own instance, or add a teradata service that has a compose file through "
                   "servicesRoots")


def test_an_external_host_resolves_its_connection_and_provisions_nothing(no_side_effects):
    from livetest import registry, services
    from livetest.plugin import build_service_tokens
    defn = registry.load_service("teradata")
    assert registry.unavailable(defn, EXTERNAL) is None

    def never(*_a, **_k):
        raise AssertionError("an external teradata must not be brought up")
    r = services.resolve("teradata", EXTERNAL, set(), compose_up=never, post_up=never)
    assert r.mode == "live" and r.started is False
    assert r.base == {"host": "td.example.com", "port": "1026", "user": "admin1", "password": "pw1",
                      "source_user": "src_u", "source_password": "pw2", "source_schema": "src_db",
                      "target_user": "tgt_u", "target_password": "pw3", "target_schema": "tgt_db",
                      "view_host": "td.example.com"}
    tokens = build_service_tokens(defn, r, schema="")
    assert tokens["TERADATA_URL"] == "jdbc:teradata://td.example.com/DBS_PORT=1026"
    assert tokens["TERADATA_SOURCE_SCHEMA"] == "src_db" and tokens["TERADATA_TARGET_USER"] == "tgt_u"


def test_only_noncredential_settings_have_defaults(no_side_effects):
    from livetest import services
    env = {key: value for key, value in EXTERNAL.items()
           if not key.endswith(("_PORT", "_SCHEMA"))}
    r = services.resolve("teradata", env, set(),
                         compose_up=None, post_up=None)
    assert (r.base["port"], r.base["user"], r.base["source_user"], r.base["target_schema"]) == \
        (1025, "admin1", "src_u", "qatarget")


def test_host_without_credentials_fails_before_any_side_effect(no_side_effects):
    from livetest import services
    with pytest.raises(services.ServiceError, match="required environment") as exc:
        services.resolve("teradata", {"SLT_TERADATA_HOST": "td.example.com"}, set())
    assert "SLT_TERADATA_PASSWORD" in str(exc.value)


def test_the_admin_connects_to_the_external_host(no_side_effects):
    from livetest import services
    from livetest.teradataadmin import TeradataAdmin
    seen = []

    class _Cur:
        description = [("N",)]

        def execute(self, sql, params=None):
            pass

        def fetchall(self):
            return [(0,)]

    def connect(**kw):
        seen.append((kw["host"], kw["port"], kw["user"]))
        return type("C", (), {"cursor": lambda self: _Cur(), "close": lambda self: None})()

    r = services.resolve("teradata", EXTERNAL, set(), compose_up=None, post_up=None)
    TeradataAdmin(r.base, connect=connect, role="target").count_rows("T1")
    assert seen == [("td.example.com", "1026", "tgt_u")]


def test_preflight_leaves_a_connection_only_service_out_until_its_host_is_set(monkeypatch,
                                                                               no_side_effects):
    from livetest import preflight
    calls = []
    monkeypatch.setattr(preflight._slt_infra, "resolve_service",
                        lambda infra, svc, env, started, resolve, progress=None: calls.append(svc))
    monkeypatch.setattr(preflight, "ensure_provisioned_once", lambda key, fn: None)
    assert preflight._bring_up_services(["teradata"], {}, infra=object()) == {}
    assert calls == []


# --- the hand-off: a consumer's own teradata service ----------------------------------------------

def _consumer_teradata(root: Path) -> Path:
    d = root / "teradata"
    d.mkdir(parents=True)
    (d / "service.yaml").write_text(
        "name: teradata\ncompose: compose.yaml\ncontainer: slt-teradata\nisolation: none\n"
        "live_override_env: SLT_TERADATA_HOST\n"
        "required_files: [deps/disk1.qcow2, deps/disk2.qcow2, deps/disk3.qcow2]\n"
        "docker_defaults: {host: localhost, port: 1025, user: test_admin, password: fixture_only}\n"
        "docker_env: {port: SLT_TERADATA_HOST_PORT}\n"
        "live_env: {host: SLT_TERADATA_HOST, port: SLT_TERADATA_PORT}\n"
        "provides: {TERADATA_URL: 'jdbc:teradata://{view_host}/DBS_PORT={port}'}\n")
    (d / "compose.yaml").write_text("services:\n  slt-teradata:\n    image: example/td:1\n"
                                    "    container_name: slt-teradata\n")
    return d


def test_a_consumer_teradata_service_replaces_the_connection_only_one(tmp_path, monkeypatch,
                                                                       no_side_effects):
    from livetest import layout, registry
    d = _consumer_teradata(tmp_path / "consumer-services")
    monkeypatch.setattr(layout, "services_roots",
                        lambda: [tmp_path / "consumer-services", REPO / "scripts" / "live" / "services"])
    defn = registry.load_service("teradata")
    assert defn.dir == d and defn.container == "slt-teradata"
    assert registry._hits("teradata") == [d, LIVE]
    missing_files = [d / "deps" / f"disk{n}.qcow2" for n in (1, 2, 3)]
    assert registry.required_paths(defn, {}) == missing_files
    # its disks are its own business: missing ones skip its tests, naming them
    assert registry.unavailable(defn, {}) == (
        f"required files missing from {d}: {', '.join(str(p) for p in missing_files)} "
        f"(supply them, or set SLT_TERADATA_HOST to use an existing instance)")
    (d / "deps").mkdir()
    for n in (1, 2, 3):
        (d / "deps" / f"disk{n}.qcow2").write_bytes(b"")
    assert registry.unavailable(defn, {}) is None


def test_required_files_must_stay_inside_the_service_dir(tmp_path):
    from livetest import registry
    d = tmp_path / "svc"
    d.mkdir()
    (d / "service.yaml").write_text("name: svc\nisolation: none\nrequired_files: [../x]\n")
    with pytest.raises(registry.RegistryError, match="must stay inside the service dir"):
        registry.load_service("svc", services_dir=tmp_path)


# --- integration tier -----------------------------------------------------------------------------

def test_integration_external_host_skips_bring_up_and_resolves_tokens(monkeypatch, no_side_effects):
    from inttest import plugin, tokens

    def never(*_a, **_k):
        raise AssertionError("an external teradata must not be brought up")
    monkeypatch.setattr(plugin._docker_mod, "ensure_up", never)
    env = {key.replace("SLT_", "INT_"): value for key, value in EXTERNAL.items()}
    env["INT_TERADATA_ADMIN_USER"] = env.pop("INT_TERADATA_USER")
    env["INT_TERADATA_ADMIN_PASSWORD"] = env.pop("INT_TERADATA_PASSWORD")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    plugin._provision_requires("case", ["teradata"], lock=None)
    got = tokens.service_tokens("teradata", env)
    assert got["TERADATA_URL"] == "jdbc:teradata://td.example.com/DBS_PORT=1026,TMODE=ANSI,CHARSET=UTF8"
    assert got["TERADATA_SOURCE_USER"] == "src_u"
    assert got["TERADATA_SOURCE_PASSWORD"] == "pw2"


def test_integration_nothing_set_skips_naming_the_setting(monkeypatch, no_side_effects):
    from inttest import plugin
    monkeypatch.setattr(plugin._docker_mod, "ensure_up",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("brought up")))
    monkeypatch.delenv("INT_TERADATA_HOST", raising=False)
    with pytest.raises(pytest.skip.Exception, match="set INT_TERADATA_HOST"):
        plugin._provision_requires("case", ["teradata"], lock=None)
