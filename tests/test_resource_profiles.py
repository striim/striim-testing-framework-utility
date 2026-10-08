"""Service-profile selection, tier semantics, deliberate overlays and the path-key roots.

Ported from the legacy framework repo. Hermetic: real service trees are only READ; every
workload subprocess is trapped. tests/conftest.py puts ``scripts/live`` and
``scripts/integration`` on the path.

The engine call sites that run this preflight (integration services/tokens/cli/plugin and
live services) are tested at the end. Not here: the registry overlay (``project-roots``) and
the installed-mode ``_builtin`` trees, which are wheel-only and not ported.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from filelock import FileLock

from livetest import paths as live_paths
from livetest import registry
from livetest import resource_profiles as rp
from livetest import services as live_services
from inttest import paths as int_paths
from inttest import cli, resources, services as int_services, tokens

pytest_plugins = ["pytester"]

REPO = Path(__file__).resolve().parents[1]
_REAL_RUN, _REAL_POPEN = subprocess.run, subprocess.Popen   # the autouse trap below replaces both
CHECKOUT = {"live": REPO / "scripts/live/services",
            "integration": REPO / "scripts/integration/services"}


def _profiles(tier: str) -> list[str]:
    """Every profile the checkout ships for ``tier`` (services and the ratified cluster recipe)."""
    return rp.list_profiles(tier, roots=[CHECKOUT[tier]],
                            kinds=(rp.KIND_SERVICE, rp.KIND_CLUSTER_RECIPE))


@pytest.fixture(autouse=True)
def _no_workloads(monkeypatch):
    def trap(*args, **kwargs):
        pytest.fail(f"workload subprocess attempted: {args[:1]!r}")
    monkeypatch.setattr(subprocess, "run", trap)
    monkeypatch.setattr(subprocess, "Popen", trap)


def _write(root: Path, files: dict) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


DEMO = {
    "demo/service.yaml": "name: demo\nisolation: none\ncompose: compose.yaml\n",
    "demo/compose.yaml": ("services:\n  demo:\n    image: x\n    volumes:\n"
                          "      - ./sql/init.sql:/init.sql:ro\n"),
    "demo/sql/init.sql": "select 1;\n",
}


def _svc_yaml(tier: str, name: str) -> dict:
    return yaml.safe_load(rp.select_profile(tier, name, services_dir=CHECKOUT[tier])
                          .file("service.yaml").read_text())


def _compose(tier: str, name: str) -> dict:
    return yaml.safe_load((CHECKOUT[tier] / name / "compose.yaml").read_text())


# ---------------------------------------------------------------------------
# Live and integration profiles resolve without a private checkout
# ---------------------------------------------------------------------------

def _tracked(tier: str) -> dict:
    """``{profile name: {origin-relative path}}`` of the files git tracks under the tier's services,
    minus the directories a profile never ships (``deps/`` keeps only its tracked .gitignore)."""
    prefix = f"scripts/{tier}/services/"
    out = subprocess.run(["git", "-C", str(REPO), "ls-files", "--", prefix],
                         capture_output=True, text=True, check=True).stdout.splitlines()
    tracked: dict = {}
    for path in out:
        name, _, rel = path[len(prefix):].partition("/")
        if rel and not set(rel.split("/")[:-1]) & rp._EXCLUDED_DIRS:
            tracked.setdefault(name, set()).add(rel)
    return tracked


@pytest.mark.parametrize("tier", ["live", "integration"])
def test_every_shipped_profile_selects_and_passes_preflight(tier, monkeypatch):
    monkeypatch.setattr(subprocess, "run", _REAL_RUN)        # one read-only `git ls-files`
    monkeypatch.setattr(subprocess, "Popen", _REAL_POPEN)
    root = CHECKOUT[tier]
    names = _profiles(tier)
    assert names, f"{root} ships no profiles"
    tracked = _tracked(tier)
    for name in names:
        prof = rp.select_profile(tier, name, services_dir=root)
        assert prof.origin == root / name
        want = rp.KIND_CLUSTER_RECIPE if (tier, name) == ("live", "striim") else rp.KIND_SERVICE
        assert prof.kind == want
        assert prof.assets and all(not rel.startswith("deps/") for rel, _m, _s in prof.assets)
        # exactly what the repo ships: nothing dropped, nothing untracked picked up
        assert {rel for rel, _m, _s in prof.assets} == tracked[name], f"{tier}/{name}"


# ---------------------------------------------------------------------------
# Tier semantics are preserved, never aliased or merged
# ---------------------------------------------------------------------------

def test_sqlserver_identifiers_are_not_aliased():
    with pytest.raises(rp.ProfileError, match="unknown service profile 'sqlserver'"):
        rp.select_profile("live", "sqlserver", services_dir=CHECKOUT["live"])
    with pytest.raises(rp.ProfileError, match="unknown service profile 'mssql'"):
        rp.select_profile("integration", "mssql", services_dir=CHECKOUT["integration"])


def test_sqlserver_routes_and_token_vocabularies():
    live, integ = _svc_yaml("live", "mssql"), _svc_yaml("integration", "sqlserver")
    assert all(t.startswith("MSSQL_") for t in live["provides"])
    assert all(t.startswith("SQLSERVER_") for t in integ["provides"])
    assert live["docker_defaults"]["database"] == "qauser"          # CDC-enabled app DB
    assert integ["docker_defaults"]["dbname"] == "intdb" and "database" not in integ["docker_defaults"]
    assert (live["container"], live["live_override_env"]) == ("slt-mssql", "SLT_MSSQL_HOST")
    assert (integ["container"], integ["live_override_env"]) == ("int-mssql", "INT_MSSQL_HOST")


def test_postgres_profiles_differ():
    live, integ = _svc_yaml("live", "postgres"), _svc_yaml("integration", "postgres")
    assert live["docker_defaults"]["dbname"] == "sltdb"
    assert integ["docker_defaults"]["dbname"] == "intdb"
    live_svc = next(iter(_compose("live", "postgres")["services"].values()))
    int_svc = next(iter(_compose("integration", "postgres")["services"].values()))
    assert live_svc["build"] == "."
    assert "wal2json" in (CHECKOUT["live"] / "postgres" / "Dockerfile").read_text()
    assert "build" not in int_svc and int_svc["image"] == "postgres:16"
    assert not (CHECKOUT["integration"] / "postgres" / "Dockerfile").exists()


def test_oracle_cdc_only_in_live():
    live, integ = _svc_yaml("live", "oracle"), _svc_yaml("integration", "oracle")
    assert "cdc_user" in live["docker_defaults"] and "ORACLE_CDC_URL" in live["provides"]
    assert "cdc_user" not in integ["docker_defaults"]
    assert not any(t.startswith("ORACLE_CDC") for t in integ["provides"])


def test_host_versus_container_endpoints(tmp_path):
    # Integration: no containerized Striim -- the app-visible host IS the harness host.
    tok = tokens.build_tokens(tmp_path, ["postgres"], env={}, services_dir=CHECKOUT["integration"])
    assert tok["POSTGRES_HOST"] == "localhost"
    tok = tokens.build_tokens(tmp_path, ["postgres"], env={"INT_PG_HOST": "db.example.com"},
                              services_dir=CHECKOUT["integration"])
    assert tok["POSTGRES_HOST"] == "db.example.com"
    # Live: the admin client dials the host; the Striim container uses SLT_STRIIM_VIEW_HOST.
    container = registry.load_service("postgres").container
    r = live_services.resolve("postgres", {"SLT_STRIIM_VIEW_HOST": "host.docker.internal"},
                              started={container},
                              compose_up=lambda d: pytest.fail("must not bring up"))
    assert r.base["host"] == "localhost" and r.base["view_host"] == "host.docker.internal"


# Services whose compose declares no healthcheck today (the spanner emulator is readied by
# its admin client). Recorded explicitly so neither a lost nor a silently added check passes.
_NO_COMPOSE_HEALTHCHECK = {("live", "spanner"), ("integration", "spanner")}


@pytest.mark.parametrize("tier,var,other", [("live", "SLT_STACK_PREFIX", "INT_STACK_PREFIX"),
                                            ("integration", "INT_STACK_PREFIX", "SLT_STACK_PREFIX")])
def test_readiness_and_prefix_interpolation_preserved(tier, var, other):
    for name in _profiles(tier):
        if (tier, name) == ("live", "striim"):
            continue                      # cluster recipe: readiness is striim_provision's
        if not (CHECKOUT[tier] / name / "compose.yaml").exists():
            continue                      # connection only (teradata): no container to be ready
        compose = _compose(tier, name)
        svcs = compose["services"].values()
        has_check = any("healthcheck" in s for s in svcs)
        assert has_check == ((tier, name) not in _NO_COMPOSE_HEALTHCHECK), \
            f"{tier}/{name}: readiness check changed"
        names = [s["container_name"] for s in svcs if "container_name" in s]
        assert names, f"{tier}/{name}: no container_name"
        for n in names:
            assert var in n and other not in n, f"{tier}/{name}: {n!r}"


# ---------------------------------------------------------------------------
# Deliberate overlays and accidental collisions
# ---------------------------------------------------------------------------

def test_overlay_whole_origin(tmp_path):
    consumer = _write(tmp_path / "consumer", {**DEMO, "demo/sql/init.sql": "-- consumer\n"})
    builtin = _write(tmp_path / "builtin", DEMO)
    with pytest.raises(rp.ProfileError, match="defined under multiple roots"):
        rp.select_profile("live", "demo", roots=[consumer, builtin])
    chosen = rp.select_profile("live", "demo", roots=[consumer, builtin], override=True)
    assert chosen.origin == consumer / "demo"
    assert chosen.file("sql/init.sql").read_text() == "-- consumer\n"
    explicit = rp.select_profile("live", "demo", services_dir=builtin)
    assert explicit.file("sql/init.sql").read_text() == "select 1;\n"


def test_overlay_missing_asset_no_fallback(tmp_path):
    consumer = _write(tmp_path / "consumer", {k: v for k, v in DEMO.items()
                                              if not k.endswith("init.sql")})
    builtin = _write(tmp_path / "builtin", DEMO)
    with pytest.raises(rp.ProfileError, match="missing bind-mount source sql/init.sql"):
        rp.select_profile("live", "demo", roots=[consumer, builtin], override=True)
    unchecked = rp.select_profile("live", "demo", roots=[consumer, builtin], override=True,
                                  check=False)
    with pytest.raises(rp.ProfileError, match="not an asset of the selected origin"):
        unchecked.file("sql/init.sql")


def test_overlay_symlink_escape(tmp_path):
    consumer = _write(tmp_path / "consumer", {k: v for k, v in DEMO.items()
                                              if not k.endswith("init.sql")})
    outside = tmp_path / "outside.sql"
    outside.write_text("select 2;\n")
    (consumer / "demo" / "sql").mkdir()
    (consumer / "demo" / "sql" / "init.sql").symlink_to(outside)
    with pytest.raises(rp.ProfileError, match="escapes the profile origin"):
        rp.select_profile("live", "demo", services_dir=consumer)
    other = _write(tmp_path / "other", DEMO)
    (other / "demo" / "linked").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(rp.ProfileError, match="symlinked directory linked escapes"):
        rp.select_profile("live", "demo", services_dir=other)


_COMPOSE_BUILD = "services:\n  demo:\n    build: ./images/demo\n"

@pytest.mark.parametrize("files,expected", [
    ({"demo/README.md": "x"}, "unknown service profile 'demo'"),
    ({"demo/compose.yaml": "services: {}\n"}, "live/demo: missing service definition service.yaml"),
    ({"demo/service.yaml": "name: demo\nisolation: none\ncompose: compose.yaml\n"},
     "live/demo: missing compose file compose.yaml"),
    ({k: v for k, v in DEMO.items() if not k.endswith("init.sql")},
     "live/demo: missing bind-mount source sql/init.sql"),
    ({"demo/service.yaml": DEMO["demo/service.yaml"], "demo/compose.yaml": _COMPOSE_BUILD},
     "live/demo: missing build Dockerfile images/demo/Dockerfile"),
    ({"demo/service.yaml": DEMO["demo/service.yaml"], "demo/compose.yaml": _COMPOSE_BUILD,
      "demo/images/demo/Dockerfile": "FROM x\nCOPY init.sql /init.sql\n"},
     "live/demo: missing COPY source images/demo/init.sql"),
], ids=["unknown", "definition", "compose", "sql-init", "dockerfile", "copy-asset"])
def test_missing_profile_dependency_preflight(tmp_path, files, expected):
    root = _write(tmp_path / "root", files)
    with pytest.raises(rp.ProfileError) as e:
        rp.select_profile("live", "demo", services_dir=root)
    assert expected in str(e.value)


_RECIPE = {
    "striim/compose.yaml": ("services:\n  node:\n    build:\n      context: ./images/r\n"
                            "      dockerfile: ./Dockerfile\n"),
    "striim/images/r/Dockerfile": ("FROM x\nRUN --mount=type=bind,source=deps,target=/deps \\\n"
                                   "    cp /deps/a.jar /opt/\nCOPY ./files /opt/files\n"),
    "striim/images/r/files/entry.sh": "#!/bin/sh\n",
}


def test_cluster_recipe_is_explicit(tmp_path):
    root = _write(tmp_path / "root", {**_RECIPE, "empty/README.md": "x"})
    prof = rp.select_profile("live", "striim", services_dir=root)       # the ratified recipe
    assert prof.kind == rp.KIND_CLUSTER_RECIPE
    assert not any(rel.startswith("images/r/deps") for rel, _m, _s in prof.assets)
    with pytest.raises(rp.ProfileError, match="unknown service profile 'empty'"):
        rp.select_profile("live", "empty", services_dir=root)
    # the recipe exception is (tier, name)-exact: the same tree is no recipe elsewhere
    with pytest.raises(rp.ProfileError, match="integration/striim: missing service definition service.yaml"):
        rp.select_profile("integration", "striim", services_dir=root)
    renamed = _write(tmp_path / "renamed", {k.replace("striim/", "recipe/"): v for k, v in _RECIPE.items()})
    with pytest.raises(rp.ProfileError, match="live/recipe: missing service definition service.yaml"):
        rp.select_profile("live", "recipe", services_dir=renamed)


# An agent image that COPYs two consumer-supplied vendor binaries.
_AGENT = {
    "striim/compose.extra-agent.yaml": ("services:\n  slt-extra-agent:\n    build:\n      context: ./images/extra-agent\n"
                                        "      dockerfile: ./Dockerfile\n"),
    "striim/images/extra-agent/Dockerfile": ("FROM x\nCOPY native/settings.reg /app/\n"
                                             "COPY native/vendor.dll native/vendor.rll /app/vendor/\n"),
    "striim/images/extra-agent/native/settings.reg": "REGEDIT4\n",
}


def test_consumer_supplied_copy_sources_are_exact(tmp_path, monkeypatch):
    monkeypatch.setattr(rp, "CONSUMER_SUPPLIED_SOURCES", {("live", "striim"): frozenset({
        "images/extra-agent/native/vendor.dll", "images/extra-agent/native/vendor.rll"})})
    # Absent consumer-supplied sources are no preflight failure for the ratified recipe...
    root = _write(tmp_path / "root", {**_RECIPE, **_AGENT})
    prof = rp.select_profile("live", "striim", services_dir=root)
    assert not any(rel.endswith((".dll", ".rll")) for rel, _m, _s in prof.assets)
    # ...but any other absent COPY source still is, even next to them (exact paths, no pattern)...
    extra = {**_RECIPE, **_AGENT, "striim/images/extra-agent/Dockerfile":
             _AGENT["striim/images/extra-agent/Dockerfile"] + "COPY native/other.dll /app/\n"}
    with pytest.raises(rp.ProfileError, match="live/striim: missing COPY source images/extra-agent/native/other.dll"):
        rp.select_profile("live", "striim", services_dir=_write(tmp_path / "extra", extra))
    # ...and the declaration is (tier, name)-exact: the same files in another profile are required.
    demo = {k.replace("striim/", "demo/"): v for k, v in _AGENT.items()}
    demo["demo/service.yaml"] = DEMO["demo/service.yaml"].replace("compose.yaml", "compose.extra-agent.yaml")
    with pytest.raises(rp.ProfileError, match="live/demo: missing COPY source images/extra-agent/native/vendor.dll"):
        rp.select_profile("live", "demo", services_dir=_write(tmp_path / "demo", demo))


@pytest.mark.parametrize("tier,name", [("integration", "postgres"), ("integration", "sqlserver"),
                                       ("live", "postgres"), ("live", "mssql")])
def test_deleting_only_service_yaml_is_a_named_preflight_failure(tmp_path, tier, name):
    root = tmp_path / "services"
    shutil.copytree(CHECKOUT[tier] / name, root / name)
    assert rp.select_profile(tier, name, services_dir=root).kind == rp.KIND_SERVICE   # positive control
    (root / name / "service.yaml").unlink()
    with pytest.raises(rp.ProfileError, match=f"{tier}/{name}: missing service definition service.yaml"):
        rp.select_profile(tier, name, services_dir=root)
    assert rp.list_profiles(tier, roots=[root]) == []


# ---------------------------------------------------------------------------
# Roots and state come from the path keys (unset falls back)
# ---------------------------------------------------------------------------

def test_live_roots_default_to_the_live_services_dir():
    assert rp.live_roots(env={}, dotenv={}) == (live_paths.services_dir({}, {}),)
    assert live_paths.services_dir({}, {}) == CHECKOUT["live"]


def test_live_roots_follow_slt_services_dir(tmp_path):
    root = _write(tmp_path / "svc", DEMO)
    assert rp.live_roots(env={"SLT_SERVICES_DIR": str(root)}, dotenv={}) == (root.resolve(),)
    assert rp.live_roots(env={}, dotenv={"SLT_SERVICES_DIR": str(root)}) == (root.resolve(),)
    prof = rp.select_profile("live", "demo", roots=rp.live_roots(env={"SLT_SERVICES_DIR": str(root)}, dotenv={}))
    assert prof.origin == root.resolve() / "demo"


def test_integration_roots_default_to_the_integration_services_dir():
    assert resources.services_roots(env={}, dotenv={}) == (CHECKOUT["integration"],)
    assert resources.select_profile("sqlserver", env={}, dotenv={}).origin == CHECKOUT["integration"] / "sqlserver"
    assert "postgres" in resources.list_profiles(env={}, dotenv={})
    assert resources.service_dir("postgres", env={}, dotenv={}) == CHECKOUT["integration"] / "postgres"


def test_integration_roots_follow_slt_int_services_dir(tmp_path):
    root = _write(tmp_path / "svc", DEMO)
    env = {"SLT_INT_SERVICES_DIR": str(root)}
    assert resources.services_roots(env=env, dotenv={}) == (root.resolve(),)
    assert resources.select_profile("demo", env=env, dotenv={}).origin == root.resolve() / "demo"
    assert resources.list_profiles(env=env, dotenv={}) == ["demo"]
    # SLT_SERVICES_DIR is the live tier's key: it never moves the integration root (assumption A1)
    assert resources.services_roots(env={"SLT_SERVICES_DIR": str(root)}, dotenv={}) == (CHECKOUT["integration"],)


def test_integration_state_root_falls_back_and_follows_slt_state_dir(tmp_path):
    assert resources.state_root(env={}, dotenv={}) == int_paths.state_dir({}, {})
    assert resources.state_root(env={}, dotenv={}) == REPO / "scripts/integration"
    assert resources.state_root(env={"SLT_STATE_DIR": str(tmp_path)}, dotenv={}) == tmp_path.resolve()


@pytest.mark.parametrize("key,call", [
    ("SLT_SERVICES_DIR", lambda env: rp.live_roots(env=env, dotenv={})),
    ("SLT_INT_SERVICES_DIR", lambda env: resources.services_roots(env=env, dotenv={})),
    ("SLT_INT_SERVICES_DIR", lambda env: resources.select_profile("postgres", env=env, dotenv={})),
    ("SLT_STATE_DIR", lambda env: resources.state_root(env=env, dotenv={})),
])
def test_set_but_missing_key_raises_naming_it(tmp_path, key, call):
    with pytest.raises((live_paths.PathConfigError, int_paths.PathConfigError), match=key):
        call({key: str(tmp_path / "nope")})


def test_integration_facade_loads_the_sibling_module_without_livetest(monkeypatch, tmp_path):
    # The integration suite runs with only scripts/integration on the path. PYTHONPATH alone does
    # not make livetest unimportable -- setup-framework.sh --venv-python installs it into the
    # interpreter -- so the child refuses the import itself, as an environment without it would.
    monkeypatch.setattr(subprocess, "run", _REAL_RUN)
    monkeypatch.setattr(subprocess, "Popen", _REAL_POPEN)
    code = ("import sys\n"
            "class _NoLivetest:\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name == 'livetest' or name.startswith('livetest.'):\n"
            "            raise ImportError(name)\n"
            "sys.meta_path.insert(0, _NoLivetest())\n"
            "from inttest import resources as r; m = r._profiles(); "
            "assert 'livetest' not in sys.modules; print(m.__name__, r.select_profile('postgres').kind)")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO / "scripts/integration",
                         env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO / "scripts/integration"),
                              "SLT_PROJECT_ROOT": str(tmp_path)},     # no developer .env reaches the child
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["_inttest_resource_profiles", "service"]


_NO_INIT = {k: v for k, v in DEMO.items() if not k.endswith("init.sql")}


def test_integration_deliberate_override_uses_one_origin(tmp_path):
    override = _write(tmp_path / "override", _NO_INIT)
    with pytest.raises(resources.ResourceError, match="missing bind-mount source sql/init.sql"):
        resources.select_profile("demo", services_dir=override)
    complete = _write(tmp_path / "complete", DEMO)
    assert resources.select_profile("demo", services_dir=complete).origin == complete / "demo"


# ---------------------------------------------------------------------------
# The actual consumers run the preflight before any docker command
# ---------------------------------------------------------------------------

def _int_root(monkeypatch, root: Path) -> Path:
    """Point the integration engine at ``root/services`` and ``root`` state, read at call time."""
    monkeypatch.setenv("SLT_INT_SERVICES_DIR", str(root / "services"))
    monkeypatch.setenv("SLT_STATE_DIR", str(root))
    return root


def test_integration_compose_up_and_ensure_up_preflight(tmp_path, monkeypatch):
    _write(tmp_path / "services", _NO_INIT)
    _int_root(monkeypatch, tmp_path)
    lock = FileLock(str(tmp_path / "compose.lock"))
    for call in (int_services.compose_up, int_services.ensure_up):
        with pytest.raises(resources.ResourceError,
                           match="integration/demo: missing bind-mount source sql/init.sql"):
            call("demo", lock)
    # the complete profile passes the same preflight (positive control, no docker reached)
    (tmp_path / "services" / "demo" / "sql").mkdir()
    (tmp_path / "services" / "demo" / "sql" / "init.sql").write_text("select 1;\n")
    assert int_services._checked_compose_file("demo") == \
        tmp_path.resolve() / "services" / "demo" / "compose.yaml"


def test_integration_plugin_ensure_up_preflight(tmp_path, monkeypatch):
    from inttest import plugin as int_plugin
    _write(tmp_path / "services", _NO_INIT)
    _int_root(monkeypatch, tmp_path)
    monkeypatch.delenv("INT_SHARED_SERVICES", raising=False)
    with pytest.raises(resources.ResourceError, match="missing bind-mount source sql/init.sql"):
        int_plugin._ensure_up("demo", FileLock(str(tmp_path / "compose.lock")))


def test_integration_non_default_compose_filename_refused(tmp_path, monkeypatch):
    _write(tmp_path / "services", {
        "demo/service.yaml": "name: demo\nisolation: none\ncompose: docker-compose.yaml\n",
        "demo/docker-compose.yaml": "services: {}\n",
        "demo/compose.yaml": "services: {}\n",
    })
    _int_root(monkeypatch, tmp_path)
    with pytest.raises(resources.ResourceError, match="non-default compose filename is refused"):
        int_services.compose_up("demo", FileLock(str(tmp_path / "compose.lock")))


def test_live_default_compose_up_preflight(tmp_path):
    consumer = tmp_path / "consumer"
    _write(consumer / "services", _NO_INIT)
    defn = registry.load_service("demo", services_dir=consumer / "services")
    with pytest.raises(live_services.ServiceError,
                       match="preflight failed for demo: live/demo: missing bind-mount source sql/init.sql"):
        live_services._default_compose_up(defn)
    _write(consumer / "services", DEMO)
    live_services._preflight_profile(defn)          # complete: passes


def test_live_declared_compose_filename_is_the_one_checked(tmp_path):
    consumer = tmp_path / "consumer"
    _write(consumer / "services", {
        "demo/service.yaml": "name: demo\nisolation: none\ncompose: stack.yaml\n",
        "demo/compose.yaml": "services: {}\n",       # present, but not the declared file
    })
    defn = registry.load_service("demo", services_dir=consumer / "services")
    assert live_services.service_compose_path(defn) == defn.dir / "stack.yaml"
    with pytest.raises(live_services.ServiceError, match="missing compose file stack.yaml"):
        live_services._default_compose_up(defn)


# ---------------------------------------------------------------------------
# Integration consumers resolve at call time
# ---------------------------------------------------------------------------

def test_integration_tokens_follow_call_time_root(tmp_path, monkeypatch):
    _write(tmp_path / "services", {
        "demo/service.yaml": ("name: demo\nisolation: none\ndocker_defaults:\n  host: h1\n"
                              "provides:\n  DEMO_HOST: \"{view_host}\"\n"),
        "demo/compose.yaml": "services: {}\n",     # tokens require a complete profile
    })
    _int_root(monkeypatch, tmp_path)
    assert tokens.service_tokens("demo", env={}) == {"DEMO_HOST": "h1"}
    assert tokens.build_tokens(tmp_path, ["demo"], env={})["DEMO_HOST"] == "h1"


def test_integration_cli_and_state_follow_call_time_root(tmp_path, monkeypatch):
    # nothing set: today's layout
    assert cli._lock_file() == REPO / "scripts/integration/.int-compose.lock"
    assert int_services.started_registry_path().parent == REPO / "scripts/integration"
    _write(tmp_path / "services", {
        "mysql/service.yaml": "name: mysql\nisolation: none\n",
        "postgres/service.yaml": "name: postgres\nisolation: none\n",
    })
    monkeypatch.setenv("SLT_INT_SERVICES_DIR", str(tmp_path / "services"))
    assert cli._services() == ["postgres", "mysql"]
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("SLT_STATE_DIR", str(state))
    from inttest import plugin as int_plugin
    assert cli._lock_file() == state.resolve() / ".int-compose.lock"
    assert int_services._provision_registry_path().parent == state.resolve()
    assert int_services.started_registry_path().parent == state.resolve()
    assert int_plugin._started_registry() == int_services.started_registry_path()
    assert int_plugin._compose_lock_path().parent == state.resolve()
    assert int_plugin._session_lock_path().parent == state.resolve()


# ---------------------------------------------------------------------------
# No shortcut turns incomplete resources into a skip; tokens are checked
# ---------------------------------------------------------------------------

def _not_skipped(call):
    try:
        call()
    except pytest.skip.Exception as e:
        pytest.fail(f"incomplete resources were converted into a skip: {e}")


def _postgres_checkout(tmp_path, *, drop_compose):
    shutil.copytree(CHECKOUT["integration"] / "postgres", tmp_path / "services" / "postgres")
    if drop_compose:
        (tmp_path / "services" / "postgres" / "compose.yaml").unlink()
    return tmp_path


def test_missing_compose_is_a_named_error_on_every_lifecycle_route(tmp_path, monkeypatch):
    from inttest import plugin as int_plugin
    _int_root(monkeypatch, _postgres_checkout(tmp_path, drop_compose=True))
    lock = FileLock(str(tmp_path / "compose.lock"))
    match = "integration/postgres: missing compose file compose.yaml"
    with pytest.raises(resources.ResourceError, match=match):
        _not_skipped(lambda: int_services.ensure_up("postgres", lock))
    with pytest.raises(resources.ResourceError, match=match):             # YAML pipeline route
        _not_skipped(lambda: int_plugin._provision_requires("case", ["postgres"], lock))
    monkeypatch.delenv("INT_SHARED_SERVICES", raising=False)
    with pytest.raises(resources.ResourceError, match=match):             # fixture route
        _not_skipped(lambda: int_plugin._ensure_up("postgres", lock))
    monkeypatch.setenv("INT_SHARED_SERVICES", "1")
    with pytest.raises(resources.ResourceError, match=match):             # broker early return
        _not_skipped(lambda: int_plugin._ensure_up("postgres", lock))


def test_unavailable_docker_still_skips_a_complete_profile(tmp_path, monkeypatch):
    from inttest import plugin as int_plugin
    _int_root(monkeypatch, _postgres_checkout(tmp_path, drop_compose=False))

    def no_docker(*args, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(int_services.subprocess, "run", no_docker)
    with pytest.raises(pytest.skip.Exception, match="cannot bring up service 'postgres'"):
        int_plugin._provision_requires("case", ["postgres"], FileLock(str(tmp_path / "compose.lock")))


_INCOMPLETE_TOKENS = {
    "demo/service.yaml": ("name: demo\nisolation: none\ncompose: compose.yaml\n"
                          "docker_defaults:\n  host: h1\nprovides:\n  DEMO_HOST: \"{view_host}\"\n"),
    "demo/compose.yaml": DEMO["demo/compose.yaml"],                  # mounts ./sql/init.sql (absent)
}
_TOKEN_MATCH = "demo.*missing bind-mount source sql/init.sql"


def test_token_apis_check_the_explicit_origin(tmp_path):
    root = _write(tmp_path / "override", _INCOMPLETE_TOKENS)
    with pytest.raises(tokens.ServiceConfigError, match=_TOKEN_MATCH):
        tokens.service_tokens("demo", env={}, services_dir=root)
    with pytest.raises(tokens.ServiceConfigError, match=_TOKEN_MATCH):
        tokens.build_tokens(tmp_path, ["demo"], env={}, services_dir=root)
    _write(root, {"demo/sql/init.sql": "select 1;\n"})               # positive control
    assert tokens.service_tokens("demo", env={}, services_dir=root) == {"DEMO_HOST": "h1"}
    assert tokens.build_tokens(tmp_path, ["demo"], env={}, services_dir=root)["DEMO_HOST"] == "h1"


def test_token_apis_check_the_default_origin(tmp_path, monkeypatch):
    _write(tmp_path / "services", _INCOMPLETE_TOKENS)
    _int_root(monkeypatch, tmp_path)
    with pytest.raises(tokens.ServiceConfigError, match=_TOKEN_MATCH):
        tokens.service_tokens("demo", env={})
    with pytest.raises(tokens.ServiceConfigError, match=_TOKEN_MATCH):
        tokens.build_tokens(tmp_path, ["demo"], env={})


def test_plugin_pipeline_token_assembly_checks_the_profile(tmp_path, monkeypatch):
    from inttest import plugin as int_plugin
    _write(tmp_path / "services", _INCOMPLETE_TOKENS)
    _int_root(monkeypatch, tmp_path)
    with pytest.raises(tokens.ServiceConfigError, match=_TOKEN_MATCH):
        int_plugin._build_tokens(tmp_path, ["demo"], parallel=False)


# ---------------------------------------------------------------------------
# Fixture-only configuration/token requests check the profile
# ---------------------------------------------------------------------------

_FIXTURE_ONLY = """
def test_pg_config_only(pg_config):
    assert pg_config["POSTGRES_PORT"] == "15432"
    assert pg_config["POSTGRES_DB"] == "intdb"

def test_tokens_only(tokens):
    assert tokens["POSTGRES_DB"] == "intdb"
    assert tokens["ORACLE_PORT"] == "11521"   # no oracle profile directory: legitimate fallback
"""


def _fixture_checkout(tmp_path, drop):
    root = tmp_path / "checkout"
    shutil.copytree(CHECKOUT["integration"] / "postgres", root / "services" / "postgres")
    if drop:
        (root / "services" / "postgres" / drop).unlink()
    return root


def _run_fixture_only(pytester, monkeypatch, root):
    _int_root(monkeypatch, root)
    for name in ("INT_SHARED_SERVICES", "POSTGRES_PORT", "POSTGRES_DB", "ORACLE_PORT",
                 "INT_PG_HOST_PORT", "INT_ORA_HOST_PORT"):
        monkeypatch.delenv(name, raising=False)
    pytester.makepyfile(test_fixture_only=_FIXTURE_ONLY)
    return pytester.runpytest_inprocess("-p", "inttest.plugin", "-o", "addopts=",
                                        "-p", "no:cacheprovider")


@pytest.mark.parametrize("drop,message", [
    ("service.yaml", "integration/postgres: missing service definition service.yaml"),
    ("compose.yaml", "integration/postgres: missing compose file compose.yaml"),
    ("sql/init.sql", "integration/postgres: missing bind-mount source sql/init.sql"),
], ids=["definition", "compose", "init"])
def test_fixture_only_requests_refuse_an_incomplete_profile(pytester, monkeypatch, tmp_path,
                                                            drop, message):
    result = _run_fixture_only(pytester, monkeypatch, _fixture_checkout(tmp_path, drop))
    result.assert_outcomes(errors=2)                    # both fixture-only tests error at setup
    result.stdout.fnmatch_lines([f"*ResourceError*{message}*"])
    result.stdout.no_fnmatch_line("*workload subprocess attempted*")


def test_fixture_only_requests_publish_a_complete_profile(pytester, monkeypatch, tmp_path):
    result = _run_fixture_only(pytester, monkeypatch, _fixture_checkout(tmp_path, None))
    result.assert_outcomes(passed=2)                    # positive control: token values preserved


# ---------------------------------------------------------------------------
# Bind-mounted directories (allow an absent directory
# source, as Docker does; a file-like absent source is still refused)
# ---------------------------------------------------------------------------

def _mounting(mount: str) -> dict:
    return {"demo/service.yaml": "name: demo\nisolation: none\ncompose: compose.yaml\n",
            "demo/compose.yaml": f"services:\n  demo:\n    image: x\n    volumes:\n      - {mount}\n"}


@pytest.fixture
def unreadable():
    """chmod helper that restores modes so tmp_path can be removed."""
    changed = []

    def lock(p: Path, mode=0):
        changed.append((p, p.stat().st_mode))
        p.chmod(mode)
    yield lock
    for p, mode in reversed(changed):
        p.chmod(mode)


@pytest.mark.parametrize("tier", ["live", "integration"])
def test_absent_directory_source_is_allowed_and_named(tmp_path, tier):
    root = _write(tmp_path / "services", _mounting("./data:/var/lib/postgresql/data"))
    prof = rp.select_profile(tier, "demo", services_dir=root)
    assert rp.absent_directory_mounts(prof) == ["data"]


def test_empty_directory_source_is_allowed(tmp_path):
    root = _write(tmp_path / "services", _mounting("./data:/var/lib/postgresql/data"))
    (root / "demo" / "data").mkdir()
    prof = rp.select_profile("live", "demo", services_dir=root)
    assert rp.absent_directory_mounts(prof) == []


def test_directory_source_holding_an_unreadable_file_is_allowed_and_not_hashed(tmp_path, unreadable):
    root = _write(tmp_path / "services", {**_mounting("./data:/var/lib/postgresql/data"),
                                          "demo/data/base/1/PG_VERSION": "16\n"})
    unreadable(root / "demo" / "data" / "base" / "1" / "PG_VERSION")     # written by the container's uid
    prof = rp.select_profile("live", "demo", services_dir=root)
    assert not any(rel.startswith("data/") for rel, _m, _s in prof.assets)
    assert {rel for rel, _m, _s in prof.assets} == {"service.yaml", "compose.yaml"}


def test_unlistable_directory_source_is_allowed(tmp_path, unreadable):
    root = _write(tmp_path / "services", {**_mounting("./data:/var/lib/postgresql/data"),
                                          "demo/data/x": "x\n"})
    unreadable(root / "demo" / "data")                                     # 0000: os.walk cannot list it
    rp.select_profile("live", "demo", services_dir=root)
    live_services._preflight_profile(registry.load_service("demo", services_dir=root))


def test_directory_source_content_changes_leave_the_hash_alone(tmp_path):
    root = _write(tmp_path / "services", {**_mounting("./data:/data"), "demo/data/a": "1\n"})
    before = rp.select_profile("live", "demo", services_dir=root).definition_hash
    (root / "demo" / "data" / "a").write_text("2\n")
    assert rp.select_profile("live", "demo", services_dir=root).definition_hash == before


@pytest.mark.parametrize("tier", ["live", "integration"])
def test_finder_ds_store_files_are_not_assets(tmp_path, tier):
    # Finder writes .DS_Store into any folder it opens; git ignores it, so shipping it would
    # make a Mac checkout's profile differ from a clone's (and from the tracked files).
    root = _write(tmp_path / "services", DEMO)
    before = rp.select_profile(tier, "demo", services_dir=root)
    _write(root, {"demo/.DS_Store": "finder\n", "demo/sql/.DS_Store": "finder\n"})
    after = rp.select_profile(tier, "demo", services_dir=root)
    assert {rel for rel, _m, _s in after.assets} == {"service.yaml", "compose.yaml", "sql/init.sql"}
    assert after.definition_hash == before.definition_hash


@pytest.mark.parametrize("tier", ["live", "integration"])
def test_absent_file_like_source_is_still_refused(tmp_path, tier):
    root = _write(tmp_path / "services", _mounting("./sql/init.sql:/docker-entrypoint-initdb.d/init.sql:ro"))
    with pytest.raises(rp.ProfileError, match=f"{tier}/demo: missing bind-mount source sql/init.sql"):
        rp.select_profile(tier, "demo", services_dir=root)


def test_an_unreadable_asset_is_a_profile_error(tmp_path, unreadable):
    root = _write(tmp_path / "services", {**_mounting("./init.sql:/init.sql:ro"), "demo/init.sql": "select 1;\n"})
    unreadable(root / "demo" / "init.sql")
    with pytest.raises(rp.ProfileError, match="live/demo: cannot read init.sql"):
        rp.select_profile("live", "demo", services_dir=root)
    with pytest.raises(live_services.ServiceError, match="preflight failed for demo: live/demo: cannot read init.sql"):
        live_services._preflight_profile(registry.load_service("demo", services_dir=root))
