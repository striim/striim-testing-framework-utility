"""Hermetic per-service pre-flight outcomes."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from livetest import preflight as pf
from livetest.service_healing import Refused

REAL_MANIFESTS_FOR = pf.manifests_for
REAL_PREPARE_SELECTED = pf._prestart.prepare_selected
REAL_GATED_OUT = pf._gated_out


@pytest.fixture
def rig(monkeypatch, tmp_path):
    monkeypatch.setattr(pf, "missing_port_overrides", lambda *a: [])
    monkeypatch.setattr(pf, "_gated_out", lambda *a: False)
    monkeypatch.setattr(pf, "load_service", lambda n: NS(name=n))
    monkeypatch.setattr(pf._prestart, "prepare_selected", lambda *a, **kw: None)
    monkeypatch.setattr(pf._prestart, "eligibility", lambda *a: None)
    monkeypatch.setattr(pf._slt_infra, "resolve_service", lambda infra, n, *a, **kw: NS(base={}))
    monkeypatch.setattr(pf, "ensure_provisioned_once", lambda key, cb: cb())
    monkeypatch.setattr(pf, "PgAdmin", lambda *a, **kw: NS(
        ensure_setup=lambda: None, sweep_stale_replication_slots=lambda log: None))
    monkeypatch.setattr(pf, "clear_provision_registry", lambda *a: None)
    monkeypatch.setattr(pf._slt_infra, "declare_or_log", lambda *a: object())
    monkeypatch.setattr(pf, "_resolve_release", lambda *a: {})
    monkeypatch.setattr(pf, "_resolve_striim", lambda *a: NS(mode="remote"))
    monkeypatch.setattr(pf, "manifests_for", lambda ids: [NS(name="a", requires=["extdb", "postgres"], modules=[])])
    return {"SLT_PREFLIGHT_OUTCOMES_PATH": str(tmp_path / "outcomes.json"),
            "SLT_PREFLIGHT_ATTEMPT": "attempt-a"}


@pytest.mark.parametrize("phase", ["pre_up", "resolve", "cold"])
def test_service_failure_continues_and_writes_summary(rig, monkeypatch, phase):
    if phase == "pre_up":
        def prepare(defn, *a, **kw):
            if defn.name == "extdb":
                raise pf._prestart.PreUpError("agent failed")
        monkeypatch.setattr(pf._prestart, "prepare_selected", prepare)
    elif phase == "resolve":
        def resolve(infra, name, *a, **kw):
            if name == "extdb":
                raise pf.ServiceError("agent failed")
            return NS(base={})
        monkeypatch.setattr(pf._slt_infra, "resolve_service", resolve)
    else:
        def setup():
            raise RuntimeError("cold setup failed")
        monkeypatch.setattr(pf, "PgAdmin", lambda *a, **kw: NS(ensure_setup=setup))
    assert pf.provision(["a"], env=rig) == 1
    record = json.loads(Path(rig["SLT_PREFLIGHT_OUTCOMES_PATH"]).read_text())
    bad, good = ("postgres", "extdb") if phase == "cold" else ("extdb", "postgres")
    assert record["version"] == 1 and record["attempt"] == "attempt-a"
    assert record["cluster"] == record["ops"] == "ready"
    assert record["services"][bad]["status"] == "unavailable"
    assert record["services"][bad]["reason"]
    assert record["services"][good]["status"] == "ready"
    if phase == "cold":
        assert "postgres" in record["cleanup_services"]


@pytest.mark.parametrize("failure", [KeyboardInterrupt, asyncio.CancelledError, Refused, pf._slt_infra.InfraOwnershipError])
def test_service_cancellation_and_refusal_stay_fatal(rig, monkeypatch, failure):
    def resolve(*a, **kw):
        raise failure("stop")
    monkeypatch.setattr(pf._slt_infra, "resolve_service", resolve)
    with pytest.raises(failure):
        pf.provision(["a"], env=rig)
    assert not Path(rig["SLT_PREFLIGHT_OUTCOMES_PATH"]).exists()


@pytest.mark.parametrize("phase", ["cluster", "ops", "ownership"])
def test_global_failure_has_no_partial_summary(rig, monkeypatch, phase):
    if phase == "cluster":
        monkeypatch.setattr(pf, "_resolve_striim", lambda *a: None)
    elif phase == "ownership":
        monkeypatch.setattr(pf._slt_infra, "declare_or_log", lambda *a: None)
    else:
        monkeypatch.setattr(pf, "manifests_for", lambda ids: [NS(name="a", requires=[], modules=[{"jar": "java/Op", "kind": "op"}])])
        monkeypatch.setattr(pf.StriimClient, "from_url", lambda *a: None)
        monkeypatch.setattr(pf, "_resolve_striim", lambda *a: NS(mode="remote", url="fake", user="fake", password="fake"))
        def build(*a, **kw):
            raise pf.opartifacts.OpArtifactError("build failed")
        monkeypatch.setattr(pf.opartifacts, "build_jar", build)
    assert pf.provision(["a"], env=rig) != 0
    assert not Path(rig["SLT_PREFLIGHT_OUTCOMES_PATH"]).exists()


@pytest.mark.parametrize("gated", [True, False])
def test_optional_unavailable_is_recorded(rig, monkeypatch, gated):
    monkeypatch.setattr(pf, "_gated_out", lambda n, env: gated and n == "extdb")
    if not gated:
        monkeypatch.setattr(pf._prestart, "prepare_selected", lambda d, *a, **kw: "missing input" if d.name == "extdb" else None)
    assert pf.provision(["a"], env=rig) == 0
    record = json.loads(Path(rig["SLT_PREFLIGHT_OUTCOMES_PATH"]).read_text())
    assert record["services"]["extdb"]["status"] == "unavailable"


def test_postflight_uses_cleanup_subset(rig, monkeypatch):
    seen = []
    monkeypatch.setattr(pf, "_take_down_services", lambda names, *a, **kw: seen.extend(names) or [])
    monkeypatch.setattr(pf, "teardown_cluster", lambda: 0)
    assert pf.teardown(["a"], env={"SLT_PREFLIGHT_CLEANUP_SERVICES": '["postgres"]'}) == 0
    assert seen == ["postgres"]


def test_partial_service_failure_still_prepares_runnable_ops(rig, monkeypatch):
    events = []
    manifests = [NS(name="jet", requires=["extdb"], modules=[{"jar": "java/JetOp", "kind": "op", "token": "JET"}]),
                 NS(name="file", requires=[], modules=[{"jar": "java/FileOp", "kind": "op", "token": "FILE"}])]
    monkeypatch.setattr(pf, "manifests_for", lambda ids: manifests)
    def prepare(d, *a, **kw):
        if d.name == "extdb":
            raise pf._prestart.PreUpError("missing input")
    monkeypatch.setattr(pf._prestart, "prepare_selected", prepare)
    monkeypatch.setattr(pf, "_resolve_striim", lambda *a: NS(mode="remote", url="fake", user="fake", password="fake"))
    monkeypatch.setattr(pf.StriimClient, "from_url", lambda *a: NS(load_open_processor_idempotent=lambda j, tag=None, before_unload=None: events.append("load")))
    def build(jar, *a, **kw):
        events.append(jar)
        return NS(name="FileOp.jar", path=Path("FileOp.jar"), op_name="FileOp", content_tag="",
                  fingerprint="f" * 64)
    monkeypatch.setattr(pf.opartifacts, "build_jar", build)
    monkeypatch.setattr(pf.opartifacts, "content_addressed", lambda built: built)
    monkeypatch.setattr(pf.opartifacts, "upload_artifacts", lambda *a, **kw: events.append("upload"))
    monkeypatch.setattr(pf, "_replaced_loaded_jars", lambda *a: [])
    monkeypatch.setattr(pf, "_registry_key", lambda *a: "key")
    monkeypatch.setattr(pf, "_loaded_jar_probe", lambda *a: None)
    monkeypatch.setattr(pf.opregistry, "ensure_registered", lambda a, b, c, cb, **kw: cb())
    assert pf.provision(["jet", "file"], env=rig) == 1
    assert events == ["java/FileOp", "upload", "load"]


def test_wrapped_admission_refusal_stays_fatal(rig, monkeypatch):
    def resolve(*a, **kw):
        try:
            raise Refused("cancelled")
        except Refused as e:
            raise pf.ServiceError("compose interrupted") from e
    monkeypatch.setattr(pf._slt_infra, "resolve_service", resolve)
    with pytest.raises(pf.ServiceError):
        pf.provision(["a"], env=rig)
    assert not Path(rig["SLT_PREFLIGHT_OUTCOMES_PATH"]).exists()


def test_service_eligibility_does_not_sink_other_services(rig, monkeypatch):
    def eligibility(defn, *a):
        if defn.name == "extdb":
            raise pf._prestart.PreUpError("unsupported service mode")
    monkeypatch.setattr(pf._prestart, "eligibility", eligibility)
    assert pf.provision(["a"], env=rig) == 1
    record = json.loads(Path(rig["SLT_PREFLIGHT_OUTCOMES_PATH"]).read_text())
    assert record["services"]["extdb"]["status"] == "unavailable"
    assert record["services"]["postgres"]["status"] == "ready"


@pytest.mark.parametrize("missing", ["extdb", "gcs"])
def test_missing_service_definition_continues_healthy_services(rig, monkeypatch, tmp_path, missing):
    from livetest import registry
    cases = tmp_path / "cases"
    for name, deps in {"jet": [missing], "pg": ["postgres"], "file": []}.items():
        target = cases / name / "test.yaml"
        target.parent.mkdir(parents=True)
        target.write_text(f"name: {name}\ntql: app.tql\nrequires: {json.dumps(deps)}\nassert:\n  smoke: true\n")
    monkeypatch.setattr(pf, "_CASES", cases)
    monkeypatch.setattr(pf, "manifests_for", REAL_MANIFESTS_FOR)
    assert len(pf.manifests_for(["jet", "pg", "file"])) == 3
    def load(name):
        if name == missing:
            return registry.load_service(name, services_dir=tmp_path / "missing-services")
        return NS(name=name, opt_in_env=None)
    monkeypatch.setattr(pf, "load_service", load)
    monkeypatch.setattr(pf, "_gated_out", REAL_GATED_OUT)
    assert pf.provision(["jet", "pg", "file"], env=rig) == 1
    record = json.loads(Path(rig["SLT_PREFLIGHT_OUTCOMES_PATH"]).read_text())
    assert record["services"][missing]["status"] == "unavailable"
    assert record["services"]["postgres"]["status"] == "ready"


@pytest.mark.parametrize("phase", ["resolve", "cold", "eligibility", "registry"])
def test_empty_exception_reason_uses_type_name(rig, monkeypatch, phase):
    def fail():
        raise TimeoutError()
    if phase == "cold":
        monkeypatch.setattr(pf, "PgAdmin", lambda *a, **kw: NS(ensure_setup=fail))
    elif phase == "resolve":
        def resolve(infra, name, *a, **kw):
            if name == "extdb":
                raise pf.ServiceError()
            return NS(base={})
        monkeypatch.setattr(pf._slt_infra, "resolve_service", resolve)
    elif phase == "eligibility":
        def eligibility(defn, *a):
            if defn.name == "extdb":
                raise pf._prestart.PreUpError()
        monkeypatch.setattr(pf._prestart, "eligibility", eligibility)
    else:
        def load(name):
            if name == "extdb":
                raise pf.RegistryError()
            return NS(name=name)
        monkeypatch.setattr(pf, "load_service", load)
    assert pf.provision(["a"], env=rig) == 1
    record = json.loads(Path(rig["SLT_PREFLIGHT_OUTCOMES_PATH"]).read_text())
    service = "postgres" if phase == "cold" else "extdb"
    expected = {"resolve": "ServiceError", "cold": "TimeoutError",
                "eligibility": "PreUpError", "registry": "RegistryError"}[phase]
    assert record["services"][service]["reason"] == expected


@pytest.mark.parametrize("kind", ["gated", "connection_only"])
@pytest.mark.parametrize("summary_requested", [False, True])
def test_soft_skips_keep_success_exit_code(rig, monkeypatch, tmp_path, kind, summary_requested):
    from livetest.registry import ServiceDef
    monkeypatch.setattr(pf, "manifests_for", lambda ids: [NS(name="a", requires=["optional"], modules=[])])
    definition = ServiceDef(name="optional", dir=tmp_path, isolation="shared")
    monkeypatch.setattr(pf, "load_service", lambda n: definition)
    monkeypatch.setattr(pf, "_gated_out", lambda *a: kind == "gated")
    monkeypatch.setattr(pf._prestart, "prepare_selected", REAL_PREPARE_SELECTED)
    assert pf.provision(["a"], env=rig if summary_requested else {}) == 0
    if summary_requested:
        record = json.loads(Path(rig["SLT_PREFLIGHT_OUTCOMES_PATH"]).read_text())
        assert record["services"]["optional"]["status"] == "unavailable"
        assert record["services"]["optional"]["reason"]
