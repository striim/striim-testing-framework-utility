import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from livetest import plugin, registry

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bigquery_service_test_driver", ROOT / "bigquery_driver.py")
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


def test_declaration_uses_independent_tokens_and_remapped_ports():
    service = registry.load_service("bigquery")
    assert service.docker_env["port"] == "SLT_BIGQUERY_REST_HOST_PORT"
    assert service.docker_env["project"] == "SLT_BIGQUERY_PROJECT"
    assert service.docker_env["dataset"] == "SLT_BIGQUERY_DATASET"
    assert service.live_override_env == "SLT_BIGQUERY_HOST"
    assert service.provides["BIGQUERY_SERVICE_ACCOUNT_KEY"] == "UploadedFiles/fake-bigquery-key.json"
    assert "gcs" not in service.provides


@pytest.mark.parametrize("endpoint", ["https://www.googleapis.com", "http://8.8.8.8:9050",
    "http://user:password@127.0.0.1:9050", "http://127.0.0.1:9050/bigquery/v2",
    "http://127.0.0.1:9050?token=value", "http://127.0.0.1", "http://[::1]:9050"])
def test_refuses_cloud_or_unqualified_endpoints(endpoint):
    with pytest.raises(ValueError):
        driver.local_http(endpoint)


def test_accepts_local_http_and_validates_bootstrap_identifiers():
    assert driver.local_http("http://127.0.0.1:19050/") == "http://127.0.0.1:19050"
    driver.identifiers("test-project", "slt_2")
    with pytest.raises(ValueError):
        driver.identifiers("test-project", "slt;DROP")


def test_existing_emulator_skips_compose_and_preserves_admin_address():
    from livetest.services import resolve
    def forbidden(_service):
        raise AssertionError("existing emulator must not start Compose")
    result = resolve("bigquery", {"SLT_BIGQUERY_HOST": "127.0.0.1", "SLT_BIGQUERY_PORT": "19050",
                                 "SLT_BIGQUERY_PROJECT": "other-project", "SLT_BIGQUERY_DATASET": "other_data"},
                     set(), compose_up=forbidden)
    assert result.mode == "live" and not result.started
    assert result.base["port"] == "19050"
    assert result.base["project"] == "other-project" and result.base["dataset"] == "other_data"


def test_docker_remap_moves_both_tokens_and_admin_address():
    from livetest.services import resolve
    from livetest.plugin import build_service_tokens
    result = resolve("bigquery", {"SLT_BIGQUERY_REST_HOST_PORT": "19050",
                                 "SLT_BIGQUERY_TOKEN_HOST_PORT": "19051",
                                 "SLT_BIGQUERY_PROJECT": "other-project", "SLT_BIGQUERY_DATASET": "other_data"},
                     set(), compose_up=lambda _service: None)
    tokens = build_service_tokens(registry.load_service("bigquery"), result, "")
    assert result.base["port"] == "19050" and result.base["token_port"] == "19051"
    assert tokens["BIGQUERY_ENDPOINT"] == "http://localhost:19050"
    assert tokens["BIGQUERY_PROJECT"] == "other-project" and tokens["BIGQUERY_DATASET"] == "other_data"


def test_missing_prebuilt_image_is_an_actionable_prerequisite_failure(monkeypatch):
    monkeypatch.setattr(driver.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1))
    assert "docker buildx build --load" in driver.unavailable({"SLT_BIGQUERY_IMAGE": "example:qualified"}, "docker")
    assert driver.unavailable({}, "docker") is None
    assert driver.build_required({})
    assert not driver.build_required({"SLT_BIGQUERY_IMAGE": "example:qualified"})
    assert driver.unavailable({"SLT_BIGQUERY_HOST": "127.0.0.1",
                               "SLT_BIGQUERY_WRITER_ENDPOINT": "http://127.0.0.1:19050",
                               "SLT_BIGQUERY_TOKEN_URI": "http://127.0.0.1:19051/token"}, "docker") is None


@pytest.mark.parametrize("override", [False, True])
def test_compose_builds_recipe_but_preserves_explicit_prebuilt_image(monkeypatch, override):
    from livetest import services
    seen = []
    monkeypatch.setenv("SLT_KEEP_SERVICES", "1")
    if override:
        monkeypatch.setenv("SLT_BIGQUERY_IMAGE", "example:qualified")
    else:
        monkeypatch.delenv("SLT_BIGQUERY_IMAGE", raising=False)
    monkeypatch.setattr(services.subprocess, "run", lambda args, **kwargs:
                        seen.append(args) or SimpleNamespace(returncode=0, stdout="", stderr=""))
    services._default_compose_up(registry.load_service("bigquery"))
    command = next(args for args in seen if "up" in args)
    assert ("--build" in command) is not override


def test_fake_key_upload_is_independent_of_gcs(monkeypatch):
    import subprocess
    config = SimpleNamespace(_slt_gcp_key_done=True)
    calls = []
    monkeypatch.setattr(plugin, "_make_fake_gcp_key", lambda uri, project: json.dumps({"token_uri": uri, "project_id": project}))
    monkeypatch.setattr(plugin.stack, "app_nodes", lambda: ("primary", "node"))
    def copy(args, **kwargs):
        calls.append((args[-1], json.loads(Path(args[2]).read_text()), kwargs["check"]))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess, "run", copy)
    plugin.ensure_fake_gcp_key(config, SimpleNamespace(mode="docker"), key_name="fake-bigquery-key.json",
                               token_uri="http://127.0.0.1:19051/token", project_id="test-project")
    assert [c[0] for c in calls] == ["primary:/opt/striim/UploadedFiles/fake-bigquery-key.json",
                                    "node:/opt/striim/UploadedFiles/fake-bigquery-key.json"]
    assert all(c[1]["token_uri"] == "http://127.0.0.1:19051/token" and c[2] for c in calls)
    assert config._slt_gcp_key_done and config._slt_bigquery_key_done


def test_lock_and_recipe_pin_the_same_source():
    lock = json.loads((ROOT / "upstream.lock").read_text())
    recipe = (ROOT / "Dockerfile").read_text()
    for value in (lock["upstream"]["commit"], lock["upstream"]["archive_sha256"], lock["build"]["go"], lock["build"]["runtime"]):
        assert value in recipe
    assert lock["build"]["framework_version"] in recipe
    series = (ROOT / "patches/series").read_text().splitlines()
    assert series == [item["file"] for item in lock["patches"]]
    dependencies = lock["dependency_patches"]
    for module in ("googlesqlite", "go-googlesql"):
        assert (ROOT / "patches" / (module + ".series")).read_text().splitlines() == [
            item["file"] for item in dependencies if item["module"].endswith("/" + module)]
    for item in dependencies:
        assert item["module"] + "@" + item["version"] in recipe
    expected = []
    for item in lock["patches"] + dependencies:
        assert hashlib.sha256((ROOT / "patches" / item["file"]).read_bytes()).hexdigest() == item["sha256"]
        expected.append(item["sha256"] + "  " + item["file"])
    assert (ROOT / "patches/SHA256SUMS").read_text().splitlines() == expected


def test_existing_docker_emulator_requires_writer_and_token_overrides(monkeypatch):
    monkeypatch.setenv("SLT_BIGQUERY_HOST", "127.0.0.1")
    monkeypatch.delenv("SLT_BIGQUERY_WRITER_ENDPOINT", raising=False)
    monkeypatch.delenv("SLT_BIGQUERY_TOKEN_URI", raising=False)
    assert "SLT_BIGQUERY_WRITER_ENDPOINT" in driver.unsupported_mode("docker")
    monkeypatch.setenv("SLT_BIGQUERY_WRITER_ENDPOINT", "http://127.0.0.1:19050")
    monkeypatch.setenv("SLT_BIGQUERY_TOKEN_URI", "http://127.0.0.1:19051/token")
    assert driver.unsupported_mode("docker") is None


def test_provision_retries_transient_bootstrap_failure():
    from livetest.bigqueryadmin import BigQueryError
    calls = []
    def setup(**kwargs):
        calls.append(kwargs)
        if len(calls) < 3:
            raise BigQueryError("exact-read-error", "connection refused during startup")
    driver.provision({"bigquery-target":{"admin":SimpleNamespace(ensure_setup=setup)}}, None, None)
    assert len(calls) == 3
    assert all(0 < c["remaining"] <= 5 for c in calls)


@pytest.mark.parametrize("lifecycle", [None, SimpleNamespace(uses_bigquery_cdc=lambda: False)])
def test_merge_without_owned_dataset_is_refused_before_render(tmp_path, lifecycle):
    (tmp_path / "app.tql").write_text("CREATE TARGET t USING Global.BigQueryWriter (Mode:'MERGE', StreamingUpload:false);")
    manifest = SimpleNamespace(source_dir=tmp_path, tql="app.tql", requires=["bigquery"], lifecycle=lifecycle)
    with pytest.raises(ValueError, match="owned CDC dataset"):
        plugin._rendered_tql(manifest, {}, {})


@pytest.mark.parametrize("properties", [
    "StreamingUpload:true", "StreamingUpload:true, StreamingConfiguration:'UseLegacyStreamingAPI=false'",
    "StreamingUpload:true, StreamingConfiguration:'UseLegacyStreamingAPI=true', Mode:'MERGE'",
    "StreamingUpload:false, GCSBucketName:'test-bucket'",
])
def test_unqualified_writer_transport_is_refused(tmp_path, properties):
    (tmp_path / "app.tql").write_text(f"CREATE TARGET t USING Global.BigQueryWriter ({properties});")
    manifest = SimpleNamespace(source_dir=tmp_path, tql="app.tql", requires=["bigquery"],
                               lifecycle=SimpleNamespace(uses_bigquery_cdc=lambda: True))
    with pytest.raises(ValueError, match="BigQuery emulator lane"):
        plugin._rendered_tql(manifest, {}, {})


@pytest.mark.parametrize("labels", [{}, {"org.opencontainers.image.version":"older"},
    {"org.opencontainers.image.version":"0.8.1-framework.8", "org.opencontainers.image.revision":"wrong"}])
def test_prebuilt_image_must_match_pinned_labels(monkeypatch, labels):
    monkeypatch.setattr(driver.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0,
        stdout=json.dumps([{"Id":"sha256:"+"a"*64, "Config":{"Labels":labels}}])))
    assert "labels" in driver.unavailable({"SLT_BIGQUERY_IMAGE":"example:qualified"}, "docker")


def test_prebuilt_image_accepts_only_the_pinned_labels(monkeypatch):
    lock = json.loads((ROOT / 'upstream.lock').read_text())
    labels = {'org.opencontainers.image.version':lock['build']['framework_version'],
              'org.opencontainers.image.revision':lock['upstream']['commit']}
    monkeypatch.setattr(driver.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0,
        stdout=json.dumps([{'Id':'sha256:'+'a'*64,'Config':{'Labels':labels}}])))
    assert driver.unavailable({'SLT_BIGQUERY_IMAGE':'example:qualified'}, 'docker') is None


@pytest.mark.parametrize('key_name,flag', [('fake-gcp-key.json','_slt_gcp_key_done'),
                                         ('fake-bigquery-key.json','_slt_bigquery_key_done')])
def test_failed_fake_key_copy_aborts_and_remains_retryable(monkeypatch, key_name, flag):
    import subprocess
    config = SimpleNamespace()
    monkeypatch.setattr(plugin, '_make_fake_gcp_key', lambda *a: '{}')
    monkeypatch.setattr(plugin.stack, 'app_nodes', lambda: ('primary',))
    def failed(*args, **kwargs):
        assert kwargs['check'] is True
        raise subprocess.CalledProcessError(1, args[0])
    monkeypatch.setattr(subprocess, 'run', failed)
    with pytest.raises(subprocess.CalledProcessError):
        plugin.ensure_fake_gcp_key(config, SimpleNamespace(mode='docker'), key_name=key_name)
    assert not getattr(config, flag, False)


def test_shipped_writer_cases_stay_in_qualified_paths():
    from livetest.bigquerycase import validate_writer
    from livetest.manifest import load_manifest
    repo = ROOT.parents[3]
    paths = list((repo / 'samples/live').glob('bigquery-*/test.yaml'))
    paths += list((repo / 'scripts/live/regression/services/bigquery').glob('*/test.yaml'))
    assert len(paths) == 13
    for path in paths:
        manifest = load_manifest(path)
        validate_writer(manifest, (manifest.source_dir / manifest.tql).read_text())


@pytest.mark.parametrize('document', ['[]', 'null', '{}', 'not JSON'])
def test_prebuilt_inspect_malformed_output_is_refused(monkeypatch, document):
    monkeypatch.setattr(driver.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout=document))
    assert 'labels' in driver.unavailable({'SLT_BIGQUERY_IMAGE':'example:qualified'}, 'docker')


def test_writer_checks_each_block_and_ignores_comment_or_string_decoys():
    from livetest.bigquerycase import validate_writer
    manifest = SimpleNamespace(requires=['bigquery'], lifecycle=None)
    text = """-- USING BigQueryWriter (StreamingUpload:true)
    CREATE TARGET a USING Global.BigQueryWriter (StreamingUpload:false,
      Tables:'BigQueryWriter', BatchPolicy:'EventCount:1,Interval:1');
    CREATE TARGET b USING Global.BigQueryWriter (StreamingUpload:true);"""
    with pytest.raises(ValueError, match='UseLegacyStreamingAPI=true'):
        validate_writer(manifest, text)


@pytest.mark.parametrize('action,text', [
    ([], "CREATE TARGET t USING Global.BigQueryWriter (StreamingUpload:false); ALTER TARGET t SET StreamingUpload=true;"),
    ([{'type':'alter_recompile','file':'alter.tql'}], "CREATE TARGET t USING Global.BigQueryWriter (StreamingUpload:false);"),
])
def test_writer_property_changes_cannot_bypass_preflight(action, text):
    from livetest.bigquerycase import validate_writer
    manifest = SimpleNamespace(requires=['bigquery'], lifecycle=None, action_specs=action)
    with pytest.raises(ValueError, match='writer property changes are unqualified'):
        validate_writer(manifest, text)
