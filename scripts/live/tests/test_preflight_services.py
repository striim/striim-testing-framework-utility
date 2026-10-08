"""The services-only provisioning path (`preflight.provision_services`/`teardown_services`).

These cover the asymmetry that makes an explicit `stop` different from end-of-run teardown:
the provision registry records a container so a later bring-up skips it, which is right
across a session's subprocesses and wrong across a user's stop/start cycle.
"""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from livetest import infra, preflight, services, stack
from livetest.registry import RegistryError


def _fake_defn(name):
    # A real `dir` so service_compose_path resolves and the orphan check is the code under
    # test; the file itself is never read (orphaned_containers is stubbed).
    return SimpleNamespace(name=name, compose="compose.yaml", container=f"slt-{name}",
                           dir=Path("/svc") / name, opt_in_env=None)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    """No Docker, and no writes to the developer's real coordination state.

    Teardown asks Docker whether a container SURVIVED `compose down` (a leftover from another
    compose project) and bring-up asks whether a recorded one is still running; neither may
    shell out here. Tests about those checks set their own answer, which wins -- monkeypatch
    applies in order.

    `_STATE_DIR` is redirected too, and that part is load-bearing: pinning the predicates without
    it makes `_prune_dead_records` decide every record is stale and call the REAL
    `forget_provisioned` against scripts/live/.slt-provision-registry.json, so running the
    unit suite de-registers a developer's actually-running containers."""
    monkeypatch.setattr(preflight, "container_running", lambda c: False)
    monkeypatch.setattr(preflight, "container_exists", lambda c: False)
    monkeypatch.setattr(preflight, "orphaned_containers", lambda paths, env=None: [])
    monkeypatch.setattr(preflight, "_STATE_DIR", tmp_path)
    # C7.1: every pre-flight entry point declares infrastructure ownership before a bring-up. These
    # tests run as an operator who declared a kept stack; the real declaration still runs, on the caller's env with
    # only the absent keys filled, so an env that declares nothing on purpose is still refused. Markers go under
    # tmp_path, never the shared /tmp/slt-locks.
    monkeypatch.setattr(stack, "_LOCK_DIR", tmp_path / "locks")
    declare_or_log = infra.declare_or_log
    monkeypatch.setattr(infra, "declare_or_log", lambda env, log: declare_or_log(
        {"SLT_INFRA_OWNERSHIP": "shared", "SLT_KEEP_SERVICES": "1", **env}, log))


@pytest.fixture(autouse=True)
def _no_cluster_env_leak(monkeypatch):
    """Both vars absent at entry, and restored afterwards.

    `provision_cluster` writes os.environ directly, and `delenv(raising=False)` records NO
    undo when the var was already absent -- so a test's fake value survives teardown. That
    matters here: pyproject's testpaths collect `tests` and `regression` in ONE process, and
    plugin.py uses `setdefault` for both, so a leaked fake WINS over the real cluster's value
    and every live test afterwards addresses a host that does not exist. setenv-then-delenv
    is what makes monkeypatch record the undo."""
    for var in ("SLT_GCS_PUBLIC_HOST", "SLT_STRIIM_VIEW_HOST"):
        monkeypatch.setenv(var, "")
        monkeypatch.delenv(var)


# --- the registry record must not outlive the container -----------------------------------

def test_forget_provisioned_lets_a_later_bring_up_run_again(tmp_path):
    calls = []
    services.ensure_provisioned_once("c1", lambda: calls.append("up"), state_dir=tmp_path)
    services.ensure_provisioned_once("c1", lambda: calls.append("up"), state_dir=tmp_path)
    assert calls == ["up"]                       # recorded, so the second call skips

    services.forget_provisioned(["c1"], state_dir=tmp_path)
    services.ensure_provisioned_once("c1", lambda: calls.append("up"), state_dir=tmp_path)
    assert calls == ["up", "up"]                 # record gone, so it provisions again


def test_forget_provisioned_leaves_other_records_alone(tmp_path):
    services.ensure_provisioned_once("c1", lambda: None, state_dir=tmp_path)
    services.ensure_provisioned_once("c2", lambda: None, state_dir=tmp_path)
    services.forget_provisioned(["c1"], state_dir=tmp_path)

    ran = []
    services.ensure_provisioned_once("c2", lambda: ran.append("up"), state_dir=tmp_path)
    assert ran == []                             # c2 still recorded


def test_forget_provisioned_on_a_missing_registry_is_a_noop(tmp_path):
    services.forget_provisioned(["c1"], state_dir=tmp_path)   # must not raise


def test_teardown_services_forgets_the_container_and_setup_keys(monkeypatch):
    # The regression this guards: stop tore the container down but left its record, so the
    # NEXT start found the key, skipped `docker compose up`, and reported success over
    # nothing running. The `<svc>-setup` key matters too -- a stale one skips ensure_setup,
    # leaving a fresh container with no roles/schemas.
    forgotten = []
    monkeypatch.setattr(preflight, "load_service", lambda n: _fake_defn(n))
    monkeypatch.setattr(preflight, "compose_down", lambda defn: None)
    monkeypatch.setattr(preflight, "forget_provisioned", lambda keys, d=None: forgotten.extend(keys))

    assert preflight.teardown_services(["postgres"], env={}) == 0
    assert set(forgotten) == {"slt-postgres", "postgres-setup"}


# --- an explicit stop is not end-of-run teardown ------------------------------------------

def test_teardown_services_ignores_slt_keep_services(monkeypatch):
    # SLT_KEEP_SERVICES means "don't tear down when a test run ends" and is the documented
    # dev default, so it is routinely exported. Honouring it here would turn a direct
    # `stop postgres` into a no-op that reports success.
    down = []
    monkeypatch.setattr(preflight, "load_service", lambda n: _fake_defn(n))
    monkeypatch.setattr(preflight, "compose_down", lambda defn: down.append(defn.container))
    monkeypatch.setattr(preflight, "forget_provisioned", lambda keys, d=None: None)

    assert preflight.teardown_services(["postgres"], env={"SLT_KEEP_SERVICES": "1"}) == 0
    assert down == ["slt-postgres"]


def test_teardown_services_ignores_the_opt_in_gate(monkeypatch):
    # Named explicitly, so it comes down whether or not SLT_SPANNER is set -- the gate
    # answers "should this come up?", never "is it running?".
    down = []
    monkeypatch.setattr(preflight, "load_service", lambda n: _fake_defn(n))
    monkeypatch.setattr(preflight, "compose_down", lambda defn: down.append(defn.container))
    monkeypatch.setattr(preflight, "forget_provisioned", lambda keys, d=None: None)

    assert preflight.teardown_services(["spanner"], env={}) == 0
    assert down == ["slt-spanner"]


# --- failures are reported, not swallowed -------------------------------------------------

def test_teardown_services_reports_a_failure(monkeypatch):
    def _boom(defn):
        raise RuntimeError("docker daemon gone")

    monkeypatch.setattr(preflight, "load_service", lambda n: _fake_defn(n))
    monkeypatch.setattr(preflight, "compose_down", _boom)
    monkeypatch.setattr(preflight, "forget_provisioned", lambda keys, d=None: None)

    assert preflight.teardown_services(["postgres"], env={}) == 1


def test_teardown_services_keeps_going_after_one_failure(monkeypatch):
    # Best-effort: one bad service must not strand the rest.
    down = []

    def _compose_down(defn):
        if defn.container == "slt-postgres":
            raise RuntimeError("nope")
        down.append(defn.container)

    monkeypatch.setattr(preflight, "load_service", lambda n: _fake_defn(n))
    monkeypatch.setattr(preflight, "compose_down", _compose_down)
    monkeypatch.setattr(preflight, "forget_provisioned", lambda keys, d=None: None)

    assert preflight.teardown_services(["postgres", "oracle"], env={}) == 1
    assert down == ["slt-oracle"]


def test_a_container_surviving_the_down_fails_the_service(monkeypatch):
    # `down` is project-scoped: a container of the same name from ANOTHER project (e.g. one
    # started before the `postgres` -> `slt-postgres` project rename) survives it and exits 0.
    # Reporting success there hands the user a "container name already in use" on the next
    # start with nothing pointing back at the teardown.
    forgotten = []
    monkeypatch.setattr(preflight, "load_service", lambda n: _fake_defn(n))
    monkeypatch.setattr(preflight, "compose_down", lambda defn: None)
    monkeypatch.setattr(preflight, "orphaned_containers",
                        lambda paths, env=None: ["slt-postgres"] if "postgres" in str(paths[0]) else [])
    monkeypatch.setattr(preflight, "forget_provisioned", lambda keys, d=None: forgotten.extend(keys))

    assert preflight.teardown_services(["postgres", "oracle"], env={}) == 1
    assert set(forgotten) == {"slt-postgres", "postgres-setup", "slt-oracle", "oracle-setup"}


def test_the_survivors_record_is_dropped_so_the_next_start_is_not_silent(monkeypatch):
    # Keeping the record was strictly worse than the bug it guarded: `_prune_dead_records`
    # sees the survivor RUNNING and keeps it, `ensure_provisioned_once` then finds the key and
    # skips `docker compose up` -- so the next start prints "services up: 1 service(s)" and
    # Docker never gets to say "container name already in use". The survivor is not ours.
    forgotten = []
    monkeypatch.setattr(preflight, "load_service", lambda n: _fake_defn(n))
    monkeypatch.setattr(preflight, "compose_down", lambda defn: None)
    monkeypatch.setattr(preflight, "orphaned_containers", lambda paths, env=None: ["slt-postgres"])
    monkeypatch.setattr(preflight, "forget_provisioned", lambda keys, d=None: forgotten.extend(keys))

    assert preflight.teardown_services(["postgres"], env={}) == 1
    assert set(forgotten) == {"slt-postgres", "postgres-setup"}


def test_a_stopped_leftover_counts_as_a_survivor(monkeypatch):
    # Docker names are unique across ALL states: an `exited` container reserves the name just
    # as hard, so a running-only check calls the teardown clean and the next start still dies.
    monkeypatch.setattr(preflight, "load_service", lambda n: _fake_defn(n))
    monkeypatch.setattr(preflight, "compose_down", lambda defn: None)
    monkeypatch.setattr(preflight, "forget_provisioned", lambda keys, d=None: None)
    monkeypatch.setattr(preflight, "container_running", lambda c: False)          # stopped...
    monkeypatch.setattr(preflight, "orphaned_containers",
                        lambda paths, env=None: ["slt-postgres"])                 # ...but present

    assert preflight.teardown_services(["postgres"], env={}) == 1


def test_a_surviving_sidecar_is_caught_too(monkeypatch):
    # gcs and kafka each run a second container (slt-token, slt-schema-registry) that
    # service.yaml never names; checking only the primary calls the service clean while the
    # sidecar still holds the name the next bring-up needs.
    monkeypatch.setattr(preflight, "load_service", lambda n: _fake_defn(n))
    monkeypatch.setattr(preflight, "compose_down", lambda defn: None)
    monkeypatch.setattr(preflight, "forget_provisioned", lambda keys, d=None: None)
    monkeypatch.setattr(preflight, "orphaned_containers", lambda paths, env=None: ["slt-token"])

    assert preflight.teardown_services(["gcs"], env={}) == 1


def test_a_malformed_service_yaml_does_not_abort_the_rest(monkeypatch):
    # registered-service membership is "has a service.yaml"; load_service additionally
    # requires name/isolation, so an invalid one must not take the other teardowns with it.
    down = []

    def _load(name):
        if name == "postgres":
            raise RegistryError("postgres/service.yaml: 'isolation' is required")
        return _fake_defn(name)

    monkeypatch.setattr(preflight, "load_service", _load)
    monkeypatch.setattr(preflight, "compose_down", lambda defn: down.append(defn.container))
    monkeypatch.setattr(preflight, "forget_provisioned", lambda keys, d=None: None)

    assert preflight.teardown_services(["postgres", "oracle"], env={}) == 1
    assert down == ["slt-oracle"]


# --- provision_services stays narrow ------------------------------------------------------

def test_provision_services_does_not_clear_the_registries(monkeypatch):
    # Additive by design: wiping would hide a prior pre-flight's containers from the
    # subprocesses that are relying on those records.
    monkeypatch.setattr(preflight, "_bring_up_services", lambda names, env, apply_gate=True, infra=None, **kw: {})
    monkeypatch.setattr(preflight, "clear_provision_registry",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("cleared the registry")))
    monkeypatch.setattr(preflight.opregistry, "clear",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("cleared opregistry")))

    assert preflight.provision_services(["postgres"], env={}) == 0


def test_provision_services_does_not_resolve_the_cluster(monkeypatch):
    # `start postgres` must work on a machine with no Striim installed yet.
    monkeypatch.setattr(preflight, "_bring_up_services", lambda names, env, apply_gate=True, infra=None, **kw: {})
    monkeypatch.setattr(preflight, "_resolve_striim",
                        lambda cfg: (_ for _ in ()).throw(AssertionError("resolved the cluster")))

    assert preflight.provision_services(["postgres"], env={}) == 0


def test_provision_services_bypasses_the_opt_in_gate(monkeypatch):
    seen = {}
    monkeypatch.setattr(preflight, "_bring_up_services",
                        lambda names, env, apply_gate=True, infra=None: seen.update(gate=apply_gate) or {})

    preflight.provision_services(["spanner"], env={})
    assert seen["gate"] is False


# --- a record must not outlive a container that died outside this CLI ----------------------

def test_provision_services_prunes_a_record_whose_container_is_gone(monkeypatch):
    # A reboot / `docker system prune` / crash / hand-run `docker compose down` leaves the
    # record behind; without pruning, the next start skips the bring-up and reports success
    # over nothing running.
    forgotten = []
    monkeypatch.setattr(preflight, "load_service", lambda n: _fake_defn(n))
    monkeypatch.setattr(preflight, "container_running", lambda c: False)
    monkeypatch.setattr(preflight, "forget_provisioned", lambda keys, d=None: forgotten.extend(keys))
    monkeypatch.setattr(preflight, "_bring_up_services", lambda names, env, apply_gate=True, infra=None, **kw: {})

    assert preflight.provision_services(["postgres"], env={}) == 0
    assert set(forgotten) == {"slt-postgres", "postgres-setup"}


def test_provision_services_keeps_the_record_of_a_live_container(monkeypatch):
    # Still running => the record is accurate, and dropping it would let the bring-up re-run
    # a disruptive post_up against a container siblings are using.
    forgotten = []
    monkeypatch.setattr(preflight, "load_service", lambda n: _fake_defn(n))
    monkeypatch.setattr(preflight, "container_running", lambda c: True)
    monkeypatch.setattr(preflight, "forget_provisioned", lambda keys, d=None: forgotten.extend(keys))
    monkeypatch.setattr(preflight, "_bring_up_services", lambda names, env, apply_gate=True, infra=None, **kw: {})

    preflight.provision_services(["postgres"], env={})
    assert forgotten == []


# --- gcs needs its -public-host resolved BEFORE compose bakes it in ------------------------

def test_gcs_bring_up_never_provisions_a_cluster(monkeypatch):
    # _resolve_striim PROVISIONS when nothing is running, so calling it here would make
    # `start live gcs` spin up a 21GB Striim just to read an IP. The compose default is
    # already right for a Docker cluster (measured: a objectwriter live test passes
    # against it on the fan-out path).
    monkeypatch.delenv("SLT_GCS_PUBLIC_HOST", raising=False)
    monkeypatch.setattr(preflight, "_resolve_striim",
                        lambda cfg: (_ for _ in ()).throw(AssertionError("resolved the cluster")))
    monkeypatch.setattr(preflight, "_prune_dead_records", lambda names: None)
    monkeypatch.setattr(preflight, "_bring_up_services", lambda names, env, apply_gate=True, infra=None, **kw: {})

    assert preflight.provision_services(["gcs"], env={}) == 0


def test_gcs_public_host_override_is_preserved(monkeypatch):
    monkeypatch.setenv("SLT_GCS_PUBLIC_HOST", "192.0.2.9:4443")
    monkeypatch.setattr(preflight, "_resolve_striim",
                        lambda cfg: (_ for _ in ()).throw(AssertionError("resolved the cluster")))
    monkeypatch.setattr(preflight, "_prune_dead_records", lambda names: None)
    monkeypatch.setattr(preflight, "_bring_up_services", lambda names, env, apply_gate=True, infra=None, **kw: {})

    assert preflight.provision_services(["gcs"], env={}) == 0
    assert os.environ["SLT_GCS_PUBLIC_HOST"] == "192.0.2.9:4443"


def test_non_gcs_bring_up_never_resolves_the_cluster(monkeypatch):
    monkeypatch.setattr(preflight, "_resolve_striim",
                        lambda cfg: (_ for _ in ()).throw(AssertionError("resolved the cluster")))
    monkeypatch.setattr(preflight, "_prune_dead_records", lambda names: None)
    monkeypatch.setattr(preflight, "_bring_up_services", lambda names, env, apply_gate=True, infra=None, **kw: {})

    assert preflight.provision_services(["postgres", "spanner"], env={}) == 0


# --- an explicit `stop <test-id>` is not end-of-run cleanup either -------------------------

def test_explicit_teardown_ignores_slt_keep_services(monkeypatch):
    taken = []
    monkeypatch.setattr(preflight, "manifests_for", lambda ids: [SimpleNamespace(requires=["kafka"], modules=[])])
    monkeypatch.setattr(preflight, "_take_down_services", lambda svcs, env, apply_gate=True: taken.extend(svcs) or [])
    monkeypatch.setattr(preflight._sp, "cluster_down",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("tore down the cluster")))

    assert preflight.teardown(["some-test"], env={"SLT_KEEP_SERVICES": "1"}, explicit=True) == 0
    assert taken == ["kafka"]


def test_explicit_teardown_reports_failures(monkeypatch):
    monkeypatch.setattr(preflight, "manifests_for", lambda ids: [SimpleNamespace(requires=["kafka"], modules=[])])
    monkeypatch.setattr(preflight, "_take_down_services", lambda svcs, env, apply_gate=True: ["kafka"])

    assert preflight.teardown(["some-test"], env={}, explicit=True) == 1


def test_end_of_run_teardown_still_honours_keep_services(monkeypatch):
    # The console's post-run path must keep its existing semantics.
    monkeypatch.setattr(preflight, "_take_down_services",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("tore services down")))
    assert preflight.teardown(["some-test"], env={"SLT_KEEP_SERVICES": "1"}) == 0


def test_provision_services_never_builds_op_jars(monkeypatch):
    # The third thing its docstring promises, and the only one that was merely implicit.
    monkeypatch.setattr(preflight, "_prune_dead_records", lambda names: None)
    monkeypatch.setattr(preflight, "_bring_up_services", lambda names, env, apply_gate=True, infra=None, **kw: {})
    monkeypatch.setattr(preflight.opartifacts, "build_jar",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("built an OP/UDF jar")))
    assert preflight.provision_services(["postgres"], env={}) == 0


def test_bring_up_applies_the_gate_to_a_derived_list(monkeypatch):
    # Services are ungated and provision by default.
    monkeypatch.setattr(preflight, "resolve",
                        lambda svc, env, started, progress=None: SimpleNamespace(base={"host": "h"}))
    res = preflight._bring_up_services(["spanner"], {}, apply_gate=True)
    assert "spanner" in res


def test_bring_up_runs_the_live_postgres_cold_db_setup(monkeypatch):
    # Deleting this block used to pass the whole live suite.
    ran = []
    monkeypatch.setattr(preflight, "resolve",
                        lambda svc, env, started, progress=None: SimpleNamespace(base={"host": "h"}))
    monkeypatch.setattr(preflight, "PgAdmin", lambda base, role: SimpleNamespace(
        ensure_setup=lambda: None, sweep_stale_replication_slots=lambda log: None))
    monkeypatch.setattr(preflight, "ensure_provisioned_once", lambda key, fn: ran.append(key))
    preflight._bring_up_services(["postgres"], {}, apply_gate=False)
    assert ran == ["postgres-setup"]


@pytest.mark.parametrize("other_member", [False, True])
def test_postgres_preflight_skips_sweep_for_another_live_shared_member(
        monkeypatch, tmp_path, capsys, other_member):
    swept = []
    context = infra.Infra("shared", 9080, tmp_path, run_id="current")
    markers = infra._shared_dir(tmp_path, context.port)
    markers.mkdir()
    (markers / str(os.getpid())).write_text("")
    if other_member:
        (markers / "999999").write_text("")
    monkeypatch.setattr(infra, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(preflight, "resolve", lambda *a, **kw: SimpleNamespace(base={}))
    monkeypatch.setattr(preflight, "PgAdmin", lambda *a, **kw: SimpleNamespace(
        ensure_setup=lambda: None, sweep_stale_replication_slots=lambda log: swept.append(True)))
    monkeypatch.setattr(preflight, "ensure_provisioned_once", lambda key, fn: None)
    preflight._bring_up_services(["postgres"], {}, apply_gate=False, infra=context)
    assert swept == ([] if other_member else [True])
    if other_member:
        assert "skipping replication slot sweep" in capsys.readouterr().out


@pytest.mark.parametrize("owner_run", ["current", "other"])
def test_postgres_preflight_skips_sweep_for_another_exclusive_lease(
        monkeypatch, tmp_path, capsys, owner_run):
    swept = []
    context = infra.Infra("exclusive", 9080, tmp_path, run_id="current")
    monkeypatch.setattr(infra, "_exclusive_lease_held", lambda *a: True)
    monkeypatch.setattr(infra, "_read_owner", lambda *a: {"runId": owner_run})
    monkeypatch.setattr(preflight._slt_infra, "resolve_service", lambda *a, **kw: SimpleNamespace(base={}))
    monkeypatch.setattr(preflight, "PgAdmin", lambda *a, **kw: SimpleNamespace(
        ensure_setup=lambda: None, sweep_stale_replication_slots=lambda log: swept.append(True)))
    monkeypatch.setattr(preflight, "ensure_provisioned_once", lambda key, fn: None)
    preflight._bring_up_services(["postgres"], {}, apply_gate=False, infra=context)
    assert swept == ([True] if owner_run == "current" else [])
    if owner_run == "other":
        assert "skipping replication slot sweep" in capsys.readouterr().out


@pytest.mark.parametrize("activate_before_drop", [False, True])
def test_postgres_preflight_sweeps_only_inactive_harness_slots_in_its_database(
        monkeypatch, tmp_path, capsys, activate_before_drop):
    import re
    from livetest.pgclient import PgAdmin

    slots = {
        "slt_t123456789": (False, "lane_db"),
        "slt_tabcdef012": (True, "lane_db"),
        "user_slot": (False, "lane_db"),
        "slt_user_slot": (False, "lane_db"),
        "slt_t123456789_extra": (False, "lane_db"),
        "slt_t000000000": (False, "other_db"),
    }
    connections = []

    class Cursor:
        description = [("slot_name",)]

        def execute(self, sql, params):
            assert "active = false" in sql
            assert "database = current_database()" in sql
            assert "slot_name ~ %s" in sql
            pattern = params[-1]
            if "pg_drop_replication_slot" in sql and activate_before_drop:
                slots[params[0]] = (True, "lane_db")
            names = [n for n, (active, db) in slots.items()
                     if not active and db == "lane_db" and re.fullmatch(pattern, n)]
            if "pg_drop_replication_slot" in sql:
                assert "pg_drop_replication_slot(slot_name)" in sql
                assert "slot_name = %s" in sql
                names = [n for n in names if n == params[0]]
                for n in names:
                    del slots[n]
            self.rows = [(n,) for n in names]

        def fetchall(self):
            return self.rows

        def close(self):
            pass

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    def connect(**kw):
        connections.append(kw)
        return Connection()

    base = {"host": "lane-postgres", "port": 55432, "dbname": "lane_db",
            "admin_user": "postgres", "admin_password": "pw"}
    monkeypatch.setattr(preflight, "resolve", lambda *a, **kw: SimpleNamespace(base=base))
    monkeypatch.setattr(preflight, "PgAdmin", lambda base, role: PgAdmin(base, connect=connect, role=role))
    # Setup may already be registered on a kept stack; the sweep must still run.
    monkeypatch.setattr(preflight, "ensure_provisioned_once", lambda key, fn: None)
    preflight._bring_up_services(["postgres"], {}, apply_gate=False,
                                 infra=infra.Infra("shared", 9080, tmp_path, run_id="current"))
    assert ("slt_t123456789" in slots) == activate_before_drop
    assert len(slots) == (6 if activate_before_drop else 5)
    assert connections and all(c["host"] == "lane-postgres" and c["port"] == 55432
                               and c["dbname"] == "lane_db" and c["user"] == "postgres"
                               for c in connections)
    assert ("dropped stale replication slot slt_t123456789" in capsys.readouterr().out) == (not activate_before_drop)


def test_a_failed_bring_up_is_an_rc_not_a_traceback(monkeypatch):
    # This runs behind a runner's `start`, where a stack trace would scroll the clean
    # diagnosis its caller already logged off the screen. It also used to be unreachable:
    # provision_services ended in an unconditional `return 0`, so a failure could only ever
    # be reported by raising.
    monkeypatch.setattr(preflight, "_prune_dead_records", lambda names: None)
    monkeypatch.setattr(preflight, "_bring_up_services",
                        lambda names, env, apply_gate=True, infra=None: (_ for _ in ()).throw(
                            services.ServiceError("docker compose up failed")))

    assert preflight.provision_services(["postgres"], env={}) == 1


def test_provision_cluster_reports_an_unreachable_cluster(monkeypatch):
    monkeypatch.setattr(preflight, "_resolve_striim", lambda cfg: None)
    assert preflight.provision_cluster() == 1


def test_provision_cluster_succeeds_on_a_reachable_one(monkeypatch):
    monkeypatch.setattr(preflight, "_resolve_striim",
                        lambda cfg: SimpleNamespace(mode="native", url="http://x:9080", view_host="h"))
    assert preflight.provision_cluster() == 0


# --- the cluster hands gcs its -public-host, and never dies trying -------------------------

def test_provision_cluster_publishes_the_gcs_public_host(monkeypatch):
    # `docker compose up` freezes -public-host into the gcs container, so `start live`
    # (cluster, then services) gets exactly one chance to set it -- here.
    # mode="native": _set_gcs_public_host reads 127.0.0.1 without a `docker exec`.
    monkeypatch.setattr(preflight, "_resolve_striim",
                        lambda cfg: SimpleNamespace(mode="native", url="http://x:9080", view_host="h"))

    assert preflight.provision_cluster(publish_gcs_host=True) == 0
    assert os.environ["SLT_GCS_PUBLIC_HOST"] == "127.0.0.1:4443"


def test_provision_cluster_leaves_gcs_alone_when_gcs_is_not_coming_up(monkeypatch):
    # Publishing unconditionally shells out `docker exec … getent host.docker.internal` on
    # every `start striim`, mutates the process-wide view_host for a service nobody asked
    # for, and warns on plain Docker Engine about a value nothing will read.
    monkeypatch.setattr(preflight, "_resolve_striim",
                        lambda cfg: SimpleNamespace(mode="docker", url="http://x:9080", view_host="h"))
    monkeypatch.setattr(preflight, "_set_gcs_public_host",
                        lambda ctx: pytest.fail("resolved a GCS address for a cluster-only start"))

    assert preflight.provision_cluster() == 0
    assert "SLT_GCS_PUBLIC_HOST" not in os.environ


def test_a_failed_gcs_host_lookup_does_not_fail_the_cluster(monkeypatch):
    # The cluster IS up; a `docker exec getent` that comes back empty only costs gcs its
    # non-default address, and reporting the bring-up as failed would be a lie.
    monkeypatch.setattr(preflight, "_resolve_striim",
                        lambda cfg: SimpleNamespace(mode="docker", url="http://x:9080", view_host="h"))
    monkeypatch.setattr(preflight, "_set_gcs_public_host",
                        lambda ctx: (_ for _ in ()).throw(RuntimeError("getent found nothing")))
    assert preflight.provision_cluster(publish_gcs_host=True) == 0


# --- `down -v` only ever hits a cluster this checkout owns ---------------------------------

def test_teardown_cluster_leaves_a_foreign_cluster_alone(monkeypatch):
    # A native Striim (or an external STRIIM_URL) leaves this compose project empty. Running
    # `down -v` anyway would delete the slt-striim-shared volume -- MDR state we never
    # created. Mirrors plugin.pytest_sessionfinish's "only if WE provisioned it".
    monkeypatch.setattr(preflight._sp, "cluster_has_containers", lambda d, rel=None: False)
    monkeypatch.setattr(preflight._sp, "cluster_down",
                        lambda *a, **k: pytest.fail("tore down a cluster we do not own"))
    assert preflight.teardown_cluster() == 0


def test_teardown_cluster_takes_down_our_own(monkeypatch):
    torn = []
    monkeypatch.setattr(preflight._sp, "cluster_has_containers", lambda d, rel=None: True)
    monkeypatch.setattr(preflight._sp, "cluster_down", lambda d, rel=None: torn.append("down"))
    assert preflight.teardown_cluster() == 0
    assert torn == ["down"]


def test_teardown_cluster_reports_a_failure(monkeypatch):
    monkeypatch.setattr(preflight._sp, "cluster_has_containers", lambda d, rel=None: True)
    monkeypatch.setattr(preflight._sp, "cluster_down",
                        lambda d, rel=None: (_ for _ in ()).throw(RuntimeError("in use")))
    assert preflight.teardown_cluster() == 1


def test_teardown_cluster_reports_a_docker_that_cannot_answer(monkeypatch):
    # "Docker did not answer" is not "the cluster is not ours". Collapsing the two let
    # `stop live` announce it had found a native Striim and exit 0 while the containers it
    # should have removed were merely unreachable (daemon stopped, still starting).
    monkeypatch.setattr(preflight._sp, "cluster_has_containers",
                        lambda d, rel=None: (_ for _ in ()).throw(RuntimeError("daemon down")))
    monkeypatch.setattr(preflight._sp, "cluster_down",
                        lambda *a, **k: pytest.fail("tore down on an unanswered ownership check"))
    assert preflight.teardown_cluster() == 1


def test_a_broken_striim_home_is_an_rc_not_a_traceback(monkeypatch):
    # _resolve_release raises on an ambiguous/absent Platform-*.jar. Outside the try that was
    # an unhandled traceback out of teardown_cluster -- which also killed cmd_down's remaining
    # halves, contradicting "every half runs, whatever the others returned".
    monkeypatch.setattr(preflight, "_resolve_release",
                        lambda cfg: (_ for _ in ()).throw(RuntimeError("ambiguous install")))
    assert preflight.teardown_cluster() == 1


def test_end_of_run_teardown_guards_the_cluster_too(monkeypatch):
    # `down -v` removes the volumes compose.yaml DECLARES whether or not this project owns a
    # container, so the console's end-of-run cleanup against a native Striim would delete the
    # slt-striim-shared MDR volume. One invariant, both teardown paths.
    monkeypatch.setattr(preflight, "manifests_for",
                        lambda ids: [SimpleNamespace(requires=[], modules=[])])
    monkeypatch.setattr(preflight, "_take_down_services", lambda svcs, env, apply_gate=True: [])
    monkeypatch.setattr(preflight._sp, "cluster_has_containers", lambda d, rel=None: False)
    monkeypatch.setattr(preflight._sp, "cluster_down",
                        lambda *a, **k: pytest.fail("tore down a cluster we do not own"))

    assert preflight.teardown(["some-test"], env={}) == 0


# --- the renames this branch made must not create invisible orphans ------------------------

def test_a_leftover_under_the_old_container_name_is_caught(monkeypatch):
    # This branch renamed slt-gcs-token -> slt-token. The survivor check reads TODAY's compose
    # file, so the old name is invisible to it and to `compose down` alike -- and it still
    # publishes 4444, so the next `start live gcs` dies on "port is already allocated" after
    # the user has already run the docker rm -f we printed for slt-gcs.
    monkeypatch.setattr(preflight, "load_service", lambda n: _fake_defn(n))
    monkeypatch.setattr(preflight, "compose_down", lambda defn: None)
    monkeypatch.setattr(preflight, "forget_provisioned", lambda keys, d=None: None)
    monkeypatch.setattr(preflight, "orphaned_containers", lambda paths, env=None: ["slt-gcs-token"])

    assert preflight.teardown_services(["gcs"], env={}) == 1


def test_a_pre_rename_cluster_is_not_reported_as_a_native_striim(monkeypatch):
    # The cluster's compose project was renamed too (`striim` -> `slt-striim`), so a cluster
    # started before this branch is invisible to `ps -aq` -- and the ownership guard then
    # announces a native Striim over three running containers and the MDR volume.
    monkeypatch.setattr(preflight._sp, "cluster_has_containers", lambda d, rel=None: False)
    monkeypatch.setattr(preflight._sp, "cluster_down",
                        lambda *a, **k: pytest.fail("tore down a project we could not see"))
    monkeypatch.setattr(preflight, "orphaned_containers",
                        lambda paths, env=None: ["slt-striim-node"])

    assert preflight.teardown_cluster() == 1


def test_no_cluster_containers_at_all_is_still_a_clean_no_op(monkeypatch):
    monkeypatch.setattr(preflight._sp, "cluster_has_containers", lambda d, rel=None: False)
    monkeypatch.setattr(preflight._sp, "cluster_down",
                        lambda *a, **k: pytest.fail("tore down a cluster we do not own"))
    assert preflight.teardown_cluster() == 0


# --- the gate applies to DERIVED lists, never to a named service ---------------------------

def test_a_derived_list_still_honours_the_opt_in_gate(monkeypatch):
    # `cli start all` expands to every registered service; without the gate that hands the
    # operator the spanner/gcs/kafka emulators they declined, and contradicts what
    # a runner's `start live` does through its default service targets.
    seen = {}
    monkeypatch.setattr(preflight, "_prune_dead_records", lambda names: None)
    monkeypatch.setattr(preflight, "_bring_up_services",
                        lambda names, env, apply_gate=True, infra=None: seen.update(gate=apply_gate) or {})

    preflight.provision_services(["spanner"], env={}, apply_gate=True)
    assert seen["gate"] is True


def test_an_explicit_stop_of_a_test_id_ignores_the_gate(monkeypatch):
    # SLT_GCS=1 start brings gcs up; a later `stop <gcs-test-id>` from a shell without the
    # flag must still remove it. The gate answers "should this come up?", never "is it
    # running?" -- the rule this branch states for a runner's default service targets.
    seen = {}
    monkeypatch.setattr(preflight, "manifests_for",
                        lambda ids: [SimpleNamespace(requires=["gcs"], modules=[])])
    monkeypatch.setattr(preflight, "_take_down_services",
                        lambda svcs, env, apply_gate=True: seen.update(gate=apply_gate) or [])

    assert preflight.teardown(["some-gcs-test"], env={}, explicit=True) == 0
    assert seen["gate"] is False


# --- the one ownership check, exercised directly -------------------------------------------

def test_declared_containers_reads_every_compose_file_given(tmp_path):
    # The cluster passes two files; a service passes one. Same function, same answer shape --
    # this is what replaced three separately-patched copies of the question.
    (tmp_path / "a.yaml").write_text("services:\n  x:\n    container_name: slt-striim\n"
                                     "  y:\n    container_name: slt-striim-node\n")
    (tmp_path / "b.yaml").write_text("services:\n  z:\n    container_name: slt-striim-agent\n")
    assert services.declared_containers([tmp_path / "a.yaml", tmp_path / "b.yaml"]) == [
        "slt-striim", "slt-striim-node", "slt-striim-agent"]


def test_declared_containers_includes_former_names(tmp_path, monkeypatch):
    # A rename is invisible to `compose down` (the old container is in no project today's file
    # names) AND to a check that reads only today's file -- so the old name is carried here.
    (tmp_path / "c.yaml").write_text("services:\n  t:\n    container_name: slt-token\n")
    monkeypatch.setattr(services, "_RENAMED_CONTAINERS", {"slt-token": ["slt-gcs-token"]})
    assert services.declared_containers([tmp_path / "c.yaml"]) == ["slt-token", "slt-gcs-token"]


def test_declared_containers_skips_unreadable_files(tmp_path):
    (tmp_path / "d.yaml").write_text("services:\n  x:\n    container_name: slt-postgres\n")
    assert services.declared_containers([tmp_path / "nope.yaml", tmp_path / "d.yaml"]) == \
        ["slt-postgres"]


def test_declared_containers_carries_the_stack_prefix(tmp_path, monkeypatch):
    (tmp_path / "e.yaml").write_text(
        "services:\n  x:\n    container_name: ${SLT_STACK_PREFIX:+${SLT_STACK_PREFIX}-}slt-gcs\n")
    monkeypatch.setenv("SLT_STACK_PREFIX", "alt")
    assert services.declared_containers([tmp_path / "e.yaml"]) == ["alt-slt-gcs"]


def test_the_cluster_and_a_service_ask_the_same_question(monkeypatch, tmp_path):
    # The consolidation itself: both call sites route through orphaned_containers, so a fix to
    # the question is a fix for both. Patching it alone is enough to drive each path.
    asked = []
    monkeypatch.setattr(preflight, "orphaned_containers",
                        lambda paths, env=None: asked.append([str(p) for p in paths]) or [])
    monkeypatch.setattr(preflight, "load_service", lambda n: _fake_defn(n))
    monkeypatch.setattr(preflight, "compose_down", lambda defn: None)
    monkeypatch.setattr(preflight, "forget_provisioned", lambda keys, d=None: None)
    monkeypatch.setattr(preflight._sp, "cluster_has_containers", lambda d, rel=None: False)

    preflight.teardown_services(["postgres"], env={})
    preflight.teardown_cluster()

    assert asked[0] == ["/svc/postgres/compose.yaml"]
    assert [p.rsplit("/", 1)[-1] for p in asked[1]] == \
        ["compose.yaml", "compose.spanner-emulator.yaml"]


def test_a_failed_gcs_lookup_does_not_abort_the_whole_preflight(monkeypatch):
    # The call sits ahead of the service bring-up AND the OP/UDF build, and _gcs_endpoint_ip
    # RAISES when `docker exec getent` comes back empty -- so unguarded it took a console
    # fan-out batch down with zero services provisioned and zero jars built, when the cost of
    # the failure is only that gcs uses the compose default. provision_cluster already
    # guarded its byte-identical call.
    monkeypatch.setenv("SLT_GCS", "1")
    monkeypatch.setattr(preflight, "manifests_for",
                        lambda ids: [SimpleNamespace(requires=["gcs"], modules=[])])
    monkeypatch.setattr(preflight, "opregistry", SimpleNamespace(clear=lambda t=None: None))
    monkeypatch.setattr(preflight, "clear_provision_registry", lambda t: None)
    monkeypatch.setattr(preflight, "_resolve_striim",
                        lambda cfg: SimpleNamespace(mode="docker", url="http://x:9080",
                                                    view_host="h", user="u", password="p"))
    monkeypatch.setattr(preflight, "_set_gcs_public_host",
                        lambda ctx: (_ for _ in ()).throw(RuntimeError("getent found nothing")))
    brought_up = []
    monkeypatch.setattr(preflight, "_bring_up_services",
                        lambda names, env, apply_gate=True, infra=None, **kw: brought_up.extend(names) or {})

    assert preflight.provision(["some-gcs-test"], env={"SLT_GCS": "1"}) == 0
    assert brought_up == ["gcs"]        # the bring-up still happened


def test_gcs_public_host_wanted_follows_the_same_rule_as_provision(monkeypatch):
    # GCS is ungated by default, so gcs_public_host_wanted is True for gcs
    assert preflight.gcs_public_host_wanted(["gcs"], apply_gate=False, env={}) is True
    assert preflight.gcs_public_host_wanted(["gcs"], apply_gate=True, env={}) is True
    assert preflight.gcs_public_host_wanted(["postgres"], apply_gate=False, env={}) is False


# --- the last review round ------------------------------------------------------------------

def test_the_gcs_public_host_uses_the_published_host_port(monkeypatch):
    # compose publishes ${SLT_GCS_HOST_PORT:-4443}:4443 and derives its OWN default
    # -public-host from the same var, so hardcoding 4443 while overriding that default hands
    # the writer resumable-upload URLs on a dead port.
    monkeypatch.setenv("SLT_GCS_HOST_PORT", "4553")
    monkeypatch.setattr(preflight, "_resolve_striim",
                        lambda cfg: SimpleNamespace(mode="native", url="http://x:9080", view_host="h"))

    assert preflight.provision_cluster(publish_gcs_host=True) == 0
    assert os.environ["SLT_GCS_PUBLIC_HOST"] == "127.0.0.1:4553"


def test_end_of_run_teardown_reports_its_failures(monkeypatch):
    # It used to discard `failed` and swallow the cluster exception, returning 0 -- so the
    # console's post-flight job showed PASSED over an entirely stranded stack.
    monkeypatch.setattr(preflight, "manifests_for",
                        lambda ids: [SimpleNamespace(requires=["postgres"], modules=[])])
    monkeypatch.setattr(preflight, "_take_down_services",
                        lambda svcs, env, apply_gate=True: ["postgres"])
    monkeypatch.setattr(preflight._sp, "cluster_has_containers", lambda d, rel=None: False)

    assert preflight.teardown(["some-test"], env={}) == 1


def test_end_of_run_teardown_reports_a_failed_cluster_down(monkeypatch):
    monkeypatch.setattr(preflight, "manifests_for",
                        lambda ids: [SimpleNamespace(requires=[], modules=[])])
    monkeypatch.setattr(preflight, "_take_down_services", lambda svcs, env, apply_gate=True: [])
    monkeypatch.setattr(preflight._sp, "cluster_has_containers", lambda d, rel=None: True)
    monkeypatch.setattr(preflight._sp, "cluster_down",
                        lambda d, rel=None: (_ for _ in ()).throw(RuntimeError("in use")))

    assert preflight.teardown(["some-test"], env={}) == 1


def test_a_malformed_service_yaml_does_not_escape_the_gate_check(monkeypatch):
    # `_gated_out` loads service.yaml too. Outside the try it raised straight out of the loop,
    # abandoning every remaining service AND the cluster -- what the loop exists to prevent.
    down = []

    def _load(name):
        if name == "postgres":
            raise RegistryError("postgres/service.yaml: 'isolation' is required")
        return _fake_defn(name)

    monkeypatch.setattr(preflight, "load_service", _load)
    monkeypatch.setattr(preflight, "compose_down", lambda defn: down.append(defn.container))
    monkeypatch.setattr(preflight, "forget_provisioned", lambda keys, d=None: None)

    assert preflight._take_down_services(["postgres", "oracle"], {}, apply_gate=True) == ["postgres"]
    assert down == ["slt-oracle"]


def test_a_volume_of_the_same_name_is_not_a_surviving_container(monkeypatch):
    # Bare `docker inspect` resolves volumes and networks too, and both carry `.Name` -- so a
    # VOLUME named slt-postgres would fail the teardown with a `docker rm -f` that cannot
    # clear it. `docker container inspect` is the whole fix.
    seen = []
    services.container_exists("slt-postgres",
                              run=lambda argv: seen.append(argv) or SimpleNamespace(returncode=1))
    assert seen[0][:3] == ["docker", "container", "inspect"]


def test_end_of_run_teardown_shares_the_clusters_orphan_check(monkeypatch):
    # It used to INLINE teardown_cluster's ownership guard without the orphan check beside it,
    # so a cluster left under the pre-rename project read as "nothing of ours" and the
    # console's post-flight reported success over three running containers and the MDR volume
    # -- while the comment above the copy claimed one invariant for both paths.
    monkeypatch.setattr(preflight, "manifests_for",
                        lambda ids: [SimpleNamespace(requires=[], modules=[])])
    monkeypatch.setattr(preflight, "_take_down_services", lambda svcs, env, apply_gate=True: [])
    monkeypatch.setattr(preflight._sp, "cluster_has_containers", lambda d, rel=None: False)
    monkeypatch.setattr(preflight, "orphaned_containers",
                        lambda paths, env=None: ["slt-striim-node"])

    assert preflight.teardown(["some-test"], env={}) == 1


def test_the_native_striim_advice_names_the_published_port(monkeypatch):
    # Advice naming a dead port is worse than none: compose publishes
    # ${SLT_GCS_HOST_PORT:-4443}, the same var _set_gcs_public_host was fixed to read.
    monkeypatch.setenv("SLT_GCS_HOST_PORT", "4553")
    monkeypatch.setattr(preflight, "_prune_dead_records", lambda names: None)
    monkeypatch.setattr(preflight, "_bring_up_services", lambda names, env, apply_gate=True, infra=None, **kw: {})
    logged = []
    monkeypatch.setattr(preflight, "_log", lambda m: logged.append(m))

    preflight.provision_services(["gcs"], env={})
    assert any("127.0.0.1:4553" in m for m in logged), logged


def test_an_explicitly_undeclared_env_is_still_refused(monkeypatch):
    # The autouse fixture fills only what the caller left absent, so it never masks the C7.1 refusal.
    calls = []
    monkeypatch.setattr(preflight, "_prune_dead_records", lambda names: calls.append("prune"))
    monkeypatch.setattr(preflight, "_bring_up_services",
                        lambda names, env, apply_gate=True, infra=None: calls.append("up") or {})
    logged = []
    monkeypatch.setattr(preflight, "_log", lambda m: logged.append(m))

    assert preflight.provision_services(["postgres"], env={"SLT_INFRA_OWNERSHIP": ""}) == 2
    assert calls == []
    assert any("declare infrastructure ownership" in m for m in logged), logged


def test_explicit_recovery_context_only_scopes_service_preflight(monkeypatch, tmp_path):
    from livetest import service_healing as h
    ctx = h.RecoveryContext(tmp_path, 'run', 'op', 'lane')
    seen = []
    monkeypatch.setattr(preflight, 'manifests_for', lambda ids: [SimpleNamespace(requires=['kafka'], modules=[])])
    monkeypatch.setattr(preflight, 'clear_provision_registry', lambda *a: None)
    monkeypatch.setattr(preflight, '_resolve_release', lambda cfg: {})
    monkeypatch.setattr(preflight, '_resolve_striim', lambda cfg: SimpleNamespace(mode='native'))
    monkeypatch.setattr(preflight, '_bring_up_services',
                        lambda *a, **kw: seen.append(h.current_context()) or {})
    assert preflight.provision(['selected'], env={}, recovery_context=ctx) == 0
    assert seen == [ctx]
    assert h.current_context() is None


def test_kept_worker_has_no_implicit_healing_authority(monkeypatch):
    from livetest import service_healing as h
    monkeypatch.setenv('SLT_KEEP_SERVICES', '1')
    monkeypatch.setenv('SLT_PARALLEL', '1')
    assert h.current_context() is None


@pytest.mark.parametrize('mode', ['native', 'docker'])
def test_selected_preflight_rejects_strict_topology_before_any_hook(monkeypatch, mode):
    from livetest import prestart
    from livetest.topology import Topology
    manifest = SimpleNamespace(requires=['extdb'], modules=[], topology='cluster')
    monkeypatch.setattr(preflight, 'manifests_for', lambda ids: [manifest])
    monkeypatch.setattr(preflight, 'clear_provision_registry', lambda *a: None)
    monkeypatch.setattr(preflight, '_resolve_release', lambda cfg: {})
    monkeypatch.setattr(preflight, '_resolve_striim', lambda cfg: SimpleNamespace(mode=mode, topology=Topology()))
    defn = _fake_defn('extdb')
    defn.unavailable_policy = 'fail'
    monkeypatch.setattr(preflight, 'load_service', lambda name: defn)
    monkeypatch.setattr(prestart, 'maybe_run', lambda *a, **kw: pytest.fail('hook ran before eligibility'))
    assert preflight.provision(['selected'], env={}) == 1


@pytest.mark.parametrize('explicit', [None, 'my-files'])
def test_selected_preflight_passes_the_driver_compose_env_to_compose(monkeypatch, explicit):
    from livetest import drivers, prestart
    from livetest.topology import Topology
    manifest = SimpleNamespace(requires=['extdb'], modules=[], topology='cluster')
    monkeypatch.setattr(preflight, 'manifests_for', lambda ids: [manifest])
    monkeypatch.setattr(preflight, 'clear_provision_registry', lambda *a: None)
    monkeypatch.setattr(preflight, '_resolve_release', lambda cfg: {})
    monkeypatch.setattr(preflight, '_resolve_striim', lambda cfg: SimpleNamespace(mode='docker', topology=Topology(has_cluster=True, has_agent=True)))
    defn = _fake_defn('extdb')
    defn.unavailable_policy = 'fail'
    monkeypatch.setattr(preflight, 'load_service', lambda name: defn)
    monkeypatch.setattr(prestart, 'maybe_run', lambda *a, **kw: False)
    from livetest import registry
    monkeypatch.setattr(registry, 'unavailable', lambda *a: None)
    fake = SimpleNamespace(unavailable=lambda *a: None,
                           compose_env=lambda env: {'SLT_EXTDB_FILES': env.get('SLT_EXTDB_FILES') or 'extdb-files-5.4.2'})
    monkeypatch.setattr(drivers, 'load', lambda d: fake if getattr(d, 'name', None) == 'extdb' else None)
    monkeypatch.setenv('SLT_EXTDB_FILES', 'remove-for-test')
    monkeypatch.delenv('SLT_EXTDB_FILES')
    captured = {}
    def resolve(infra, svc, env, *a, **kw):
        captured.update(env=env.get('SLT_EXTDB_FILES'), compose=services._compose_env().get('SLT_EXTDB_FILES'))
        return SimpleNamespace(base={})
    monkeypatch.setattr(preflight._slt_infra, 'resolve_service', resolve)
    env = {'SLT_EXTDB_FILES': explicit} if explicit else {}
    assert preflight.provision(['selected'], env=env) == 0
    assert captured == dict(env=explicit or 'extdb-files-5.4.2', compose=explicit or 'extdb-files-5.4.2')
