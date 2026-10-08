"""Console fan-out pre-flight / post-flight driver (spec §D.3, §B.4).

The console runs live tests in parallel as N independent single-test ``pytest`` subprocesses
(``SLT_PARALLEL=1 SLT_OPS_PRELOADED=1 SLT_KEEP_SERVICES=1``). Shared, cluster-global setup must
therefore happen ONCE, before fan-out — not per subprocess — and be torn down once after. This
module is that once-only step, invoked as a subprocess by the console:

    python -m livetest.preflight --tests <id> [<id> ...]     # provision (default)
    python -m livetest.preflight --tests <id> [...] --teardown
    python -m livetest.preflight --restart-app-nodes         # poison recovery (spec §D.5)

Provision computes the UNION of services + OP/UDF modules the selected tests need, then:
  1. clears the per-run coordination registries (fresh run),
  2. provisions/reuses the Striim cluster,
  3. provisions each required service container once,
  4. runs cold-DB ``ensure_setup`` for postgres/mssql once (removes the two-subprocess race on
     role-create / ``sp_cdc_enable_db``),
  5. builds + uploads + registers the OP/UDF union once.

It reuses the pytest plugin's own provisioning path verbatim by passing a minimal stand-in
``config`` (``_resolve_striim``/``_resolve_release`` only touch ``config.pluginmanager`` — which
degrades to no-op progress here — plus attributes they cache on it), so the validated pytest code
path is untouched. Steps 3–5 write the SAME coordination registries the subprocesses read
(``ensure_provisioned_once`` keys, ``opregistry``), so each subprocess finds the work already done
and skips it. This driver MUST run with ``SLT_PARALLEL=1`` in its env (it is the parallel
coordinator); ``services.resolve`` and the DB setup only route through the shared registries then.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import uuid
import sys
import subprocess
from pathlib import Path

from livetest import lockdir, opartifacts, opregistry, stack
from livetest import infra as _slt_infra   # the shared-service adapter, C7.1
from livetest import striim_provision as _sp
from livetest.manifest import load_manifest
from livetest.mssqladmin import MssqlAdmin
from livetest.pgclient import PgAdmin
from livetest.teradataadmin import TeradataAdmin
from livetest.verticaadmin import VerticaAdmin
from livetest.plugin import (
    _EXTRA_LIVE_CASES, _LIVE_CASES, _STATE_DIR, _STRIIM_DIR, _gcs_endpoint_ip, _loaded_jar_probe, _op_jar_fingerprint,
    _module_build_lock, _register_op_jar, _registry_key, _resolve_release, _resolve_striim,
)
from livetest.registry import RegistryError, load_service, unavailable as registry_unavailable
from livetest import prestart as _prestart
from livetest.services import (
    clear_provision_registry, compose_down, container_exists, container_running,
    ensure_provisioned_once, forget_provisioned, orphaned_containers, resolve,
    service_compose_path, ServiceError, ProvisionRefused, DockerUnavailable,
)
from livetest.striim import StriimClient

_CASES = _LIVE_CASES
_EXTRA_CASES = _EXTRA_LIVE_CASES


def _log(msg: str) -> None:
    # One newline-terminated line per update — the console job runner reads stdout via
    # readline(), so a bare progress spinner would block it (mirrors plugin._make_progress).
    print(f"[preflight] {msg}", flush=True)


class _PreflightConfig:
    """Minimal stand-in for a pytest ``Config`` so ``plugin._resolve_striim`` /
    ``_resolve_release`` run verbatim outside pytest. ``pluginmanager.getplugin()`` returns None,
    so ``_make_progress`` degrades to no-op reporters (progress goes to :func:`_log` instead).
    The reused functions cache their results on attributes of this object (``_slt_striim`` etc.)."""

    class _PM:
        def getplugin(self, name):   # noqa: D401 - stub
            return None

    def __init__(self):
        self.pluginmanager = self._PM()


# ---------------------------------------------------------------------------------------------
# Union computation (pure — unit-tested without Docker)
# ---------------------------------------------------------------------------------------------
def _case_manifests() -> list:
    """Every test.yaml under every live case root (SLT_LIVE_CASES may list several)."""
    return [path for root in [_CASES, *_EXTRA_CASES] if root.exists()
            for path in sorted(root.rglob("test.yaml"))]


def known_test_ids() -> set:
    """Every manifest ``name`` on disk. A membership probe for callers validating a target
    before provisioning -- ``manifests_for`` would log a WARNING for the miss, which is right
    for the console (a stale checkout) and wrong for a plain "is this a test id?" question."""
    names = set()
    for path in _case_manifests():
        try:
            names.add(load_manifest(path).name)
        except Exception as e:
            # As manifests_for: skipped, never silently.
            _log(f"WARNING: skipped unloadable manifest {path}: {e}")
            continue
    return names


def manifests_for(test_ids) -> list:
    """Load the manifests for ``test_ids`` (matched on manifest ``name`` == pytest -k id). Unknown
    ids are reported and skipped, not fatal — the console selection can outrun a stale checkout."""
    wanted = set(test_ids)
    found = {}
    for path in _case_manifests():
        try:
            m = load_manifest(path)
        except Exception as e:
            # A malformed sibling manifest must not sink the whole pre-flight, but say so: if
            # it is one of the selected tests, its OP/UDF and services are silently omitted.
            _log(f"WARNING: skipped unloadable manifest {path}: {e}")
            continue
        if m.name in wanted:
            found[m.name] = m
    missing = wanted - set(found)
    if missing:
        _log(f"WARNING: {len(missing)} selected test-id(s) not found on disk: "
             f"{', '.join(sorted(missing))}")
    # Preserve the caller's order where possible (deterministic logs).
    return [found[i] for i in test_ids if i in found]


def compute_unions(manifests) -> tuple[list[str], list[dict]]:
    """(service names, op/udf modules) unions across ``manifests``. Services are de-duplicated
    preserving first-seen order; OP/UDF modules are de-duplicated by ``module_name(jar)`` (the
    same key the build cache uses), each carrying its ``jar`` ref + ``kind`` ("op"/"udf")."""
    services: list[str] = []
    seen_svc = set()
    modules: list[dict] = []
    seen_mod = set()
    for m in manifests:
        for svc in m.requires:
            if svc not in seen_svc:
                seen_svc.add(svc)
                services.append(svc)
        for mod in m.modules:
            key = opartifacts.module_name(mod["jar"])
            if key not in seen_mod:
                seen_mod.add(key)
                modules.append(mod)
    return services, modules


_PORT_VAR_RE = re.compile(r"\$\{(SLT_[A-Z0-9_]*(?:PORT|HOST_PORT))(?::-([0-9]+))?\}")


def missing_port_overrides(services, env=None) -> list:
    """Host-port variables a PREFIXED stack leaves at their default, which is a collision.

    A published host port is global to the daemon: `ec-slt-zookeeper` and the default stack's
    `slt-zookeeper` cannot both bind 2181. Every such port therefore needs an override in the
    stack's env, and `.env` carries one per port -- but nothing checked, so a variable that was
    added to `.env` after the shell had been started, or added to a compose file and never to
    `.env`, surfaced as `Bind for 0.0.0.0:2181 failed: port is already allocated` from the
    daemon, naming a container rather than the variable to set. Observed 2026-09-23 for
    SLT_ZOOKEEPER_CLIENT_PORT, whose sibling kafka ports WERE set in the same environment --
    the asymmetry that says "stale shell", and exactly what this returns.

    Only meaningful with a stack prefix: an unprefixed stack owns the defaults.
    """
    env = os.environ if env is None else env
    if not (env.get("SLT_STACK_PREFIX") or "").strip():
        return []
    missing = []
    for svc in sorted(set(services)):
        try:
            path = service_compose_path(load_service(svc))
        except Exception:
            continue
        if not path or not pathlib.Path(path).is_file():
            continue
        text = pathlib.Path(path).read_text()
        for var, default in _PORT_VAR_RE.findall(text):
            if default and not (env.get(var) or "").strip():
                missing.append((svc, var, default))
    return missing


def _sweep_postgres_slots(admin, infra) -> None:
    if not isinstance(infra, _slt_infra.Infra):
        _log("postgres: skipping replication slot sweep: endpoint ownership is unavailable")
        return
    registry = _slt_infra._registry(infra.lock_dir, infra.port)
    try:
        others = [pid for pid in _slt_infra._live_shared_markers(
            infra.lock_dir, infra.port, _slt_infra._pid_alive) if pid != os.getpid()]
        if others:
            _log(f"postgres: skipping replication slot sweep: other live endpoint members {others}")
            return
        owner = _slt_infra._read_owner(Path(str(_slt_infra._exclusive_lease_path(
            infra.lock_dir, infra.port)) + ".owner")) or {}
        if _slt_infra._exclusive_lease_held(infra.lock_dir, infra.port) and owner.get("runId") != infra.run_id:
            _log("postgres: skipping replication slot sweep: another run holds the exclusive endpoint lease")
            return
        # Keep registration serialized through the sweep, so a new member cannot join midway.
        admin.sweep_stale_replication_slots(_log)
    finally:
        registry.release()


def _bring_up_services(services, env, apply_gate: bool = True, infra=None,
                       pre_up: bool | None = None, mode="docker", *, outcomes=None,
                       cleanup_services=None, failures=None) -> dict:
    """Provision each service once and run the cold-DB setup, returning {name: ResolvedService}.

    ensure_provisioned_once (engaged because SLT_PARALLEL is set) records each container so the
    subprocesses skip the bring-up, and the postgres/mssql setup runs under the SAME
    once-across-processes keys they use, so they find it done and skip it too (spec §C.7 --
    removes the two-subprocess race on role-create / sp_cdc_enable_db).

    `apply_gate` is on when the service list was DERIVED (a manifest union, or `all`) and off
    when a caller named the services explicitly -- `start spanner` means start spanner, not
    "start spanner if SLT_SPANNER happens to be set".

    A service whose `required_files` (service.yaml) are missing is left out with the reason, as
    its tests skip: bringing it up would only fail inside compose.

    `pre_up` says whether a service's `pre_up` hook (livetest.prestart) may run here, before
    that check (only when something asked for the service). It defaults to
    "named explicitly": `start <name>` runs it, a derived `start all` does not. `provision(test_ids)`
    passes True, because a selected test requires the service.
    """
    from livetest import paths
    env = paths.effective_env(env)
    if pre_up is None:
        pre_up = not apply_gate
    for svc, var, default in missing_port_overrides(services, env):
        _log(f"WARNING service {svc}: {var} is unset, so its host port defaults to {default} -- "
             f"which the unprefixed stack also publishes. Set {var} (see .env) or this stack will "
             f"fail to bind. If .env already sets it, your shell predates that line: re-source it.")
    started: set = set()
    resolved: dict = {}
    from livetest.service_healing import Refused, current_context, _redact
    from filelock import Timeout
    admins = {"postgres": PgAdmin, "mssql": MssqlAdmin,
              "teradata": TeradataAdmin, "vertica": VerticaAdmin}
    for svc in services:
        if outcomes is not None and outcomes.get(svc, {}).get("status") == "unavailable":
            continue
        try:
            if apply_gate and _gated_out(svc, env):
                why = "opt-in gate not set"
            else:
                defn = load_service(svc)
                why = _prestart.prepare_selected(defn, env, mode, allow_hook=pre_up, log=_log)
            if why:
                _log(f"service {svc}: {why} — skipping (its tests will skip too)")
                if outcomes is not None:
                    outcomes[svc] = {"status": "unavailable", "reason": _redact(why)}
                continue
            _log(f"provisioning service {svc}")
            recovery = current_context()
            lock_options = {}
            if recovery:
                recovery.check()
                recovery.log(f"preflight recovery: waiting for shared provisioning lock ({svc})")
                lock_options["lock_timeout"] = recovery.timeout()
            # Compose can leave containers behind even when readiness fails.
            if cleanup_services is not None:
                cleanup_services.append(svc)
            resolved[svc] = _slt_infra.resolve_service(
                infra, svc, env, started, resolve,
                progress=lambda label, phase: _log(f"{label}: {phase}"), **lock_options)
            if svc in admins:
                _log(f"{svc}: ensure_setup, once")
                admin = admins[svc](resolved[svc].base, role="source")
                ensure_provisioned_once(f"{svc}-setup", admin.ensure_setup)
                if svc == "postgres":
                    _sweep_postgres_slots(admin, infra)
        except (_slt_infra.InfraOwnershipError, Refused, Timeout, ProvisionRefused, DockerUnavailable):
            raise
        except Exception as e:
            # Cancellation may have been wrapped by a provisioning adapter.
            cause = e
            while cause is not None:
                if not isinstance(cause, Exception):
                    raise
                if isinstance(cause, (_slt_infra.InfraOwnershipError, Refused, Timeout, ProvisionRefused, DockerUnavailable)):
                    raise
                cause = cause.__cause__ or cause.__context__
            if outcomes is None:
                raise
            resolved.pop(svc, None)
            if failures is not None:
                failures.add(svc)
            outcomes[svc] = {"status": "unavailable", "reason": _redact(e) or type(e).__name__}
            _log(f"ERROR service {svc}: {outcomes[svc]['reason']}")
            continue
        if outcomes is not None:
            outcomes[svc] = {"status": "ready", "reason": ""}
    return resolved


def _take_down_services(services, env, apply_gate: bool = True) -> list:
    """Compose-down each service, best-effort (a failure must not strand the rest), and
    return the names that failed.

    Forgetting the provision-registry keys is what keeps a torn-down container from
    outliving its record: `ensure_provisioned_once` skips a bring-up whose key is already
    present, so leaving the record behind makes the NEXT start a silent no-op. `provision`
    is self-healing (it clears the whole registry first); an explicit stop is not.
    """
    failed = []
    for svc in services:
        try:
            # Inside the try: `_gated_out` loads service.yaml too, so a malformed one raised
            # straight out of the loop and abandoned every remaining service AND the cluster
            # -- the one thing this best-effort loop exists to prevent.
            if apply_gate and _gated_out(svc, env):
                continue
            defn = load_service(svc)
            if not getattr(defn, "compose", None):
                continue
            _log(f"tearing down service {svc}")
            compose_down(defn)
            survivors = _surviving_containers(defn)
            # The record goes either way, and BEFORE the raise: a survivor is not ours (it
            # belongs to another project), so keeping its record would make the next start
            # find the key, skip `docker compose up`, and report success -- turning Docker's
            # loud "container name already in use" into a silent no-op, which is the exact
            # bug this whole path exists to kill.
            forget_provisioned([defn.container, f"{svc}-setup"], _STATE_DIR)
            if survivors:
                # `down` is project-scoped, so containers of the same name left behind by a
                # DIFFERENT compose project survive it and it still exits 0. That happens for
                # real: the projects were renamed (`postgres` -> `slt-postgres`), so containers
                # from before that rename are invisible to today's `down`.
                raise ServiceError(
                    f"{', '.join(survivors)} still present after `compose down` — "
                    f"belongs to another compose project (likely from before the project "
                    f"rename). Remove it once with: docker rm -f {' '.join(survivors)}")
        except Exception as e:
            _log(f"WARNING: failed to tear down service {svc!r}: {e!r}")
            failed.append(svc)
    return failed


def _surviving_containers(defn) -> list:
    """The service's containers that still exist after its `compose down`.

    The service half of `services.orphaned_containers`; the cluster half calls the same
    function with `_sp.compose_files(...)`. Falls back to service.yaml's primary only when the
    compose file names nothing we could read."""
    path = service_compose_path(defn)
    return (orphaned_containers([path]) if path else
            [c for c in [defn.container] if c and container_exists(c)])


def gcs_public_host_wanted(services, apply_gate: bool, env=None) -> bool:
    """Whether a cluster bring-up should resolve the GCS `-public-host` for `services`.

    The same test :func:`provision` applies: gcs must be in the list AND, when that list was
    DERIVED, not gated out. Without the gate half, `cli start all` with SLT_GCS unset shells
    out `docker exec … getent` and mutates process-wide SLT_GCS_PUBLIC_HOST/
    SLT_STRIIM_VIEW_HOST for an emulator the bring-up then skips -- and in a mixed invocation
    that mutated environment carries on into the test-id pre-flight."""
    env = os.environ if env is None else env
    return "gcs" in services and not (apply_gate and _gated_out("gcs", env))


def _surviving_cluster_containers() -> list:
    """Cluster containers that exist under their own names while the compose project is empty.

    The cluster half of `services.orphaned_containers` -- the same question the service
    teardown asks, against the cluster's own compose files. The project was renamed on this
    branch (`striim` -> `slt-striim`), so a cluster started before it belongs to a project
    today's `ps -aq` never looks at; without this the ownership guard reads that as "no
    containers of ours" and reports a native Striim over three running containers and the
    MDR volume."""
    return orphaned_containers(_sp.compose_files(_STRIIM_DIR))


def _gated_out(svc: str, env) -> bool:
    """A service whose opt-in gate (service.yaml ``opt_in_env``) is set neither directly nor via
    ``SLT_EMULATORS`` is skipped — the subprocesses requiring it will ``pytest.skip`` too, so
    provisioning it would waste a container no test uses (mirrors plugin.py's runtest gate)."""
    gate = load_service(svc).opt_in_env
    return bool(gate) and not (env.get(gate) or env.get("SLT_EMULATORS"))


# ---------------------------------------------------------------------------------------------
# Provision / teardown / restart
# ---------------------------------------------------------------------------------------------
def _replaced_loaded_jars(ctx, client, builts) -> list[str]:
    """Raw-byte replacements of loaded UDF jars on Docker app nodes, before any upload changes
    the evidence. UDFs LOAD from UploadedFiles, so their file alone is insufficient: confirm
    LIST LIBRARIES as well (an unavailable probe is treated conservatively). Native/remote
    servers have no local app containers this driver can restart.

    OP jars are not checked: they are content-named and never overwritten on the cluster
    (opartifacts.content_addressed), so a name cannot be replaced. A byte difference under
    one name is a timestamp-only rebuild that is never uploaded, and treating it as a
    replacement would restart the app nodes on every pre-flight without ever converging.
    """
    if ctx.mode != "docker":
        return []
    replaced = []
    loaded = {}
    for mod, built in builts:
        if mod["kind"] != "udf":
            continue
        want = opartifacts.file_sha256(built.path)
        for node in stack.app_nodes():
            got = subprocess.run(["docker", "exec", node, "sha256sum",
                                  f"/opt/striim/UploadedFiles/{built.name}"],
                                 capture_output=True, text=True)
            words = (got.stdout or "").split()
            if got.returncode != 0 or not words or words[0] == want:
                continue
            if _loaded_jar_probe(client, built.name, cache=loaded)() is not False:
                replaced.append(built.name)
                break
    return replaced


_RESTART_COMPLETED = "COMPLETED\n"


def _replacement_restart_pending(marker) -> bool:
    # Only the complete sentinel acknowledges success; empty/truncated writes stay pending.
    return (marker is not None and marker.exists() and
            marker.read_text() != _RESTART_COMPLETED)


def provision(test_ids, env=None, *, recovery_context=None) -> int:
    env = os.environ if env is None else env
    manifests = manifests_for(test_ids)
    if not manifests:
        _log("no known tests selected — nothing to provision")
        return 0
    services, modules = compute_unions(manifests)

    # Fresh run: drop stale service state so a prior run's records can't make a subprocess skip
    # a bring-up. This driver OWNS these registries; the subprocesses (SLT_OPS_PRELOADED) never
    # clear them (plugin.pytest_configure).
    #
    # The OP registry is deliberately NOT cleared -- same reason as in pytest_configure. Wiping
    # it guaranteed a re-register (UNLOAD + LOAD OPEN PROCESSOR) of every OP jar on every run,
    # byte-identical or not; the records are now validated against the cluster instead, via the
    # `verify` callback below.
    #
    # SLT_RUN_EPOCH scopes REMEMBERED FAILURES to one run (opregistry._current_run). Stamped
    # here for this driver; the fan-out subprocesses get a freshly built env
    # (from the console that drives the harness) so they do not inherit it, and must not need to --
    # _current_run stamps a per-process value when unset rather than a shared constant, so a
    # missing stamp costs at most one extra attempt instead of a permanent block.
    os.environ.setdefault("SLT_RUN_EPOCH", uuid.uuid4().hex[:12])
    # A pre-flight is a live execution entry point; ownership
    # is declared before any registry clear or cluster resolution (undeclared: exit 2).
    _slt_decl = _slt_infra.declare_or_log(os.environ, _log)
    if _slt_decl is None:
        return 2
    clear_provision_registry(_STATE_DIR)

    cfg = _PreflightConfig()
    cfg._slt_infra = _slt_decl
    release = _resolve_release(cfg)
    _log(f"release STRIIM_VERSION={release.get('STRIIM_VERSION')}; provisioning cluster")
    ctx = _resolve_striim(cfg)
    if ctx is None:
        _log(f"ERROR: cluster not available: {getattr(cfg, '_slt_striim_reason', 'unknown')}")
        return 1

    eligible = []
    outcomes, cleanup_services, failures = {}, [], set()
    from livetest.service_healing import _redact
    from livetest.topology import topology_satisfies
    try:
        for manifest in manifests:
            required = getattr(manifest, 'topology', 'single')
            topology = getattr(ctx, 'topology', None)
            for svc in manifest.requires:
                try:
                    _prestart.eligibility(load_service(svc), ctx.mode, required, topology)
                except (_prestart.PreUpError, RegistryError) as e:
                    if (isinstance(e, _prestart.PreUpError) and topology is not None
                            and not topology_satisfies(required, topology)[0]):
                        _log(f"ERROR: {e}")
                        return 1
                    failures.add(svc)
                    outcomes[svc] = {"status": "unavailable", "reason": _redact(e) or type(e).__name__}
                    _log(f"ERROR service {svc}: {outcomes[svc]['reason']}")
            if topology is not None:
                ok, why = topology_satisfies(required, topology)
                if not ok:
                    _log(f'selected test: {why} — skipping')
                    continue
            eligible.append(manifest)
    except _prestart.PreUpError as e:
        _log(f'ERROR: {e}')
        return 1
    services, modules = compute_unions(eligible)

    if ("gcs" in services and outcomes.get("gcs", {}).get("status") != "unavailable"
            and not _gated_out("gcs", env)):
        # Same reason as the services-only path: compose FREEZES -public-host into the
        # container, and the fan-out subprocesses run with SLT_OPS_PRELOADED, which skips
        # pytest_configure's registry clear -- so they find gcs already recorded, skip the
        # bring-up, and inherit whatever host it was started with. Without this the console's
        # parallel mode runs every GCS test against an emulator whose resumable-upload URLs
        # the adapter rejects. plugin.runtest does the same thing for a serial run.
        # Never fatal, exactly as in provision_cluster: `_gcs_endpoint_ip` RAISES when the
        # `docker exec getent` comes back empty, and this sits ahead of the service bring-up
        # and the OP/UDF build -- so an unguarded call takes a whole console fan-out batch
        # down with zero services provisioned and zero jars built, where the cost of the
        # failure is only that the gcs tests use the compose default.
        try:
            _set_gcs_public_host(ctx)
        except Exception as e:
            _log(f"WARNING: could not resolve the GCS -public-host from the cluster ({e!r}); "
                 f"gcs will use the compose default")

    # The services come from the selected tests' manifests, so a service they need may fetch
    # its one-time inputs.
    try:
        from livetest.service_healing import preflight_context
        with preflight_context(recovery_context):
            resolved = _bring_up_services(services, env, infra=_slt_decl, pre_up=True, mode=ctx.mode,
                                          outcomes=outcomes, cleanup_services=cleanup_services,
                                          failures=failures)
    except _prestart.PreUpError:
        return 1                  # logged by _bring_up_services; the selected tests need it

    # Build only the OP/UDF union for tests whose service requirements are ready.
    _, modules = compute_unions([m for m in eligible if all(
        outcomes.get(svc, {}).get("status") == "ready" for svc in m.requires)])

    # OP/UDF union — build once, upload, register once. The subprocesses (SLT_OPS_PRELOADED) only
    # derive their ${*_JAR}/${*_NAME} tokens from a cache-hit rebuild and skip loading entirely.
    pending = (stack.lock_path(".slt-jar-restart-pending") if ctx.mode == "docker" else None)
    if modules or _replacement_restart_pending(pending):
        client = StriimClient.from_url(ctx.url, ctx.user, ctx.password)
        builts = []
        for mod in modules:
            try:
                # The same per-module lock build_modules holds: a sibling runner's `mvn package`
                # must not rewrite target/<jar> while it is built, read and copied here.
                with _module_build_lock(mod):
                    built = opartifacts.build_jar(mod["jar"], release,
                                                  report=lambda reason: _log(f"build: {reason}"))
                    if mod["kind"] == "op":
                        # Uploaded and loaded under a content name; see opartifacts.content_addressed.
                        built = opartifacts.content_addressed(built)
            except (opartifacts.OpArtifactError, OSError) as e:
                _log(f"ERROR: could not build OP/UDF module {mod['jar']}: {e}")
                return 1
            builts.append((mod, built))
        replaced = _replaced_loaded_jars(ctx, client, builts)
        if pending is not None:
            # UploadedFiles is also the UDF load path: publication destroys old-byte
            # evidence. Persist the restart obligation first, across cancellation/retry.
            if _replacement_restart_pending(pending):
                replaced = list(dict.fromkeys(pending.read_text().splitlines() + replaced))
            if replaced:
                lockdir.write_text(pending, "\n".join(replaced) + "\n")
        # Publish the complete final set of UDF jars BEFORE restarting: no UDF upload belongs
        # below this point, so fresh JVMs only ever see this set, including unchanged modules.
        # OP jars are content-named, never replaced and never restart the nodes; they are
        # uploaded by their registration (_register_op_jar) below.
        udf_paths = [built.path for mod, built in builts if mod["kind"] == "udf"]
        if udf_paths:
            opartifacts.upload_artifacts(ctx, udf_paths)
        if replaced or _replacement_restart_pending(pending):
            why = ", ".join(replaced) or "pending replacement from interrupted pre-flight"
            _log(f"restarting app nodes: loaded jar bytes changed: {why}")
            # Owns registry clearing and reauthentication; the agent is left running.
            _sp.restart_app_nodes(client)
            # Only after restart, reauthentication and readiness succeeded. In the shared
            # sticky directory another user's writable marker cannot be unlinked.
            try:
                pending.unlink()
            except PermissionError:
                lockdir.write_text(pending, _RESTART_COMPLETED)
        for mod, built in builts:
            jar = built.name
            if mod["kind"] != "udf":
                _log(f"registering OP jar {jar} (LOAD OPEN PROCESSOR), once")
                _register_op_jar(ctx, client, built)
                continue
            _log(f"registering UDF jar {jar} (load_jar), once")
            cb = (lambda j=jar: client.load_jar(j))
            # LIST LIBRARIES covers both kinds, so both are verifiable against the cluster;
            # _registry_key scopes the record so a JVM restart (libraries still listed, class
            # loaders gone) forces a re-register instead of being trusted through.
            opregistry.ensure_registered(None, jar, _registry_key(built), cb,
                                         verify=_loaded_jar_probe(client, jar))

    if recovery_context is not None:
        recovery_context.check()
    _log(f"pre-flight complete: {len(resolved)} service(s), {len(modules)} OP/UDF module(s)")
    if env.get("SLT_PREFLIGHT_OUTCOMES_PATH") and env.get("SLT_PREFLIGHT_ATTEMPT"):
        record = dict(version=1, attempt=env["SLT_PREFLIGHT_ATTEMPT"], cluster="ready",
                      ops="ready", services=outcomes, cleanup_services=cleanup_services)
        target = Path(env["SLT_PREFLIGHT_OUTCOMES_PATH"])
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(record), encoding="utf-8")
        temporary.replace(target)
    return 1 if failures else 0


def teardown(test_ids, env=None, explicit: bool = False) -> int:
    """Mirror ``plugin.pytest_sessionfinish``: with ``SLT_KEEP_SERVICES`` this is a no-op (infra
    stays up between runs; the next pre-flight re-registers from a clean slate). Otherwise tear
    down each provisioned service container and the Striim cluster (best-effort).

    ``explicit`` marks a user-typed ``livetest.cli stop <test-id>`` rather than the console's
    end-of-run cleanup. It then behaves like :func:`teardown_services`: SLT_KEEP_SERVICES is
    ignored (it means "don't tear down when a RUN ends", and honouring it would turn a direct
    command into a no-op reporting success), failures are reported, and the Striim cluster is
    left alone -- re-provisioning one costs minutes, and these commands are documented
    as covering services only.
    """
    env = os.environ if env is None else env
    if not explicit and (env.get("SLT_KEEP_SERVICES") or env.get("SLT_KEEP_RESOURCES_ON_ERROR")):
        _log("SLT_KEEP_SERVICES set — leaving services + cluster up")
        return 0
    manifests = manifests_for(test_ids)
    services, _ = compute_unions(manifests)
    if not explicit and env.get("SLT_PREFLIGHT_CLEANUP_SERVICES") is not None:
        cleanup = json.loads(env["SLT_PREFLIGHT_CLEANUP_SERVICES"])
        if not isinstance(cleanup, list) or any(not isinstance(n, str) for n in cleanup):
            raise ValueError("invalid pre-flight cleanup services")
        services = [svc for svc in services if svc in cleanup]
    # A typed `stop <test-id>` ignores the gate, exactly as teardown_services does: the gate
    # answers "should this come up?", never "is it running?", so filtering here orphans the
    # container a gated-in run (SLT_GCS=1 start) left behind, from a shell that no longer
    # exports the flag. The end-of-run path keeps the gate: it provisioned under it too.
    failed = _take_down_services(services, env, apply_gate=not explicit)
    if failed:
        _log(f"ERROR: could not tear down: {', '.join(failed)}")
    if explicit:
        return 1 if failed else 0
    # The end-of-run path reports failures too. It used to discard `failed` and swallow the
    # cluster exception, returning 0 unconditionally -- so the console's post-flight job
    # ("python -m livetest.preflight --teardown --tests …") showed PASSED over a stack that
    # was entirely stranded. Best-effort means "keep going", not "say nothing happened".
    rc = 1 if failed else 0
    # CALL teardown_cluster rather than inline its guard: an inlined copy had the ownership
    # check but not the orphan check beside it, so a cluster left under the pre-rename project
    # read as "nothing of ours" and the console's post-flight reported success over three
    # running containers and the MDR volume -- while the comment above the copy claimed one
    # invariant for both paths. One invariant means one function.
    return teardown_cluster() or rc


def _prune_dead_records(names) -> None:
    """Forget provision records whose container is no longer running.

    `ensure_provisioned_once` trusts the registry without asking Docker, so a container that
    died OUTSIDE this CLI -- a reboot, `docker system prune`, a crash, a hand-run
    `docker compose down` -- leaves a record that makes the next start skip the bring-up and
    report success over nothing. `provision` is immune because it clears the whole registry
    first; this path is additive by design (it must not discard a concurrent pre-flight's
    records), so it prunes only what it is about to bring up.
    """
    for svc in names:
        try:
            defn = load_service(svc)
        except Exception:
            continue                     # unknown/malformed: bring-up will report it
        if defn.container and not container_running(defn.container):
            forget_provisioned([defn.container, f"{svc}-setup"], _STATE_DIR)


def _note_gcs_public_host() -> None:
    """Note the GCS emulator's `-public-host` before bring-up. Never provisions.

    `docker compose up` FREEZES `-public-host` into the container command (services/gcs/
    compose.yaml), so it cannot be corrected later without recreating the container. The value
    must be an address the Striim WRITER can reach, because fake-gcs-server echoes it back in
    resumable-upload URLs.

    Deliberately does NOT resolve the cluster to compute it. `_resolve_striim` PROVISIONS a
    Docker cluster when none is running, so doing that here would make `start live gcs` -- a
    one-service command -- spin up a 21GB three-container Striim just to read an IP, and
    contradict the documented "services only, never the cluster".

    The compose default (`host.docker.internal:4443`) is already correct for a Docker cluster,
    which is the case measured: a objectwriter live test passes against it on the fan-out
    path, where nothing re-ups the container. It is wrong only for a NATIVE Striim, which
    cannot resolve that name -- hence the note rather than a guess or a refusal. `provision()`
    and `provision_cluster()` set the exact value for free, having already resolved the cluster
    for their own reasons -- so `start live` (cluster first, then services) does get it right.
    """
    if os.environ.get("SLT_GCS_PUBLIC_HOST"):
        return
    # Informational, not a problem: the default is correct for a Docker cluster, which is the
    # common case. Only a native Striim needs the override, so say so once, briefly. The port
    # is the PUBLISHED one -- advice naming a dead port is worse than none, and compose
    # publishes ${SLT_GCS_HOST_PORT:-4443} (same reason _set_gcs_public_host reads it).
    port = os.environ.get("SLT_GCS_HOST_PORT") or "4443"
    _log(f"gcs: -public-host at the compose default (fine for a Docker cluster; a NATIVE Striim "
         f"needs SLT_GCS_PUBLIC_HOST=127.0.0.1:{port})")


def _set_gcs_public_host(ctx) -> None:
    """Publish the compose-time GCS vars from an already-resolved cluster.

    setdefault, not assignment: an operator who set these keeps their override (compose
    documents SLT_GCS_PUBLIC_HOST as always winning), and this matches plugin.runtest.

    The port is the HOST port, not the container's 4443: compose publishes
    `${SLT_GCS_HOST_PORT:-4443}:4443` and derives its own default -public-host from the same
    var, so hardcoding 4443 here means `SLT_GCS_HOST_PORT=4553 start live` -- the documented
    escape hatch for a busy :4443 -- hands the Striim writer resumable-upload URLs on a dead
    port. Overriding the compose default has to reproduce the part of it that still holds."""
    port = os.environ.get("SLT_GCS_HOST_PORT") or "4443"
    os.environ.setdefault("SLT_STRIIM_VIEW_HOST", ctx.view_host)
    os.environ.setdefault("SLT_GCS_PUBLIC_HOST", f"{_gcs_endpoint_ip(ctx)}:{port}")
    _log(f"gcs: -public-host {os.environ['SLT_GCS_PUBLIC_HOST']}")


def provision_cluster(publish_gcs_host: bool = False) -> int:
    """Bring up (or reuse) the Striim cluster on its own, for `livetest.cli start striim`.

    The cluster is not a registry service -- it has compose files but no service.yaml, and
    striim_provision owns its lifecycle -- so `start live` would otherwise leave you with
    six databases and nothing to run a pipeline on. `_resolve_striim` reuses a reachable
    Striim (native or already-running containers) and provisions the Docker cluster only
    when there is none, which is the same rule a test run applies.

    ``publish_gcs_host`` is set by the caller when gcs is actually in the bring-up, mirroring
    :func:`provision`'s ``if "gcs" in services`` guard. Unconditional publishing would shell
    out `docker exec … getent host.docker.internal` on every `start striim`, mutate the
    process-wide view_host for a service nobody asked for, and warn on plain Docker Engine
    (where that name does not resolve) about a value nothing will read."""
    _slt_decl = _slt_infra.declare_or_log(os.environ, _log)
    if _slt_decl is None:
        return 2
    cfg = _PreflightConfig()
    cfg._slt_infra = _slt_decl
    ctx = _resolve_striim(cfg)
    if ctx is None:
        _log(f"ERROR: could not start the Striim cluster: "
             f"{getattr(cfg, '_slt_striim_reason', 'unknown')}")
        return 1
    _log(f"striim: {ctx.mode} cluster ready at {ctx.url}")
    # The cluster is resolved, so the GCS emulator's -public-host costs nothing to compute
    # here -- and `docker compose up` FREEZES it into the container, so a `start live` that
    # brings gcs up afterwards is the only chance to get it right in one command. setdefault
    # semantics: an operator's own SLT_GCS_PUBLIC_HOST still wins. Never fatal: the cluster
    # IS up, and a failed address lookup only costs gcs its non-default value.
    if publish_gcs_host:
        try:
            _set_gcs_public_host(ctx)
        except Exception as e:
            _log(f"WARNING: could not resolve the GCS -public-host from the cluster ({e!r}); "
                 f"gcs will use the compose default")
    return 0


def teardown_cluster() -> int:
    """Tear the Striim cluster down, for `livetest.cli stop striim` and the default `stop live`.

    Guarded the way plugin.pytest_sessionfinish is (it tears down "only if WE provisioned it"):
    `cluster_down` is `down -v`, which removes the slt-striim-shared volume holding the MDR. A
    native Striim, or one reachable at an external STRIIM_URL, leaves this checkout's compose
    project with no containers -- there is nothing of ours to take down, and running it anyway
    would delete a volume we never created.
    """
    try:
        # Inside the try, and resolved once: _resolve_release raises on a broken STRIIM_HOME
        # (no Platform-*.jar, or two of them), and outside it that became an unhandled
        # traceback that took the REST of `stop` down with it.
        release = _resolve_release(_PreflightConfig())
        if not _sp.cluster_has_containers(_STRIIM_DIR, release):
            orphans = _surviving_cluster_containers()
            if orphans:
                # The volume too: the project rename renames the named volume with it, so the
                # pre-rename volume is invisible to today's `down -v` forever and an operator
                # who runs only the `docker rm -f` leaks it silently. Prefix-scoped like the
                # container names beside it -- under SLT_STACK_PREFIX=alt the old project was
                # `alt-striim`, so a hardcoded name is one a copy-paste cannot remove.
                volume = f"{stack.prefixed('striim')}_slt-striim-shared"
                _log(f"ERROR: {', '.join(orphans)} still present but NOT in this checkout's "
                     f"compose project (started before it was renamed `striim` -> "
                     f"`slt-striim`). Remove them once with: docker rm -f {' '.join(orphans)} "
                     f"&& docker volume rm {volume}")
                return 1
            _log("striim: no containers from this checkout's compose project — "
                 "leaving the cluster alone (a native/external Striim is never torn down here)")
            return 0
        _log("tearing down Striim cluster")
        _sp.cluster_down(_STRIIM_DIR, release)
        return 0
    except Exception as e:
        _log(f"ERROR: failed to tear down the Striim cluster: {e!r}")
        return 1


def provision_services(names, env=None, apply_gate: bool = False) -> int:
    """Bring up `names` (already-resolved service names) and nothing else.

    The services-only half of :func:`provision`, for `livetest.cli start <service>` and
    a runner's `start` command. Deliberately NOT a thin wrapper around ``provision``:

      - no manifest lookup -- the caller names services, not test ids;
      - no registry CLEARING -- this bring-up is additive, and wiping the registries would
        make a prior full pre-flight's recorded containers invisible to the subprocesses;
      - no cluster resolve and no OP/UDF build -- provisioning a Docker service needs
        neither, and requiring a reachable cluster would make `start postgres` fail on a
        machine that has no Striim yet.

    ``apply_gate`` follows `_bring_up_services`' rule: off when the caller NAMED these (an
    explicit `start gcs` means start it), on when the list was DERIVED -- `cli start all`
    expands to every registered service, and without the gate that hands an operator the
    spanner/gcs/kafka emulators they opted out of, which is also the opposite of what
    a runner's `start live` does through its default service targets.
    """
    env = os.environ if env is None else env
    # The services-only bring-up is a live provisioning entry point;
    # ownership is declared before any record is pruned or service brought up (undeclared: exit 2).
    _slt_svc_decl = _slt_infra.declare_or_log(env, _log)
    if _slt_svc_decl is None:
        return 2
    if "gcs" in names:
        _note_gcs_public_host()
    _prune_dead_records(names)
    # A bring-up failure is an rc, not a traceback: this runs behind a runner's `start`, and
    # a Docker daemon that is down would otherwise print a stack trace over the clean
    # diagnosis its caller already logged.
    try:
        resolved = _bring_up_services(names, env, apply_gate=apply_gate, infra=_slt_svc_decl)
    except Exception as e:
        _log(f"ERROR: could not bring up services: {e!r}")
        return 1
    _log(f"services up: {len(resolved)} service(s)")
    return 0


def teardown_services(names, env=None) -> int:
    """Tear down `names` and nothing else -- the services-only mirror of :func:`teardown`.
    Leaves the Striim cluster alone: `stop postgres` must not take the cluster with it.

    Deliberately does NOT honour SLT_KEEP_SERVICES, unlike :func:`teardown`. There it means
    "don't tear down when a test run ends", which is the documented dev default (README's
    "always pass SLT_KEEP_SERVICES=1") and routinely exported in a shell. Applying it to an
    explicit `stop <service>` would turn a direct command into a no-op reporting success.
    """
    env = os.environ if env is None else env
    failed = _take_down_services(names, env, apply_gate=False)
    if failed:
        _log(f"ERROR: could not tear down: {', '.join(failed)}")
        return 1
    return 0


def restart_app_nodes(env=None) -> int:
    """Restart the shared app nodes once (poison recovery, spec §D.5). The console serialises this
    across the drained pool, then re-queues the poisoned test."""
    _slt_decl = _slt_infra.declare_or_log(os.environ, _log)
    if _slt_decl is None:
        return 2
    cfg = _PreflightConfig()
    cfg._slt_infra = _slt_decl
    ctx = _resolve_striim(cfg)
    if ctx is None:
        _log(f"ERROR: cluster not available: {getattr(cfg, '_slt_striim_reason', 'unknown')}")
        return 1
    client = StriimClient.from_url(ctx.url, ctx.user, ctx.password)
    _log("restarting app nodes (OP-loader poison recovery)")
    _sp.restart_app_nodes(client)
    _log("app nodes restarted")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m livetest.preflight",
                                description="Console fan-out pre-flight / post-flight driver.")
    p.add_argument("--tests", nargs="*", default=[],
                   help="pytest -k test ids to compute the service + OP/UDF unions from")
    p.add_argument("--teardown", action="store_true",
                   help="tear down (mirror pytest_sessionfinish) instead of provision")
    p.add_argument("--restart-app-nodes", action="store_true",
                   help="restart the shared app nodes once (poison recovery); ignores --tests")
    args = p.parse_args(argv)
    # The project manifest named by GOLD_TARGETS, as livetest.cli does: its servicesRoots add the
    # consumer's services to what a pre-flight provisions (console impact F1).
    if (os.environ.get("GOLD_TARGETS") or "").strip():
        from livetest import project
        try:
            project.load_and_activate()
        except Exception as e:
            _log(f"ERROR: cannot activate the project manifest: {e}")
            return 2
    if args.restart_app_nodes:
        return restart_app_nodes()
    if args.teardown:
        return teardown(args.tests)
    return provision(args.tests)


if __name__ == "__main__":
    sys.exit(main())
