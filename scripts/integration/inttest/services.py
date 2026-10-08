"""Docker compose helpers for integration-tier service lifecycle (Phase 1/2d).

Mirrors scripts/live/livetest/services.py but simplified for database-only testing
(no Striim provisioning, no kafka/gcs service coordination).

Three layers:

  - `compose_up`/`compose_down`/`is_up` (Phase 1): thin wrappers over `docker
    compose up -d --wait` / `down -v` / `ps --status running` for one service.
    `up` is already idempotent against an already-healthy stack.
  - `ensure_up`/`ensure_down`: the `requires:` lifecycle the SPEC
    §5/§12 pipeline drives -- filelock-serialized across pytest-xdist workers, and
    backed by a small on-disk "started services" registry directly under
    scripts/integration/ (mirrors scripts/live's flat coordination-file layout) so a
    session-end teardown (or a future broker) knows what THIS host brought up.
    `.int-services-up.json` mirrors (same path, same JSON-list-of-names shape) the
    registry `inttest/plugin.py`'s current stub `_read_started`/`_write_started`
    maintains, so the two can coexist / either can drive teardown. `ensure_up`
    registers a service ONLY if this call is the one that actually started it
    (decided atomically under `lock`, and returned as a `bool` so a caller like
    `ensure_up_for_test` knows without a second, separately-racy check).
  - `ensure_up_for_test` (a `contextmanager`): borrows a service for the duration
    of a `with` block, tearing it back down after ONLY if this call started it --
    for standalone pytest tests (not the YAML `requires:` pipeline) that want the
    same "leave an already-running service alone" guarantee `ensure_up` gives.

Supported services: postgres, oracle, spanner, gcs -- all provisioned and torn down the same
way, no service-specific gating here. `ensure_up`/`ensure_down`/`ensure_up_for_test`
treat all four identically; the only per-service logic outside this module is
`dbroutes.py`'s driver dispatch (gcs needs none -- no `ddl:`/`seed:` routing) and
`inttest/plugin.py`'s per-test isolation strategy (fixed qasource/qatarget schemas +
per-test `${TID}` table prefix for Postgres, per-test table prefix for Oracle,
`${TID}`-prefixed tables for Spanner, one fixed `${TID}`-prefixed-object-path bucket
for gcs).

This module is dependency-light and import-time side-effect-free: importing it
never touches Docker, a database, or a network socket. All work happens inside the
functions, which shell out to `docker compose`.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Iterator

from filelock import FileLock

_HERE = Path(__file__).resolve().parent  # scripts/integration/inttest/
_STARTED_REGISTRY_NAME = ".int-services-up.json"
_PROVISION_REGISTRY_NAME = ".int-provision-registry.json"
_PROVISION_LOCK_NAME = ".int-provision-registry.lock"


def _state_root() -> Path:
    # Coordination state at call time: SLT_STATE_DIR, else scripts/integration -- the same
    # dir cli.py and plugin.py use (gitignored).
    from inttest import resources as _resources
    return _resources.state_root()


def state_name(base: str, env=None) -> str:
    """Prefix-scope a coordination filename, keeping its dotfile-ness:
    ``.int-services-up.json`` -> ``.ec-int-services-up.json``. Mirrors the live tier's
    ``livetest.stack.state_name``.

    These files record what THIS STACK started, and INT_STACK_PREFIX is what makes two
    stacks distinct -- compose.yaml prefixes the project name and every container_name,
    so ``ec`` and ``alt`` own entirely separate containers. The state describing them
    has to be separated the same way.

    Unscoped, a single checkout running two prefixes shares one record, and the second
    stack reads the first's list of started services and SKIPS bringing up its own. The
    symptom is not a clean failure: services silently never start, so a case needing one
    fails on a connection error, or -- worse -- blocks waiting on a container that was
    never launched. That is a real incident, not a hypothetical: a stale
    ``["postgres"]`` here left the ec stack without gcs, and hung a run for 40 minutes
    entering the first Spanner case.

    Two separate CHECKOUTS never collided (each has its own scripts/integration/), which
    is exactly why this survived: it only bites when one checkout drives two stacks.
    """
    p = (env if env is not None else os.environ).get("INT_STACK_PREFIX", "").strip()
    if not p:
        return base
    return f".{p}-{base[1:]}" if base.startswith(".") else f"{p}-{base}"


def started_registry_path(state_dir: Path | None = None) -> Path:
    return (Path(state_dir) if state_dir is not None else _state_root()) / state_name(_STARTED_REGISTRY_NAME)

# The services this tier provisions/tests. Not enforced by ensure_up/ensure_down
# (any service dir under services/<name>/ works), but documents SPEC §12's scope and
# is what dbroutes.py's _PARAM_SPECS mirrors (live Docker coverage: postgres in
# test_services_live.py, spanner in test_spanner_live.py; oracle has no standalone
# live test today).
_BUILTIN_SERVICES = frozenset({"postgres", "oracle", "spanner", "sqlserver", "gcs", "mysql",
                               "teradata", "vertica"})


def supported_services() -> frozenset:
    """The names this tier provisions: the built-ins, plus every service a services root defines
    (a consumer's servicesRoots included, console impact F3), computed at call time."""
    from inttest import resources as _resources
    try:
        found = set(_resources.list_profiles())
    except Exception:
        found = set()
    return frozenset(_BUILTIN_SERVICES | found)


def __getattr__(name):
    # SUPPORTED_SERVICES stays readable as a module attribute, now derived from the roots.
    if name == "SUPPORTED_SERVICES":
        return supported_services()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _compose_file(service_name: str) -> Path:
    # Resolved at call time through the integration profile seam (SLT_INT_SERVICES_DIR, else
    # scripts/integration/services). Read-only lookup (names, cleanup): nothing is checked.
    from inttest import resources as _resources
    return _resources.service_dir(service_name) / "compose.yaml"


def _checked_compose_file(service_name: str) -> Path:
    # Construction preflight: the whole profile (definition, compose, bind-mounted init
    # files, build contexts, COPY sources) is checked in ONE origin before any docker
    # command; a missing dependency raises inttest.resources.ResourceError naming the path.
    from inttest import resources as _resources
    return _resources.select_profile(service_name).origin / "compose.yaml"


def compose_up(service_name: str, lock: FileLock) -> None:
    """Bring up the named service's docker-compose stack.

    Runs: docker compose -f services/{service_name}/compose.yaml up -d --wait

    The --wait flag causes docker compose to wait for healthchecks to pass before
    returning. The -d flag runs in detached mode (daemon). This operation is
    idempotent: calling it on an already-healthy stack returns silently.

    Args:
        service_name: name of the service (e.g. 'postgres', 'oracle', 'spanner')
        lock: FileLock coordinating across pytest-xdist workers (held by caller)

    Raises:
        CalledProcessError: if `docker compose up` fails
        FileNotFoundError: if docker is not available
    """
    subprocess.run(
        ["docker", "compose", "-f", str(_checked_compose_file(service_name)), "up", "-d", "--wait"],
        check=True, env=_compose_up_env(service_name),
    )


def _service_spec(service_name: str) -> dict:
    """The service's service.yaml as a dict ({} when absent or unreadable)."""
    from inttest import resources as _resources
    path = _resources.service_dir(service_name) / "service.yaml"
    try:
        import yaml
        spec = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}
    from livetest.registry import hook_policy, _required_files_env
    spec.update(hook_policy(spec, path))
    spec['required_files_env'] = _required_files_env(spec.get('required_files_env'), path)
    return spec


def live_service_dir(service_name: str) -> Path:
    """The live tier's service dir of the same name, found as the live tier finds it: a
    consumer ``servicesRoots`` entry, then ``SLT_SERVICES_DIR``, then the built-in live services.
    Without the live engine installed: ``SLT_SERVICES_DIR``, else the framework's live services."""
    try:
        from livetest import project as _live_project, registry as _live_registry
    except ImportError:
        from inttest import paths as _paths
        live = _paths._path("SLT_SERVICES_DIR", _paths.framework_home() / "live" / "services")
        return live / service_name
    if _live_project._ACTIVE is None:
        _live_project.load_and_activate()      # GOLD_TARGETS' servicesRoots, as a live run sees them
    found = _live_registry._find(service_name)
    return found if found is not None else Path(_live_registry._SERVICES_DIR) / service_name


def _compose_up_env(service_name: str) -> dict:
    """The environment `up` runs in: this process's, plus each ``live_service_paths`` entry of
    the service's service.yaml that the shell leaves unset. An entry maps a variable its compose
    file reads to a path inside the live tier's service of the same name, so a service can
    boot files the live tier holds (a VM's disks, say) without copying them."""
    from inttest import paths
    env = paths.effective_env(os.environ)
    mapping = _service_spec(service_name).get("live_service_paths") or {}
    if mapping:
        live = live_service_dir(service_name)
        import yaml
        from livetest.registry import required_path
        try:
            live_spec = yaml.safe_load((live / 'service.yaml').read_text()) or {}
        except (OSError, yaml.YAMLError):
            live_spec = {}
        for var, rel in mapping.items():
            value = (env.get(var) or "").strip()
            if value:
                from inttest import resources
                selected = Path(value).expanduser()
                if not selected.is_absolute():
                    env[var] = str((resources.service_dir(service_name) / selected).resolve())
            else:
                env[var] = str(required_path(live, rel, env, live_spec.get('required_files_env')))
    return env


def required_files(service_name: str) -> list[Path]:
    """The files the service needs that git does not carry, as absolute paths: its own
    ``required_files`` (relative to its dir), plus the live service's ``required_files`` that lie
    under one of its ``live_service_paths`` (the files it boots from the live tier, a VM's disks
    say). Checked before a bring-up, as the live tier's registry.unavailable does."""
    from inttest import resources as _resources
    spec = _service_spec(service_name)
    sdir = _resources.service_dir(service_name)
    from livetest.registry import required_path
    env = _compose_up_env(service_name)
    out = [required_path(sdir, f, env, spec.get('required_files_env')) for f in (spec.get("required_files") or [])]
    shared = [str(rel).strip("/") for rel in (spec.get("live_service_paths") or {}).values()]
    if shared:
        live = live_service_dir(service_name)
        try:
            import yaml
            live_spec = yaml.safe_load((live / "service.yaml").read_text()) or {}
        except Exception:
            live_spec = {}
        for f in live_spec.get("required_files") or []:
            if any(f == rel or f.startswith(rel + "/") for rel in shared):
                # Integration cache overrides are inserted first; shared live overrides are fallback.
                out.append(required_path(live, f, env, {**(spec.get('live_service_paths') or {}),
                                                       **(live_spec.get('required_files_env') or {})}))
    return out


def unavailable(service_name: str) -> str | None:
    """Why the service cannot be brought up here (a required file missing), or None. With its
    existing instance set (``live_override_env``) nothing is brought up, so None. A missing
    ``python_module`` (the live tier's rule) comes first."""
    spec = _service_spec(service_name)
    from livetest.registry import missing_python_module
    why = missing_python_module(spec.get("python_module"), spec.get("python_module_hint"))
    if why:
        return why
    gate = spec.get("live_override_env")
    if gate and (os.environ.get(gate) or "").strip():
        return None
    missing = [str(f) for f in required_files(service_name) if not f.is_file()]
    if not missing:
        return None
    alt = f", or set {gate} to use an existing instance" if gate else ""
    return f"required files missing: {', '.join(missing)} (see the service's README{alt})"


def run_pre_up(service_name: str, log=print) -> bool:
    """Run the service's ``pre_up`` hook through the live tier's runner (livetest.prestart.run_hook:
    the same rules, lock, timeout and skip-when-present), only for a case that requires the
    service or a start that names it, and not with its existing instance set. The script also
    gets SLT_LIVE_SERVICE_DIR, the live tier's service of the same name. False when nothing ran;
    raises livetest.prestart.PreUpError when it failed."""
    from inttest import resources as _resources
    spec = _service_spec(service_name)
    script = spec.get("pre_up")
    if not script:
        return False
    gate = spec.get("live_override_env")
    if gate and (os.environ.get(gate) or "").strip():
        return False
    try:
        from livetest import prestart
    except ImportError:
        log(f"[{service_name}] pre_up not run: it needs the live engine installed (livetest)")
        return False
    return prestart.run_hook(
        service_name, _resources.service_dir(service_name), script,
        required=required_files(service_name), timeout=spec.get("pre_up_timeout"),
        extra_env={"SLT_LIVE_SERVICE_DIR": str(live_service_dir(service_name))},
        env=_compose_up_env(service_name), check=spec["pre_up_check"],
        unavailable_policy=spec["unavailable_policy"], log=log)


def is_connection_only(service_name: str) -> bool:
    """A definition with nothing to start (neither ``compose`` nor ``container``) that names an
    existing instance instead (``live_override_env``), as the shipped teradata does: there is
    nothing to start or stop; its cases run against the instance its settings name."""
    spec = _service_spec(service_name)
    return (bool(spec) and not spec.get("compose") and not spec.get("container")
            and bool(spec.get("live_override_env")))


# --------------------------------------------------------------------------------------------
# What this checkout believes it owns
#
# The integration-tier half of scripts/live/livetest/services.py's block of the same name --
# same question, same answer shape, deliberately the same function names, because the two
# tiers are separate installable packages and cannot share a module. Keep them in step: the
# earlier versions of this check drifted apart within a single commit (one asked "running?",
# the other "exists?") and each drift was a silent missed orphan.
# --------------------------------------------------------------------------------------------

# `${INT_STACK_PREFIX:+${INT_STACK_PREFIX}-}` and friends: stripping the whole interpolation
# leaves the literal base name, which the prefix is then re-applied to from the CURRENT env.
_INTERPOLATION = re.compile(r"\$\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}")

# current container name -> names it USED to have; see the live tier's _RENAMED_CONTAINERS.
# Empty: this tier renamed its compose PROJECTS on this branch, not its containers.
_RENAMED_CONTAINERS: dict = {}


def declared_containers(compose_paths) -> list[str]:
    """Every container name `compose_paths` declare, prefix-scoped, plus their former names.

    The compose file, not service.yaml: `container:` names only the primary, while a service
    with a sidecar reserves that name just as exclusively. A line scan rather than PyYAML
    keeps this module dependency-light (`inttest.cli stop gcs` must work in a checkout with
    only the runtime deps installed)."""
    prefix = os.environ.get("INT_STACK_PREFIX", "")
    names: list[str] = []
    for path in compose_paths:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("#") or not line.startswith("container_name:"):
                continue
            value = _INTERPOLATION.sub("", line.split(":", 1)[1])
            value = value.split("#", 1)[0].strip().strip("'\"").lstrip("-")
            if not value:
                continue
            for base in [value, *_RENAMED_CONTAINERS.get(value, [])]:
                name = f"{prefix}-{base}" if prefix else base
                if name not in names:
                    names.append(name)
    return names


def orphaned_containers(compose_paths) -> list[str]:
    """Declared containers that still EXIST -- what a `compose down` left behind."""
    return [c for c in declared_containers(compose_paths) if container_exists(c)]


def compose_container_names(service_name: str) -> list[str]:
    """`declared_containers` for one registered service, by name."""
    return declared_containers([_compose_file(service_name)])


def container_exists(container: str) -> bool:
    """True iff a container by that NAME exists, in ANY state -- the question that matters
    after a `down`, since Docker names are unique across states and an `exited` leftover
    blocks the next `up` exactly as a running one does."""
    # `docker container inspect`, not bare `docker inspect`: the bare form also resolves
    # images, volumes and networks, and volumes/networks carry `.Name` -- so a VOLUME named
    # int-postgres would read as a surviving container (see the live tier's twin).
    try:
        r = subprocess.run(["docker", "container", "inspect", "-f", "{{.Name}}", container],
                           capture_output=True, text=True)
    except OSError:
        return False
    return r.returncode == 0


def surviving_containers(service_name: str) -> list[str]:
    """`orphaned_containers` for one registered service, by name.

    Called by `inttest.cli`, not by `compose_down` itself: raising from inside the teardown
    would skip `ensure_down`'s registry bookkeeping and let a cleanup error replace the real
    failure of a test using `ensure_up_for_test`. Mirrors the live tier, where the same check
    lives in `preflight._take_down_services` rather than in `services.compose_down`."""
    return orphaned_containers([_compose_file(service_name)])


def compose_down(service_name: str, lock: FileLock) -> None:
    """Tear down the named service's docker-compose stack.

    Runs: docker compose -f services/{service_name}/compose.yaml down -v

    The -v flag removes named volumes declared in the compose file, ensuring a
    clean teardown (no orphaned volumes).

    Args:
        service_name: name of the service (e.g. 'postgres', 'oracle', 'spanner')
        lock: FileLock coordinating across pytest-xdist workers (held by caller)

    Raises:
        CalledProcessError: if `docker compose down` fails
        FileNotFoundError: if docker is not available
    """
    subprocess.run(
        ["docker", "compose", "-f", str(_compose_file(service_name)), "down", "-v"],
        check=True,
    )


def is_up(service_name: str) -> bool:
    """True iff `service_name`'s compose stack already has at least one running
    container -- a direct Docker-side check (`docker compose ps --status running`),
    not a TCP/network reachability guess. Note this is weaker than "healthy": a
    container can be `running` while still mid-healthcheck (Postgres/Oracle's
    compose files both have multi-second-to-multi-minute healthcheck windows), so
    a caller that needs a guaranteed-ready service must still go through
    `ensure_up`/`compose_up`'s `--wait` gate rather than trusting `is_up` alone
    for that purpose -- it answers "is anything running I shouldn't clobber", not
    "is it safe to connect yet". `services/<name>/compose.yaml` missing, or
    `docker` itself unavailable, both read as "not up" rather than raising -- a
    caller deciding whether to bring something up doesn't need to distinguish
    those from a genuine "down"."""
    compose_file = _compose_file(service_name)
    if not compose_file.exists():
        return False
    try:
        r = subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "ps", "-q", "--status", "running"],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    return bool(r.stdout.strip())


# ============================================================================
# Started-services registry (`.int-services-up.json`, scripts/integration/ root)
#
# A flat JSON array of service names this host has brought up via ensure_up and not
# yet torn down via ensure_down. Read-modify-write always happens under `lock`, so
# concurrent xdist workers never race a lost update. Mirrors (same path, same shape)
# inttest/plugin.py's own _read_started/_write_started -- either side can read/write
# it; both treat "file absent" as "nothing started" rather than an error.
# ============================================================================


def _read_started() -> set[str]:
    try:
        return set(json.loads(started_registry_path().read_text()))
    except (FileNotFoundError, ValueError):
        return set()


def _write_started(names: set[str]) -> None:
    started_registry_path().write_text(json.dumps(sorted(names)))


def started_services() -> set[str]:
    """Every service name currently recorded as started by `ensure_up` (this host,
    not yet `ensure_down`'d). Used by session-teardown code (or a test) to discover
    what needs tearing down without re-deriving it from `requires:` lists."""
    return _read_started()


# ============================================================================
# ensure_up / ensure_down -- the `requires:` lifecycle (SPEC §5/§12)
# ============================================================================


def ensure_up(service_name: str, lock: FileLock) -> bool:
    """Idempotently bring up `service_name`, serialized across pytest-xdist workers
    via `lock`, and record it in the started-services registry -- but ONLY if this
    call is the one that actually transitioned it from down to up. A service that
    was already running before this call is left untouched by the registry: it
    isn't this call's to tear down later (`pytest_sessionfinish`/`ensure_down` both
    drive off the registry), so registering it would get an already-running,
    not-ours service killed at session end alongside the ones this run genuinely
    started.

    Returns True iff THIS call is the one that started the service (and therefore
    owns tearing it back down later), False if it was already up. The up-check,
    the compose call, and the registry write all happen under `lock`, so this is a
    single atomic decision -- a caller must NOT re-derive "did I start it" with its
    own separate `is_up()` call after the fact, since that second check runs
    outside this lock and can race a sibling worker's `ensure_up` (see
    `ensure_up_for_test`, which relies on this return value for exactly that
    reason).

    Idempotent in two senses: `docker compose up -d --wait` itself no-ops against an
    already-healthy stack (Phase 1 behavior, unchanged), and calling this twice for
    the same service is a no-op the second time as far as the registry is concerned
    (a set add, not an append -- `ensure_up("postgres", lock)` from two different
    tests in the same run does not double-start anything or corrupt the registry).

    Args:
        service_name: name of the service dir under `services/` (e.g. 'postgres',
            'oracle'). Any name works; SUPPORTED_SERVICES documents what this slice
            tests.
        lock: FileLock coordinating across pytest-xdist workers. Held for the
            duration of the up-check, the compose call, and the registry update, so
            two workers racing to bring up the same service serialize rather than
            both shelling out to `docker compose up` concurrently (and rather than
            both seeing "not yet up" and both claiming credit for starting it).

    Raises:
        CalledProcessError: if `docker compose up` fails.
        FileNotFoundError: if docker is not available, or `services/<name>/compose.yaml`
            does not exist.
    """
    # Whole-profile preflight BEFORE the missing-compose FileNotFoundError (callers convert
    # that into a skip): incomplete resources are a named ResourceError instead.
    _checked_compose_file(service_name)
    compose_file = _compose_file(service_name)
    if not compose_file.exists():
        raise FileNotFoundError(
            f"services/{service_name}/compose.yaml does not exist -- cannot bring up "
            f"the {service_name!r} service"
        )
    with lock:
        already_up = is_up(service_name)
        compose_up(service_name, lock)
        if already_up:
            return False
        started = _read_started()
        started.add(service_name)
        _write_started(started)
        return True


def ensure_down(service_name: str, lock: FileLock) -> None:
    """Tear down `service_name`, serialized across pytest-xdist workers via `lock`,
    and remove it from the started-services registry.

    Safe to call on a service `ensure_up` never started (or already torn down): the
    registry update is a set discard (no-op if absent), and `compose_down` itself is
    handed to `docker compose down -v`, which no-ops against an already-absent stack.

    Args:
        service_name: name of the service dir under `services/`.
        lock: FileLock coordinating across pytest-xdist workers.

    Raises:
        CalledProcessError: if `docker compose down` fails.
        FileNotFoundError: if docker is not available.
    """
    with lock:
        compose_down(service_name, lock)
        started = _read_started()
        started.discard(service_name)
        _write_started(started)


@contextlib.contextmanager
def ensure_up_for_test(service_name: str, lock: FileLock) -> Iterator[None]:
    """Bring up `service_name` for the duration of the `with` block, tearing it
    back down on exit -- but ONLY if this call is the one that started it. A
    service already running when the block is entered is left running
    afterward, untouched: a test borrows the service, it doesn't take ownership
    of its lifecycle away from whoever (a prior test, a developer, `INT_KEEP_
    SERVICES`) already had it up.

    Always calls `ensure_up` (never skips it) so the `--wait` healthcheck gate
    still applies even when the service turns out to already be running --
    `ensure_up`'s own return value (True iff THIS call started it), not a
    separate `is_up()` check, decides whether to tear down afterward: deciding
    ownership via a second, unlocked `is_up()` call here would race a sibling
    pytest-xdist worker's `ensure_up` (both could observe "not yet up" before
    either registers), risking one worker tearing down a service another
    worker is still using.

    Usage::

        with services.ensure_up_for_test("postgres", lock):
            ...  # real Postgres is guaranteed reachable here

    Raises whatever `ensure_up` raises (`FileNotFoundError`/`CalledProcessError`)
    if bringing the service up fails; nothing is torn down in that case since
    nothing was confirmed started."""
    started_here = ensure_up(service_name, lock)
    try:
        yield
    finally:
        if started_here:
            ensure_down(service_name, lock)


# ============================================================================
# Run-exactly-once across xdist workers (docs/internals/INTEGRATION-ENGINE.md#token-isolation).
# A scoped port of scripts/live/livetest/services.py's
# `ensure_provisioned_once`/`clear_provision_registry` -- same registry+lock
# shape AND, as of the commit carrying this comment, the same stack-prefix scoping
# (`state_name`, above). It previously said the opposite -- "no stack-prefix scoping,
# because this tier has no analogue to live's SLT_STACK_PREFIX" -- on the premise that
# two concurrent integration stacks from one checkout was not a supported scenario.
# INT_STACK_PREFIX is that analogue, compose.yaml has honoured it throughout, and the
# unscoped state is what let a stale `["postgres"]` strand the ec stack without gcs.
# `.gitignore`'s `.*-int-*` globs, carried over from live's convention, already cover
# the scoped names -- so nothing there needed to change.
# ============================================================================


def _provision_registry_path(state_dir: Path | None = None) -> Path:
    return (Path(state_dir) if state_dir is not None else _state_root()) / state_name(_PROVISION_REGISTRY_NAME)


def _provision_lock_path(state_dir: Path | None = None) -> Path:
    return (Path(state_dir) if state_dir is not None else _state_root()) / state_name(_PROVISION_LOCK_NAME)


def clear_provision_registry(state_dir: Path | None = None) -> None:
    """Delete the service-provision registry (controller, session start). A
    prior run's stale record must not make a worker skip provisioning (e.g.
    `PgAdmin.ensure_setup`) against a container that was torn down and
    recreated since -- `services.ensure_up_for_test` does that routinely."""
    try:
        _provision_registry_path(state_dir).unlink()
    except FileNotFoundError:
        pass


def ensure_provisioned_once(key: str, do_provision, state_dir: Path | None = None) -> bool:
    """Run `do_provision` exactly once across xdist workers, keyed by `key`
    (e.g. "postgres-setup") -- mirrors `scripts/live/livetest/services.py`'s
    function of the same name. A filelock + JSON registry lets the first
    worker to reach this call run `do_provision` while the rest block on the
    lock, then find `key` already recorded and skip. Workers never
    de-provision (asymmetric by design, matching live).

    Returns True iff THIS call performed the provisioning."""
    registry = _provision_registry_path(state_dir)
    lock = _provision_lock_path(state_dir)
    with FileLock(str(lock)):
        try:
            state = json.loads(registry.read_text())
        except (FileNotFoundError, ValueError):
            state = {}
        if key in state:
            return False
        do_provision()
        state[key] = "1"
        registry.write_text(json.dumps(state))
        return True
