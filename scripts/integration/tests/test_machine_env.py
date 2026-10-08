"""Machine settings are the lowest layer; fixtures never use the host configuration."""
from pathlib import Path
import pytest
from inttest import paths
from livetest import striim_provision, evidence

DOTENV_VALUES = paths.dotenv_values


@pytest.fixture
def layers(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "dotenv_values", DOTENV_VALUES)
    machine = tmp_path / "config/striim-test/machine.env"
    machine.parent.mkdir(parents=True)
    machine.write_text("SLT_STRIIM_PRIMARY_CPUS=4\nSLT_STRIIM_NODE_CPUS=4\n"
                       "SLT_STRIIM_MEM_MAX=3072m\nSLT_STRIIM_MEM_LIMIT=5g\n"
                       "SLT_PG_ADMIN_USER=machine\nSTRIIM_PASS=machine-password\n"
                       + "".join(f"{k}=fake-{k.lower()}\n" for k in paths.LICENCE_KEYS))
    machine.chmod(0o600)
    root = tmp_path / "checkout"
    root.mkdir()
    env = {"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "config"),
           "SLT_PROJECT_ROOT": str(root)}
    return machine, root, env


def test_precedence_and_caps(layers):
    machine, root, env = layers
    assert paths.dotenv_values(env)["SLT_STRIIM_PRIMARY_CPUS"] == "4"
    (root / ".env").write_text("SLT_STRIIM_PRIMARY_CPUS=2\nSTRIIM_PASSWORD=checkout-password\n"
                               "PRODUCT_KEY=checkout-must-not-be-used\n")
    got = paths.effective_env(env)
    assert got["SLT_STRIIM_PRIMARY_CPUS"] == "2"
    assert got["PRODUCT_KEY"] == "fake-product_key"
    assert paths.setting("STRIIM_PASS", env) == "checkout-password"
    assert paths.effective_env({**env, "SLT_STRIIM_PRIMARY_CPUS": "1"})["SLT_STRIIM_PRIMARY_CPUS"] == "1"
    assert paths.effective_env({**env, "LICENCE_KEY": "shell-licence"})["LICENCE_KEY"] == "shell-licence"


@pytest.mark.parametrize("key", ["SLT_STACK_PREFIX", "INT_STACK_PREFIX", "SLT_PG_HOST_PORT",
                                 "SLT_ZOOKEEPER_CLIENT_PORT", "STRIIM_URL", "SLT_FRAMEWORK_HOME"])
def test_lane_keys_refused(layers, key, capsys):
    machine, root, env = layers
    with machine.open("a") as stream:
        stream.write(f"{key}=lane-value-must-not-appear\n")
    assert key not in paths.machine_values(env)
    warning = capsys.readouterr().err
    assert key in warning and "lane-value-must-not-appear" not in warning


def test_licence_reaches_provisioning_and_redacts(layers, monkeypatch):
    machine, root, env = layers
    from striim_test import dispatch
    from livetest import paths as live_paths
    monkeypatch.setattr(live_paths, "dotenv_values", DOTENV_VALUES)
    overlay = dispatch.service_env(env)
    assert all(overlay[k] == f"fake-{k.lower()}" for k in paths.LICENCE_KEYS)
    assert overlay["SLT_STRIIM_NODE_CPUS"] == "4"
    effective = paths.effective_env({**env, **overlay})
    enriched, sources = striim_provision.enrich_license_env(effective)
    for key in paths.LICENCE_KEYS:
        assert enriched[key] == f"fake-{key.lower()}"
        assert sources[key] == "env"
    secrets = evidence.known_secrets(enriched)
    assert all(enriched[k] in secrets for k in paths.LICENCE_KEYS)
    assert all(enriched[k] not in evidence.redact_text(" ".join(enriched[k] for k in paths.LICENCE_KEYS), secrets)
               for k in paths.LICENCE_KEYS)


def test_mode_warning_once_and_secret_free(layers, capsys):
    machine, root, env = layers
    machine.chmod(0o644)
    paths.machine_values(env)
    paths.machine_values(env)
    warning = capsys.readouterr().err
    assert warning.count("group/world-readable") == 1
    assert "fake-" not in warning


def test_override_and_missing(layers):
    machine, root, env = layers
    assert paths.machine_values({**env, "SLT_MACHINE_ENV": str(root / "missing")}) == {}
    override = root / "override.env"
    override.write_text("SLT_STRIIM_NODE_CPUS=3\n")
    override.chmod(0o600)
    assert paths.machine_values({**env, "SLT_MACHINE_ENV": str(override)}) == {"SLT_STRIIM_NODE_CPUS": "3"}
    assert paths.machine_env_path({"HOME": "/tmp/home"}) == Path("/tmp/home/.config/striim-test/machine.env")
