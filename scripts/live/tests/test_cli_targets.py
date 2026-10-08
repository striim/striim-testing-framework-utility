"""`livetest.cli` target resolution: service names vs test ids vs unknown.

The split matters because the two kinds route to different provisioning paths --
services to `preflight.provision_services` (containers only), test ids to the full
`preflight.provision` (cluster + OP/UDF union) -- and because an unknown name used to
reach preflight, which logs a warning and returns 0, making a typo look like a
successful bring-up that started nothing.
"""
import os

import pytest

from livetest import cli


def test_service_names_resolve_to_services(monkeypatch):
    monkeypatch.setattr(cli, "all_services", lambda: ["gcs", "postgres"])
    monkeypatch.setattr(cli, "known_test_ids", set)
    assert cli._resolve_targets(["postgres"]) == (["postgres"], [], False)


def test_all_expands_to_every_registered_service(monkeypatch):
    monkeypatch.setattr(cli, "all_services", lambda: ["gcs", "postgres"])
    monkeypatch.setattr(cli, "known_test_ids", set)
    assert cli._resolve_targets(["all"]) == (["gcs", "postgres"], [], True)


def test_test_ids_stay_test_ids(monkeypatch):
    monkeypatch.setattr(cli, "all_services", lambda: ["postgres"])
    monkeypatch.setattr(cli, "known_test_ids", lambda: {"some-live-case"})
    assert cli._resolve_targets(["some-live-case"]) == ([], ["some-live-case"], False)


def test_mixed_targets_split_by_kind(monkeypatch):
    monkeypatch.setattr(cli, "all_services", lambda: ["postgres"])
    monkeypatch.setattr(cli, "known_test_ids", lambda: {"some-live-case"})
    assert cli._resolve_targets(["postgres", "some-live-case"]) == (["postgres"], ["some-live-case"], False)


def test_repeated_service_is_not_provisioned_twice(monkeypatch):
    monkeypatch.setattr(cli, "all_services", lambda: ["gcs", "postgres"])
    monkeypatch.setattr(cli, "known_test_ids", set)
    assert cli._resolve_targets(["postgres", "all", "postgres"])[0] == ["postgres", "gcs"]


def test_unknown_target_is_fatal(monkeypatch):
    monkeypatch.setattr(cli, "all_services", lambda: ["postgres"])
    monkeypatch.setattr(cli, "known_test_ids", set)
    with pytest.raises(SystemExit) as exc:
        cli._resolve_targets(["nosuch"])
    assert "nosuch" in str(exc.value)


def test_start_runs_as_the_parallel_coordinator(monkeypatch):
    # preflight only routes through the shared provision registry when SLT_PARALLEL is
    # set; without it the containers come up unrecorded and the test subprocesses redo
    # the bring-up they were meant to skip.
    # setenv, not delenv: delenv(raising=False) records NO undo when the var is absent, so
    # _as_coordinator's write would survive teardown and leak into the rest of the session
    # (SLT_PARALLEL also drives ${TID} object-name tokenization).
    monkeypatch.setenv("SLT_PARALLEL", "0")
    monkeypatch.setattr(cli, "all_services", lambda: ["postgres"])
    monkeypatch.setattr(cli, "known_test_ids", set)
    seen = {}

    def _fake_provision(names, apply_gate=False):
        seen["parallel"] = os.environ.get("SLT_PARALLEL")
        return 0

    monkeypatch.setattr(cli, "preflight_provision_services", _fake_provision)
    assert cli.cmd_up(["postgres"]) == 0
    assert seen["parallel"] == "1"


def test_stop_does_not_touch_the_cluster_path(monkeypatch):
    monkeypatch.setattr(cli, "all_services", lambda: ["postgres"])
    monkeypatch.setattr(cli, "known_test_ids", set)
    monkeypatch.setattr(cli, "preflight_teardown",
                        lambda ids: pytest.fail("service stop must not run the full teardown"))
    monkeypatch.setattr(cli, "preflight_teardown_services", lambda names: 0)
    assert cli.cmd_down(["postgres"]) == 0


# --- the two routes must both actually fire -----------------------------------------------

def test_test_id_targets_reach_the_full_preflight(monkeypatch):
    # Dropping test-id targets entirely used to pass every test: the split was asserted, the
    # routing was not.
    monkeypatch.setenv("SLT_PARALLEL", "0")
    monkeypatch.setattr(cli, "all_services", lambda: ["postgres"])
    monkeypatch.setattr(cli, "known_test_ids", lambda: {"some-live-case"})
    seen = []
    monkeypatch.setattr(cli, "preflight_provision", lambda ids: seen.append(ids) or 0)
    assert cli.cmd_up(["some-live-case"]) == 0
    assert seen == [["some-live-case"]]


def test_test_id_teardown_reaches_the_full_preflight_explicitly(monkeypatch):
    monkeypatch.setenv("SLT_PARALLEL", "0")
    monkeypatch.setattr(cli, "all_services", lambda: ["postgres"])
    monkeypatch.setattr(cli, "known_test_ids", lambda: {"some-live-case"})
    seen = {}
    monkeypatch.setattr(cli, "preflight_teardown",
                        lambda ids, explicit=False: seen.update(ids=ids, explicit=explicit) or 0)
    assert cli.cmd_down(["some-live-case"]) == 0
    assert seen == {"ids": ["some-live-case"], "explicit": True}


# --- the cluster is a target of its own ----------------------------------------------------

def test_striim_target_provisions_the_cluster(monkeypatch):
    monkeypatch.setenv("SLT_PARALLEL", "0")
    monkeypatch.setattr(cli, "all_services", lambda: ["postgres"])
    monkeypatch.setattr(cli, "known_test_ids", set)
    calls = []
    monkeypatch.setattr(cli, "preflight_provision_cluster", lambda publish_gcs_host=False: calls.append("cluster") or 0)
    monkeypatch.setattr(cli, "preflight_provision_services",
                        lambda names, apply_gate=False: pytest.fail(
                            "striim must not route through the service path"))
    assert cli.cmd_up(["striim"]) == 0
    assert calls == ["cluster"]


def test_all_includes_the_cluster_and_the_services(monkeypatch):
    monkeypatch.setenv("SLT_PARALLEL", "0")
    monkeypatch.setattr(cli, "all_services", lambda: ["gcs", "postgres"])
    monkeypatch.setattr(cli, "known_test_ids", set)
    order = []
    monkeypatch.setattr(cli, "preflight_provision_cluster", lambda publish_gcs_host=False: order.append("cluster") or 0)
    monkeypatch.setattr(cli, "preflight_provision_services", lambda names, apply_gate=False: order.append(names) or 0)
    assert cli.cmd_up(["all"]) == 0
    # cluster first: gcs reads its -public-host off a resolved cluster
    assert order == ["cluster", ["gcs", "postgres"]]


def test_naming_a_service_leaves_the_cluster_alone(monkeypatch):
    monkeypatch.setenv("SLT_PARALLEL", "0")
    monkeypatch.setattr(cli, "all_services", lambda: ["postgres"])
    monkeypatch.setattr(cli, "known_test_ids", set)
    monkeypatch.setattr(cli, "preflight_provision_cluster",
                        lambda publish_gcs_host=False: pytest.fail(
                            "start live postgres must not touch the cluster"))
    monkeypatch.setattr(cli, "preflight_provision_services", lambda names, apply_gate=False: 0)
    assert cli.cmd_up(["postgres"]) == 0


def test_stop_striim_tears_the_cluster_down_last(monkeypatch):
    monkeypatch.setenv("SLT_PARALLEL", "0")
    monkeypatch.setattr(cli, "all_services", lambda: ["postgres"])
    monkeypatch.setattr(cli, "known_test_ids", set)
    order = []
    monkeypatch.setattr(cli, "preflight_teardown_services", lambda names: order.append("services") or 0)
    monkeypatch.setattr(cli, "preflight_teardown_cluster", lambda: order.append("cluster") or 0)
    assert cli.cmd_down(["all"]) == 0
    assert order == ["services", "cluster"]


def test_the_cluster_resolves_the_gcs_host_only_when_gcs_is_coming_up(monkeypatch):
    # Resolving it unconditionally shells out `docker exec … getent` on every `start striim`
    # and warns, on plain Docker Engine, about a value nothing will read.
    monkeypatch.setenv("SLT_PARALLEL", "0")
    monkeypatch.setattr(cli, "all_services", lambda: ["gcs", "postgres"])
    monkeypatch.setattr(cli, "known_test_ids", set)
    monkeypatch.setattr(cli, "preflight_provision_services", lambda names, apply_gate=False: 0)
    seen = []
    monkeypatch.setattr(cli, "preflight_provision_cluster",
                        lambda publish_gcs_host=False: seen.append(publish_gcs_host) or 0)

    cli.cmd_up(["striim", "postgres"])
    cli.cmd_up(["striim", "gcs"])
    assert seen == [False, True]


# --- neither half may be skipped because another one failed --------------------------------

def test_a_failed_service_stop_still_tears_the_test_ids_down(monkeypatch):
    # Short-circuiting here strands exactly what `stop` was asked to remove: postgres refusing
    # to come down must not leave the test id's own services (gcs, kafka) running.
    monkeypatch.setenv("SLT_PARALLEL", "0")
    monkeypatch.setattr(cli, "all_services", lambda: ["postgres"])
    monkeypatch.setattr(cli, "known_test_ids", lambda: {"gcs-diff"})
    torn = []
    monkeypatch.setattr(cli, "preflight_teardown_services", lambda names: 1)
    monkeypatch.setattr(cli, "preflight_teardown",
                        lambda ids, explicit=False: torn.extend(ids) or 0)

    assert cli.cmd_down(["postgres", "gcs-diff"]) == 1     # the failure still surfaces
    assert torn == ["gcs-diff"]


def test_a_failed_cluster_stop_still_tears_the_services_down(monkeypatch):
    monkeypatch.setenv("SLT_PARALLEL", "0")
    monkeypatch.setattr(cli, "all_services", lambda: ["postgres"])
    monkeypatch.setattr(cli, "known_test_ids", set)
    torn = []
    monkeypatch.setattr(cli, "preflight_teardown_services", lambda names: torn.extend(names) or 0)
    monkeypatch.setattr(cli, "preflight_teardown_cluster", lambda: 1)

    assert cli.cmd_down(["all"]) == 1
    assert torn == ["postgres"]


def test_a_failed_cluster_start_still_brings_the_services_up(monkeypatch):
    # provision_services resolves no cluster by design, so `start live` on a machine with no
    # Striim distribution should still hand you the databases -- plus the error.
    monkeypatch.setenv("SLT_PARALLEL", "0")
    monkeypatch.setattr(cli, "all_services", lambda: ["postgres"])
    monkeypatch.setattr(cli, "known_test_ids", set)
    up = []
    monkeypatch.setattr(cli, "preflight_provision_cluster", lambda publish_gcs_host=False: 1)
    monkeypatch.setattr(cli, "preflight_provision_services", lambda names, apply_gate=False: up.extend(names) or 0)

    assert cli.cmd_up(["all"]) == 1
    assert up == ["postgres"]


def test_a_failed_cluster_start_skips_the_test_id_preflight(monkeypatch):
    # That path resolves the same cluster and would fail identically -- noise, not coverage.
    monkeypatch.setenv("SLT_PARALLEL", "0")
    monkeypatch.setattr(cli, "all_services", lambda: ["postgres"])
    monkeypatch.setattr(cli, "known_test_ids", lambda: {"gcs-diff"})
    monkeypatch.setattr(cli, "preflight_provision_cluster", lambda publish_gcs_host=False: 1)
    monkeypatch.setattr(cli, "preflight_provision",
                        lambda ids: pytest.fail("ran the full pre-flight without a cluster"))

    assert cli.cmd_up(["striim", "gcs-diff"]) == 1


# --- a mixed invocation must not undo its own first half -----------------------------------

def test_a_mixed_invocation_runs_the_test_id_preflight_first(monkeypatch):
    # provision() opens with clear_provision_registry, so running it AFTER the services would
    # discard the record provision_services just wrote: the next bring-up misses, _compose_reset
    # runs `down -v`, and a postgres this command brought up AND ran ensure_setup on is
    # destroyed and rebuilt seconds later.
    monkeypatch.setenv("SLT_PARALLEL", "0")
    monkeypatch.setattr(cli, "all_services", lambda: ["postgres"])
    monkeypatch.setattr(cli, "known_test_ids", lambda: {"gcs-diff"})
    order = []
    monkeypatch.setattr(cli, "preflight_provision", lambda ids: order.append("test-ids") or 0)
    monkeypatch.setattr(cli, "preflight_provision_services",
                        lambda names, apply_gate=False: order.append("services") or 0)

    assert cli.cmd_up(["postgres", "gcs-diff"]) == 0
    assert order == ["test-ids", "services"]


def test_a_gated_out_gcs_does_not_make_the_cluster_resolve_its_address(monkeypatch):
    # `start all` with SLT_GCS unset: provision_services(apply_gate=True) skips gcs, so
    # shelling out `docker exec … getent` and mutating process-wide SLT_GCS_PUBLIC_HOST /
    # SLT_STRIIM_VIEW_HOST is work for an emulator that is not coming up -- and in a mixed
    # invocation that mutated environment carries into the test-id pre-flight.
    monkeypatch.setenv("SLT_PARALLEL", "0")
    monkeypatch.delenv("SLT_GCS", raising=False)
    monkeypatch.delenv("SLT_EMULATORS", raising=False)
    monkeypatch.setattr(cli, "all_services", lambda: ["gcs", "postgres"])
    monkeypatch.setattr(cli, "known_test_ids", set)
    monkeypatch.setattr(cli, "preflight_provision_services", lambda names, apply_gate=False: 0)
    seen = []
    monkeypatch.setattr(cli, "preflight_provision_cluster",
                        lambda publish_gcs_host=False: seen.append(publish_gcs_host) or 0)

    cli.cmd_up(["all"])                                  # derived -> all services included (ungated)
    cli.cmd_up(["striim", "gcs"])                        # named   -> gcs included
    assert seen == [True, True]


def test_the_cluster_comes_down_after_the_test_ids_services(monkeypatch):
    # `stop striim <test-id>` -- the test-id teardown removes that test's SERVICES, so it
    # belongs on the service side of the cluster. It used to run after the engine was already
    # gone, the exact ordering the "cluster last" rule forbids.
    monkeypatch.setenv("SLT_PARALLEL", "0")
    monkeypatch.setattr(cli, "all_services", lambda: ["postgres"])
    monkeypatch.setattr(cli, "known_test_ids", lambda: {"gcs-diff"})
    order = []
    monkeypatch.setattr(cli, "preflight_teardown_services", lambda names: order.append("services") or 0)
    monkeypatch.setattr(cli, "preflight_teardown",
                        lambda ids, explicit=False: order.append("test-ids") or 0)
    monkeypatch.setattr(cli, "preflight_teardown_cluster", lambda: order.append("cluster") or 0)

    assert cli.cmd_down(["postgres", "striim", "gcs-diff"]) == 0
    assert order == ["services", "test-ids", "cluster"]


# --- the stack prefix every compose call of `stop` uses --------------------------------------

@pytest.fixture(autouse=False)
def real_dotenv(monkeypatch):
    """The hermetic conftest blanks the .env layer; these tests need a real one, in tmp_path."""
    from livetest import paths
    monkeypatch.setattr(paths, "dotenv_values", lambda env=None: paths.read_dotenv(paths.dotenv_path(env)))

def test_a_prefix_set_only_in_dotenv_reaches_the_process_environment(tmp_path, real_dotenv):
    """The Striim cluster's compose calls read os.environ only; the database ones read .env too.
    Without adopting it, `stop striim postgres` would stop this stack's Postgres and the
    UNPREFIXED Striim cluster, someone else's."""
    (tmp_path / ".env").write_text("SLT_STACK_PREFIX=review-a\n")
    env = {"SLT_PROJECT_ROOT": str(tmp_path)}
    cli._adopt_stack_prefix(env)
    assert env["SLT_STACK_PREFIX"] == "review-a"


def test_a_prefix_exported_in_the_shell_wins(tmp_path, real_dotenv):
    (tmp_path / ".env").write_text("SLT_STACK_PREFIX=review-a\n")
    env = {"SLT_PROJECT_ROOT": str(tmp_path), "SLT_STACK_PREFIX": "mine"}
    cli._adopt_stack_prefix(env)
    assert env["SLT_STACK_PREFIX"] == "mine"


def test_no_prefix_anywhere_stays_unset(tmp_path, real_dotenv):
    env = {"SLT_PROJECT_ROOT": str(tmp_path)}
    cli._adopt_stack_prefix(env)
    assert "SLT_STACK_PREFIX" not in env


def test_stop_runs_with_the_dotenv_prefix_in_its_environment(tmp_path, monkeypatch, real_dotenv):
    (tmp_path / ".env").write_text("SLT_STACK_PREFIX=review-a\n")
    monkeypatch.setenv("SLT_PROJECT_ROOT", str(tmp_path))
    monkeypatch.delenv("SLT_STACK_PREFIX", raising=False)
    monkeypatch.delenv("GOLD_TARGETS", raising=False)
    seen = []
    monkeypatch.setattr(cli, "cmd_down", lambda targets: seen.append((targets, os.environ.get("SLT_STACK_PREFIX"))) or 0)
    monkeypatch.setattr("sys.argv", ["livetest.cli", "stop", "striim", "postgres"])
    assert cli.main() == 0
    assert seen == [(["striim", "postgres"], "review-a")]
