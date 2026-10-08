"""The integration tier reads the project manifest's servicesRoots, runs pre_up hooks, and finds
the live tier's files through live_service_paths."""
import pytest

_MANIFEST = ("schemaVersion: 1\ntargets: []\nsuites:\n  live: tests/live\n  integration: tests/integration\n"
             "servicesRoots:\n  - services\n")
_INT_TD = """name: teradata
compose: compose.yaml
container: int-teradata
isolation: none
live_override_env: INT_TERADATA_HOST
pre_up: pre-up.sh
live_service_paths:
  INT_TERADATA_DEPS_DIR: deps
docker_defaults: {port: 1025}
live_env: {host: INT_TERADATA_HOST}
"""
_INT_COMPOSE = """services:
  int-teradata:
    image: busybox
    volumes:
      - ${INT_TERADATA_DEPS_DIR:-./deps}:/disks:ro
"""


@pytest.fixture
def consumer(tmp_path, monkeypatch):
    """A consumer repo laid out with services/teradata (live) and
    services/integration/teradata, named by gold-targets.yaml's servicesRoots."""
    from livetest import layout, project
    root = tmp_path / "consumer"
    live = root / "services" / "teradata"
    live.mkdir(parents=True)
    (live / "service.yaml").write_text("name: teradata\ncompose: compose.yaml\ncontainer: slt-teradata\n"
                                       "isolation: none\nlive_override_env: SLT_TERADATA_HOST\n"
                                       "required_files: [deps/disk1.qcow2]\n")
    (live / "compose.yaml").write_text("services: {}\n")
    it = root / "services" / "integration" / "teradata"
    it.mkdir(parents=True)
    (it / "service.yaml").write_text(_INT_TD)
    (it / "compose.yaml").write_text(_INT_COMPOSE)
    (it / "pre-up.sh").write_text('#!/bin/sh\necho "$SLT_SERVICE_DIR|$SLT_LIVE_SERVICE_DIR" > ran\n'
                                  'mkdir -p "$SLT_LIVE_SERVICE_DIR/deps"\n'
                                  ': > "$SLT_LIVE_SERVICE_DIR/deps/disk1.qcow2"\n')
    (it / "pre-up.sh").chmod(0o755)
    (root / "tests" / "live").mkdir(parents=True)
    (root / "tests" / "integration").mkdir(parents=True)
    (root / "gold-targets.yaml").write_text(_MANIFEST)
    monkeypatch.setenv("GOLD_TARGETS", str(root / "gold-targets.yaml"))
    from livetest import stack
    monkeypatch.setattr(stack, "_LOCK_DIR", tmp_path / "locks")
    project.load_and_activate()
    yield root
    monkeypatch.delenv("GOLD_TARGETS")
    layout._reset()
    project.load_and_activate()


def test_a_consumer_integration_service_overrides_the_shipped_one_by_name(consumer, capsys):
    from inttest import resources
    it = consumer / "services" / "integration" / "teradata"
    assert resources.consumer_roots() == (it.parent.resolve(),)
    assert resources.service_dir("teradata") == it.resolve()
    # The shipped teradata is connection-only: replacing it is its use, so it is not reported.
    assert "[services] teradata" not in capsys.readouterr().err
    assert resources.select_profile("teradata").origin == it.resolve()
    assert resources.list_profiles().count("teradata") == 1


def test_overriding_a_shipped_container_service_is_reported(consumer, capsys):
    from inttest import resources
    pg = consumer / "services" / "integration" / "postgres"
    pg.mkdir()
    (pg / "service.yaml").write_text("name: postgres\ncompose: compose.yaml\ncontainer: my-pg\n"
                                     "isolation: none\n")
    assert resources.service_dir("postgres") == pg.resolve()
    assert f"[services] postgres: using {pg.resolve()}, which overrides" in capsys.readouterr().err


def test_live_service_paths_point_at_the_live_tiers_service_of_the_same_name(consumer, monkeypatch):
    from inttest import services
    monkeypatch.delenv("INT_TERADATA_DEPS_DIR", raising=False)
    env = services._compose_up_env("teradata")
    assert env["INT_TERADATA_DEPS_DIR"] == str((consumer / "services" / "teradata" / "deps").resolve())
    monkeypatch.setenv("INT_TERADATA_DEPS_DIR", "/elsewhere")          # the shell wins
    assert services._compose_up_env("teradata")["INT_TERADATA_DEPS_DIR"] == "/elsewhere"


def test_the_integration_hook_runs_with_both_service_dirs(consumer, monkeypatch):
    from inttest import services
    monkeypatch.delenv("SLT_PRE_UP")          # the suite's own switch, lifted for this test
    it = (consumer / "services" / "integration" / "teradata").resolve()
    assert services.run_pre_up("teradata") is True
    assert (it / "ran").read_text().strip() == f"{it}|{(consumer / 'services' / 'teradata').resolve()}"


@pytest.mark.parametrize("env", [{"SLT_PRE_UP": "0"}, {"INT_TERADATA_HOST": "td.example"}])
def test_the_integration_hook_stops_for_the_switch_or_an_existing_instance(consumer, monkeypatch, env):
    from inttest import services
    monkeypatch.delenv("SLT_PRE_UP")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert services.run_pre_up("teradata") is False
    assert not (consumer / "services" / "integration" / "teradata" / "ran").exists()


def test_start_all_runs_no_hook_and_a_named_start_does(consumer, monkeypatch):
    from inttest import cli
    ups = []
    monkeypatch.setattr(cli, "compose_up", lambda s, lock: ups.append(s))
    monkeypatch.setattr(cli, "_postgres_setup", lambda: None)
    monkeypatch.setattr(cli, "run_pre_up", lambda s: ups.append(f"pre_up:{s}"))
    monkeypatch.setattr(cli, "unavailable", lambda s: None)
    assert cli.cmd_up(["all"]) == 0
    assert not [u for u in ups if u.startswith("pre_up:")]
    ups.clear()
    assert cli.cmd_up(["teradata"]) == 0
    assert ups == ["pre_up:teradata", "teradata"]


def test_start_and_stop_all_leave_a_connection_only_service_alone(monkeypatch, tmp_path, capsys):
    # Review TD1: the shipped integration teradata has no compose file; `start all`/`stop all`
    # ran compose on it and failed.
    from inttest import cli
    calls = []
    monkeypatch.setattr(cli, "compose_up", lambda s, lock: calls.append(("up", s)))
    monkeypatch.setattr(cli, "compose_down", lambda s, lock: calls.append(("down", s)))
    monkeypatch.setattr(cli, "surviving_containers", lambda s: [])
    monkeypatch.setattr(cli, "_postgres_setup", lambda: None)
    monkeypatch.setattr(cli, "run_pre_up", lambda s: None)
    assert "teradata" not in cli._registered()
    assert cli.cmd_up(["all"]) == 0 and cli.cmd_down(["all"]) == 0
    assert not [c for c in calls if c[1] == "teradata"]
    calls.clear()
    assert cli.cmd_up(["teradata"]) == 0 and calls == []
    assert "connection only" in capsys.readouterr().out


# ---- review round: H1-H4 ----------------------------------------------------------------------

def _live_disk(consumer):
    return consumer / "services" / "teradata" / "deps" / "disk1.qcow2"


def test_integration_required_files_include_the_live_files_it_boots(consumer):
    # Review H3: the integration service boots the live service's disks (live_service_paths), so
    # the live required_files under that path are its required files too.
    from inttest import services
    assert services.required_files("teradata") == [_live_disk(consumer).resolve()]
    assert "required files missing" in services.unavailable("teradata")
    _live_disk(consumer).parent.mkdir(parents=True)
    _live_disk(consumer).write_text("")
    assert services.unavailable("teradata") is None


def test_a_case_skips_naming_the_files_where_it_used_to_fail_in_compose(consumer, monkeypatch):
    from filelock import FileLock
    from inttest import plugin
    monkeypatch.setattr(plugin._docker_mod, "ensure_up",
                        lambda *a, **k: pytest.fail("must not reach compose up"))
    with pytest.raises(pytest.skip.Exception, match="required files missing"):
        plugin._provision_requires("case", ["teradata"], FileLock(str(consumer / "l.lock")))


def test_start_all_leaves_out_a_service_whose_files_are_missing(consumer, monkeypatch, capsys):
    from inttest import cli
    ups = []
    monkeypatch.setattr(cli, "compose_up", lambda s, lock: ups.append(s))
    monkeypatch.setattr(cli, "_postgres_setup", lambda: None)
    assert cli.cmd_up(["all"]) == 0
    assert "teradata" not in ups and "required files missing" in capsys.readouterr().out


def test_a_failed_integration_hook_fails_the_case(consumer, monkeypatch):
    # Review H2, integration side.
    from filelock import FileLock
    from inttest import plugin
    monkeypatch.delenv("SLT_PRE_UP")
    monkeypatch.setattr(plugin._docker_mod, "ensure_up",
                        lambda *a, **k: pytest.fail("must not reach compose up"))
    (consumer / "services" / "integration" / "teradata" / "pre-up.sh").write_text("#!/bin/sh\nexit 9\n")
    with pytest.raises(pytest.fail.Exception, match="exited 9"):
        plugin._provision_requires("case", ["teradata"], FileLock(str(consumer / "l.lock")))


def test_the_integration_hook_is_skipped_when_its_files_are_present(consumer, monkeypatch):
    # Review H1, integration side.
    from inttest import services
    monkeypatch.delenv("SLT_PRE_UP")
    _live_disk(consumer).parent.mkdir(parents=True)
    _live_disk(consumer).write_text("")
    assert services.run_pre_up("teradata") is False
    assert not (consumer / "services" / "integration" / "teradata" / "ran").exists()


def test_the_integration_tier_uses_the_live_hook_runner(consumer, monkeypatch):
    # Review H4: one runner (lock, switch, timeout, skip-when-present) for both tiers.
    from inttest import services
    from livetest import prestart
    seen = {}
    monkeypatch.setattr(prestart, "run_hook", lambda name, d, script, **k: seen.update(
        name=name, script=script, required=k["required"], timeout=k["timeout"]) or True)
    assert services.run_pre_up("teradata") is True
    assert seen == {"name": "teradata", "script": "pre-up.sh",
                    "required": [_live_disk(consumer).resolve()], "timeout": None}


def test_an_entry_is_read_with_the_live_tiers_precedence(tmp_path, monkeypatch):
    # Review H4: a repo-root entry holding services/ and scripts/integration/services/ (consumer during
    # 5.4) gives services/integration in both tiers, never the old scripts/integration/services tree.
    from inttest import resources
    from livetest import layout
    root = tmp_path / "consumer"
    (root / "services" / "integration" / "teradata").mkdir(parents=True)
    (root / "scripts" / "integration" / "services" / "postgres").mkdir(parents=True)
    assert layout._services_dir_for(root) == root / "services"
    assert resources._integration_dir_for(root) == root / "services" / "integration"


def test_a_consumer_integration_service_under_a_new_name_is_supported(consumer):
    # Console impact F3: SUPPORTED_SERVICES comes from the roots, so a consumer's service is not
    # refused as "unsupported" by _provision_requires.
    from inttest import services
    extra = consumer / "services" / "integration" / "mongo"
    extra.mkdir()
    (extra / "service.yaml").write_text("name: mongo\ncompose: compose.yaml\ncontainer: int-mongo\nisolation: none\n")
    (extra / "compose.yaml").write_text("services: {}\n")
    assert "mongo" in services.SUPPORTED_SERVICES
    assert {"postgres", "teradata"} <= services.SUPPORTED_SERVICES
