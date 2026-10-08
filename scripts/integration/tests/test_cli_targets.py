"""`inttest.cli` target resolution and the cold-DB setup `start postgres` runs.

The setup is what makes a service this CLI starts usable rather than merely running:
without it `start postgres` hands you an empty database, since the roles and the fixed
qasource/qatarget schemas are otherwise created by a pytest fixture that never runs here.
"""
import pytest

from inttest import cli


def test_named_services_keep_dependency_order():
    # Declaration order, not argv order -- postgres before oracle before spanner.
    assert cli._resolve_services(["spanner", "postgres"]) == ["postgres", "spanner"]


def test_all_expands_to_every_service():
    assert cli._resolve_services(["all"]) == cli._services()


def test_repeated_service_is_not_started_twice():
    assert cli._resolve_services(["postgres", "postgres"]) == ["postgres"]


def test_unknown_target_is_fatal():
    with pytest.raises(SystemExit) as exc:
        cli._resolve_services(["nosuch"])
    assert "nosuch" in str(exc.value)


@pytest.fixture(autouse=True)
def _no_docker(monkeypatch):
    """`cmd_down` asks Docker whether a container survived its `compose down`; a hermetic test
    must not. Without this the suite passes only on a machine with no int-* containers -- it
    failed for real the moment the unit tests ran while the integration services were up,
    which is the ordinary state during a debugging session. Tests about the check set their
    own answer, which wins."""
    monkeypatch.setattr(cli, "surviving_containers", lambda svc: [])


def _capture(monkeypatch):
    started, setups = [], []
    monkeypatch.setattr(cli, "compose_up", lambda svc, lock: started.append(svc))
    monkeypatch.setattr(cli, "compose_down", lambda svc, lock: started.append(("down", svc)))
    monkeypatch.setattr(cli, "_postgres_setup", lambda: setups.append("postgres"))
    return started, setups


def test_start_postgres_runs_the_cold_db_setup(monkeypatch):
    started, setups = _capture(monkeypatch)
    assert cli.cmd_up(["postgres"]) == 0
    assert started == ["postgres"] and setups == ["postgres"]


def test_start_without_postgres_skips_the_setup(monkeypatch):
    started, setups = _capture(monkeypatch)
    assert cli.cmd_up(["gcs"]) == 0
    assert started == ["gcs"] and setups == []


def test_failed_setup_fails_the_command(monkeypatch):
    # A container that came up but has no roles/schemas is not a usable postgres --
    # reporting success here would strand the caller with a half-provisioned service.
    _capture(monkeypatch)

    def _boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(cli, "_postgres_setup", _boom)
    assert cli.cmd_up(["postgres"]) == 1


def test_stop_tears_down_in_reverse_order(monkeypatch):
    started, _ = _capture(monkeypatch)
    assert cli.cmd_down(["postgres", "spanner"]) == 0
    assert started == [("down", "spanner"), ("down", "postgres")]


def test_service_list_is_derived_from_disk(tmp_path, monkeypatch):
    # Hardcoding it let a runner's directory scan drift from this list, so adding a service
    # made the runner route a name this CLI exits on -- with the whole suite still green.
    d = tmp_path / "services"
    for name in ("postgres", "mysql"):
        (d / name).mkdir(parents=True)
        (d / name / "service.yaml").write_text(f"name: {name}\nisolation: shared\n")
    monkeypatch.setenv("SLT_INT_SERVICES_DIR", str(d))
    # declared order first, then anything new, alphabetically
    assert cli._registered() == ["postgres", "mysql"]


def test_postgres_setup_does_not_import_pytest_at_module_load():
    # _postgres_setup imports inttest.plugin lazily so `inttest.cli start gcs` keeps working
    # in a checkout with only the runtime deps installed.
    import subprocess, sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    probe = subprocess.run(
        [sys.executable, "-c",
         "import sys; import inttest.cli; "
         "print('inttest.plugin' in sys.modules or 'pytest' in sys.modules)"],
        cwd=root, capture_output=True, text=True)
    assert probe.stdout.strip() == "False", probe.stdout + probe.stderr


def test_postgres_setup_resolves_its_tokens_without_pytest():
    # The lazy import exists so a runtime-only checkout can run the CLI, but the tokens came
    # from `plugin`, which imports pytest -- a [dev]-only extra. So `start postgres`, and
    # therefore every `start integration` (its default set always contains
    # postgres), died with ModuleNotFoundError exactly where the laziness was meant to help.
    # Module load alone does not catch it; the token resolution has to be exercised.
    import subprocess, sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    probe = subprocess.run(
        [sys.executable, "-c",
         "import sys; from inttest import tokens; "
         "t = tokens.service_tokens('postgres'); "
         "assert t['POSTGRES_DB'], t; "
         "print('pytest' in sys.modules or 'inttest.plugin' in sys.modules)"],
        cwd=root, capture_output=True, text=True)
    assert probe.returncode == 0, probe.stdout + probe.stderr
    assert probe.stdout.strip() == "False", probe.stdout + probe.stderr
