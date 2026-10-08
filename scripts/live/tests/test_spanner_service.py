import os

from livetest import registry
from livetest.registry import load_service
from livetest.services import resolve
from livetest.plugin import build_service_tokens

def test_spanner_service_def_loads():
    defn = load_service("spanner")
    assert defn.isolation == "none"
    assert defn.container == "slt-spanner"
    assert defn.live_override_env == "SLT_SPANNER_HOST"
    for key in ("SPANNER_PROJECT", "SPANNER_INSTANCE", "SPANNER_GSQL_DB",
                "SPANNER_PG_DB", "SPANNER_GSQL_URL", "SPANNER_PG_URL"):
        assert key in defn.provides

def test_spanner_resolve_docker_defaults():
    r = resolve("spanner", env={}, started=set(), compose_up=lambda defn: None)
    assert r.mode == "docker"
    assert r.base["project"] == "test-project"
    assert r.base["instance"] == "test-inst"
    assert r.base["gsql_db"] == "gsql" and r.base["pg_db"] == "pgdb"
    assert r.base["port"] == 9010
    assert r.base["view_host"] == "localhost"

def test_spanner_resolve_live_override():
    env = {"SLT_SPANNER_HOST": "sp", "SLT_SPANNER_PORT": "9999",
           "SLT_SPANNER_PROJECT": "proj", "SLT_SPANNER_INSTANCE": "inst",
           "SLT_SPANNER_GSQL_DB": "g", "SLT_SPANNER_PG_DB": "p"}
    r = resolve("spanner", env=env, started=set(),
                compose_up=lambda defn: (_ for _ in ()).throw(AssertionError("no compose")))
    assert r.mode == "live" and r.base["project"] == "proj" and r.base["gsql_db"] == "g"

def test_spanner_url_tokens_render_with_view_host():
    defn = load_service("spanner")
    r = resolve("spanner", env={"SLT_STRIIM_VIEW_HOST": "host.docker.internal"},
                started=set(), compose_up=lambda defn: None)
    tokens = build_service_tokens(defn, r, schema="")
    assert tokens["SPANNER_GSQL_URL"] == (
        "jdbc:cloudspanner://host.docker.internal:9010/projects/test-project"
        "/instances/test-inst/databases/gsql?autoConfigEmulator=true")
    assert tokens["SPANNER_PG_URL"].endswith("databases/pgdb?autoConfigEmulator=true")
    assert tokens["SPANNER_INSTANCE"] == "test-inst"


# --- docker_env: the published-port override the harness used to ignore -------------------
# Added 2026-08-19. compose.yaml has always interpolated ${SLT_*_HOST_PORT}, but resolve()
# built its base from docker_defaults alone -- so remapping a busy port moved the container
# and not the client dialing it, and every `requires:` test errored on a connection that
# could not succeed. The bug was found in spanner and is generic: six other services publish
# ports the same way.

def test_docker_env_overrides_the_published_port(monkeypatch):
    from livetest import services
    monkeypatch.setenv("SLT_SPANNER_GRPC_HOST_PORT", "9110")
    monkeypatch.setenv("SLT_SPANNER_REST_HOST_PORT", "9120")
    defn = registry.load_service("spanner")
    base = dict(defn.docker_defaults)
    for key, envname in defn.docker_env.items():
        override = (os.environ.get(envname) or "").strip()
        if override:
            base[key] = override
    assert base["port"] == "9110", "the admin client must follow the remapped gRPC port"
    assert base["admin_port"] == "9120"


def test_docker_env_absent_leaves_the_default(monkeypatch):
    monkeypatch.delenv("SLT_SPANNER_GRPC_HOST_PORT", raising=False)
    defn = registry.load_service("spanner")
    base = dict(defn.docker_defaults)
    for key, envname in defn.docker_env.items():
        if (os.environ.get(envname) or "").strip():
            base[key] = os.environ[envname]
    assert base["port"] == 9010, "an unset override must not disturb the default"


def test_every_service_publishing_an_overridable_port_declares_docker_env():
    """The gap was generic: spanner was fixed and six siblings had the identical shape."""
    import re
    from pathlib import Path
    services_dir = Path(__file__).resolve().parents[1] / "services"
    missing = []
    for compose in sorted(services_dir.glob("*/compose.yaml")):
        # services/striim is the CLUSTER, not a `requires:`-able service -- it ships a compose
        # file and no service.yaml, so there is no docker_env for it to declare.
        if not (compose.parent / "service.yaml").exists():
            continue
        published = re.findall(r"\$\{(SLT_[A-Z_]*HOST_PORT)", compose.read_text())
        if not published:
            continue
        declared = set(registry.load_service(compose.parent.name).docker_env.values())
        for var in published:
            if var not in declared:
                missing.append(f"{compose.parent.name}:{var}")
    assert not missing, (
        "these services publish an overridable host port that resolve() will ignore, so the "
        "container moves and its client does not: " + ", ".join(missing))
