"""External database credentials must be explicit before any service is used."""
import pytest
from livetest.services import resolve, ServiceError

REQUIRED = ['SLT_TERADATA_USER', 'SLT_TERADATA_PASSWORD', 'SLT_TERADATA_SOURCE_USER', 'SLT_TERADATA_SOURCE_PASSWORD', 'SLT_TERADATA_TARGET_USER', 'SLT_TERADATA_TARGET_PASSWORD']

def settings():
    return {"SLT_TERADATA_HOST": "db.example.com", **{key: "fixture-only-value" for key in REQUIRED}}

@pytest.mark.parametrize("key", REQUIRED)
@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_credential_names_the_setting_without_disclosing_values(key, value):
    env = settings()
    if value is None:
        env.pop(key)
    else:
        env[key] = value
    with pytest.raises(ServiceError) as exc:
        resolve("teradata", env=env, started=set(), compose_up=lambda _: pytest.fail("no compose"))
    assert key in str(exc.value)
    assert "required environment" in str(exc.value)
    assert "fixture-only-value" not in str(exc.value)

def test_explicit_credentials_are_preserved():
    env = settings()
    env["SLT_TERADATA_SOURCE_PASSWORD"] = "  fixture value with spaces  "
    result = resolve("teradata", env=env, started=set(), compose_up=lambda _: pytest.fail("no compose"))
    assert result.base["source_password"] == env["SLT_TERADATA_SOURCE_PASSWORD"]

def test_service_definition_has_no_credential_defaults():
    from pathlib import Path
    import yaml
    raw = yaml.safe_load((Path(__file__).resolve().parents[1] / "services/teradata/service.yaml").read_text())
    assert not any(key.endswith(("user", "password")) for key in raw["docker_defaults"])


@pytest.mark.parametrize("value", ["ENV_KEY", ["lower"], [1], ["ENV_KEY", "ENV_KEY"]])
def test_invalid_required_environment_declaration_is_rejected(tmp_path, value):
    from livetest.registry import hook_policy, RegistryError
    with pytest.raises(RegistryError, match="required_env"):
        hook_policy({"required_env": value}, tmp_path / "service.yaml")
