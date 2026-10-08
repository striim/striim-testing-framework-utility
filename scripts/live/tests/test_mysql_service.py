from livetest.registry import load_service
from livetest.services import resolve
from livetest.plugin import build_service_tokens


def test_mysql_service_def_loads():
    defn = load_service("mysql")
    assert defn.isolation == "none"
    assert defn.container == "slt-mysql"
    assert defn.live_override_env == "SLT_MYSQL_HOST"
    # the token surface the examples rely on
    for key in ("MYSQL_HOST", "MYSQL_PORT", "MYSQL_URL", "MYSQL_SOURCE_USER",
                "MYSQL_TARGET_USER", "MYSQL_SOURCE_SCHEMA", "MYSQL_TARGET_SCHEMA"):
        assert key in defn.provides


def test_mysql_resolve_docker_defaults():
    started = set()
    r = resolve("mysql", env={}, started=started, compose_up=lambda defn: None, post_up=lambda defn: None)
    assert r.mode == "docker" and r.started is True
    assert r.base["host"] == "localhost"
    assert r.base["port"] == 3306
    assert r.base["admin_user"] == "root"
    assert r.base["source_user"] == "qasource"
    assert r.base["target_user"] == "qatarget"
    assert r.base["source_schema"] == "qasource"
    assert r.base["target_schema"] == "qatarget"
    assert "slt-mysql" in started


def test_mysql_resolve_live_override():
    env = {
        "SLT_MYSQL_HOST": "mysqlhost",
        "SLT_MYSQL_PORT": "3307",
        "SLT_MYSQL_ADMIN_USER": "admin",
        "SLT_MYSQL_ADMIN_PASSWORD": "adminpw",
        "SLT_MYSQL_SOURCE_USER": "src",
        "SLT_MYSQL_SOURCE_PASSWORD": "srcpw",
        "SLT_MYSQL_SOURCE_SCHEMA": "src_db",
        "SLT_MYSQL_TARGET_USER": "tgt",
        "SLT_MYSQL_TARGET_PASSWORD": "tgtpw",
        "SLT_MYSQL_TARGET_SCHEMA": "tgt_db",
    }
    r = resolve("mysql", env=env, started=set(),
                compose_up=lambda defn: (_ for _ in ()).throw(AssertionError("should not compose up")))
    assert r.mode == "live" and r.started is False
    assert r.base["host"] == "mysqlhost"
    assert r.base["port"] == "3307"
    assert r.base["source_user"] == "src"
    assert r.base["target_user"] == "tgt"


def test_mysql_view_host_for_docker_striim():
    r = resolve("mysql", env={"SLT_STRIIM_VIEW_HOST": "host.docker.internal"},
                started=set(), compose_up=lambda defn: None, post_up=lambda defn: None)
    assert r.base["view_host"] == "host.docker.internal"


def test_mysql_url_token_renders_with_view_host():
    defn = load_service("mysql")
    r = resolve("mysql", env={"SLT_STRIIM_VIEW_HOST": "host.docker.internal"},
                started=set(), compose_up=lambda defn: None, post_up=lambda defn: None)
    tokens = build_service_tokens(defn, r, schema="")
    assert tokens["MYSQL_HOST"] == "host.docker.internal"
    assert tokens["MYSQL_PORT"] == "3306"
    assert tokens["MYSQL_URL"] == "jdbc:mysql://host.docker.internal:3306/"
    assert tokens["MYSQL_SOURCE_USER"] == "qasource"
    assert tokens["MYSQL_TARGET_USER"] == "qatarget"
    assert tokens["MYSQL_SOURCE_SCHEMA"] == "qasource"
    assert tokens["MYSQL_TARGET_SCHEMA"] == "qatarget"
