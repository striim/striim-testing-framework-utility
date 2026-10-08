"""STRIIM_URL/USER/PASS come through livetest.paths: process env > .env > today's default."""
import types

import pytest

from livetest import opregistry, paths, plugin


@pytest.fixture
def dotenv(monkeypatch):
    values = {}
    monkeypatch.setattr(paths, "dotenv_values", lambda env=None: values)
    for k in ("STRIIM_URL", "STRIIM_USER", "STRIIM_PASS", "STRIIM_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    return values


def test_url_from_dotenv_reaches_opregistry(dotenv, monkeypatch):
    monkeypatch.delenv("SLT_SERVICES_HOST", raising=False)
    assert opregistry.cluster_tag() == ""                      # unset: historical filename
    dotenv["STRIIM_URL"] = "http://native-host:9080"
    assert opregistry.cluster_tag() != ""                      # .env URL now keys the registry
    assert opregistry.cluster_tag() == opregistry.cluster_tag({"STRIIM_URL": "http://native-host:9080"})


def test_process_env_beats_dotenv(dotenv, monkeypatch):
    dotenv["STRIIM_URL"] = "http://dot:9080"
    monkeypatch.setenv("STRIIM_URL", "http://env:9080")
    assert paths.setting("STRIIM_URL") == "http://env:9080"


def test_unset_keeps_docker_defaults(dotenv):
    assert paths.setting("STRIIM_URL") is None
    assert opregistry._resolved_url(paths.effective_env()) == "http://localhost:9080"


def test_report_header_names_keys_not_values(dotenv, tmp_path):
    dotenv["STRIIM_PASSWORD"] = "s3cret"
    lines = plugin.pytest_report_header(config=None, start_path=tmp_path)
    text = "\n".join(lines if isinstance(lines, list) else [lines or ""])
    assert "STRIIM_PASSWORD" in text and "s3cret" not in text


class _Probed(Exception):
    pass


def _credentials(monkeypatch, tmp_path):
    """(url, user, pw) that _resolve_striim hands to its first reachability probe."""
    seen = []

    def probe(url, user, pw):
        seen.append((url, user, pw))
        raise _Probed

    monkeypatch.setattr(plugin, "probe_reachable", probe)
    monkeypatch.setattr(plugin, "_cluster_provision_lock", lambda *a, **k: tmp_path / "cluster.lock")
    monkeypatch.delenv("SLT_SERVICES_HOST", raising=False)
    with pytest.raises(_Probed):
        plugin._resolve_striim(types.SimpleNamespace())
    return seen[0]


def test_resolve_striim_unset_keeps_docker_defaults(dotenv, monkeypatch, tmp_path):
    assert _credentials(monkeypatch, tmp_path) == ("http://localhost:9080", "admin", "striim")


def test_resolve_striim_takes_dotenv_url_user_and_password_alias(dotenv, monkeypatch, tmp_path):
    dotenv.update({"STRIIM_URL": "http://native-host:9080", "STRIIM_USER": "ops",
                   "STRIIM_PASSWORD": "pw2"})
    assert _credentials(monkeypatch, tmp_path) == ("http://native-host:9080", "ops", "pw2")


def _header(tmp_path):
    return plugin.pytest_report_header(config=None, start_path=tmp_path)


def test_report_header_empty_when_process_env_overrides(dotenv, monkeypatch, tmp_path):
    dotenv["STRIIM_URL"] = "http://dot:9080"
    monkeypatch.setenv("STRIIM_URL", "http://env:9080")
    assert _header(tmp_path) == []
    dotenv.clear()
    dotenv["STRIIM_PASSWORD"] = "dotpw"          # the alias counts as overridden too
    monkeypatch.setenv("STRIIM_PASS", "envpw")
    assert _header(tmp_path) == []


def test_report_header_names_only_the_alias_taken(dotenv, tmp_path):
    dotenv.update({"STRIIM_PASS": "a", "STRIIM_PASSWORD": "b"})
    text = "\n".join(_header(tmp_path))
    assert "STRIIM_PASS" in text and "STRIIM_PASSWORD" not in text


def test_report_header_skips_integration_only_keys(dotenv, tmp_path):
    dotenv.update({"SLT_INT_CASES": "/x", "SLT_INT_SERVICES_DIR": "/y"})
    assert _header(tmp_path) == []
