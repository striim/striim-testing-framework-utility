"""Hermetic tests for the Striim license/cluster env fallback.

When COMPANY_NAME/CLUSTER_NAME/PRODUCT_KEY/LICENCE_KEY are unset, `docker compose` warns
"variable is not set. Defaulting to a blank string" and the cluster is doomed. A user with a
configured local Striim install already has these values in
$STRIIM_HOME/conf/startUp.properties, so the harness fills the gaps from that file
(livetest.striim_provision.enrich_license_env), and services.py injects the enriched env into
the compose subprocess. These tests use FAKE values only — no real license material, no docker.
"""
import types

import pytest

from livetest.striim_provision import (
    _LICENSE_VARS,
    _read_startup_license,
    cluster_config_from_home,
    enrich_license_env,
)


# startUp.properties field -> compose var (the verified mapping).
_ALL_FIELDS = {
    "WAClusterName": "CLUSTER_NAME",
    "CompanyName": "COMPANY_NAME",
    "ProductKey": "PRODUCT_KEY",
    "LicenceKey": "LICENCE_KEY",
}


def _write_startup(home, body):
    """Write a fake <home>/conf/startUp.properties with the given raw body."""
    conf = home / "conf"
    conf.mkdir(parents=True, exist_ok=True)
    (conf / "startUp.properties").write_text(body)
    return home


def _fake_install(home):
    """A well-formed fake install: all four fields uncommented + a block of COMMENTED
    duplicates below (exactly the shape the real file ships) that must be ignored."""
    return _write_startup(
        home,
        "\n".join(
            [
                "# Striim startUp.properties (FAKE values for tests)",
                "WAClusterName=fake-cluster",
                "CompanyName=Fake Co",
                "ProductKey=PKEY-FAKE-0000",
                "LicenceKey=LKEY-FAKE-0000",
                "# Commented duplicates that MUST be ignored:",
                "# WAClusterName=commented-cluster",
                "# CompanyName=Commented Co",
                "# ProductKey=PKEY-COMMENTED",
                "# LicenceKey=LKEY-COMMENTED",
                "",
            ]
        ),
    )


@pytest.fixture(autouse=True)
def _clear_license_env(monkeypatch):
    """Every test starts with the four vars and STRIIM_HOME unset in the ambient env, so a
    stray value on the developer's shell can't mask a fallback assertion."""
    for var in _LICENSE_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("STRIIM_HOME", raising=False)


# --------------------------------------------------------------------------- enrich_license_env


def test_all_four_derived_when_env_empty(tmp_path):
    _fake_install(tmp_path)
    enriched, sources = enrich_license_env({}, home=str(tmp_path))
    assert enriched["CLUSTER_NAME"] == "fake-cluster"
    assert enriched["COMPANY_NAME"] == "Fake Co"
    assert enriched["PRODUCT_KEY"] == "PKEY-FAKE-0000"
    assert enriched["LICENCE_KEY"] == "LKEY-FAKE-0000"
    assert sources == {v: "derived" for v in _LICENSE_VARS}


def test_explicit_env_wins_over_file(tmp_path):
    _fake_install(tmp_path)
    env = {"COMPANY_NAME": "Env Wins Inc", "STRIIM_HOME": str(tmp_path)}
    enriched, sources = enrich_license_env(env)
    # the explicit var keeps its value and is sourced from env; the gaps still derive
    assert enriched["COMPANY_NAME"] == "Env Wins Inc"
    assert sources["COMPANY_NAME"] == "env"
    assert enriched["CLUSTER_NAME"] == "fake-cluster"
    assert sources["CLUSTER_NAME"] == "derived"
    assert sources["PRODUCT_KEY"] == "derived" and sources["LICENCE_KEY"] == "derived"


def test_blank_env_var_is_treated_as_unset(tmp_path):
    _fake_install(tmp_path)
    # empty string AND whitespace-only both count as "unset" and derive from the file
    env = {"COMPANY_NAME": "", "PRODUCT_KEY": "   ", "STRIIM_HOME": str(tmp_path)}
    enriched, sources = enrich_license_env(env)
    assert enriched["COMPANY_NAME"] == "Fake Co" and sources["COMPANY_NAME"] == "derived"
    assert enriched["PRODUCT_KEY"] == "PKEY-FAKE-0000" and sources["PRODUCT_KEY"] == "derived"


def test_commented_lines_ignored_and_first_uncommented_wins(tmp_path):
    _write_startup(
        tmp_path,
        "\n".join(
            [
                "# ProductKey=PKEY-COMMENTED-ABOVE",
                "ProductKey=PKEY-FIRST-WINS",
                "ProductKey=PKEY-SECOND-LOSES",
                "  # CompanyName=indented-comment-ignored",
                "CompanyName=Real Co",
                "# LicenceKey=ONLY-EVER-COMMENTED",  # never uncommented -> not derivable
                "WAClusterName=c1",
                "",
            ]
        ),
    )
    enriched, sources = enrich_license_env({}, home=str(tmp_path))
    assert enriched["PRODUCT_KEY"] == "PKEY-FIRST-WINS"   # first uncommented occurrence
    assert enriched["COMPANY_NAME"] == "Real Co"          # indented comment skipped
    assert enriched["CLUSTER_NAME"] == "c1"
    # LicenceKey appears ONLY as a comment -> not derivable, stays unset
    assert "LICENCE_KEY" not in enriched
    assert sources["LICENCE_KEY"] == "unset"


def test_missing_file_leaves_env_unchanged(tmp_path):
    # home exists but has no conf/startUp.properties
    env = {"UNRELATED": "keep"}
    enriched, sources = enrich_license_env(env, home=str(tmp_path))
    assert enriched == {"UNRELATED": "keep"}
    assert sources == {v: "unset" for v in _LICENSE_VARS}


def test_no_home_at_all_is_a_noop():
    enriched, sources = enrich_license_env({}, home=None)
    assert enriched == {}
    assert sources == {v: "unset" for v in _LICENSE_VARS}


def test_home_resolved_from_env_mapping(tmp_path):
    _fake_install(tmp_path)
    # STRIIM_HOME carried in the env mapping (not the process env) is honored
    enriched, sources = enrich_license_env({"STRIIM_HOME": str(tmp_path)})
    assert sources == {v: "derived" for v in _LICENSE_VARS}
    assert enriched["COMPANY_NAME"] == "Fake Co"


def test_home_resolved_from_os_environ(tmp_path, monkeypatch):
    _fake_install(tmp_path)
    monkeypatch.setenv("STRIIM_HOME", str(tmp_path))
    # env mapping has no STRIIM_HOME; the os.environ fallback supplies it
    enriched, sources = enrich_license_env({})
    assert sources == {v: "derived" for v in _LICENSE_VARS}
    assert enriched["CLUSTER_NAME"] == "fake-cluster"


def test_does_not_mutate_the_input_mapping(tmp_path):
    _fake_install(tmp_path)
    env = {"STRIIM_HOME": str(tmp_path)}
    enrich_license_env(env)
    # the caller's mapping (e.g. os.environ) is never mutated — only the returned copy is
    assert set(env) == {"STRIIM_HOME"}


def test_enrich_does_not_mutate_os_environ(tmp_path, monkeypatch):
    _fake_install(tmp_path)
    monkeypatch.setenv("STRIIM_HOME", str(tmp_path))
    import os

    enrich_license_env(os.environ)
    for var in _LICENSE_VARS:
        assert var not in os.environ   # os.environ stays pristine; only the copy is enriched


def test_sources_never_carry_secret_values(tmp_path):
    _fake_install(tmp_path)
    _, sources = enrich_license_env({}, home=str(tmp_path))
    # sources maps NAME -> origin only; no license value ever appears in it
    assert set(sources.values()) <= {"env", "derived", "unset"}
    for secret in ("Fake Co", "PKEY-FAKE-0000", "LKEY-FAKE-0000", "fake-cluster"):
        assert secret not in sources and secret not in "".join(sources.values())


# ----------------------------------------------------------------- cluster_config_from_home


def test_cluster_config_from_home_derives_all(tmp_path):
    _fake_install(tmp_path)
    cfg = cluster_config_from_home(str(tmp_path))
    assert cfg == {
        "CLUSTER_NAME": "fake-cluster",
        "COMPANY_NAME": "Fake Co",
        "PRODUCT_KEY": "PKEY-FAKE-0000",
        "LICENCE_KEY": "LKEY-FAKE-0000",
    }


def test_cluster_config_from_home_empty_without_home():
    assert cluster_config_from_home(None) == {}


def test_read_startup_license_first_uncommented_wins(tmp_path):
    _write_startup(tmp_path, "# CompanyName=commented\nCompanyName=first\nCompanyName=second\n")
    assert _read_startup_license(str(tmp_path)) == {"COMPANY_NAME": "first"}


# ---------------------------------------------------------------- services.py compose wiring


def test_compose_env_injects_derived_license_vars(tmp_path, monkeypatch):
    _fake_install(tmp_path)
    monkeypatch.setenv("STRIIM_HOME", str(tmp_path))
    from livetest.services import _compose_env

    env = _compose_env()
    assert env["COMPANY_NAME"] == "Fake Co"
    assert env["CLUSTER_NAME"] == "fake-cluster"
    assert env["PRODUCT_KEY"] == "PKEY-FAKE-0000"
    assert env["LICENCE_KEY"] == "LKEY-FAKE-0000"


def test_compose_env_explicit_os_environ_wins(tmp_path, monkeypatch):
    _fake_install(tmp_path)
    monkeypatch.setenv("STRIIM_HOME", str(tmp_path))
    monkeypatch.setenv("COMPANY_NAME", "Explicit From Shell")
    from livetest.services import _compose_env

    env = _compose_env()
    assert env["COMPANY_NAME"] == "Explicit From Shell"   # shell env wins
    assert env["CLUSTER_NAME"] == "fake-cluster"          # gap still derives


def test_default_compose_up_passes_enriched_env_to_subprocess(tmp_path, monkeypatch):
    _fake_install(tmp_path)
    monkeypatch.setenv("STRIIM_HOME", str(tmp_path))
    # SLT_KEEP_SERVICES skips the pre-up `down -v` reset, leaving a single `up` subprocess call
    monkeypatch.setenv("SLT_KEEP_SERVICES", "1")

    import livetest.services as services_mod
    from livetest.registry import load_service

    captured = {}

    def fake_run(argv, **kw):
        captured["env"] = kw.get("env")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(services_mod.subprocess, "run", fake_run)
    services_mod._default_compose_up(load_service("postgres"))   # any service — wiring is generic

    env = captured["env"]
    assert env is not None, "compose subprocess must be given an explicit env"
    assert env["COMPANY_NAME"] == "Fake Co"
    assert env["CLUSTER_NAME"] == "fake-cluster"
    assert env["PRODUCT_KEY"] == "PKEY-FAKE-0000"
    assert env["LICENCE_KEY"] == "LKEY-FAKE-0000"
