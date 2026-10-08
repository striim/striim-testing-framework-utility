import re
from pathlib import Path

import yaml

from livetest.registry import load_service
from livetest.services import resolve
from livetest.plugin import build_service_tokens

SERVICE_DIR = Path(__file__).resolve().parents[1] / "services" / "vertica"

# ---- service definition + token rendering ----------------------------------

def test_vertica_service_def_loads():
    defn = load_service("vertica")
    assert defn.isolation == "none"
    assert defn.container == "slt-vertica"
    assert defn.live_override_env == "SLT_VERTICA_HOST"
    assert defn.opt_in_env is None
    for key in ("VERTICA_HOST", "VERTICA_PORT", "VERTICA_DB", "VERTICA_URL",
                "VERTICA_SOURCE_USER", "VERTICA_SOURCE_PASSWORD", "VERTICA_SOURCE_SCHEMA",
                "VERTICA_TARGET_USER", "VERTICA_TARGET_PASSWORD", "VERTICA_TARGET_SCHEMA"):
        assert key in defn.provides


def test_vertica_resolve_docker_defaults():
    started = set()
    r = resolve("vertica", env={}, started=started, compose_up=lambda defn: None, post_up=lambda defn: None)
    assert r.mode == "docker" and r.started is True
    assert r.base["host"] == "localhost"
    assert r.base["port"] == 5433
    assert r.base["dbname"] == "sltdb"
    assert r.base["admin_user"] == "dbadmin"
    assert r.base["source_user"] == "qasource"
    assert r.base["target_user"] == "qatarget"
    assert r.base["source_schema"] == "qasource"
    assert r.base["target_schema"] == "qatarget"
    assert "slt-vertica" in started


def test_vertica_resolve_live_override():
    env = {
        "SLT_VERTICA_HOST": "verticahost",
        "SLT_VERTICA_PORT": "5434",
        "SLT_VERTICA_DB": "custdb",
        "SLT_VERTICA_ADMIN_USER": "admin",
        "SLT_VERTICA_ADMIN_PASSWORD": "adminpw",
        "SLT_VERTICA_SOURCE_USER": "src",
        "SLT_VERTICA_SOURCE_PASSWORD": "srcpw",
        "SLT_VERTICA_SOURCE_SCHEMA": "src_schema",
        "SLT_VERTICA_TARGET_USER": "tgt",
        "SLT_VERTICA_TARGET_PASSWORD": "tgtpw",
        "SLT_VERTICA_TARGET_SCHEMA": "tgt_schema",
    }
    r = resolve("vertica", env=env, started=set(),
                compose_up=lambda defn: (_ for _ in ()).throw(AssertionError("should not compose up")))
    assert r.mode == "live" and r.started is False
    assert r.base["host"] == "verticahost"
    assert r.base["port"] == "5434"
    assert r.base["dbname"] == "custdb"
    assert r.base["source_user"] == "src"
    assert r.base["target_user"] == "tgt"
    assert r.base["target_schema"] == "tgt_schema"


def test_vertica_remapped_host_port_moves_the_client():
    r = resolve("vertica", env={"SLT_VERTICA_HOST_PORT": "15999"}, started=set(),
                compose_up=lambda defn: None, post_up=lambda defn: None)
    assert str(r.base["port"]) == "15999"


def test_vertica_view_host_for_docker_striim():
    r = resolve("vertica", env={"SLT_STRIIM_VIEW_HOST": "host.docker.internal"},
                started=set(), compose_up=lambda defn: None, post_up=lambda defn: None)
    assert r.base["view_host"] == "host.docker.internal"


def test_vertica_url_token_renders_with_view_host():
    defn = load_service("vertica")
    r = resolve("vertica", env={"SLT_STRIIM_VIEW_HOST": "host.docker.internal"},
                started=set(), compose_up=lambda defn: None, post_up=lambda defn: None)
    tokens = build_service_tokens(defn, r, schema="")
    assert tokens["VERTICA_HOST"] == "host.docker.internal"
    assert tokens["VERTICA_PORT"] == "5433"
    assert tokens["VERTICA_DB"] == "sltdb"
    assert tokens["VERTICA_URL"] == "jdbc:vertica://host.docker.internal:5433/sltdb"
    assert tokens["VERTICA_SOURCE_USER"] == "qasource"
    assert tokens["VERTICA_TARGET_USER"] == "qatarget"
    assert tokens["VERTICA_SOURCE_SCHEMA"] == "qasource"
    assert tokens["VERTICA_TARGET_SCHEMA"] == "qatarget"

# ---- entrypoint/healthcheck/init consistency --------------------------------
#
# compose.yaml, service.yaml, entrypoint.sh and init.sql each hardcode values the others rely
# on (the marker path, the database name, the account names and passwords) with no shared
# constant between them: a shell healthcheck cannot import Python, and vice versa. A typo in
# one would show only as "never healthy" or "cannot log in". These tests are the tripwire, as
# in test_mssql.py.

def _compose_service() -> dict:
    return yaml.safe_load((SERVICE_DIR / "compose.yaml").read_text())["services"]["slt-vertica"]


def _healthcheck_test() -> str:
    return _compose_service()["healthcheck"]["test"][1]


def _entrypoint_text() -> str:
    return (SERVICE_DIR / "images" / "vertica" / "entrypoint.sh").read_text()


def _entrypoint_var(name: str) -> str:
    m = re.search(rf"^{name}=(\S+)$", _entrypoint_text(), re.MULTILINE)
    assert m, f"entrypoint.sh must define {name}=<value>"
    return m.group(1)


def _defaults() -> dict:
    return yaml.safe_load((SERVICE_DIR / "service.yaml").read_text())["docker_defaults"]


def test_healthcheck_marker_path_matches_entrypoint():
    assert _entrypoint_var("DONE_MARKER") in _healthcheck_test()


def test_entrypoint_clears_marker_before_writing_it():
    text = _entrypoint_text()
    clear_idx = text.find('rm -f "$DONE_MARKER"')
    write_idx = text.find('touch "$DONE_MARKER"')
    assert clear_idx != -1, (
        "entrypoint.sh must clear DONE_MARKER at boot -- /data has no volume, so the marker "
        "survives docker stop/start; a stale one lets the healthcheck go green before THIS "
        "boot's database is up.")
    assert write_idx != -1, "entrypoint.sh must touch DONE_MARKER once the database is up"
    assert clear_idx < write_idx, "the clear must happen before the write, not after"


def test_healthcheck_probes_a_data_account_login():
    test_cmd = _healthcheck_test()
    assert "-U qasource" in test_cmd or "-U qatarget" in test_cmd
    assert "-U dbadmin" not in test_cmd


def test_entrypoint_database_and_password_match_service_defaults():
    defaults = _defaults()
    assert _entrypoint_var("DB") == defaults["dbname"]
    assert _entrypoint_var("PASSWORD") == defaults["admin_password"]
    assert f"-d {defaults['dbname']}" in _healthcheck_test()


def test_init_sql_creates_the_service_accounts_and_schemas():
    defaults = _defaults()
    sql = (SERVICE_DIR / "images" / "vertica" / "init.sql").read_text()
    for role in ("source", "target"):
        user, schema = defaults[f"{role}_user"], defaults[f"{role}_schema"]
        assert f"CREATE USER {user} IDENTIFIED BY '{defaults[f'{role}_password']}';" in sql
        assert f"CREATE SCHEMA {schema} AUTHORIZATION {user};" in sql


def test_published_port_matches_service_defaults():
    port = _defaults()["port"]
    assert f"${{SLT_VERTICA_HOST_PORT:-{port}}}:5433" in _compose_service()["ports"]


def test_both_tiers_declare_the_required_open_file_limit():
    for tier, name in (("live", "slt-vertica"), ("integration", "int-vertica")):
        compose = SERVICE_DIR.parents[2] / tier / "services" / "vertica" / "compose.yaml"
        service = yaml.safe_load(compose.read_text())["services"][name]
        assert service.get("ulimits", {}).get("nofile") == {"soft": 32768, "hard": 32768}
