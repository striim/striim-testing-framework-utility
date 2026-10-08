from pathlib import Path
import pytest
from livetest.registry import load_service, RegistryError
from tests import _hermetic_child

def test_loads_postgres_service():
    s = load_service("postgres")
    assert s.name == "postgres"
    assert s.isolation == "none"          # fixed qasource/qatarget schemas, serial (was per-test schema)
    assert s.compose == "compose.yaml"
    assert s.live_override_env == "SLT_PG_HOST"
    assert s.docker_defaults["port"] in (5432, "5432")
    assert s.provides["PG_URL"] == "jdbc:postgresql://{view_host}:{port}/{dbname}"

def test_unknown_service_raises():
    with pytest.raises(RegistryError, match="unknown"):
        load_service("nosuchdb")   # mysql WAS the example until services/mysql/ landed


def test_opt_in_env_declared_per_service():
    # All container services run by default (opt_in_env is None).
    assert load_service("postgres").opt_in_env is None
    assert load_service("oracle").opt_in_env is None
    assert load_service("spanner").opt_in_env is None
    assert load_service("gcs").opt_in_env is None
    assert load_service("kafka").opt_in_env is None
    assert load_service("mysql").opt_in_env is None


def test_mssql_uses_full_engine_compose():
    s = load_service("mssql")
    assert s.compose == "compose.yaml"
    assert s.container == "slt-mssql"


# ---- consumer roots: the project manifest's servicesRoots reach the registry -------
# A name is looked up root by root: set_roots / servicesRoots in their order, the built-in
# services dir last. The first root that defines it wins, so a consumer root overrides a
# built-in of the same name, and the override is reported once on stderr naming both paths.

def _svc(root, name, container):
    d = root / name
    d.mkdir(parents=True)
    (d / "service.yaml").write_text(f"name: {name}\nisolation: none\ncompose: compose.yaml\n"
                                    f"container: {container}\n")
    return d


@pytest.fixture
def overlay():
    from livetest import layout, registry
    layout._reset()
    registry._REPORTED.clear()
    yield layout
    layout._reset()
    registry._REPORTED.clear()


def test_a_consumer_root_overrides_a_builtin_of_the_same_name(tmp_path, overlay, capsys):
    mine = _svc(tmp_path, "postgres", "my-postgres")
    overlay.set_roots(services=[tmp_path])
    s = load_service("postgres")
    assert s.dir == mine and s.container == "my-postgres"
    err = capsys.readouterr().err
    assert f"postgres: using {mine}, which overrides" in err
    assert str(Path(__file__).resolve().parents[1] / "services" / "postgres") in err
    load_service("postgres")
    assert capsys.readouterr().err == "", "the override is reported once per process"


def test_replacing_a_connection_only_builtin_is_not_reported(tmp_path, overlay, capsys):
    # The shipped teradata has no compose and no container: a consumer service that ships one
    # is what it is there for, so the run says nothing about it, in the header or on stderr.
    from livetest import registry
    mine = _svc(tmp_path, "teradata", "my-teradata")
    overlay.set_roots(services=[tmp_path])
    assert load_service("teradata").dir == mine
    assert capsys.readouterr().err == ""
    assert not any("teradata" in line for line in registry.overrides())


def test_without_a_consumer_root_the_builtin_is_used_silently(overlay, capsys):
    assert load_service("postgres").dir == \
        Path(__file__).resolve().parents[1] / "services" / "postgres"
    assert capsys.readouterr().err == ""


def test_the_first_consumer_root_wins_over_a_later_one(tmp_path, overlay):
    a, b = tmp_path / "a", tmp_path / "b"
    first = _svc(a, "extra", "extra-a")
    _svc(b, "extra", "extra-b")
    overlay.set_roots(services=[a, b])
    assert load_service("extra").dir == first


def test_all_services_lists_consumer_services_once(tmp_path, overlay):
    from livetest.registry import all_services
    _svc(tmp_path, "extdb", "slt-extdb")
    _svc(tmp_path, "postgres", "my-postgres")
    overlay.set_roots(services=[tmp_path])
    names = all_services()
    assert "extdb" in names and names.count("postgres") == 1 and "oracle" in names


def test_manifest_services_roots_reach_the_registry(tmp_path, overlay, monkeypatch, capsys):
    from livetest import project
    mine = _svc(tmp_path / "consumer" / "services", "extdb", "slt-extdb")
    _svc(tmp_path / "consumer" / "services", "postgres", "consumer-postgres")
    monkeypatch.setenv("SLT_TEST_FIELD_SERVICES", "services")
    (tmp_path / "consumer" / "striim-test.yaml").write_text(
        "schemaVersion: 1\ntargets: []\nsuites:\n  live: .\n"
        "servicesRoots:\n  - ${SLT_TEST_FIELD_SERVICES}\n")
    project.load_and_activate(tmp_path / "consumer" / "striim-test.yaml")
    assert load_service("extdb").dir == mine
    assert load_service("postgres").container == "consumer-postgres"
    assert "postgres: using" in capsys.readouterr().err
    project.load_and_activate(None)
    with pytest.raises(RegistryError, match="unknown service 'extdb'"):
        load_service("extdb")


# The override is announced where a run shows it. The first load_service of a
# run is inside a test, and pytest drops a passing test's stderr (striim-test runs with -rfEs),
# so the line has to come from the report header.

_OVERRIDE_CONFTEST = '''
import pytest
from livetest import project

@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    project.load_and_activate("{manifest}")
'''


def test_a_green_run_shows_the_override_in_its_report_header(tmp_path):
    import os
    import subprocess
    import sys
    live = Path(__file__).resolve().parents[1]
    consumer = tmp_path / "consumer"
    _svc(consumer / "services", "postgres", "consumer-postgres")
    manifest = consumer / "gold-targets.yaml"
    manifest.write_text("schemaVersion: 1\ntargets: []\nsuites:\n  live: .\n"
                        "servicesRoots:\n  - services\n")
    run = tmp_path / "run"
    run.mkdir()
    (run / "conftest.py").write_text(_OVERRIDE_CONFTEST.format(manifest=manifest))
    (run / "test_green.py").write_text(
        "from livetest.registry import load_service\n\n"
        "def test_green():\n    assert load_service('postgres').container == 'consumer-postgres'\n")
    base = {k: v for k, v in os.environ.items()
            if not (k.startswith(("SLT_", "PYTEST_", "GOLD_")) or k in ("STRIIM_URL", "PYTHONPATH"))}
    env = _hermetic_child.child_env(run / "no-such-settings-file", base=base, PYTHONPATH=str(live),
                                    PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run([sys.executable, "-m", "pytest", "-p", "livetest.plugin", "-o", "addopts=",
                        "-p", "no:cacheprovider", "-rfEs", str(run)],
                       cwd=run, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    shown = r.stdout + r.stderr
    assert f"[services] postgres: using {consumer.resolve() / 'services' / 'postgres'}, which overrides" \
        in shown, shown[-3000:]
    assert shown.count("[services] postgres:") == 1
