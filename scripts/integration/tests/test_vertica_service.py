"""The integration tier's vertica service definition, hermetically: tokens, overrides, and the
values compose.yaml, entrypoint.sh, init.sql and service.yaml must agree on. Mirrors
scripts/live/tests/test_vertica_service.py for this tier's copy."""
import re
from pathlib import Path

import yaml

from inttest import tokens as tokens_mod
from inttest.services import supported_services

SERVICES = Path(__file__).resolve().parents[1] / "services"
SERVICE_DIR = SERVICES / "vertica"
LIVE_IMAGE = Path(__file__).resolve().parents[2] / "live" / "services" / "vertica" / "images" / "vertica"


def test_vertica_is_a_builtin_service():
    assert "vertica" in supported_services()


def test_docker_default_tokens():
    t = tokens_mod.service_tokens("vertica", env={})
    assert t["VERTICA_HOST"] == "localhost"
    assert t["VERTICA_PORT"] == "15433"
    assert t["VERTICA_DB"] == "intdb"
    assert t["VERTICA_URL"] == "jdbc:vertica://localhost:15433/intdb"
    assert (t["VERTICA_ADMIN_USER"], t["VERTICA_ADMIN_PASSWORD"]) == ("dbadmin", "striim")
    assert (t["VERTICA_SOURCE_USER"], t["VERTICA_SOURCE_SCHEMA"]) == ("qasource", "qasource")
    assert (t["VERTICA_TARGET_USER"], t["VERTICA_TARGET_SCHEMA"]) == ("qatarget", "qatarget")


def test_remapped_host_port_moves_the_client():
    t = tokens_mod.service_tokens("vertica", env={"INT_VERTICA_HOST_PORT": "15999"})
    assert t["VERTICA_PORT"] == "15999" and "15999" in t["VERTICA_URL"]


def test_live_env_wins():
    env = {"INT_VERTICA_HOST": "verticahost", "INT_VERTICA_PORT": "5434", "INT_VERTICA_DB": "custdb",
           "INT_VERTICA_TARGET_USER": "tgt", "INT_VERTICA_TARGET_SCHEMA": "tgt_schema"}
    t = tokens_mod.service_tokens("vertica", env=env)
    assert t["VERTICA_URL"] == "jdbc:vertica://verticahost:5434/custdb"
    assert (t["VERTICA_TARGET_USER"], t["VERTICA_TARGET_SCHEMA"]) == ("tgt", "tgt_schema")

# ---- entrypoint/healthcheck/init consistency --------------------------------

def _compose_service() -> dict:
    return yaml.safe_load((SERVICE_DIR / "compose.yaml").read_text())["services"]["int-vertica"]


def _healthcheck_test() -> str:
    return _compose_service()["healthcheck"]["test"][1]


def _entrypoint_text(image: Path = SERVICE_DIR / "images" / "vertica") -> str:
    return (image / "entrypoint.sh").read_text()


def _entrypoint_var(name: str) -> str:
    m = re.search(rf"^{name}=(\S+)$", _entrypoint_text(), re.MULTILINE)
    assert m, f"entrypoint.sh must define {name}=<value>"
    return m.group(1)


def _defaults() -> dict:
    return yaml.safe_load((SERVICE_DIR / "service.yaml").read_text())["docker_defaults"]


def test_healthcheck_marker_and_database_match_entrypoint_and_defaults():
    defaults = _defaults()
    assert _entrypoint_var("DONE_MARKER") in _healthcheck_test()
    assert _entrypoint_var("DB") == defaults["dbname"]
    assert _entrypoint_var("PASSWORD") == defaults["admin_password"]
    assert f"-d {defaults['dbname']}" in _healthcheck_test()
    assert "-U qasource" in _healthcheck_test()


def test_published_port_matches_service_defaults():
    port = _defaults()["port"]
    assert f"${{INT_VERTICA_HOST_PORT:-{port}}}:5433" in _compose_service()["ports"]


def test_image_differs_from_the_live_tiers_copy_only_by_names():
    # The integration copy is a port of the live one; it must not drift in behaviour.
    # Allowed: the container name in comments, the marker and the database name.
    ours = SERVICE_DIR / "images" / "vertica"
    assert (ours / "init.sql").read_text() == (LIVE_IMAGE / "init.sql").read_text()

    def normalise(text: str) -> str:
        text = text.replace("slt-vertica", "<name>").replace("int-vertica", "<name>")
        text = text.replace(".slt-init-done", "<marker>").replace(".int-init-done", "<marker>")
        return re.sub(r"^DB=\S+$", "DB=<db>", text, flags=re.MULTILINE)

    assert normalise(_entrypoint_text(ours)) == normalise(_entrypoint_text(LIVE_IMAGE))
    ours_df = [ln for ln in (ours / "Dockerfile").read_text().splitlines() if not ln.startswith("#")]
    live_df = [ln for ln in (LIVE_IMAGE / "Dockerfile").read_text().splitlines() if not ln.startswith("#")]
    assert ours_df == live_df
