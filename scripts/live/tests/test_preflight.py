"""Unit tests for the console fan-out pre-flight driver (livetest/preflight.py).

The provisioning itself needs Docker + a cluster and is not exercised here; these cover the pure
union computation, gate logic, and CLI routing (the parts that must be correct BEFORE any
container is touched)."""
import types

import pytest

from livetest import preflight


def _mani(name, requires=(), modules=()):
    return types.SimpleNamespace(name=name, requires=list(requires), modules=list(modules))


def _mod(jar, kind, token="OP"):
    return {"jar": jar, "token": token, "kind": kind, "upload": []}


def test_compute_unions_dedupes_services_preserving_order():
    manifests = [
        _mani("a", requires=["oracle", "kafka"]),
        _mani("b", requires=["kafka", "postgres"]),
        _mani("c", requires=["oracle"]),
    ]
    services, modules = preflight.compute_unions(manifests)
    assert services == ["oracle", "kafka", "postgres"]   # first-seen order, no dupes
    assert modules == []


def test_compute_unions_dedupes_op_modules_by_module_name():
    # Two tests share the same ExampleJsonUdf UDF jar; the union keeps ONE entry (keyed on the
    # module dir name, the build-cache key), so it is built + registered exactly once.
    j = "java/UserDefinedFunctions/ExampleJsonUdf"
    op = "java/OpenProcessors/ExampleMapOp"
    manifests = [
        _mani("a", modules=[_mod(j, kind="udf", token="UDF")]),
        _mani("b", modules=[_mod(j, kind="udf", token="UDF")]),
        _mani("c", modules=[_mod(op, kind="op")]),
    ]
    _services, modules = preflight.compute_unions(manifests)
    assert [m["jar"] for m in modules] == [j, op]         # ExampleJsonUdf de-duplicated
    assert [m["kind"] for m in modules] == ["udf", "op"]


def test_compute_unions_accepts_pomxml_and_dir_refs_as_same_module():
    # A pom.xml ref and its dir ref name the same module (module_name strips /pom.xml).
    manifests = [
        _mani("a", modules=[_mod("java/OpenProcessors/FooOp", kind="op")]),
        _mani("b", modules=[_mod("java/OpenProcessors/FooOp/pom.xml", kind="op")]),
    ]
    _services, modules = preflight.compute_unions(manifests)
    assert len(modules) == 1


def test_services_ungated_by_default():
    # All services run unconditionally without gate flags.
    assert preflight.load_service("kafka").opt_in_env is None
    assert preflight._gated_out("kafka", {}) is False
    assert preflight._gated_out("spanner", {}) is False
    assert preflight._gated_out("gcs", {}) is False
    assert preflight._gated_out("mysql", {}) is False
    assert preflight._gated_out("oracle", {}) is False
    assert preflight._gated_out("postgres", {}) is False


def test_main_routes_to_provision(monkeypatch):
    calls = {}
    def _prov(ids):
        calls["provision"] = ids
        return 0
    monkeypatch.setattr(preflight, "provision", _prov)
    monkeypatch.setattr(preflight, "teardown", lambda ids: pytest.fail("should not tear down"))
    rc = preflight.main(["--tests", "x", "y"])
    assert rc == 0 and calls["provision"] == ["x", "y"]


def test_main_routes_to_teardown(monkeypatch):
    calls = {}
    def _td(ids):
        calls["teardown"] = ids
        return 0
    monkeypatch.setattr(preflight, "teardown", _td)
    monkeypatch.setattr(preflight, "provision", lambda ids: pytest.fail("should not provision"))
    rc = preflight.main(["--tests", "x", "--teardown"])
    assert rc == 0 and calls["teardown"] == ["x"]


def test_main_routes_to_restart(monkeypatch):
    calls = {}
    def _restart():
        calls["restart"] = True
        return 0
    monkeypatch.setattr(preflight, "restart_app_nodes", _restart)
    monkeypatch.setattr(preflight, "provision", lambda ids: pytest.fail("should not provision"))
    rc = preflight.main(["--restart-app-nodes"])
    assert rc == 0 and calls["restart"] is True


def test_teardown_noop_when_keep_services(monkeypatch):
    # SLT_KEEP_SERVICES -> teardown must not touch any container (mirrors pytest_sessionfinish).
    monkeypatch.setattr(preflight, "load_service", lambda s: pytest.fail("must not inspect services"))
    monkeypatch.setattr(preflight, "compose_down", lambda d: pytest.fail("must not tear down"))
    assert preflight.teardown(["a"], env={"SLT_KEEP_SERVICES": "1"}) == 0


def test_provision_no_known_tests_is_noop(monkeypatch):
    monkeypatch.setattr(preflight, "manifests_for", lambda ids: [])
    # must not clear registries or touch the cluster when nothing is selected
    monkeypatch.setattr(preflight, "clear_provision_registry",
                        lambda d: pytest.fail("must not clear registry"))
    assert preflight.provision(["nope"]) == 0


def test_preflight_case_scan_uses_live_cases(monkeypatch, tmp_path):
    cases = tmp_path / "cases"
    for name in ("alpha", "beta"):
        d = cases / "suite" / name
        d.mkdir(parents=True)
        (d / "app.tql").write_text("")
        (d / "test.yaml").write_text(f"name: {name}\ntql: app.tql\nassert:\n  smoke: true\n")
    monkeypatch.setattr(preflight, "_CASES", cases, raising=False)
    assert preflight.known_test_ids() == {"alpha", "beta"}


def test_preflight_case_scan_reads_every_live_case_root(monkeypatch, tmp_path):
    for root, name in (("primary", "alpha"), ("extra", "beta")):
        d = tmp_path / root / "suite" / name
        d.mkdir(parents=True)
        (d / "app.tql").write_text("")
        (d / "test.yaml").write_text(f"name: {name}\ntql: app.tql\nrequires: [{root}svc]\nassert:\n  smoke: true\n")
    monkeypatch.setattr(preflight, "_CASES", tmp_path / "primary", raising=False)
    monkeypatch.setattr(preflight, "_EXTRA_CASES", [tmp_path / "extra"], raising=False)
    assert preflight.known_test_ids() == {"alpha", "beta"}
    assert [m.name for m in preflight.manifests_for(["beta", "alpha"])] == ["beta", "alpha"]


@pytest.mark.parametrize("kind", ["op", "udf"])
@pytest.mark.parametrize("old_bytes,count,restarts", [
    (b"old", 1, 1), (b"final", 1, 0), (b"old", 3, 1), (None, 1, 0),
])
def test_provision_jar_replacement(monkeypatch, tmp_path, capsys, kind,
                                   old_bytes, count, restarts):
    import hashlib

    def final_name(name):
        # OP jars are uploaded and loaded under a content name; UDF jars keep theirs.
        if kind != "op":
            return name
        return preflight.opartifacts.content_addressed_name(
            name, hashlib.sha256(b"final").hexdigest()[:12], "5.4")

    def record(name):
        return "same"

    monkeypatch.setattr(preflight.stack, "_LOCK_DIR", tmp_path / "locks")
    events = []
    modules = [_mod(f"java/Module{i}", kind) for i in range(count)]
    builts = {}
    records = {}
    for i, mod in enumerate(modules):
        path = tmp_path / f"Module{i}-5.4.jar"
        path.write_bytes(b"final")
        built = preflight.opartifacts.BuiltArtifact(path, path.name, f"Module{i}")
        builts[mod["jar"]] = built
        if old_bytes == b"final":
            records[built.name] = record(built.name)
    # Also include an unchanged jar: a restart must register the WHOLE final union.
    path = tmp_path / "Unchanged-5.4.jar"
    path.write_bytes(b"final")
    modules.append(_mod("java/Unchanged", kind))
    builts["java/Unchanged"] = preflight.opartifacts.BuiltArtifact(
        path, path.name, "Unchanged")
    if old_bytes is not None:
        records[path.name] = record(path.name)
    ctx = types.SimpleNamespace(mode="docker", url="fake", user="fake", password="fake")
    client = types.SimpleNamespace(
        load_jar=lambda jar: events.append(("register", jar)),
        load_open_processor_idempotent=lambda jar, tag=None, before_unload=None: events.append(("register", jar)))
    monkeypatch.setattr(preflight, "manifests_for", lambda ids: [_mani("a", modules=modules)])
    monkeypatch.setattr(preflight._slt_infra, "declare_or_log", lambda *a: object())
    monkeypatch.setattr(preflight, "clear_provision_registry", lambda *a: None)
    monkeypatch.setattr(preflight, "_resolve_release", lambda cfg: {})
    monkeypatch.setattr(preflight, "_resolve_striim", lambda cfg: ctx)
    monkeypatch.setattr(preflight, "_bring_up_services", lambda *a, **kw: {})
    monkeypatch.setattr(preflight.StriimClient, "from_url", lambda *a: client)
    monkeypatch.setattr(preflight.opartifacts, "build_jar", lambda ref, *a, **kw: builts[ref])
    monkeypatch.setattr(preflight.opartifacts, "upload_artifacts",
                        lambda ctx, files, **kw: events.extend(("upload", p.name) for p in files))
    # OP jars register through plugin._register_op_jar, which uses plugin's own names.
    from livetest import plugin
    for mod in (preflight, plugin):
        monkeypatch.setattr(mod, "_registry_key", lambda built: "same")
        monkeypatch.setattr(mod, "_loaded_jar_probe",
                            lambda client, jar, **kw: lambda: old_bytes is not None)
    monkeypatch.setattr(preflight.stack, "app_nodes", lambda: ["lane-striim", "lane-node"])

    def run(argv, **kw):
        assert argv[:2] == ["docker", "exec"]
        assert argv[2] in ["lane-striim", "lane-node"]   # never the agent
        assert argv[3] == "sha256sum"
        expected_dir = ".striim/OpenProcessor" if kind == "op" else "UploadedFiles"
        assert expected_dir in argv[4]
        prior = (b"final" if "Unchanged" in argv[4] and old_bytes is not None
                 else old_bytes)
        digest = hashlib.sha256(prior).hexdigest() if prior is not None else ""
        return types.SimpleNamespace(returncode=0 if prior is not None else 1,
                                     stdout=f"{digest}  {argv[4]}\n")

    import subprocess
    monkeypatch.setattr(subprocess, "run", run)

    def restart(c):
        assert c is client
        events.append(("restart", None))
        records.clear()   # restart_app_nodes owns registry clearing + reauthentication

    def register(_dir, jar, fingerprint, cb, **kw):
        if records.get(jar) != fingerprint:
            cb()
            records[jar] = fingerprint

    monkeypatch.setattr(preflight._sp, "restart_app_nodes", restart)
    monkeypatch.setattr(preflight.opregistry, "ensure_registered", register)
    assert preflight.provision(["a"], env={}) == 0
    # OP jars are content-named and never overwritten on the cluster, so they never call for a
    # restart; only UDF jars still do.
    restarts = restarts if kind == "udf" else 0
    assert sum(e[0] == "restart" for e in events) == restarts
    if restarts:
        pivot = events.index(("restart", None))
        assert all(e[0] == "upload" for e in events[:pivot])
        assert all(e[0] == "register" for e in events[pivot + 1:])
        assert [e[1] for e in events[pivot + 1:]] == [final_name(b.name) for b in builts.values()]
        lines = [line for line in capsys.readouterr().out.splitlines()
                 if "restarting app nodes" in line]
        assert len(lines) == 1
        assert all(final_name(f"Module{i}-5.4.jar") in lines[0] for i in range(count))
    elif old_bytes == b"final":
        assert not any(e[0] == "register" for e in events)


@pytest.mark.parametrize("loaded,expected", [(True, ["Udf-5.4.jar"]),
                                            (False, []), (None, ["Udf-5.4.jar"])])
def test_udf_replacement_requires_loaded_evidence(monkeypatch, tmp_path, loaded, expected):
    import hashlib
    import subprocess

    path = tmp_path / "Udf-5.4.jar"
    path.write_bytes(b"final")
    built = preflight.opartifacts.BuiltArtifact(path, path.name, "Udf")
    monkeypatch.setattr(preflight.stack, "app_nodes", lambda: ["lane-striim", "lane-node"])
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        # The primary is current; only the worker retains the old bytes.
        data = b"final" if argv[2] == "lane-striim" else b"old"
        return types.SimpleNamespace(returncode=0, stdout=hashlib.sha256(data).hexdigest())

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(preflight, "_loaded_jar_probe",
                        lambda *a, **kw: lambda: loaded)
    assert preflight._replaced_loaded_jars(types.SimpleNamespace(mode="docker"), object(),
                                          [(_mod("java/Udf", "udf"), built)]) == expected
    assert [argv[2] for argv in calls] == ["lane-striim", "lane-node"]


@pytest.fixture
def replacement_preflight(monkeypatch, tmp_path):
    """Real upload/restart/registry helpers; fake only Docker and Striim boundaries."""
    import hashlib
    import subprocess

    path = tmp_path / "Udf-5.4.jar"
    path.write_bytes(b"new")
    built = preflight.opartifacts.BuiltArtifact(path, path.name, "Udf")
    ctx = types.SimpleNamespace(mode="docker", url="fake", user="fake", password="fake")
    state = types.SimpleNamespace(events=[], published_markers=[], fail=None, interrupt=False,
                                  disk={node: b"old" for node in ("ftcl1-slt-striim", "ftcl1-slt-node")})
    monkeypatch.setenv("SLT_STACK_PREFIX", "ftcl1")
    monkeypatch.setenv("SLT_CLUSTER_SETTLE", "0")
    monkeypatch.setattr(preflight.stack, "_LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(preflight, "manifests_for", lambda ids: [
        _mani("a", modules=[_mod("java/Udf", "udf")])])
    monkeypatch.setattr(preflight._slt_infra, "declare_or_log", lambda *a: object())
    monkeypatch.setattr(preflight, "clear_provision_registry", lambda *a: None)
    monkeypatch.setattr(preflight, "_resolve_release", lambda cfg: {})
    monkeypatch.setattr(preflight, "_resolve_striim", lambda cfg: ctx)
    monkeypatch.setattr(preflight, "_bring_up_services", lambda *a, **kw: {})
    monkeypatch.setattr(preflight.opartifacts, "build_jar", lambda *a, **kw: built)
    monkeypatch.setattr(preflight, "_registry_key", lambda built: "new-fingerprint")
    monkeypatch.setattr(preflight, "_loaded_jar_probe", lambda *a, **kw: lambda: True)
    client = types.SimpleNamespace(load_jar=lambda jar: state.events.append("LOAD"),
                                   list_deployment_groups=lambda: [{"output": []}])
    monkeypatch.setattr(preflight.StriimClient, "from_url", lambda *a: client)
    # Healthy API/readiness cannot substitute for a failed restart.
    monkeypatch.setattr(preflight._sp, "_reauthenticate",
                        lambda *a, **kw: state.events.append("reauth"))
    monkeypatch.setattr(preflight._sp, "wait_cluster_ready",
                        lambda *a, **kw: state.events.append("ready"))
    preflight.opregistry.record(built.name, "old-fingerprint")

    def run(argv, **kw):
        if argv[:3] == ["docker", "exec", "-u"]:
            argv = argv[:2] + argv[4:]         # the user an exec runs as is not the operation
        operation = argv[1] if argv[1] in ("cp", "restart") else argv[3]
        if operation == "sha256sum":
            return types.SimpleNamespace(returncode=0, stderr="",
                                         stdout=hashlib.sha256(state.disk[argv[2]]).hexdigest())
        state.events.append(operation)
        if operation == "cp":
            state.published_markers.append(state.marker.exists())
        if operation == "restart":
            if state.fail == "timeout":
                raise subprocess.TimeoutExpired(argv, kw["timeout"])
            if state.interrupt:
                raise KeyboardInterrupt("cancelled after staging")
        if operation == state.fail:
            return types.SimpleNamespace(returncode=1, stdout="", stderr="injected failure")
        if operation == "mv":
            state.disk[argv[2]] = b"new"
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    state.jar = built.name
    state.marker = preflight.stack.lock_path(".slt-jar-restart-pending")
    return state


def test_failed_restart_does_not_load_on_healthy_old_client(replacement_preflight):
    import json

    state = replacement_preflight
    state.fail = "restart"
    with pytest.raises(RuntimeError, match="injected failure"):
        preflight.provision(["a"], env={})
    assert "LOAD" not in state.events
    assert "reauth" not in state.events and "ready" not in state.events
    assert json.loads(preflight.opregistry.registry_path().read_text())[state.jar] == "old-fingerprint"
    assert state.marker.exists()


@pytest.mark.parametrize("operation", ["cp", "chmod", "mv"])
def test_failed_staging_does_not_restart_load_or_record(replacement_preflight, operation):
    import json

    state = replacement_preflight
    state.fail = operation
    with pytest.raises(preflight.opartifacts.OpArtifactError,
                       match=r"ftcl1-slt-striim.*Udf-5.4.jar.*injected failure"):
        preflight.provision(["a"], env={})
    assert "restart" not in state.events and "LOAD" not in state.events
    assert state.disk["ftcl1-slt-striim"] == b"old"
    assert json.loads(preflight.opregistry.registry_path().read_text())[state.jar] == "old-fingerprint"
    assert state.marker.exists()


def test_interrupted_udf_staging_restarts_on_next_preflight(replacement_preflight):
    state = replacement_preflight
    state.interrupt = True
    with pytest.raises(KeyboardInterrupt, match="cancelled after staging"):
        preflight.provision(["a"], env={})
    assert set(state.disk.values()) == {b"new"}
    assert all(state.published_markers)
    assert "LOAD" not in state.events
    assert state.marker.exists()
    # The marker must precede publication and survive the helper's registry clear.
    preflight.opregistry.clear()
    assert state.marker.exists()
    state.interrupt = False
    state.events.clear()
    assert preflight.provision(["a"], env={}) == 0
    assert state.events == ["restart", "reauth", "ready", "LOAD"]
    assert not state.marker.exists()



def test_restart_timeout_preserves_pending_and_does_not_load(replacement_preflight):
    import subprocess

    state = replacement_preflight
    state.fail = "timeout"
    with pytest.raises(subprocess.TimeoutExpired):
        preflight.provision(["a"], env={})
    assert "LOAD" not in state.events and "reauth" not in state.events
    assert state.marker.exists()


def test_empty_pending_marker_still_requires_restart(replacement_preflight):
    state = replacement_preflight
    state.disk = {node: b"new" for node in state.disk}
    state.marker.write_text("")   # cancellation while writing the marker's jar list
    assert preflight.provision(["a"], env={}) == 0
    assert state.events == ["restart", "reauth", "ready", "LOAD"]
    assert not state.marker.exists()


def test_another_users_restart_marker_completes_without_repeat_restart(
        replacement_preflight, monkeypatch, tmp_path):
    import errno
    from pathlib import Path

    state = replacement_preflight
    real_unlink = Path.unlink

    def refused(self, *args, **kwargs):
        if self == state.marker:
            raise PermissionError(errno.EPERM, "Operation not permitted", str(self))
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refused)
    assert preflight.provision(["a"], env={}) == 0
    assert state.events.count("restart") == 1
    assert state.events[-1] == "LOAD"
    assert state.marker.read_text() == "COMPLETED\n"
    state.events.clear()
    assert preflight.provision(["a"], env={}) == 0
    assert state.events == []
    assert state.marker.read_text() == "COMPLETED\n"
    # A later real replacement must replace COMPLETED before the first upload.
    (tmp_path / state.jar).write_bytes(b"newer")
    state.fail = "cp"
    with pytest.raises(preflight.opartifacts.OpArtifactError):
        preflight.provision(["a"], env={})
    assert state.marker.read_text() == state.jar + "\n"


def test_op_jars_never_count_as_replaced(monkeypatch, tmp_path):
    # Content-named and never overwritten: a byte difference under the name is a timestamp-only
    # rebuild, and counting it would restart the app nodes on every pre-flight.
    import subprocess
    path = tmp_path / "FooOp-abababababab-5.4.jar"
    path.write_bytes(b"rebuilt")
    built = preflight.opartifacts.BuiltArtifact(path, path.name, "FooOp")
    monkeypatch.setattr(preflight.stack, "app_nodes", lambda: ["lane-striim", "lane-node"])
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: calls.append(argv) or
                        types.SimpleNamespace(returncode=0, stdout="0" * 64 + "  x\n"))
    assert preflight._replaced_loaded_jars(types.SimpleNamespace(mode="docker"), object(),
                                          [(_mod("java/FooOp", "op"), built)]) == []
    assert calls == []
