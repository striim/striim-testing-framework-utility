# scripts/live/tests/test_discovery.py
from pathlib import Path

import pytest

from livetest.plugin import (
    testid_to_ns_app, build_service_tokens, substitute_targets,
    StriimContext, striim_group_tokens, striim_url_tokens, striim_web_url,
    _slug, _tid_oracle,
)
from livetest.manifest import load_manifest
from livetest.registry import load_service, ServiceDef
from livetest.services import ResolvedService, ServiceError
from livetest.topology import Topology

def test_hello_single_manifest_loads():
    # collection is driven by pytest testpaths + pytest_collect_file (not a manual scan);
    # here we just confirm the canonical manifest parses.
    m = load_manifest(Path(__file__).resolve().parents[1] / "regression/hello/hello-single/test.yaml")
    assert m.name == "hello-single"

def test_testid_to_ns_app_is_unique_and_prefixed():
    m = load_manifest(Path(__file__).resolve().parents[1] / "regression/hello/hello-single/test.yaml")
    ns, app = testid_to_ns_app(m)
    assert ns.startswith("SLT_")
    assert app.startswith(ns + ".")
    assert app.endswith("App")

def test_build_service_tokens_expands_pg_url():
    defn = load_service("postgres")
    resolved = ResolvedService("postgres", "docker",
        {"host": "localhost", "port": 5432, "dbname": "sltdb",
         "admin_user": "postgres", "admin_password": "striim",
         "source_user": "qasource", "source_password": "striim", "source_schema": "qasource",
         "target_user": "qatarget", "target_password": "striim", "target_schema": "qatarget",
         "view_host": "host.docker.internal"}, True)
    toks = build_service_tokens(defn, resolved, "slt_pg_smoke")
    assert toks["PG_HOST"] == "host.docker.internal"     # view_host, for the Striim app
    assert toks["PG_URL"] == "jdbc:postgresql://host.docker.internal:5432/sltdb"
    assert toks["PG_SOURCE_USER"] == "qasource"
    assert toks["PG_TARGET_USER"] == "qatarget"
    assert toks["PG_SOURCE_SCHEMA"] == "qasource"
    assert toks["PG_TARGET_SCHEMA"] == "qatarget"
    assert toks["PG_SLOT"] == "slt_pg_smoke"             # per-test replication-slot name

def test_build_service_tokens_raises_service_error_on_bad_template_key():
    defn = ServiceDef(name="x", dir=Path("."), isolation="schema",
                       provides={"BAD": "{nope}"})
    resolved = ResolvedService("x", "docker", {}, True)
    with pytest.raises(ServiceError):
        build_service_tokens(defn, resolved, "some_schema")

def test_substitute_targets_renders_pg_schema():
    specs = [{"target": "${PG_SCHEMA}.hello", "min_rows": 1}]
    out = substitute_targets(specs, {"PG_SCHEMA": "slt_x"})
    assert out[0]["target"] == "slt_x.hello"
    assert out[0]["min_rows"] == 1
    assert specs[0]["target"] == "${PG_SCHEMA}.hello"   # original unchanged

def test_substitute_targets_also_renders_source():
    from livetest.plugin import substitute_targets
    specs = [{"source": "${PG_SCHEMA}.src", "target": "${PG_SCHEMA}.tgt"}]
    out = substitute_targets(specs, {"PG_SCHEMA": "slt_x"})
    assert out[0]["source"] == "slt_x.src"
    assert out[0]["target"] == "slt_x.tgt"
    assert specs[0]["source"] == "${PG_SCHEMA}.src"   # original unchanged

def test_striim_group_tokens():
    ctx = StriimContext(url="u", user="a", password="p", mode="docker",
                        topology=Topology(), view_host="host.docker.internal",
                        groups={"app": "default", "source": "Agents"})
    toks = striim_group_tokens(ctx)
    assert toks == {"APP_GROUP": "default", "SOURCE_GROUP": "Agents"}

def _ctx(mode, url="http://localhost:9080"):
    return StriimContext(url=url, user="a", password="p", mode=mode,
                         topology=Topology(), view_host="host.docker.internal",
                         groups={"app": "default", "source": "Agents"})

def test_striim_web_url_is_container_reachable_in_docker_mode():
    # An OP deployed into the agent calls back into the server's REST API. `localhost` inside
    # the agent container is the AGENT, so the token must NOT resolve to a loopback URL and
    # must NOT inherit the host-side ctx.url -- doing either is what killed the app mid-flow
    # with "Unable to run command: drop type ... | Error: Connection refused".
    url = striim_web_url(_ctx("docker"))
    assert "localhost" not in url
    assert url == "http://slt-striim:9080"
    assert striim_url_tokens(_ctx("docker")) == {"STRIIM_WEB_URL": "http://slt-striim:9080"}

def test_striim_web_url_falls_back_to_harness_url_off_docker():
    # A native single-node run has no server/agent split, so there is nothing to redirect.
    assert striim_web_url(_ctx("native", url="http://localhost:9080")) == "http://localhost:9080"

def test_tid_token_equals_slug_used_for_namespace():
    # ${TID} (spec §A.1) must be EXACTLY _slug(m.name) -- the same string baked into the
    # namespace by testid_to_ns_app, so an object named `${TID}SRC` and the app's own
    # namespace `SLT_<slug>` always agree on identity.
    m = load_manifest(Path(__file__).resolve().parents[1] / "regression/hello/hello-single/test.yaml")
    ns, _app = testid_to_ns_app(m)
    tid = _slug(m.name)
    assert ns == f"SLT_{tid}"

def test_tid_oracle_is_short_uppercase_letter_first_and_deterministic():
    # spec §A.1a: ${TID_ORACLE} must stay well under Oracle's practical CDC identifier-
    # length ceiling even for the longest real test names, always start with a letter
    # (Oracle identifiers can't start with a digit), and already be uppercase (it doubles
    # as the case-matching form, no separate _UPPER needed).
    longest_real_name = "spanner-json-array-element-pk-update"  # 37 chars, longest in corpus
    tid = _tid_oracle(longest_real_name)
    assert len(tid) == 10
    assert tid[0].isalpha()
    assert tid == tid.upper()
    assert tid.isalnum()
    assert _tid_oracle(longest_real_name) == tid  # deterministic

def test_tid_oracle_differs_across_test_names():
    assert _tid_oracle("spanner-array-integer-basic") != _tid_oracle("spanner-array-string-basic")
