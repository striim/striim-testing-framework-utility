"""services/vertica-eon and services/minio, hermetically: definitions, tokens, overrides, and the
values compose.yaml, service.yaml, entrypoint.sh and init.sql must agree on (as test_vertica_service)."""
import re
import subprocess
from pathlib import Path

import yaml

from livetest.plugin import build_service_tokens
from livetest.registry import load_service
from livetest.services import resolve

SERVICES = Path(__file__).resolve().parents[1] / "services"
EON = SERVICES / "vertica-eon"
IMAGE = EON / "images" / "vertica-eon"
MINIO = SERVICES / "minio"


def _up(name, env=None):
    return resolve(name, env=env or {}, started=set(), compose_up=lambda d: None, post_up=lambda d: None)


def _compose(d: Path) -> dict:
    return yaml.safe_load((d / "compose.yaml").read_text())


def _node(n: int) -> dict:
    return _compose(EON)["services"][f"slt-vertica-eon-{n}"]


def _entrypoint() -> str:
    return (IMAGE / "entrypoint.sh").read_text()


def _entrypoint_var(name: str) -> str:
    m = re.search(rf"^{name}=(\S+)$", _entrypoint(), re.MULTILINE)
    assert m, f"entrypoint.sh must define {name}=<value>"
    return m.group(1)


def _defaults(d: Path = EON) -> dict:
    return yaml.safe_load((d / "service.yaml").read_text())["docker_defaults"]


def _bash(expr: str, env: dict) -> str:
    """A compose ${...} expression, rendered the way compose does (the forms used are bash's too)."""
    return subprocess.check_output(["bash", "-c", f'printf "%s" "{expr}"'], env=env, text=True)


# ---- definitions and tokens ------------------------------------------------------------------

def test_vertica_eon_service_def_loads():
    defn = load_service("vertica-eon")
    assert defn.isolation == "none"
    assert defn.container == "slt-vertica-eon-1"
    assert defn.live_override_env == "SLT_VERTICA_EON_HOST"
    assert defn.depends_on == ["minio"]
    assert defn.opt_in_env is None
    for key in ("VERTICA_EON_HOST", "VERTICA_EON_PORT", "VERTICA_EON_SECONDARY_PORT", "VERTICA_EON_DB",
                "VERTICA_EON_URL", "VERTICA_EON_SECONDARY_URL",
                "VERTICA_EON_SOURCE_USER", "VERTICA_EON_SOURCE_PASSWORD", "VERTICA_EON_SOURCE_SCHEMA",
                "VERTICA_EON_TARGET_USER", "VERTICA_EON_TARGET_PASSWORD", "VERTICA_EON_TARGET_SCHEMA"):
        assert key in defn.provides
    # distinct from the one-node service's tokens, so a case can require both
    assert not set(defn.provides) & set(load_service("vertica").provides)


def test_vertica_eon_tokens_from_docker_defaults():
    r = _up("vertica-eon", {"SLT_STRIIM_VIEW_HOST": "host.docker.internal"})
    assert r.mode == "docker" and r.base["host"] == "localhost"
    t = build_service_tokens(load_service("vertica-eon"), r, schema="")
    assert t["VERTICA_EON_URL"] == "jdbc:vertica://host.docker.internal:25433/sltdb"
    assert t["VERTICA_EON_SECONDARY_URL"] == "jdbc:vertica://host.docker.internal:25434/sltdb"
    assert (t["VERTICA_EON_SOURCE_USER"], t["VERTICA_EON_SOURCE_SCHEMA"]) == ("qasource", "qasource")
    assert (t["VERTICA_EON_TARGET_USER"], t["VERTICA_EON_TARGET_SCHEMA"]) == ("qatarget", "qatarget")
    assert t["VERTICA_EON_TARGET_PASSWORD"] == "striim"


def test_vertica_eon_remapped_host_ports_move_the_client():
    r = _up("vertica-eon", {"SLT_VERTICA_EON_HOST_PORT": "26433", "SLT_VERTICA_EON_SECONDARY_HOST_PORT": "26434"})
    assert (str(r.base["port"]), str(r.base["secondary_port"])) == ("26433", "26434")


def test_vertica_eon_live_override():
    env = {"SLT_VERTICA_EON_HOST": "eonhost", "SLT_VERTICA_EON_PORT": "5433",
           "SLT_VERTICA_EON_SECONDARY_PORT": "5434", "SLT_VERTICA_EON_DB": "custdb",
           "SLT_VERTICA_EON_TARGET_USER": "tgt", "SLT_VERTICA_EON_VIEW_HOST": "eon.vpn"}
    r = resolve("vertica-eon", env=env, started=set(),
                compose_up=lambda d: (_ for _ in ()).throw(AssertionError("should not compose up")))
    assert r.mode == "live" and r.started is False
    assert (r.base["host"], r.base["port"], r.base["secondary_port"]) == ("eonhost", "5433", "5434")
    assert (r.base["dbname"], r.base["target_user"], r.base["source_user"]) == ("custdb", "tgt", "qasource")
    # SLT_<NAME>_VIEW_HOST, with the name's dash as an underscore
    assert r.base["view_host"] == "eon.vpn"


def test_minio_service_def_and_tokens():
    defn = load_service("minio")
    assert defn.container == "slt-minio" and defn.depends_on == []
    assert defn.live_override_env is None          # test-only: always the container
    r = _up("minio", {"SLT_STRIIM_VIEW_HOST": "host.docker.internal", "SLT_MINIO_HOST_PORT": "19999"})
    t = build_service_tokens(defn, r, schema="")
    assert t["MINIO_ENDPOINT"] == "http://host.docker.internal:19999"
    assert (t["MINIO_ACCESS_KEY"], t["MINIO_SECRET_KEY"]) == ("minioadmin", "minioadmin-test")


# ---- compose, entrypoint and init.sql agree ---------------------------------------------------

def test_published_ports_match_service_defaults():
    d = _defaults()
    assert f"${{SLT_BIND_ADDRESS:-0.0.0.0}}:${{SLT_VERTICA_EON_HOST_PORT:-{d['port']}}}:5433" in _node(1)["ports"]
    assert (f"${{SLT_BIND_ADDRESS:-0.0.0.0}}:${{SLT_VERTICA_EON_SECONDARY_HOST_PORT:-{d['secondary_port']}}}:5433"
            in _node(2)["ports"])
    m = _compose(MINIO)["services"]["slt-minio"]
    assert f"${{SLT_BIND_ADDRESS:-0.0.0.0}}:${{SLT_MINIO_HOST_PORT:-{_defaults(MINIO)['port']}}}:9000" in m["ports"]


def test_host_ports_are_distinct_from_every_other_service():
    ours = {str(v) for d in (EON, MINIO) for k, v in _defaults(d).items() if k.endswith("port")}
    others = set()
    for tier in ("live", "integration"):
        for f in (SERVICES.parents[1] / tier / "services").glob("*/service.yaml"):
            if f.parent in (EON, MINIO):
                continue
            raw = yaml.safe_load(f.read_text()) or {}
            others |= {str(v) for k, v in (raw.get("docker_defaults") or {}).items() if k.endswith("port")}
    assert ours == {"25433", "25434", "19000"} and not ours & others


def test_names_and_certificates_follow_the_stack_prefix():
    for n in (1, 2):
        svc = _node(n)
        assert svc["container_name"] == svc["hostname"] == f"${{SLT_STACK_PREFIX:+${{SLT_STACK_PREFIX}}-}}slt-vertica-eon-{n}"
    env = _node(1)["environment"]
    assert _bash(env["EON_PRIMARY_HOST"], {"SLT_STACK_PREFIX": "eontest"}) == "eontest-slt-vertica-eon-1"
    assert _bash(env["EON_SECONDARY_HOST"], {}) == "slt-vertica-eon-2"
    # the certificate names both hosts and both node IPs
    text = _entrypoint()
    assert 'DNS:$PRIMARY,DNS:$SECONDARY' in text and 'for ip in ${EON_CERT_IPS:-}' in text


def test_node_ips_and_cert_ips_come_from_one_subnet_setting():
    doc = _compose(EON)
    subnet = doc["networks"]["slt-vertica-eon-net"]["ipam"]["config"][0]["subnet"]
    ips = [_node(n)["networks"]["slt-vertica-eon-net"]["ipv4_address"] for n in (1, 2)]
    cert_ips = _node(1)["environment"]["EON_CERT_IPS"]
    for env, base in (({}, "172.16.250"), ({"SLT_VERTICA_EON_SUBNET": "172.16.251"}, "172.16.251")):
        assert _bash(subnet, env) == f"{base}.0/24"
        assert [_bash(ip, env) for ip in ips] == [f"{base}.11", f"{base}.12"]
        assert _bash(cert_ips, env) == f"{base}.11 {base}.12"


def test_s3_endpoint_follows_the_minio_port():
    env = _node(1)["environment"]
    assert _bash(env["EON_S3_ENDPOINT"], {"SLT_MINIO_HOST_PORT": "19999"}) == "host.docker.internal:19999"
    assert _node(1)["extra_hosts"] == ["host.docker.internal:host-gateway"]
    m = _compose(MINIO)["services"]["slt-minio"]["environment"]
    assert (env["EON_S3_ACCESS_KEY"], env["EON_S3_SECRET_KEY"]) == (m["MINIO_ROOT_USER"], m["MINIO_ROOT_PASSWORD"])


def test_healthcheck_marker_database_and_password_match_entrypoint():
    d = _defaults()
    for n in (1, 2):
        test = _node(n)["healthcheck"]["test"][1]
        assert _entrypoint_var("DONE_MARKER") in test
        assert f"-d {d['dbname']}" in test and "-U qasource" in test and "-U dbadmin" not in test
    assert _entrypoint_var("DB") == d["dbname"]
    assert _entrypoint_var("PASSWORD") == d["admin_password"]


def test_entrypoint_clears_marker_before_writing_it():
    text = _entrypoint()
    assert -1 < text.find('rm -f "$DONE_MARKER"') < text.find('touch "$DONE_MARKER"')


def test_a_partial_first_boot_says_to_recreate():
    text = _entrypoint()
    assert 'fail "/data/$DB exists but the first boot did not finish; $RECREATE"' in text
    assert "RECREATE=\"recreate the stack: python -m livetest.cli stop vertica-eon" in text
    # the marker of a finished first boot is written only after init.sql
    assert text.find("-f /usr/config/init.sql") < text.find('touch "$CREATED"')


def test_init_sql_creates_the_service_accounts_and_schemas():
    d = _defaults()
    sql = (IMAGE / "init.sql").read_text()
    for role in ("source", "target"):
        user, schema = d[f"{role}_user"], d[f"{role}_schema"]
        assert f"CREATE USER {user} IDENTIFIED BY '{d[f'{role}_password']}';" in sql
        assert f"CREATE SCHEMA {schema} AUTHORIZATION {user};" in sql


def test_resources_and_base_image():
    for n in (1, 2):
        svc = _node(n)
        assert svc["ulimits"]["nofile"] == {"soft": 32768, "hard": 32768}
        assert _bash(svc["mem_limit"], {}) == "2g"
        assert _bash(svc["build"]["args"]["BASE"], {}) == "opentext/vertica-k8s:25.3.0-8-multiarch"
    assert "ARG BASE=opentext/vertica-k8s:25.3.0-8-multiarch" in (IMAGE / "Dockerfile").read_text()
    m = _compose(MINIO)["services"]["slt-minio"]
    assert _bash(m["mem_limit"], {}) == "512m"
    assert re.fullmatch(r"cgr\.dev/chainguard/minio@sha256:[0-9a-f]{64}", m["image"])
    assert "/minio/health/live" in m["healthcheck"]["test"][-1]
