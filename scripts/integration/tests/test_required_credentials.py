"""External database credentials must be explicit before any service is used."""
import pytest
from inttest.tokens import service_tokens, ServiceConfigError

REQUIRED = ['INT_TERADATA_ADMIN_USER', 'INT_TERADATA_ADMIN_PASSWORD', 'INT_TERADATA_SOURCE_USER', 'INT_TERADATA_SOURCE_PASSWORD', 'INT_TERADATA_TARGET_USER', 'INT_TERADATA_TARGET_PASSWORD']

def settings():
    return {"INT_TERADATA_HOST": "db.example.com", **{key: "fixture-only-value" for key in REQUIRED}}

@pytest.mark.parametrize("key", REQUIRED)
@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_credential_names_the_setting_without_disclosing_values(key, value):
    env = settings()
    if value is None:
        env.pop(key)
    else:
        env[key] = value
    with pytest.raises(ServiceConfigError) as exc:
        service_tokens("teradata", env=env)
    assert key in str(exc.value)
    assert "required environment" in str(exc.value)
    assert "fixture-only-value" not in str(exc.value)

def test_explicit_credentials_are_preserved():
    env = settings()
    env["INT_TERADATA_SOURCE_PASSWORD"] = "  fixture value with spaces  "
    result = service_tokens("teradata", env=env)
    assert result["TERADATA_SOURCE_PASSWORD"] == env["INT_TERADATA_SOURCE_PASSWORD"]

def test_service_definition_has_no_credential_defaults():
    from pathlib import Path
    import yaml
    raw = yaml.safe_load((Path(__file__).resolve().parents[1] / "services/teradata/service.yaml").read_text())
    assert not any(key.endswith(("user", "password")) for key in raw["docker_defaults"])
