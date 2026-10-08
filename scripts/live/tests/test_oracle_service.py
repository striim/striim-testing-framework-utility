from livetest.registry import load_service
from livetest.services import resolve
from livetest.plugin import build_service_tokens

def test_oracle_service_def_loads():
    defn = load_service("oracle")
    assert defn.isolation == "none"
    assert defn.container == "slt-oracle"
    assert defn.live_override_env == "SLT_ORA_HOST"
    # the CDC token surface the examples rely on
    for key in ("ORACLE_URL", "ORACLE_CDC_URL", "ORACLE_SOURCE_USER", "ORACLE_TARGET_USER",
                "ORACLE_CDC_USER", "ORACLE_CDC_PASSWORD"):
        assert key in defn.provides
    assert defn.post_up is None   # archivelog + setup are baked into the image (no runtime restart)

def test_oracle_resolve_docker_defaults():
    started = set()
    r = resolve("oracle", env={}, started=started, compose_up=lambda defn: None, post_up=lambda defn: None)
    assert r.mode == "docker" and r.started is True
    assert r.base["service"] == "FREEPDB1"              # PDB — data users
    assert r.base["cdb_service"] == "FREE"              # CDB root — CDC reader
    assert r.base["cdc_user"] == "c##striim"            # common user for PDB LogMiner CDC
    assert r.base["view_host"] == "localhost"           # native Striim default
    assert "slt-oracle" in started

def test_oracle_resolve_live_override():
    env = {
        "SLT_ORA_HOST": "orahost", "SLT_ORA_PORT": "1522",
        "SLT_ORA_SERVICE": "ORCLPDB", "SLT_ORA_USER": "app",
        "SLT_ORA_PASSWORD": "pw", "SLT_ORA_CDC_USER": "cdc",
        "SLT_ORA_CDC_PASSWORD": "cdcpw",
    }
    r = resolve("oracle", env=env, started=set(), compose_up=lambda defn: (_ for _ in ()).throw(AssertionError("should not compose up")))
    assert r.mode == "live" and r.started is False
    assert r.base["host"] == "orahost" and r.base["service"] == "ORCLPDB"
    assert r.base["cdc_user"] == "cdc"

def test_oracle_view_host_for_docker_striim():
    r = resolve("oracle", env={"SLT_STRIIM_VIEW_HOST": "host.docker.internal"},
                started=set(), compose_up=lambda defn: None, post_up=lambda defn: None)
    assert r.base["view_host"] == "host.docker.internal"

def test_oracle_url_token_renders_with_view_host():
    defn = load_service("oracle")
    r = resolve("oracle", env={"SLT_STRIIM_VIEW_HOST": "host.docker.internal"},
                started=set(), compose_up=lambda defn: None, post_up=lambda defn: None)
    tokens = build_service_tokens(defn, r, schema="")
    assert tokens["ORACLE_URL"] == "jdbc:oracle:thin:@//host.docker.internal:1521/FREEPDB1"
    assert tokens["ORACLE_CDC_URL"] == "jdbc:oracle:thin:@//host.docker.internal:1521/FREE"
    assert tokens["ORACLE_CDC_USER"] == "c##striim"
