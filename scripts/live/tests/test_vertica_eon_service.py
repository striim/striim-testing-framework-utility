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


# ---- dependency order ------------------------------------------------------------------------

def test_teardown_order_puts_a_dependent_first_and_adds_nothing():
    from livetest import registry
    assert registry.teardown_order(["minio", "postgres", "vertica-eon"]) == ["postgres", "vertica-eon", "minio"]
    assert registry.teardown_order(["minio"]) == ["minio"]
    assert registry.teardown_order(["vertica-eon", "minio"]) == ["vertica-eon", "minio"]
    assert registry.dependents("minio") == ["vertica-eon"]
    assert registry.dependents("vertica-eon") == []


def test_teardown_order_keeps_services_without_dependencies_in_their_given_order():
    from livetest import registry
    names = ["postgres", "oracle", "kafka", "mssql"]
    assert registry.teardown_order(names) == names


def test_a_fresh_minio_bring_up_resets_vertica_eon_first(monkeypatch):
    import types
    from livetest import services as services_mod
    monkeypatch.delenv("SLT_KEEP_SERVICES", raising=False)
    monkeypatch.setenv("SLT_BIND_ADDRESS", "127.0.0.1")
    resets, forgotten = [], []
    monkeypatch.setattr(services_mod, "_preflight_profile", lambda defn: None)
    monkeypatch.setattr(services_mod, "_compose_reset", lambda compose: resets.append(Path(compose).parent.name))
    monkeypatch.setattr(services_mod, "forget_provisioned", lambda keys, state_dir=None: forgotten.extend(keys))
    monkeypatch.setattr(services_mod.dockercmd, "run",
                        lambda argv, **kw: types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    services_mod._default_compose_up(load_service("minio"))
    assert resets == ["vertica-eon", "minio"]
    assert forgotten == ["slt-vertica-eon-1", "vertica-eon-setup"]
    resets.clear()
    services_mod._default_compose_up(load_service("vertica-eon"))
    assert resets == ["vertica-eon"]


def test_vertica_eon_service_def_loads():
    defn = load_service("vertica-eon")
    assert defn.isolation == "none"
    assert defn.container == "slt-vertica-eon-1"
    assert defn.live_override_env == "SLT_VERTICA_EON_HOST"
    assert defn.depends_on == ["minio"]
    assert defn.opt_in_env is None   # start all includes it: a required service must never be gated out
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


def test_a_stale_created_marker_is_cleared_before_create_db():
    text = _entrypoint()
    assert -1 < text.find('rm -f "$CREATED"') < text.find('"$VCLUSTER" create_db') < text.find('touch "$CREATED"')


def test_the_secondary_waits_for_this_life_of_the_primary_before_start_node():
    text = _entrypoint()
    primary, secondary = text.split('\nelse\n  # On a full start')
    primary = primary[primary.find('if [ "$ROLE" = primary ]; then'):]
    assert _entrypoint_var("READY") == "$SHARED/.primary-ready"
    assert _entrypoint_var("BOOT") == "$SHARED/.primary-boot"
    # READY cleared at entry (and on SIGTERM), then this life's token to BOOT, and the same
    # token to READY once start_db or create is over
    write_ready = 'printf \'%s\\n\' "$TOKEN" > "$READY.tmp"'
    assert -1 < primary.find('rm -f "$READY"') < primary.find('> "$BOOT.tmp"') < primary.find(write_ready)
    assert primary.find('"$VCLUSTER" start_db') < primary.rfind(write_ready)
    wait = secondary.find('wait_primary_life 600 || fail')
    assert -1 < wait < secondary.find("node_state") < secondary.find('"$VCLUSTER" start_node')


def _wait_primary_life(tmp_path, boot, ready, answers, steps=(), n=4):
    """Run entrypoint.sh's wait_primary_life against BOOT/READY files in tmp_path. ``answers``:
    whether the primary answers SQL; ``steps``: (file, content) written at each sleep, in turn."""
    fn = re.search(r"^wait_primary_life\(\) \{\n.*?^\}\n", _entrypoint(), re.MULTILINE | re.DOTALL).group(0)
    for name, content in (("boot", boot), ("ready", ready)):
        if content is not None:
            (tmp_path / name).write_text(content + "\n")
    script = "\n".join([
        f'BOOT={tmp_path}/boot READY={tmp_path}/ready PRIMARY=p i=0',
        f'sql() {{ {"true" if answers else "false"}; }}',
        "steps=(" + " ".join(f"'{f}={c}'" for f, c in steps) + ")",
        'sleep() { local s=${steps[$i]:-}; i=$((i+1)); [ -n "$s" ] && printf "%s\\n" "${s#*=}" > "'
        + str(tmp_path) + '/${s%%=*}"; return 0; }',
        fn, f"wait_primary_life {n}"])
    return subprocess.run(["bash", "-c", script]).returncode


def test_the_secondary_ignores_the_ready_file_a_killed_primary_left(tmp_path):
    # stale pair, primary not answering and never starting: times out
    assert _wait_primary_life(tmp_path, "t1", "t1", answers=False) == 1
    # stale pair, then the primary's new life: READY removed, new BOOT, READY written
    assert _wait_primary_life(tmp_path, "t1", "t1", answers=False,
                              steps=[("ready", ""), ("boot", "t2"), ("ready", "t2")]) == 0


def test_the_secondary_waits_for_ready_to_match_this_lifes_boot(tmp_path):
    assert _wait_primary_life(tmp_path, "t2", None, answers=True) == 1
    assert _wait_primary_life(tmp_path, "t2", "t1", answers=True) == 1
    assert _wait_primary_life(tmp_path, None, None, answers=True) == 1


def test_a_secondary_restarted_alone_accepts_the_live_primarys_ready(tmp_path):
    assert _wait_primary_life(tmp_path, "t1", "t1", answers=True, n=1) == 0


def test_the_primary_fails_when_the_secondary_nma_is_down():
    assert 'wait_for 120 nma_up "$SECONDARY" || fail' in _entrypoint()


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
