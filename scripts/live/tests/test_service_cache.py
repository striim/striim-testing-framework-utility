"""Manifest token discovery is reused only while all its inputs remain current."""
import os
from pathlib import Path

import pytest
import yaml

from livetest import layout, manifest, paths, registry, service_env


def service(root, name="sample", token="SAMPLE_URL"):
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    file = directory / "service.yaml"
    file.write_text(f"name: {name}\nisolation: none\nprovides:\n  {token}: sample\n")
    return file


@pytest.fixture
def tree(tmp_path, monkeypatch):
    layout._reset()
    # The suite's autouse fixture suppresses checkout dotenv discovery; exercise it here.
    monkeypatch.setattr(paths, "dotenv_values", lambda env=None: service_env.values(paths, env))
    root = tmp_path / "services"
    root.mkdir()
    integration = tmp_path / "integration"
    integration.mkdir()
    for key in ("GOLD_TARGETS", "SLT_FRAMEWORK_HOME", "SLT_PROJECT_ROOT",
                "SLT_SERVICES_DIR", "SLT_INT_SERVICES_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SLT_FRAMEWORK_DOTENV", str(tmp_path / ".env"))
    monkeypatch.setenv("SLT_MACHINE_ENV", str(tmp_path / "machine.env"))
    (tmp_path / ".env").write_text(f"SLT_SERVICES_DIR={root}\nSLT_INT_SERVICES_DIR={integration}\n")
    monkeypatch.setattr(registry, "_SERVICES_DIR", root)
    service(root)
    calls = []
    original = service_env.declarations

    def counted(env=None):
        calls.append(True)
        return original(env)

    monkeypatch.setattr(service_env, "declarations", counted)
    yield root, calls
    layout._reset()


def test_reuses_discovery_and_returns_independent_sets(tree):
    root, calls = tree
    first = manifest._service_tokens()
    count = len(calls)
    first.add("CALLER_TOKEN")
    assert manifest._service_tokens() == {"SAMPLE_URL"}
    assert len(calls) == count


def test_reserved_tokens_follow_module_tokens_and_cannot_be_mutated(tree):
    reserved = manifest._reserved_tokens([{"token": "FIRST"}])
    assert {"FIRST_JAR", "FIRST_NAME", "SAMPLE_URL"} <= reserved
    reserved.add("CALLER_TOKEN")
    later = manifest._reserved_tokens([{"token": "SECOND"}])
    assert {"SECOND_JAR", "SECOND_NAME", "SAMPLE_URL"} <= later
    assert not {"FIRST_JAR", "FIRST_NAME", "CALLER_TOKEN"} & later


def test_invalid_service_edit_is_not_hidden_or_cached(tree):
    root, calls = tree
    assert manifest._service_tokens() == {"SAMPLE_URL"}
    file = root / "sample/service.yaml"
    file.write_text("name: [\n")
    with pytest.raises(yaml.YAMLError):
        manifest._service_tokens()
    service(root, token="REPAIRED_URL")
    assert manifest._service_tokens() == {"REPAIRED_URL"}


@pytest.mark.parametrize("explicit_root", [False, True])
def test_symlink_overlay_retargeted_to_equal_size_declaration(tree, monkeypatch, explicit_root):
    root, calls = tree
    if explicit_root:
        monkeypatch.setenv("SLT_SERVICES_DIR", str(root))
    first = root.parent / "first"
    second = root.parent / "second"
    one = service(first, token="FIRST_URL")
    two = service(second, token="OTHER_URL")
    stat = one.stat()
    os.utime(two, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    link = root.parent / "overlay"
    link.symlink_to(first, target_is_directory=True)
    layout.set_roots(services=[link])
    assert manifest._service_tokens() == {"FIRST_URL"}
    link.unlink()
    link.symlink_to(second, target_is_directory=True)
    assert manifest._service_tokens() == {"OTHER_URL"}


@pytest.mark.parametrize("change", ["edit", "add", "remove", "add_yaml", "remove_yaml"])
def test_service_membership_and_contents_invalidate(tree, change):
    root, calls = tree
    (root / "empty").mkdir()
    assert manifest._service_tokens() == {"SAMPLE_URL"}
    count = len(calls)
    if change == "edit":
        file = root / "sample/service.yaml"
        previous = file.stat()
        file.write_text(file.read_text().replace("SAMPLE_URL", "EDITED_URL"))
        os.utime(file, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1))
        expected = {"EDITED_URL"}
    elif change in ("add", "add_yaml"):
        service(root, "added" if change == "add" else "empty", "ADDED_URL")
        expected = {"SAMPLE_URL", "ADDED_URL"}
    else:
        (root / "sample/service.yaml").unlink()
        if change == "remove":
            (root / "sample").rmdir()
        expected = set()
    assert manifest._service_tokens() == expected
    assert len(calls) > count


@pytest.mark.parametrize("source", ["dotenv", "machine", "environment", "compose"])
def test_settings_and_compose_invalidate(tree, monkeypatch, source):
    root, calls = tree
    file = root / "sample/service.yaml"
    file.write_text(file.read_text() + "compose: compose.yaml\n")
    compose = file.parent / "compose.yaml"
    compose.write_text("name: ${SAMPLE_SETTING}\n")
    assert manifest._service_tokens() == {"SAMPLE_URL"}
    count = len(calls)
    if source == "dotenv":
        dotenv = root.parent / ".env"
        dotenv.write_text(dotenv.read_text() + "SLT_STACK_PREFIX=alternate\n")
    elif source == "machine":
        (root.parent / "machine.env").write_text("SAMPLE_SETTING=value\n")
    elif source == "environment":
        monkeypatch.setenv("SLT_STACK_PREFIX", "alternate")
    else:
        compose.write_text("name: ${CHANGED_SETTING}\n")
    assert manifest._service_tokens() == {"SAMPLE_URL"}
    assert len(calls) > count


def test_overlay_changes_invalidate(tree):
    root, calls = tree
    assert manifest._service_tokens() == {"SAMPLE_URL"}
    overlay = root.parent / "overlay"
    service(overlay, token="OVERLAY_URL")
    layout.set_roots(services=[overlay])
    assert manifest._service_tokens() == {"OVERLAY_URL"}
    layout.set_roots(services=[])
    layout.set_manifest_roots(services=[overlay])
    assert manifest._service_tokens() == {"OVERLAY_URL"}
    layout.set_manifest_roots()
    assert manifest._service_tokens() == {"SAMPLE_URL"}


def test_project_manifest_edit_invalidates_declarations(tree, monkeypatch):
    root, calls = tree
    project_file = root.parent / "gold-targets.yaml"
    project_file.write_text("schemaVersion: 1\nsuites:\n  live: cases\n")
    monkeypatch.setenv("GOLD_TARGETS", str(project_file))
    assert manifest._service_tokens() == {"SAMPLE_URL"}
    count = len(calls)
    project_file.write_text(project_file.read_text() + "servicesRoots: [services]\n")
    assert manifest._service_tokens() == {"SAMPLE_URL"}
    assert len(calls) > count


def test_integration_declaration_edit_invalidates(tree):
    root, calls = tree
    file = service(root.parent / "integration", "other", "OTHER_URL")
    assert manifest._service_tokens() == {"SAMPLE_URL"}
    file.write_text("name: [\n")
    with pytest.raises(yaml.YAMLError):
        manifest._service_tokens()


def test_cached_and_uncached_manifest_results_match(tree, monkeypatch):
    root, calls = tree
    file = root.parent / "test.yaml"
    file.write_text("name: sample\ntql: app.tql\nassert:\n  smoke: true\n"
                    "tokens:\n  CUSTOM_TOKEN: value\n")
    first = manifest.load_manifest(file)
    cached = manifest.load_manifest(file)
    monkeypatch.setattr(manifest, "_service_tokens", manifest._service_tokens.__wrapped__)
    uncached = manifest.load_manifest(file)
    assert first == cached == uncached
    file.write_text(file.read_text().replace("CUSTOM_TOKEN", "SAMPLE_URL"))
    with pytest.raises(manifest.ManifestError, match="harness-provided"):
        manifest.load_manifest(file)
