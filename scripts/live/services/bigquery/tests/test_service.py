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
    assert lock["patches"] == []
    assert not (ROOT / "patches/series").read_text().strip()


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
