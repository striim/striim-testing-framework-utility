"""Unit tests for inttest.tokens -- ${TOKEN} substitution + per-test token table
assembly (docs/INTEGRATION-TESTS.md).

Pure Python: no Docker, no Striim, no pytest-plugin collection. Service-token merge
tests point at a tmp_path fake service.yaml (matching the real on-disk shape found
under services/postgres|oracle|spanner/service.yaml) as well as the real
services/postgres/service.yaml, so both a synthetic and a real file are exercised.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from inttest.tokens import (
    ServiceConfigError,
    SubstitutionError,
    build_tokens,
    isolation_tokens,
    missing_tokens,
    render,
)

_REAL_SERVICES_DIR = Path(__file__).resolve().parent.parent / "services"


# ============================================================================
# render() / missing_tokens()
# ============================================================================


def test_render_success():
    assert render("hello ${NAME}", {"NAME": "world"}) == "hello world"


def test_render_multiple_tokens_same_value_reused():
    text = "${A}/${B}/${A}"
    assert render(text, {"A": "x", "B": "y"}) == "x/y/x"


def test_render_missing_token_raises_and_names_it():
    with pytest.raises(SubstitutionError) as exc_info:
        render("hello ${MISSING}", {})
    assert "MISSING" in str(exc_info.value)


def test_render_multiple_missing_tokens_all_named():
    with pytest.raises(SubstitutionError) as exc_info:
        render("${FOO} and ${BAR} and ${FOO}", {})
    message = str(exc_info.value)
    assert "FOO" in message
    assert "BAR" in message


def test_missing_tokens_returns_set_of_unresolved_names():
    result = missing_tokens("${A} ${B} ${C}", {"A": "1"})
    assert result == {"B", "C"}


def test_missing_tokens_empty_when_all_resolved():
    assert missing_tokens("${A}", {"A": "1"}) == set()


def test_render_no_tokens_passthrough():
    assert render("no tokens here", {}) == "no tokens here"


# ============================================================================
# ${TID} / ${TID_ORACLE} isolation tokens
# ============================================================================


def test_isolation_tokens_serial_are_empty():
    tokens = isolation_tokens(parallel=False)
    assert tokens["TID"] == ""
    assert tokens["TID_ORACLE"] == ""


def test_isolation_tokens_parallel_tid_is_lowercase_t_plus_hex_plus_underscore():
    tokens = isolation_tokens(parallel=True)
    tid = tokens["TID"]
    assert tid.endswith("_")
    body = tid[:-1]
    assert body.startswith("t")
    assert len(body) == 10  # "t" + 9 hex chars, matching live's _tid_oracle shape
    assert body[1:] == body[1:].lower()
    int(body[1:], 16)  # the body is valid hex


def test_isolation_tokens_parallel_tid_oracle_is_uppercase_t_plus_hex_plus_underscore():
    tokens = isolation_tokens(parallel=True)
    tid_oracle = tokens["TID_ORACLE"]
    assert tid_oracle.endswith("_")
    body = tid_oracle[:-1]
    assert body.startswith("T")
    assert len(body) == 10  # "T" + 9 hex chars, matching live's _tid_oracle shape
    assert body[1:] == body[1:].upper()
    int(body[1:], 16)  # the body is valid hex


def test_isolation_tokens_tid_and_tid_oracle_share_the_same_random_body():
    # Deliberate: one `secrets` call per isolation_tokens() call, not two
    # independent ones -- ${TID} and ${TID_ORACLE} are the SAME id, just cased
    # differently for each dialect's identifier convention.
    tokens = isolation_tokens(parallel=True)
    assert tokens["TID"][:-1] == "t" + tokens["TID_ORACLE"][:-1][1:].lower()


def test_isolation_tokens_fresh_every_call():
    # Deliberately NOT deterministic (unlike the pre-random-id design): isolation
    # only needs two calls to differ, not to reproduce a prior run's value --
    # see isolation_tokens' docstring.
    a = isolation_tokens(parallel=True)
    b = isolation_tokens(parallel=True)
    assert a != b


# ============================================================================
# build_tokens(): ${TEST_DIR} injection
# ============================================================================


def test_build_tokens_test_dir_is_absolute(tmp_path):
    test_dir = tmp_path / "some-test"
    test_dir.mkdir()
    tokens = build_tokens(test_dir, [], env={})
    assert tokens["TEST_DIR"] == str(test_dir.resolve())
    assert Path(tokens["TEST_DIR"]).is_absolute()


def test_build_tokens_no_requires_has_no_service_tokens(tmp_path):
    tokens = build_tokens(tmp_path, [], env={})
    assert set(tokens) == {"TEST_DIR", "TID", "TID_ORACLE"}


def test_build_tokens_serial_by_default(tmp_path):
    tokens = build_tokens(tmp_path, [], env={})
    assert tokens["TID"] == ""
    assert tokens["TID_ORACLE"] == ""


def test_build_tokens_parallel_flag_propagates(tmp_path):
    tokens = build_tokens(tmp_path, [], parallel=True, env={})
    assert tokens["TID"] != ""
    assert tokens["TID_ORACLE"] != ""


# ============================================================================
# Service `provides:` token merge -- synthetic fake service.yaml
# ============================================================================


_FAKE_SERVICE_YAML = """\
name: fakedb
compose: compose.yaml
container: int-fakedb
docker_defaults:
  host: localhost
  port: 15999
  dbname: fakedb
  source_user: qasource
  source_password: striim
live_env:
  host: INT_FAKEDB_HOST
  port: INT_FAKEDB_PORT
  dbname: INT_FAKEDB_DB
  source_user: INT_FAKEDB_SOURCE_USER
  source_password: INT_FAKEDB_SOURCE_PASSWORD
provides:
  FAKEDB_HOST: "{view_host}"
  FAKEDB_PORT: "{port}"
  FAKEDB_DB: "{dbname}"
  FAKEDB_SOURCE_USER: "{source_user}"
  FAKEDB_SOURCE_PASSWORD: "{source_password}"
  FAKEDB_URL: "jdbc:fakedb://{view_host}:{port}/{dbname}"
"""


def _write_fake_service(services_dir: Path, name: str = "fakedb", text: str = _FAKE_SERVICE_YAML) -> Path:
    svc_dir = services_dir / name
    svc_dir.mkdir(parents=True, exist_ok=True)
    (svc_dir / "service.yaml").write_text(text)
    # tokens require a complete profile: write the compose file the definition declares
    (svc_dir / "compose.yaml").write_text("services: {}\n")
    return services_dir


def test_service_tokens_merge_from_docker_defaults(tmp_path):
    services_dir = _write_fake_service(tmp_path / "services")
    tokens = build_tokens(tmp_path, ["fakedb"], env={}, services_dir=services_dir)
    assert tokens["FAKEDB_HOST"] == "localhost"
    assert tokens["FAKEDB_PORT"] == "15999"
    assert tokens["FAKEDB_DB"] == "fakedb"
    assert tokens["FAKEDB_SOURCE_USER"] == "qasource"
    assert tokens["FAKEDB_URL"] == "jdbc:fakedb://localhost:15999/fakedb"


def test_service_tokens_env_override_wins_over_docker_defaults(tmp_path):
    services_dir = _write_fake_service(tmp_path / "services")
    env = {
        "INT_FAKEDB_HOST": "override-host",
        "INT_FAKEDB_PORT": "9999",
    }
    tokens = build_tokens(tmp_path, ["fakedb"], env=env, services_dir=services_dir)
    assert tokens["FAKEDB_HOST"] == "override-host"
    assert tokens["FAKEDB_PORT"] == "9999"
    # Unset override falls back to docker_defaults.
    assert tokens["FAKEDB_DB"] == "fakedb"
    assert tokens["FAKEDB_URL"] == "jdbc:fakedb://override-host:9999/fakedb"


def test_service_tokens_merge_with_test_dir_and_isolation_tokens(tmp_path):
    services_dir = _write_fake_service(tmp_path / "services")
    tokens = build_tokens(
        tmp_path, ["fakedb"], parallel=True, env={}, services_dir=services_dir,
    )
    assert tokens["TEST_DIR"] == str(tmp_path.resolve())
    assert tokens["TID"] != ""
    assert "FAKEDB_URL" in tokens


def test_required_service_without_service_yaml_raises(tmp_path):
    services_dir = tmp_path / "services"
    services_dir.mkdir()
    with pytest.raises(ServiceConfigError, match="no service.yaml"):
        build_tokens(tmp_path, ["nonexistent"], env={}, services_dir=services_dir)


def test_service_provides_template_unknown_key_raises(tmp_path):
    bad_yaml = """\
docker_defaults:
  host: localhost
provides:
  FAKEDB_URL: "jdbc:fakedb://{host}:{missing_key}"
"""
    services_dir = _write_fake_service(tmp_path / "services", text=bad_yaml)
    with pytest.raises(ServiceConfigError, match="unknown key"):
        build_tokens(tmp_path, ["fakedb"], env={}, services_dir=services_dir)


def test_service_tokens_render_into_a_property_template(tmp_path):
    """End-to-end: a properties-style ${...} template renders using build_tokens()'s
    merged table, mirroring how test.yaml's `properties:` values get substituted
    (SPEC §3/§6)."""
    services_dir = _write_fake_service(tmp_path / "services")
    tokens = build_tokens(tmp_path, ["fakedb"], env={}, services_dir=services_dir)
    rendered = render("${FAKEDB_SOURCE_USER}@${FAKEDB_URL}", tokens)
    assert rendered == "qasource@jdbc:fakedb://localhost:15999/fakedb"


# ============================================================================
# Service `provides:` token merge -- the REAL services/postgres/service.yaml
# ============================================================================


@pytest.mark.skipif(not (_REAL_SERVICES_DIR / "postgres" / "service.yaml").exists(),
                     reason="services/postgres/service.yaml not present in this checkout")
def test_real_postgres_service_yaml_publishes_expected_tokens(tmp_path):
    tokens = build_tokens(tmp_path, ["postgres"], env={}, services_dir=_REAL_SERVICES_DIR)
    for name in (
        "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB",
        "POSTGRES_ADMIN_USER", "POSTGRES_ADMIN_PASSWORD",
        "POSTGRES_SOURCE_USER", "POSTGRES_SOURCE_PASSWORD", "POSTGRES_SOURCE_SCHEMA",
        "POSTGRES_TARGET_USER", "POSTGRES_TARGET_PASSWORD", "POSTGRES_TARGET_SCHEMA",
        "POSTGRES_URL",
    ):
        assert name in tokens, f"expected {name!r} in tokens, got {sorted(tokens)}"
    assert tokens["POSTGRES_URL"] == "jdbc:postgresql://localhost:15432/intdb"


@pytest.mark.skipif(not (_REAL_SERVICES_DIR / "postgres" / "service.yaml").exists(),
                     reason="services/postgres/service.yaml not present in this checkout")
def test_real_postgres_service_yaml_env_override(tmp_path):
    env = {"INT_PG_HOST": "some-other-host"}
    tokens = build_tokens(tmp_path, ["postgres"], env=env, services_dir=_REAL_SERVICES_DIR)
    assert tokens["POSTGRES_HOST"] == "some-other-host"
    assert "some-other-host" in tokens["POSTGRES_URL"]


@pytest.mark.skipif(not (_REAL_SERVICES_DIR / "oracle" / "service.yaml").exists(),
                     reason="services/oracle/service.yaml not present in this checkout")
def test_real_oracle_service_yaml_publishes_expected_tokens(tmp_path):
    tokens = build_tokens(tmp_path, ["oracle"], env={}, services_dir=_REAL_SERVICES_DIR)
    for name in ("ORACLE_HOST", "ORACLE_PORT", "ORACLE_SERVICE",
                 "ORACLE_SOURCE_USER", "ORACLE_SOURCE_PASSWORD", "ORACLE_URL"):
        assert name in tokens
    assert tokens["ORACLE_URL"].startswith("jdbc:oracle:thin:@//")


@pytest.mark.skipif(not (_REAL_SERVICES_DIR / "spanner" / "service.yaml").exists(),
                     reason="services/spanner/service.yaml not present in this checkout")
def test_real_spanner_service_yaml_publishes_expected_tokens(tmp_path):
    tokens = build_tokens(tmp_path, ["spanner"], env={}, services_dir=_REAL_SERVICES_DIR)
    for name in (
        "SPANNER_PROJECT", "SPANNER_INSTANCE", "SPANNER_HOST", "SPANNER_GRPC_PORT",
        "SPANNER_GSQL_DB", "SPANNER_PG_DB", "SPANNER_GSQL_URL", "SPANNER_PG_URL",
    ):
        assert name in tokens, f"expected {name!r} in tokens, got {sorted(tokens)}"
    # Pins the exact URL shape, the same way test_real_postgres_service_yaml_
    # publishes_expected_tokens pins POSTGRES_URL -- guards service.yaml <-> dbroutes
    # agreement (dbroutes.connection_params reads SPANNER_HOST/SPANNER_GRPC_PORT, which
    # only exist because service.yaml's `provides:` was extended in this same slice).
    assert tokens["SPANNER_GSQL_URL"] == (
        "jdbc:cloudspanner://localhost:19010/projects/test-project/"
        "instances/test-inst/databases/gsql?autoConfigEmulator=true"
    )


@pytest.mark.skipif(not (_REAL_SERVICES_DIR / "spanner" / "service.yaml").exists(),
                     reason="services/spanner/service.yaml not present in this checkout")
def test_real_spanner_service_yaml_env_override(tmp_path):
    env = {"INT_SPANNER_PORT": "29010"}
    tokens = build_tokens(tmp_path, ["spanner"], env=env, services_dir=_REAL_SERVICES_DIR)
    assert tokens["SPANNER_GRPC_PORT"] == "29010"
    assert ":29010/" in tokens["SPANNER_GSQL_URL"]


def test_multiple_requires_merge_together(tmp_path):
    """requires: [postgres, oracle] merges both services' tokens into one table."""
    if not (_REAL_SERVICES_DIR / "postgres" / "service.yaml").exists():
        pytest.skip("services/postgres/service.yaml not present in this checkout")
    if not (_REAL_SERVICES_DIR / "oracle" / "service.yaml").exists():
        pytest.skip("services/oracle/service.yaml not present in this checkout")
    tokens = build_tokens(tmp_path, ["postgres", "oracle"], env={}, services_dir=_REAL_SERVICES_DIR)
    assert "POSTGRES_URL" in tokens
    assert "ORACLE_URL" in tokens


def test_default_gcs_bucket_is_a_generic_test_resource():
    from inttest.tokens import service_tokens
    assert service_tokens("gcs", env={})["GCS_BUCKET"] == "int-test-bucket"
