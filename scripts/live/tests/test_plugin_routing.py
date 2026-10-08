import types
from pathlib import Path

import pytest

from livetest.plugin import (
    specs_by_db, _run_admin_sql, _gcs_endpoint_ip, pytest_configure, _node_log_tail,
    require_service_admin, admin_groups, should_keep_resources,
    _resolve_release, build_modules, upload_modules, upload_op_uploads,
    _running_striim_version, _upload_dest_name, _rendered_tql,
)
from livetest.manifest import TestManifest as _Manifest, _normalize_uploads
from livetest import opartifacts, plugin as _plugin


# ---- _running_striim_version (strict: reused cluster must match the built version) --------

def test_running_striim_version_parses_image_tag(monkeypatch):
    monkeypatch.setattr(_plugin.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="slt-striim:5.4.0.6C\n"))
    assert _running_striim_version() == "5.4.0.6C"

def test_running_striim_version_none_when_no_tag(monkeypatch):
    monkeypatch.setattr(_plugin.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="slt-striim\n"))
    assert _running_striim_version() is None

def test_running_striim_version_none_on_docker_error(monkeypatch):
    monkeypatch.setattr(_plugin.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=1, stdout=""))
    assert _running_striim_version() is None


# ---- _node_log_tail (OP-loader recovery classify, review #5) ----------------

def test_node_log_tail_native_returns_empty():
    assert _node_log_tail(types.SimpleNamespace(mode="native")) == ""

def test_node_log_tail_docker_concats_both_nodes():
    seen = []
    def run(argv):
        seen.append(argv)
        return types.SimpleNamespace(stdout="LOGDATA", returncode=0)
    out = _node_log_tail(types.SimpleNamespace(mode="docker"), run=run)
    assert out.count("LOGDATA") == 2                      # both app nodes read
    assert all("tail" in a and "docker" in a for a in seen)


# ---- xdist serial guard (#7) ------------------------------------------------

def test_xdist_guard_rejects_parallel_run(monkeypatch):
    # Clear SLT_PARALLEL: the guard only fires when it's UNSET, and this suite may itself be
    # invoked under a parallel run (SLT_PARALLEL set), which would otherwise mask the guard.
    monkeypatch.delenv("SLT_PARALLEL", raising=False)
    cfg = types.SimpleNamespace(option=types.SimpleNamespace(numprocesses=4, dist="load"))
    with pytest.raises(pytest.UsageError, match="SERIALLY"):
        pytest_configure(cfg)

def test_xdist_guard_allows_serial_run(monkeypatch):
    monkeypatch.delenv("SLT_PARALLEL", raising=False)
    cfg = types.SimpleNamespace(option=types.SimpleNamespace(numprocesses=None, dist="no"))
    pytest_configure(cfg)   # no -n/--dist -> no raise regardless of env

def test_xdist_guard_tolerates_missing_options():
    # xdist not installed -> numprocesses/dist options don't exist; guard must not fire.
    pytest_configure(types.SimpleNamespace(option=types.SimpleNamespace()))

def test_every_service_marker_is_registered_in_pyproject():
    # An unregistered marker warns at use, and the suite may run warnings as errors. Keep the
    # plugin's list and pyproject's registration in lockstep.
    import pathlib
    from livetest.plugin import _SERVICE_MARKERS
    text = (pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    for svc in _SERVICE_MARKERS:
        assert f'"{svc}: live test whose manifest requires' in text, f"{svc} not registered"


# ---- _gcs_endpoint_ip -------------------------------------------------------

def test_gcs_endpoint_ip_native_is_loopback():
    assert _gcs_endpoint_ip(types.SimpleNamespace(mode="native")) == "127.0.0.1"

def test_gcs_endpoint_ip_docker_returns_resolved_ip():
    ctx = types.SimpleNamespace(mode="docker")
    run = lambda argv: types.SimpleNamespace(returncode=0, stdout="198.51.100.254  host.docker.internal", stderr="")
    assert _gcs_endpoint_ip(ctx, run=run) == "198.51.100.254"

def test_gcs_endpoint_ip_docker_raises_on_unresolvable_instead_of_wrong_loopback():
    # The footgun fix: a failed getent must NOT silently return 127.0.0.1 (the container's
    # own loopback in docker mode) — it must fail loud with the root cause.
    ctx = types.SimpleNamespace(mode="docker")
    run = lambda argv: types.SimpleNamespace(returncode=2, stdout="", stderr="getent: not found")
    with pytest.raises(RuntimeError, match="host.docker.internal"):
        _gcs_endpoint_ip(ctx, run=run)

def test_specs_by_db_defaults_to_postgres_source():
    # a spec with no db defaults to the postgres SOURCE route (qasource); oracle-source is explicit.
    specs = [{"target": "a"}, {"target": "b", "db": "oracle-source"}, {"target": "c", "db": "postgres-source"}]
    groups = specs_by_db(specs)
    assert set(groups) == {"postgres-source", "oracle-source"}
    assert [s["target"] for s in groups["postgres-source"]] == ["a", "c"]
    assert [s["target"] for s in groups["oracle-source"]] == ["b"]

class _RecAdmin:
    def __init__(self): self.calls = []
    def run_sql(self, *args): self.calls.append(args)

def test_run_admin_sql_fixed_schema_omits_schema():
    a = _RecAdmin()
    _run_admin_sql({"admin": a, "schema": None}, "CREATE TABLE QASOURCE.T (I NUMBER)")
    assert a.calls == [("CREATE TABLE QASOURCE.T (I NUMBER)",)]


# ---- require_service_admin / admin_groups / should_keep_resources -----------

def test_require_service_admin_returns_entry_and_raises_on_missing():
    admins = {"postgres": {"admin": object(), "schema": "s"}}
    assert require_service_admin(admins, "postgres", "my_test", "file 'x.sql'") is admins["postgres"]
    with pytest.raises(AssertionError, match="requires") as ei:
        require_service_admin(admins, "oracle", "my_test", "file 'x.sql'")
    assert "oracle" in str(ei.value)

def test_admin_groups_routes_by_db():
    pg_admin = object()
    ora_admin = object()
    admins = {
        "postgres-source": {"admin": pg_admin, "schema": "s"},
        "oracle-source": {"admin": ora_admin, "schema": None},
    }
    specs = [{"target": "a"}, {"target": "b", "db": "oracle-source"}, {"target": "c"}]
    groups = admin_groups(admins, specs, "my_test", "data")
    by_admin = {id(admin): [s["target"] for s in group] for admin, group in groups}
    assert by_admin[id(pg_admin)] == ["a", "c"]
    assert by_admin[id(ora_admin)] == ["b"]

def test_admin_groups_fails_fast_on_unrequired_db():
    admins = {"postgres": {"admin": object(), "schema": "s"}}
    specs = [{"target": "a", "db": "oracle"}]
    with pytest.raises(AssertionError, match="requires"):
        admin_groups(admins, specs, "my_test", "data")

def test_should_keep_resources_truth_table():
    assert should_keep_resources({"SLT_KEEP_RESOURCES_ON_ERROR": "1"}, True, False) is True
    # The attempt is what counts: an app that crashes AT deploy/start raises out of
    # deploy_tql (deployed never set), but the namespace/app exist server-side and must
    # be kept for inspection -- the regression behind this row was teardown_namespace
    # destroying a crashed app despite SLT_KEEP_RESOURCES_ON_ERROR=1.
    assert should_keep_resources({"SLT_KEEP_RESOURCES_ON_ERROR": "1"}, True, False) is True   # crash at deploy/start
    assert should_keep_resources({}, True, False) is False                                   # env not set
    assert should_keep_resources({"SLT_KEEP_RESOURCES_ON_ERROR": "1"}, False, False) is False  # failed before any deploy attempt
    assert should_keep_resources({"SLT_KEEP_RESOURCES_ON_ERROR": "1"}, True, True) is False    # succeeded
    # SLT_KEEP_RESOURCES keeps regardless of outcome or deploy state
    assert should_keep_resources({"SLT_KEEP_RESOURCES": "1"}, True, True) is True     # kept on success
    assert should_keep_resources({"SLT_KEEP_RESOURCES": "1"}, True, False) is True    # kept on failure
    assert should_keep_resources({"SLT_KEEP_RESOURCES": "1"}, False, True) is True    # kept even if not deployed
    # SLT_SKIP_VERIFY is independent of keep: it does NOT keep on its own (a skip-verify
    # run without SLT_KEEP_RESOURCES tears down, and only warns). Guards the design
    # decision that the flags don't imply one another.
    assert should_keep_resources({"SLT_SKIP_VERIFY": "1"}, True, False) is False
    assert should_keep_resources({"SLT_SKIP_VERIFY": "1", "SLT_KEEP_RESOURCES": "1"}, True, False) is True


def test_skip_shared_teardown_truth_table():
    from livetest.plugin import _skip_shared_teardown
    # Any xdist worker must never tear down shared infra (another process owns it).
    assert _skip_shared_teardown({"PYTEST_XDIST_WORKER": "gw0"}) is True
    # SLT_KEEP_SERVICES keeps shared infra up (existing behavior, now folded in).
    assert _skip_shared_teardown({"SLT_KEEP_SERVICES": "1"}) is True
    # A clean serial run tears down as before.
    assert _skip_shared_teardown({}) is False


def test_op_poison_marker_path_is_sibling_of_junit(tmp_path):
    from livetest.plugin import _op_poison_marker_path
    # The poison marker sits next to the junit xml with a .op-poisoned.flag suffix, so the
    # console post-flight (§C.5) can find it by deriving the path from the junit it named.
    assert _op_poison_marker_path(tmp_path / "live-foo.xml") == tmp_path / "live-foo.xml.op-poisoned.flag"
    # No --junitxml -> nothing to pair a marker with.
    assert _op_poison_marker_path(None) is None


# ---- teardown_derived_resources (per-test GCS bucket / Kafka topic teardown, spec §A.4) ----

from livetest.plugin import teardown_derived_resources


class _RecGcsAdmin:
    def __init__(self): self.deleted = []
    def delete_bucket(self, name): self.deleted.append(name)


class _RecKafkaAdmin:
    def __init__(self): self.deleted = []
    def delete_topic(self, name): self.deleted.append(name)


def test_teardown_derived_resources_deletes_each_bucket_and_topic():
    gcs = _RecGcsAdmin()
    kafka = _RecKafkaAdmin()
    teardown_derived_resources(
        gcs_cleanup=[(gcs, "slt-t1-src"), (gcs, "slt-t1-tgt")],
        kafka_cleanup=[(kafka, "slt_t1_src"), (kafka, "slt_t1_tgt")],
    )
    assert gcs.deleted == ["slt-t1-src", "slt-t1-tgt"]
    assert kafka.deleted == ["slt_t1_src", "slt_t1_tgt"]


def test_teardown_derived_resources_is_best_effort():
    class _BoomAdmin:
        def delete_bucket(self, name): raise RuntimeError("boom")
        def delete_topic(self, name): raise RuntimeError("boom")
    boom = _BoomAdmin()
    ok = _RecKafkaAdmin()
    # one raising admin must not stop the others from being torn down
    teardown_derived_resources(gcs_cleanup=[(boom, "x")], kafka_cleanup=[(ok, "y")])
    assert ok.deleted == ["y"]


def test_teardown_derived_resources_empty_lists_are_a_noop():
    teardown_derived_resources(gcs_cleanup=[], kafka_cleanup=[])   # must not raise


# ---- derive_per_test_base call site (per-test kafka/gcs identity, spec §A.3) ----

def test_runtest_keys_derived_names_on_hashed_per_test_id():
    # kafka/gcs name derivation must key on the UN-GATED hashed per-test id --
    # _tid_oracle(m.name).lower(), the same identity the ${TID} token carries when
    # parallel, minus that token's trailing "_" separator -- NOT the readable slug
    # (unbounded length, and not the TID the run's other per-test objects carry) and
    # NOT the gated token value (empty when serial). runtest needs live services, so
    # pin the call site at source level rather than executing it.
    import inspect
    src = inspect.getsource(_plugin.LiveItem._runtest)
    # the hashed per-test id is run + worker + case scoped
    # (livetest.runident) and still un-gated; kafka/gcs derivation still keys on it.
    assert "ident = _slt_runident.derive(m.name, os.environ)" in src
    assert "slug, per_test = ident.slug, ident.per_test" in src
    assert "derive_per_test_base(svc, resolved.base, per_test)" in src
    assert src.index("slug, per_test = ident.slug, ident.per_test") \
        < src.index("derive_per_test_base(svc, resolved.base, per_test)")


# ---- _resolve_release (cached once per session on config) ------------------------

def test_resolve_release_caches_on_config(monkeypatch):
    from livetest import plugin as plugin_mod
    calls = []
    monkeypatch.setattr(plugin_mod._releases, "resolve_release",
                         lambda env: calls.append(env) or {"STRIIM_SERIES": "5.4"})
    cfg = types.SimpleNamespace()
    first = _resolve_release(cfg)
    second = _resolve_release(cfg)
    assert first is second == {"STRIIM_SERIES": "5.4"}
    assert len(calls) == 1   # resolved only once, cached on the config object thereafter


def test_resolve_release_reports_autodetect_from_striim_home(monkeypatch):
    # When STRIIM_HOME is set, the harness auto-detects and surfaces the version it built + tests.
    from livetest import plugin as plugin_mod
    monkeypatch.setattr(plugin_mod._releases, "resolve_release",
                         lambda env: {"STRIIM_VERSION": "5.4.0.6C", "STRIIM_SERIES": "5.4", "JAVA_RELEASE": "17"})
    monkeypatch.setenv("STRIIM_HOME", "/opt/Striim")
    lines = []
    tr = types.SimpleNamespace(write_line=lambda s: lines.append(s))
    cfg = types.SimpleNamespace(pluginmanager=types.SimpleNamespace(getplugin=lambda name: tr))
    _resolve_release(cfg)
    assert len(lines) == 1
    assert "STRIIM_VERSION=5.4.0.6C" in lines[0]
    assert "detected from STRIIM_HOME=/opt/Striim" in lines[0]


def test_resolve_release_reports_default_when_striim_home_unset(monkeypatch):
    from livetest import plugin as plugin_mod
    monkeypatch.setattr(plugin_mod._releases, "resolve_release",
                         lambda env: {"STRIIM_VERSION": "5.4.0.6", "STRIIM_SERIES": "5.4", "JAVA_RELEASE": "17"})
    monkeypatch.delenv("STRIIM_HOME", raising=False)
    lines = []
    tr = types.SimpleNamespace(write_line=lambda s: lines.append(s))
    cfg = types.SimpleNamespace(pluginmanager=types.SimpleNamespace(getplugin=lambda name: tr))
    _resolve_release(cfg)
    assert "STRIIM_HOME unset — using default" in lines[0]


# ---- build_modules / upload_modules (op:/udf: sibling-key, single-or-list) --------

def _manifest(modules, uploads=None, source_dir=None) -> _Manifest:
    source_dir = source_dir or Path("/test/dir")
    return _Manifest(
        name="t", tql="app.tql", dir=source_dir,
        modules=modules,
        op_uploads=_normalize_uploads(uploads, Path("test.yaml")), source_dir=source_dir)

def test_build_modules_builds_each_module_against_the_release(monkeypatch, tmp_path):
    calls = []
    def fake_build_jar(jar_ref, release, report=None):
        calls.append((jar_ref, release))
        name = f"{jar_ref.rsplit('/', 1)[-1]}-5.4.jar"
        # A REAL file: build_modules hashes the jar while it still holds the build lock, so a
        # fake pointing at a nonexistent path no longer models a successful build.
        jar = tmp_path / name
        jar.write_bytes(name.encode())
        return opartifacts.BuiltArtifact(path=jar, name=name,
                                          op_name=name[:-len("-5.4.jar")])
    monkeypatch.setattr(opartifacts, "build_jar", fake_build_jar)
    m = _manifest([
        {"jar": "java/OpenProcessors/FooOp", "token": "FOO", "kind": "op", "upload": []},
        {"jar": "java/UserDefinedFunctions/BarUdf", "token": "BAR", "kind": "udf", "upload": []},
    ])
    release = {"STRIIM_SERIES": "5.4"}
    builts = build_modules(m, release)
    assert [c[0] for c in calls] == ["java/OpenProcessors/FooOp", "java/UserDefinedFunctions/BarUdf"]
    assert all(c[1] is release for c in calls)
    # The fake jar is not a zip, so its content tag falls back to the file's sha256.
    import hashlib
    foo = opartifacts.content_addressed_name(
        "FooOp-5.4.jar", hashlib.sha256(b"FooOp-5.4.jar").hexdigest()[:12], "5.4")
    # OP jars are uploaded under a content name; UDF jars keep theirs. op_name is unchanged.
    assert [b.name for _, b in builts] == [foo, "BarUdf-5.4.jar"]
    assert [b.op_name for _, b in builts] == ["FooOp", "BarUdf"]
    # module dicts are passed through unchanged, paired with their BuiltArtifact
    assert [mod["token"] for mod, _ in builts] == ["FOO", "BAR"]

def test_build_modules_reports_rebuild_trigger_per_module(monkeypatch, tmp_path):
    # A module whose build_jar fires the `report` callback surfaces the trigger reason
    # tagged with the module's token, via the run-level (label, phase) reporter.
    def fake_build_jar(jar_ref, release, report=None):
        if report is not None:
            report("source changed (Foo.java is newer than FooOp-5.4.jar) -> clean rebuild")
        name = f"{jar_ref.rsplit('/', 1)[-1]}-5.4.jar"
        jar = tmp_path / name
        jar.write_bytes(name.encode())
        return opartifacts.BuiltArtifact(path=jar, name=name,
                                          op_name=name[:-len("-5.4.jar")])
    monkeypatch.setattr(opartifacts, "build_jar", fake_build_jar)
    m = _manifest([{"jar": "java/OpenProcessors/FooOp", "token": "FOO", "kind": "op", "upload": []}])
    reported = []
    build_modules(m, {"STRIIM_SERIES": "5.4"}, report=lambda label, phase: reported.append((label, phase)))
    assert reported == [("t", "rebuilding FOO jar: source changed "
                              "(Foo.java is newer than FooOp-5.4.jar) -> clean rebuild")]

def test_build_modules_propagates_op_artifact_error_for_skip(monkeypatch):
    monkeypatch.setattr(opartifacts, "build_jar",
                         lambda *a, **k: (_ for _ in ()).throw(opartifacts.OpArtifactError("boom")))
    m = _manifest([{"jar": "java/OpenProcessors/FooOp", "token": "FOO", "kind": "op", "upload": []}])
    with pytest.raises(opartifacts.OpArtifactError, match="boom"):
        build_modules(m, {"STRIIM_SERIES": "5.4"})

def test_upload_modules_op_kind_sets_tokens_and_does_not_load(tmp_path, monkeypatch):
    monkeypatch.setattr(opartifacts, "upload_artifacts", lambda ctx, files: None)
    built = opartifacts.BuiltArtifact(path=tmp_path / "ExampleMapOpV8G-5.4.jar",
                                       name="ExampleMapOpV8G-5.4.jar", op_name="ExampleMapOpV8G")
    mod = {"jar": "java/OpenProcessors/ExampleMapOp", "token": "OP", "kind": "op", "upload": []}
    m = _manifest([mod], source_dir=tmp_path)
    tokens = {}
    client = types.SimpleNamespace(load_jar=lambda name: pytest.fail("an op: module must not LOAD"))
    files, jar_names = upload_modules(m, [(mod, built)], object(), client, tokens)
    assert tokens["OP_JAR"] == "ExampleMapOpV8G-5.4.jar"
    assert tokens["OP_NAME"] == "ExampleMapOpV8G"
    assert jar_names == ["ExampleMapOpV8G-5.4.jar"]
    assert files == [built.path]

def test_upload_modules_udf_kind_loads_jar_with_default_token(tmp_path, monkeypatch):
    monkeypatch.setattr(opartifacts, "upload_artifacts", lambda ctx, files: None)
    built = opartifacts.BuiltArtifact(path=tmp_path / "ExampleJsonUdfV1-5.4.jar",
                                       name="ExampleJsonUdfV1-5.4.jar", op_name="ExampleJsonUdfV1")
    mod = {"jar": "java/UserDefinedFunctions/ExampleJsonUdf", "token": "UDF", "kind": "udf", "upload": []}
    m = _manifest([mod], source_dir=tmp_path)
    tokens = {}
    loaded = []
    client = types.SimpleNamespace(load_jar=lambda name: loaded.append(name))
    upload_modules(m, [(mod, built)], object(), client, tokens)
    assert tokens["UDF_JAR"] == "ExampleJsonUdfV1-5.4.jar"
    assert tokens["UDF_NAME"] == "ExampleJsonUdfV1"
    assert loaded == ["ExampleJsonUdfV1-5.4.jar"]

def test_upload_modules_list_form_sets_each_token_uploads_all_and_loads_udf_only(tmp_path, monkeypatch):
    uploaded = {}
    monkeypatch.setattr(opartifacts, "upload_artifacts",
                         lambda ctx, files: uploaded.setdefault("files", list(files)))
    foo = opartifacts.BuiltArtifact(path=tmp_path / "FooOp-5.4.jar", name="FooOp-5.4.jar", op_name="FooOp")
    bar = opartifacts.BuiltArtifact(path=tmp_path / "BarUdf-5.4.jar", name="BarUdf-5.4.jar", op_name="BarUdf")
    foo_mod = {"jar": "java/OpenProcessors/FooOp", "token": "FOO", "kind": "op", "upload": []}
    bar_mod = {"jar": "java/UserDefinedFunctions/BarUdf", "token": "BAR", "kind": "udf", "upload": []}
    m = _manifest([foo_mod, bar_mod], source_dir=tmp_path)
    tokens = {}
    loaded = []
    client = types.SimpleNamespace(load_jar=lambda name: loaded.append(name))
    files, jar_names = upload_modules(m, [(foo_mod, foo), (bar_mod, bar)], object(), client, tokens)
    assert tokens["FOO_JAR"] == "FooOp-5.4.jar" and tokens["FOO_NAME"] == "FooOp"
    assert tokens["BAR_JAR"] == "BarUdf-5.4.jar" and tokens["BAR_NAME"] == "BarUdf"
    assert loaded == ["BarUdf-5.4.jar"]                # only the udf: module is LOADed
    assert jar_names == ["FooOp-5.4.jar", "BarUdf-5.4.jar"]
    assert set(uploaded["files"]) == {foo.path, bar.path}   # jars only -- uploads moved out
    assert set(files) == set(uploaded["files"])

def test_upload_modules_load_false_suppresses_udf_load_for_xdist(tmp_path, monkeypatch):
    # Parallel path (spec §B.4): with load=False, upload_modules still uploads + sets
    # tokens but does NOT globally load the UDF jar -- the caller register-once loads it
    # exactly once across xdist workers instead. Tokens must still be derived (workers need
    # ${*_JAR}/${*_NAME} to render the TQL).
    monkeypatch.setattr(opartifacts, "upload_artifacts", lambda ctx, files: None)
    built = opartifacts.BuiltArtifact(path=tmp_path / "ExampleJsonUdfV1-5.4.jar",
                                       name="ExampleJsonUdfV1-5.4.jar", op_name="ExampleJsonUdfV1")
    mod = {"jar": "java/UserDefinedFunctions/ExampleJsonUdf", "token": "UDF", "kind": "udf", "upload": []}
    m = _manifest([mod], source_dir=tmp_path)
    tokens = {}
    client = types.SimpleNamespace(
        load_jar=lambda name: pytest.fail("load=False must not globally LOAD the UDF jar"))
    files, jar_names = upload_modules(m, [(mod, built)], object(), client, tokens, load=False)
    assert tokens["UDF_JAR"] == "ExampleJsonUdfV1-5.4.jar"   # token still derived for the worker
    assert jar_names == ["ExampleJsonUdfV1-5.4.jar"]


# ---- upload_op_uploads (per-test render + rename of op.uploads, spec B.3) ------------

def test_upload_op_uploads_renders_text_and_renames_per_test(tmp_path, monkeypatch):
    uploaded = {}
    monkeypatch.setattr(opartifacts, "upload_artifacts",
                         lambda ctx, files: uploaded.setdefault("files", list(files)))
    src = tmp_path / "merge.json"
    src.write_text('{"tableName": "QASOURCE.${TID}SRC"}')
    m = _manifest([], uploads=["merge.json"], source_dir=tmp_path)
    tokens = {"TID": "mytest_"}   # the ${TID} value carries its own trailing '_' when parallel
    paths, renames = upload_op_uploads(m, object(), tokens)
    assert [p.name for p in paths] == ["mytest_merge.json"]
    assert paths[0].read_text() == '{"tableName": "QASOURCE.mytest_SRC"}'
    assert uploaded["files"] == paths
    assert renames == {"merge.json": "mytest_merge.json"}


def test_upload_op_uploads_serial_keeps_original_name(tmp_path, monkeypatch):
    # Serial run: ${TID} is "" so the uploaded file keeps its original name (no leading '_').
    uploaded = {}
    monkeypatch.setattr(opartifacts, "upload_artifacts",
                         lambda ctx, files: uploaded.setdefault("files", list(files)))
    src = tmp_path / "merge.json"
    src.write_text('{"tableName": "QASOURCE.${TID}SRC"}')
    m = _manifest([], uploads=["merge.json"], source_dir=tmp_path)
    paths, renames = upload_op_uploads(m, object(), {"TID": ""})
    assert [p.name for p in paths] == ["merge.json"]
    assert paths[0].read_text() == '{"tableName": "QASOURCE.SRC"}'
    assert renames == {"merge.json": "merge.json"}


def test_upload_op_uploads_leaves_binary_content_untouched_but_renames(tmp_path, monkeypatch):
    monkeypatch.setattr(opartifacts, "upload_artifacts", lambda ctx, files: None)
    src = tmp_path / "products.csv"
    binary = b"\xff\xfe\x00not-utf8"
    src.write_bytes(binary)
    m = _manifest([], uploads=["products.csv"], source_dir=tmp_path)
    paths, renames = upload_op_uploads(m, object(), {"TID": "mytest_"})
    assert paths[0].name == "mytest_products.csv"
    assert paths[0].read_bytes() == binary   # untouched -- only the FILENAME changed
    assert renames == {"products.csv": "mytest_products.csv"}


def test_upload_op_uploads_empty_list_is_a_noop(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(opartifacts, "upload_artifacts", lambda ctx, files: called.append(files))
    m = _manifest([], uploads=[], source_dir=tmp_path)
    paths, renames = upload_op_uploads(m, object(), {"TID": "mytest"})
    assert paths == []
    assert renames == {}


def test_upload_op_uploads_explicit_to_ignores_tid_uses_rendered_to(tmp_path, monkeypatch):
    # {from, to} entries fully own the uploaded name -- no automatic ${TID} prefix -- so a
    # shipped example's own clean "UploadedFiles/<from>" reference can be rewritten to the
    # per-test <to> name instead (see _rendered_tql below).
    monkeypatch.setattr(opartifacts, "upload_artifacts", lambda ctx, files: None)
    src = tmp_path / "customer_lookup.json"
    src.write_text('{"tableName": "CUSTOMERS"}')
    m = _manifest([], uploads=[{"from": "customer_lookup.json", "to": "${NS}-customer_lookup.json"}],
                  source_dir=tmp_path)
    paths, renames = upload_op_uploads(m, object(), {"TID": "mytest_", "NS": "SLT_mytest"})
    assert [p.name for p in paths] == ["SLT_mytest-customer_lookup.json"]
    assert renames == {"customer_lookup.json": "SLT_mytest-customer_lookup.json"}


def test_rendered_tql_rewrites_untokenized_upload_reference(tmp_path):
    (tmp_path / "app.tql").write_text(
        "ConfigFile: 'UploadedFiles/customer_lookup.json', Ns: '${NS}'")
    m = _manifest([], uploads=[{"from": "customer_lookup.json", "to": "${NS}-customer_lookup.json"}],
                  source_dir=tmp_path)
    tokens = {"NS": "SLT_mytest"}
    renames = {"customer_lookup.json": "SLT_mytest-customer_lookup.json"}
    text = _rendered_tql(m, tokens, renames)
    assert "UploadedFiles/SLT_mytest-customer_lookup.json" in text
    assert "Ns: 'SLT_mytest'" in text


def test_upload_dest_name_back_compat_string_vs_explicit_to():
    assert _upload_dest_name("merge.json", None, "mytest_", {}) == "mytest_merge.json"
    assert _upload_dest_name("merge.json", "${NS}-merge.json", "mytest_", {"NS": "SLT_x"}) == "SLT_x-merge.json"


def test_upload_op_uploads_bare_to_matching_from_is_a_pure_passthrough(tmp_path, monkeypatch):
    # {from: x, to: x} (no token in `to`) -- the shape every LookupOp example test.yaml
    # actually uses: the shipped example's TQL keeps its clean, untokenized ConfigFile
    # reference, and the upload lands under that exact same bare name (no TID prefix, no NS
    # rename) -- correct for a test that never runs concurrently with another test uploading
    # the same basename, but NOT collision-safe if it does (see TEST-YAML.md).
    monkeypatch.setattr(opartifacts, "upload_artifacts", lambda ctx, files: None)
    src = tmp_path / "products_lookup.json"
    src.write_text('{"tableName": "${TID}PRODUCTS"}')
    m = _manifest([], uploads=[{"from": "products_lookup.json", "to": "products_lookup.json"}],
                  source_dir=tmp_path)
    paths, renames = upload_op_uploads(m, object(), {"TID": "mytest_"})
    assert [p.name for p in paths] == ["products_lookup.json"]
    assert renames == {"products_lookup.json": "products_lookup.json"}
    (tmp_path / "app.tql").write_text("ConfigFile: 'UploadedFiles/products_lookup.json'")
    text = _rendered_tql(m, {"TID": "mytest_"}, renames)
    assert "UploadedFiles/products_lookup.json" in text


# ---- upload=False: the jar upload moves inside the register-once lock (xdist) ----


def test_upload_modules_upload_false_sets_tokens_without_uploading(tmp_path, monkeypatch):
    # Parallel path: the caller uploads inside the register-once lock instead, because
    # `docker cp` of the same jar to the same path from N workers interleaves and a
    # sibling's LOAD reads a half-written file ("ZipFile invalid LOC header").
    monkeypatch.setattr(
        opartifacts, "upload_artifacts",
        lambda ctx, files: pytest.fail("upload=False must not upload here"))
    built = opartifacts.BuiltArtifact(path=tmp_path / "FooOp-5.4.jar",
                                      name="FooOp-5.4.jar", op_name="FooOp")
    mod = {"jar": "java/OpenProcessors/FooOp", "token": "FOO", "kind": "op", "upload": []}
    m = _manifest([mod], source_dir=tmp_path)
    tokens = {}
    files, jar_names = upload_modules(
        m, [(mod, built)], object(), object(), tokens, load=False, upload=False)
    # Tokens are still derived: EVERY worker needs them to render its TQL, regardless
    # of which worker actually performed the upload.
    assert tokens["FOO_JAR"] == "FooOp-5.4.jar" and tokens["FOO_NAME"] == "FooOp"
    assert jar_names == ["FooOp-5.4.jar"]
    assert files == [built.path]          # still reported for poison-recovery re-upload


def test_upload_modules_uploads_by_default(tmp_path, monkeypatch):
    # The serial path is unchanged: one process, no race, upload here as before.
    seen = {}
    monkeypatch.setattr(opartifacts, "upload_artifacts",
                        lambda ctx, files: seen.setdefault("files", list(files)))
    built = opartifacts.BuiltArtifact(path=tmp_path / "FooOp-5.4.jar",
                                      name="FooOp-5.4.jar", op_name="FooOp")
    mod = {"jar": "java/OpenProcessors/FooOp", "token": "FOO", "kind": "op", "upload": []}
    m = _manifest([mod], source_dir=tmp_path)
    upload_modules(m, [(mod, built)], object(), object(), {})
    assert seen["files"] == [built.path]


def test_register_once_uploads_exactly_once_across_workers(tmp_path, monkeypatch):
    """THE REGRESSION: N workers must produce exactly ONE upload of a shared jar.

    Drives the real opregistry across simulated workers with the same closure shape
    plugin.py builds, asserting the upload is inside the lock rather than beside it.
    """
    from livetest import opregistry
    built = opartifacts.BuiltArtifact(path=tmp_path / "Big-5.4.jar",
                                      name="Big-5.4.jar", op_name="Big")
    uploads, loads = [], []

    def worker():
        def _register(b=built):
            uploads.append(b.path)          # upload INSIDE the lock ...
            loads.append(b.name)            # ... immediately before the load
        opregistry.ensure_registered(tmp_path, built.name, "fp-1", _register)

    for _ in range(3):                       # -n 3
        worker()

    assert uploads == [built.path], "a shared jar must upload exactly once across workers"
    assert loads == [built.name]


def test_register_once_re_uploads_when_the_jar_changes(tmp_path):
    # A rebuilt jar has a new fingerprint, so it must be re-uploaded, not skipped --
    # otherwise workers would LOAD stale bytes.
    from livetest import opregistry
    uploads = []
    for fp in ("fp-1", "fp-1", "fp-2"):
        opregistry.ensure_registered(tmp_path, "Big-5.4.jar", fp,
                                     lambda fp=fp: uploads.append(fp))
    assert uploads == ["fp-1", "fp-2"]


# --- per-service markers ----------------------------------------------------------------
# Each live item is marked with the services its manifest declares, so a runner can select on
# what a test USES rather than on what it is called. `-k "not spanner"` -- what README.md used
# to prescribe for parallel runs -- misses every test that reaches Spanner through a
# customer-named directory.

def test_declared_services_reads_requires(tmp_path):
    from livetest.plugin import _declared_services
    p = tmp_path / "test.yaml"
    p.write_text("name: x\ntql: app.tql\nrequires: [oracle, spanner]\nassert:\n  smoke: true\n")
    assert _declared_services(p) == ["oracle", "spanner"]

def test_declared_services_is_empty_when_requires_absent(tmp_path):
    from livetest.plugin import _declared_services
    p = tmp_path / "test.yaml"
    p.write_text("name: x\ntql: app.tql\nassert:\n  smoke: true\n")
    assert _declared_services(p) == []

def test_declared_services_tolerates_unparseable_yaml(tmp_path):
    # Collection must not die on a manifest the loader would reject: that error belongs at RUN
    # time, where it names the file and the reason. An unreadable file simply gets no service
    # markers, which lands it in the default serial-safe bucket rather than dropping it.
    from livetest.plugin import _declared_services
    p = tmp_path / "test.yaml"
    p.write_text("name: [unclosed\n")
    assert _declared_services(p) == []

def test_declared_services_ignores_non_string_entries(tmp_path):
    from livetest.plugin import _declared_services
    p = tmp_path / "test.yaml"
    p.write_text("name: x\ntql: app.tql\nrequires: [oracle, 7, null]\nassert:\n  smoke: true\n")
    assert _declared_services(p) == ["oracle"]

def test_every_service_dir_has_a_marker():
    # The marker list must keep up with services/: a service nobody can select on is a service
    # the parallel split cannot protect.
    import pathlib
    from livetest.plugin import _SERVICE_MARKERS
    services = {p.name for p in (pathlib.Path(__file__).resolve().parents[1] / "services").iterdir()
                if p.is_dir() and p.name != "striim"}
    assert services <= set(_SERVICE_MARKERS), f"unmarked services: {sorted(services - set(_SERVICE_MARKERS))}"


def test_op_jar_records_are_keyed_by_the_op_not_the_build_or_file(monkeypatch):
    # One record per OP (its Striim-Module-Name): a new build, or the same OP under another file
    # name, replaces it in the same write; a sibling holding another build's record sees the
    # mismatch and re-registers.
    from livetest import plugin
    monkeypatch.setattr(plugin, "_cluster_generation", lambda: "gen")
    a = opartifacts.BuiltArtifact(Path("x"), "FooOp-aaaaaaaaaaaa-5.4.jar", "FooOp",
                                  fingerprint="a" * 64, built_name="FooOp-5.4.jar", module_name="FooOp")
    b = opartifacts.BuiltArtifact(Path("y"), "FooOpOld-bbbbbbbbbbbb-5.4.jar", "FooOpOld",
                                  fingerprint="b" * 64, built_name="FooOpOld-5.4.jar", module_name="FooOp")
    assert plugin._op_key(a) == plugin._op_key(b) == "op:FooOp"
    assert plugin._registry_key(a) != plugin._registry_key(b)
    no_manifest = opartifacts.BuiltArtifact(Path("z"), "Bar-cccccccccccc-5.4.jar", "Bar",
                                            built_name="Bar-5.4.jar")
    assert plugin._op_key(no_manifest) == "Bar-5.4.jar"
