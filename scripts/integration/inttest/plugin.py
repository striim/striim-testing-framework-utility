"""Core pytest fixture harness for the integration-test tier (Phase 1).

This is the sibling of scripts/live/livetest/plugin.py, but deliberately smaller: the
integration tier targets a single OpenProcessor/UDF's Java-level behavior against real
Postgres/Oracle/Spanner services, not a full Striim cluster + TQL app. See README.md
for the architecture, the docker-compose layout under services/*, and the SQL
templates under sql/*.

Two isolation patterns coexist, matching sql/*/templates.sql:
  1. TID-prefixed table names inside the FIXED qasource/qatarget schemas/users
     (${TID} for Postgres/Spanner, ${TID_ORACLE} for Oracle) -- the default, and the
     only one that works today without elevated grants (see `pg_schema`/`ora_schema`
     docstrings below for why).
  2. A dedicated per-test Postgres schema (`pg_schema`, via the `pg_admin` superuser
     connection) for a test that wants full isolation beyond table-name prefixing.

Token derivation (`_slug` / `_tid_oracle`) is copied verbatim from scripts/live's
livetest/plugin.py so that a value like ${TID_ORACLE} means the same short,
Oracle-safe, collision-resistant hash in both tiers.

INT_SHARED_SERVICES: a later phase will let an external broker bring up shared service
containers once and export their endpoints, the way the (now-deleted)
scripts/integration/services.sh's `it_shared()` mode worked for the old per-operator
bash harnesses. That broker integration is NOT implemented here -- only structured
for: `tokens()` reads every value through `os.getenv(NAME, default)` first, so any env
vars a broker exports simply take priority for free, and `_ensure_up()` short-circuits
(never touches docker) whenever INT_SHARED_SERVICES=1, on the assumption a broker
already owns container lifecycle.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from filelock import FileLock, Timeout

try:
    import yaml
except ImportError:  # pragma: no cover - pyyaml is a declared dependency (requirements.txt)
    yaml = None

# The real SPEC §3/§5 modules the YAML discovery + execution
# pipeline (section 11 below) is wired against. Imported under module aliases (never
# bare `from . import tokens`) so none of them shadow this file's own `tokens` fixture
# or its still-in-use `render`/`SubstitutionError` engine (section 2 below).
from . import manifest as manifest_mod
from . import paths as paths_mod
from . import releases as releases_mod
from . import opartifacts as opartifacts_mod
from . import dbroutes as dbroutes_mod
from . import harness as harness_mod
from . import pgclient as pgclient_mod
from . import waevent as waevent_mod
from . import tokens as tokens_mod
from .tokens import build_tokens as _build_tokens

# PERF_SPEC.md's own YAML tree/pytest wiring (section 12 below). Same module-alias
# convention as above.
from . import perf as perf_mod
from . import perfmanifest as perfmanifest_mod
from . import perfreport as perfreport_mod

_HERE = Path(__file__).resolve().parent
_INTTEST = _HERE


def _collected_int_cases() -> Path:
    """The case root collection reads. striim-test collects SLT_INT_CASES (project_io hands
    paths.int_cases() to the tier child), so a set key is the root, and _PERF_DIR, derived from
    it, stays beside the cases actually collected. Unset, it is this engine's own regression/
    (testpaths), whatever SLT_FRAMEWORK_HOME says."""
    if paths_mod._lookup("SLT_INT_CASES")[1]:
        return paths_mod.int_cases()
    return _HERE.parent / "regression"


_PERF_DIR = _collected_int_cases().parent / "perf"
_PERF_RESULTS_DIR = paths_mod.perf_results_dir()  # JSON report default dir (PERF_SPEC.md §11), git-ignored

# ============================================================================
# 1. TOKEN DERIVATION (copied verbatim from scripts/live/livetest/plugin.py so
#    ${TID}/${TID_ORACLE} mean the same thing in both test tiers)
# ============================================================================


def _parallel(env: dict) -> bool:
    """True when tests execute CONCURRENTLY against the shared cluster, in EITHER model:
    pytest-xdist workers (``PYTEST_XDIST_WORKER`` — the CLI ``-n`` path) OR the console's N
    independent single-test subprocesses (``SLT_PARALLEL`` set, no xdist worker env — spec
    §D.3.2). The concurrency-safety branches (per-test ``${TID}`` DB reset instead of a
    whole-schema wipe that would clobber a sibling; ``ensure_provisioned_once`` bring-up; OP/UDF
    register-once) must engage in BOTH — Phase 2 keyed them on the xdist worker var alone, which
    the console's separate-subprocess model does not set. Serial (neither var) is unchanged; the
    xdist worker case is unchanged (the var is still present)."""
    return bool(env.get("PYTEST_XDIST_WORKER") or env.get("SLT_PARALLEL"))


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")


def _tid_oracle(name: str) -> str:
    """Short, deterministic, collision-resistant per-test identifier for Oracle object
    names (spec §A.1a). ${TID} (the full test-name slug) is too long once combined with
    a table name -- Oracle's CDC/LogMiner layer emits an unparseable "UNSUPPORTED" redo
    record and crashes the app for combined identifiers past a certain length (observed:
    34 chars OK, 35-52 chars CRASH -- exact threshold not pinned down, so this stays
    comfortably short rather than hugging the boundary). A "T" prefix guarantees the
    result starts with a letter (Oracle identifiers can't start with a digit); already
    uppercase, so it also serves as the case-matching form (no separate _UPPER needed)
    for fields string-matched against a live event's metadata.TableName."""
    return "T" + hashlib.sha256(name.encode()).hexdigest()[:9].upper()


# ============================================================================
# 2. TOKEN SUBSTITUTION (for rendering sql/*/templates.sql-style ${TOKEN} files)
# ============================================================================

_TOKEN_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class SubstitutionError(Exception):
    """Raised when token substitution fails due to missing tokens."""
    pass


def missing_tokens(text: str, tokens: dict) -> list:
    names = {m.group(1) for m in _TOKEN_RE.finditer(text)}
    return sorted(n for n in names if n not in tokens)


def render(text: str, tokens: dict) -> str:
    """Substitute ${TOKEN} placeholders (as used by sql/*/templates.sql) from `tokens`."""
    missing = missing_tokens(text, tokens)
    if missing:
        raise SubstitutionError(f"missing token values: {', '.join(missing)}")
    return _TOKEN_RE.sub(lambda m: str(tokens[m.group(1)]), text)


# ============================================================================
# 3. service.yaml LOADING + TOKEN DEFAULTS
# ============================================================================

# Hardcoded fallbacks (matching README.md) used only when a service.yaml is missing
# or unreadable (e.g. PyYAML not installed) -- service.yaml is the source of truth
# once present; this just keeps the harness usable before every service.yaml lands.
_FALLBACK_DEFAULTS = {
    "postgres": dict(
        host="localhost", port="15432", dbname="intdb",
        admin_user="postgres", admin_password="striim",
        source_user="qasource", source_password="striim", source_schema="qasource",
        target_user="qatarget", target_password="striim", target_schema="qatarget",
    ),
    "oracle": dict(
        host="localhost", port="11521", service="FREEPDB1",
        source_user="qasource", source_password="striim", source_schema="QASOURCE",
        target_user="qatarget", target_password="striim", target_schema="QATARGET",
    ),
    "spanner": dict(
        host="localhost", port="19010", admin_port="19020",
        project="test-project", instance="test-inst", gsql_db="gsql", pg_db="pgdb",
    ),
    # No "gcs" entry: unlike postgres/oracle/spanner, gcs has no legacy
    # _gcs_tokens()-style fixture path calling _docker_defaults("gcs") -- the YAML
    # requires: pipeline resolves it entirely through tokens.py's service.yaml
    # reader, which raises rather than falling back when a service.yaml is
    # missing. Adding an entry here would be unreachable code.
}


def _service_file(name: str, filename: str) -> Path:
    # One file of service `name`, from that profile's single origin, at call time
    # (SLT_INT_SERVICES_DIR, else scripts/integration/services).
    from . import resources as _resources
    return _resources.service_dir(name) / filename


def _load_service_yaml(name: str) -> dict:
    """Best-effort load of services/<name>/service.yaml. Returns {} (never raises) if
    the file, or PyYAML itself, isn't available -- callers fall back to
    _FALLBACK_DEFAULTS, which keeps the harness usable while a service.yaml is still
    being scaffolded (e.g. spanner has no service.yaml yet -- see services/spanner/)."""
    path = _service_file(name, "service.yaml")
    if yaml is None or not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _checked_fixture_profile(name: str) -> None:
    """The configuration/token fixtures publish tokens only for a COMPLETE profile.
    Read-only checked selection from the origin _load_service_yaml reads: a profile directory
    missing service.yaml, its compose file or an init file is a named
    inttest.resources.ResourceError. _FALLBACK_DEFAULTS remain only for a service with no
    profile directory at all. No docker, no lifecycle."""
    from . import resources as _resources
    if not _resources.service_dir(name).is_dir():
        return
    _resources.select_profile(name)


def _docker_defaults(name: str) -> dict:
    """`docker_defaults` with `docker_env`'s published-host-port overrides applied.

    The `docker_env` layer is the same one `tokens.py::_resolve_service_base` applies for
    the YAML `requires:` pipeline, and it must be applied here too or the two halves of
    the harness resolve one token name (`POSTGRES_PORT`) from two different environments:
    a second stack that sets only `INT_PG_HOST_PORT` would move the container while every
    fixture kept dialing docker_defaults' :15432. The bare-name overrides below
    (`POSTGRES_PORT` and friends) still win over this -- they are the more specific,
    client-side statement of where to dial."""
    _checked_fixture_profile(name)
    raw = _load_service_yaml(name)
    merged = dict(_FALLBACK_DEFAULTS.get(name, {}))
    merged.update(raw.get("docker_defaults", {}) or {})
    for key, envname in (raw.get("docker_env") or {}).items():
        override = (os.environ.get(envname) or "").strip()
        if override:
            merged[key] = override
    return merged


def _require_service_opt_in(name: str) -> None:
    """Skip if `name`'s service.yaml declares an `opt_in_env` gate that isn't set,
    directly or via the INT_EMULATORS umbrella -- mirrors scripts/live/livetest/
    plugin.py's identical SLT_SPANNER/SLT_EMULATORS gate. Some services need
    heavier / side-effecting provisioning (Spanner's emulator redirect is
    JVM-global on the cluster and affects non-Spanner tests too), so a test that
    `requires` one stays opt-in rather than provisioning unconditionally."""
    gate = _load_service_yaml(name).get("opt_in_env")
    if gate and not (os.environ.get(gate) or os.environ.get("INT_EMULATORS")):
        pytest.skip(f"set {gate}=1 (or INT_EMULATORS=1) to opt in to the {name!r} service")


def _shared_services() -> bool:
    """True once a Phase-2 broker owns shared-service lifecycle (see module docstring).
    Not implemented here; this is the structural hook a future broker integration
    flips on."""
    return os.environ.get("INT_SHARED_SERVICES", "").strip() == "1"


def _postgres_tokens() -> dict:
    d = _docker_defaults("postgres")
    return {
        "POSTGRES_HOST": os.getenv("POSTGRES_HOST", d.get("host", "localhost")),
        "POSTGRES_PORT": os.getenv("POSTGRES_PORT", str(d.get("port", "15432"))),
        "POSTGRES_DB": os.getenv("POSTGRES_DB", d.get("dbname", "intdb")),
        "POSTGRES_ADMIN_USER": os.getenv("POSTGRES_ADMIN_USER", d.get("admin_user", "postgres")),
        "POSTGRES_ADMIN_PASSWORD": os.getenv("POSTGRES_ADMIN_PASSWORD", d.get("admin_password", "striim")),
        "POSTGRES_SOURCE_USER": os.getenv("POSTGRES_SOURCE_USER", d.get("source_user", "qasource")),
        "POSTGRES_SOURCE_PASSWORD": os.getenv("POSTGRES_SOURCE_PASSWORD", d.get("source_password", "striim")),
        "POSTGRES_SOURCE_SCHEMA": os.getenv("POSTGRES_SOURCE_SCHEMA", d.get("source_schema", "qasource")),
        "POSTGRES_TARGET_USER": os.getenv("POSTGRES_TARGET_USER", d.get("target_user", "qatarget")),
        "POSTGRES_TARGET_PASSWORD": os.getenv("POSTGRES_TARGET_PASSWORD", d.get("target_password", "striim")),
        "POSTGRES_TARGET_SCHEMA": os.getenv("POSTGRES_TARGET_SCHEMA", d.get("target_schema", "qatarget")),
    }


def _oracle_tokens() -> dict:
    d = _docker_defaults("oracle")
    return {
        "ORACLE_HOST": os.getenv("ORACLE_HOST", d.get("host", "localhost")),
        "ORACLE_PORT": os.getenv("ORACLE_PORT", str(d.get("port", "11521"))),
        "ORACLE_SERVICE": os.getenv("ORACLE_SERVICE", d.get("service", "FREEPDB1")),
        "ORACLE_SOURCE_USER": os.getenv("ORACLE_SOURCE_USER", d.get("source_user", "qasource")),
        "ORACLE_SOURCE_PASSWORD": os.getenv("ORACLE_SOURCE_PASSWORD", d.get("source_password", "striim")),
        "ORACLE_SOURCE_SCHEMA": os.getenv("ORACLE_SOURCE_SCHEMA", d.get("source_schema", "QASOURCE")),
        "ORACLE_TARGET_USER": os.getenv("ORACLE_TARGET_USER", d.get("target_user", "qatarget")),
        "ORACLE_TARGET_PASSWORD": os.getenv("ORACLE_TARGET_PASSWORD", d.get("target_password", "striim")),
        "ORACLE_TARGET_SCHEMA": os.getenv("ORACLE_TARGET_SCHEMA", d.get("target_schema", "QATARGET")),
    }


def _spanner_tokens() -> dict:
    d = _docker_defaults("spanner")
    return {
        "SPANNER_HOST": os.getenv("SPANNER_HOST", d.get("host", "localhost")),
        "SPANNER_GRPC_PORT": os.getenv("SPANNER_GRPC_PORT", str(d.get("port", "19010"))),
        "SPANNER_ADMIN_PORT": os.getenv("SPANNER_ADMIN_PORT", str(d.get("admin_port", "19020"))),
        "SPANNER_PROJECT": os.getenv("SPANNER_PROJECT", d.get("project", "test-project")),
        "SPANNER_INSTANCE": os.getenv("SPANNER_INSTANCE", d.get("instance", "test-inst")),
        "SPANNER_GSQL_DB": os.getenv("SPANNER_GSQL_DB", d.get("gsql_db", "gsql")),
        "SPANNER_PG_DB": os.getenv("SPANNER_PG_DB", d.get("pg_db", "pgdb")),
    }


# ============================================================================
# 4. FIXTURES: Tokens
# ============================================================================


@pytest.fixture
def test_id(request) -> str:
    """${TID}: the slugged test nodeid (e.g. test_lookup_by_key[cache-hit] ->
    test_lookup_by_key_cache_hit)."""
    return _slug(request.node.nodeid)


@pytest.fixture
def tid_oracle(request) -> str:
    """${TID_ORACLE}: the hashed, Oracle-safe per-test id (e.g. T3F9A2B1C4)."""
    return _tid_oracle(request.node.nodeid)


@pytest.fixture(scope="session")
def pg_config() -> dict:
    """Session-scoped so the (session-scoped) connection fixtures below can depend on
    it -- ${TID}/${TID_ORACLE} are per-test and therefore intentionally NOT part of
    this dict; see `tokens` for the merged per-test view."""
    return _postgres_tokens()


@pytest.fixture(scope="session")
def ora_config() -> dict:
    return _oracle_tokens()


@pytest.fixture(scope="session")
def spanner_config() -> dict:
    return _spanner_tokens()


@pytest.fixture
def tokens(test_id, tid_oracle, pg_config, ora_config, spanner_config) -> dict:
    """All tokens for this test: the per-test TID/TID_ORACLE plus every service's
    connection/schema tokens (POSTGRES_*, ORACLE_*, SPANNER_*)."""
    merged = {"TID": test_id, "TID_ORACLE": tid_oracle}
    merged.update(pg_config)
    merged.update(ora_config)
    merged.update(spanner_config)
    return merged


# ============================================================================
# 5. Docker Compose lifecycle (session-scoped, FileLock-coordinated across xdist
#    workers) -- see requirement 3/6 in the task: only one worker actually runs
#    `docker compose up`/`down`; everyone else reuses the services.
# ============================================================================


try:
    from . import services as _docker_mod
except ImportError:
    _docker_mod = None

# Prefix-scoped via services.state_name: these describe what THIS stack started, and
# INT_STACK_PREFIX is what separates two stacks in one checkout. See its docstring.
def _int_state_name(base: str) -> str:
    # services is imported LAZILY above (it is optional -- _docker_mod may be None when
    # the docker deps are absent), so resolve the scoping the same way rather than at
    # module import. Falling back to the bare name keeps a deps-less checkout working
    # exactly as it did before; it only loses prefix scoping, which such a checkout is
    # not using anyway since it cannot bring containers up.
    if _docker_mod is not None:
        return _docker_mod.state_name(base)
    return base


def _state_root() -> Path:
    # Coordination state at call time: SLT_STATE_DIR, else scripts/integration. The same
    # root services.py and cli.py use, so the started registry and compose lock stay shared.
    from . import resources as _resources
    return _resources.state_root()


def _started_registry() -> Path:
    return _state_root() / _int_state_name(".int-services-up.json")


def _compose_lock_path() -> Path:
    return _state_root() / _int_state_name(".int-compose.lock")


def _op_build_lock_path(op_jar_ref: str) -> Path:
    # One lock per module, so different modules still build in parallel.
    return _state_root() / _int_state_name(
        f".int-op-build-{opartifacts_mod.module_name(op_jar_ref)}.lock")


def _session_lock_path() -> Path:
    # Prefix-scoped like every other state file here: INT_STACK_PREFIX is what separates
    # two DELIBERATE stacks in one checkout, and those are exactly the concurrent sessions
    # that are safe -- they own different containers and different state files.
    return _state_root() / _int_state_name(".int-session.lock")


#: Held for the whole session by the controller; see `_acquire_session_lock`. Module-level
#: rather than a local, because the OS lock lives only as long as this object does.
_session_lock = None


def _acquire_session_lock(config) -> None:
    """Refuse a SECOND concurrent pytest session against this checkout's services.

    WHY THIS IS NOT PARANOIA. In a serial run `${TID}` is the EMPTY STRING
    (`tokens.isolation_tokens`), so every session addresses the SAME fixed table names in
    the SAME shared schemas. Two sessions therefore drop, recreate and truncate each
    other's fixtures mid-test. Reproduced 2026-08-29 by running a full regression beside a
    second session; the failures landed in whichever module happened to be executing, and
    said things like:

        ORA-00001: unique constraint (QATARGET.SYS_C008874) violated
        target table qatarget.customers could not be found, or reported no columns
        event 0: userdata is missing key 'PRODUCT_NAME'

    None of which names the real cause. Three different modules were blamed across three
    runs before the pattern was spotted, and one of those wrong guesses reached a merged
    document. A one-line refusal here is worth more than any amount of after-the-fact
    diagnosis.

    IT APPLIES TO A PARALLEL RUN TOO. `SLT_PARALLEL=1` gives each test a unique `${TID}`,
    which fixes the table collision but NOT the second hazard: service teardown is
    per-service, not per-session (`services.ensure_up` registers only what it STARTED), so
    a session that borrowed an already-running service has it destroyed -- `docker compose
    down -v`, volume included -- when the owner finishes.

    Skipped for `--collect-only` (it touches no service), for xdist workers (they are one
    session with the controller), under `INT_SHARED_SERVICES` (a broker owns lifecycle, so
    concurrency is the design), and under `INT_ALLOW_CONCURRENT_SESSIONS` for anyone who
    has read the above and means it.
    """
    global _session_lock
    if _session_lock is not None:
        # Already held by THIS process, so this is the same session asking twice -- pytest
        # calling configure again, or a test driving this hook directly. One process is one
        # session; re-acquiring a second fd on the same file would deadlock against
        # ourselves and report it as a foreign session.
        return
    if getattr(config.option, "collectonly", False):
        # --collect-only parses YAML and touches no service (provisioning happens in
        # runtest), so it is safe beside a running suite. Refusing it would block a
        # read-only "what would run?" on a colleague's long regression, for no gain.
        return
    if os.environ.get("PYTEST_XDIST_WORKER"):
        return                      # a worker shares the controller's session
    if _shared_services() or os.environ.get("INT_ALLOW_CONCURRENT_SESSIONS"):
        return
    lock = FileLock(str(_session_lock_path()), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        raise pytest.UsageError(
            f"another integration-test session is already running against this checkout "
            f"({_session_lock_path().name}).\n"
            f"\n"
            f"They cannot share it. In a serial run ${{TID}} is EMPTY, so both sessions "
            f"address the same fixed table names in the same schemas and will drop, "
            f"recreate and truncate each other's fixtures mid-test -- surfacing as a "
            f"unique-constraint violation, a missing table, or a missing lookup row in "
            f"whichever module happens to be running, never as the real cause.\n"
            f"\n"
            f"Wait for it to finish, or -- if you genuinely want two stacks -- give this "
            f"one its own INT_STACK_PREFIX so it owns separate containers and state. "
            f"INT_ALLOW_CONCURRENT_SESSIONS=1 bypasses this check."
        ) from None
    _session_lock = lock


def _release_session_lock() -> None:
    """Drops the session lock if this process holds it. Idempotent."""
    global _session_lock
    if _session_lock is not None:
        try:
            _session_lock.release()
        except Exception:  # noqa: BLE001 - teardown must not mask the run's real outcome
            pass
        _session_lock = None


def _read_started() -> set:
    try:
        return set(json.loads(_started_registry().read_text()))
    except (FileNotFoundError, ValueError):
        return set()


def _write_started(names: set) -> None:
    _started_registry().write_text(json.dumps(sorted(names)))


@pytest.fixture(scope="session")
def compose_lock() -> FileLock:
    """FileLock coordinating `docker compose up`/`down` across pytest-xdist workers
    (each worker is a separate process with its own Python state, so an in-memory
    lock/set would not coordinate them). Reentrant within one process/thread, so
    nesting `with compose_lock:` around a call into services.docker (which also
    locks internally) is safe.

    No timeout, as in the live tier and cli.py: a waiter blocks until the holder's bring-up
    ends. A 120 s wait failed workers queued behind a Teradata boot (up to ~2 h under
    emulation), and a timeout adds no protection -- a hung holder hangs the run either way."""
    return FileLock(str(_compose_lock_path()))


def _ensure_up(name: str, lock: FileLock) -> bool:
    """Bring up the named service's docker-compose stack, once per host (the lock
    serializes workers; `docker compose up -d --wait` is itself idempotent against an
    already-healthy stack). Registers `name` in the started-services registry ONLY if
    this call is the one that actually brought it up from down -- a service already
    running before this call (a developer's manual `docker compose up`, a prior kept
    run) is left out of the registry entirely, so `pytest_sessionfinish` below doesn't
    tear down something this session didn't start. Returns True iff this call did the
    registering (mirrors `services.ensure_up`'s return contract, even though no
    current caller here consumes it)."""
    # Whole-profile preflight from ONE origin BEFORE every shortcut (broker-owned lifecycle,
    # missing-compose skip, docker): incomplete resources are a named
    # inttest.resources.ResourceError, never a skip. An unavailable docker still skips below.
    from . import resources as _resources
    _resources.select_profile(name)
    if _shared_services():
        return False  # Phase 2: a broker already owns lifecycle -- see module docstring.
    if _docker_mod is None:
        pytest.skip(f"services/docker.py not found; cannot bring up the {name!r} service")
    compose_file = _service_file(name, "compose.yaml")
    if not compose_file.exists():
        pytest.skip(
            f"services/{name}/compose.yaml does not exist yet (Phase 2) -- "
            f"cannot bring up the {name!r} integration service"
        )
    with lock:
        already_up = _docker_mod.is_up(name)
        try:
            _docker_mod.compose_up(name, lock)
        except FileNotFoundError as e:
            pytest.skip(f"docker not available: {e}")
        except subprocess.CalledProcessError as e:
            # The fixture-driven path (the YAML path is _provision_requires): a
            # declared service that will not start is the environment breaking, not a case
            # that does not apply. A skip here made a whole fixture-gated test module read
            # green with nothing run.
            pytest.fail(f"`docker compose up` failed for {name!r}: {e}")
        if already_up:
            return False
        started = _read_started()
        started.add(name)
        _write_started(started)
        return True


def pytest_unconfigure(config):
    """Release the session lock. Separate from `pytest_sessionfinish` on purpose: that hook
    returns early for workers and under INT_SHARED_SERVICES, and does not run at all when collection fails,
    whereas the lock must be dropped on every exit path."""
    _release_session_lock()


def pytest_sessionfinish(session, exitstatus):
    """Tear down every service THIS host brought up -- but only from the xdist
    controller (workers each reach sessionfinish independently and must not race each
    other's teardown), and never under INT_SHARED_SERVICES (a broker owns lifecycle).

    Under INT_KEEP_SERVICES (fast local iteration without re-provisioning containers
    between runs), the containers are deliberately left running -- but the registry
    is still cleared either way: keeping a service running hands its lifecycle over
    to whoever kept it (the next run's `ensure_up`/`_ensure_up` will correctly see it
    as already-up and not re-register it), so a stale registry entry pointing at a
    service THIS session no longer owns must not survive to be misread as "this
    session started it" by a future `pytest_sessionfinish`."""
    if _shared_services():
        return
    if os.environ.get("PYTEST_XDIST_WORKER"):
        return  # a worker, not the controller -- see live's plugin.py for the same guard
    if _docker_mod is None or not _started_registry().exists():
        return
    lock = FileLock(str(_compose_lock_path()))
    with lock:
        started = _read_started()
        if not os.environ.get("INT_KEEP_SERVICES"):
            for name in started:
                try:
                    _docker_mod.compose_down(name, lock)
                except Exception as e:  # noqa: BLE001 - teardown must not mask the run's real outcome
                    print(f"[integration] WARNING: failed to tear down {name!r}: {e!r} "
                          f"(container may still be running)")
        try:
            _started_registry().unlink()
        except FileNotFoundError:
            pass


# ============================================================================
# 6. FIXTURES: Database connections (session-scoped, reused across tests)
# ============================================================================


def _connect_retry(connect, attempts: int = 30, delay: float = 2.0):
    """Retry a connect() callable through container cold-start transients (a
    healthcheck can report ready a beat before the first real client connection
    succeeds -- mirrors scripts/live's PgAdmin/OraAdmin retry loops)."""
    last = None
    for _ in range(attempts):
        try:
            return connect()
        except Exception as e:  # noqa: BLE001 - broad on purpose; re-raised after retries
            last = e
            time.sleep(delay)
    raise RuntimeError(f"could not connect after {attempts} attempts: {last!r}") from last


@pytest.fixture(scope="session")
def pg_admin(pg_config, compose_lock):
    """Postgres connection as the admin (superuser) role. QASOURCE/QATARGET are NOT
    granted CREATE SCHEMA (see services/postgres/init.sql), so schema-level DDL for
    `pg_schema`/`cleanup_postgres` goes through this connection instead."""
    import psycopg2

    _ensure_up("postgres", compose_lock)
    conn = _connect_retry(lambda: psycopg2.connect(
        host=pg_config["POSTGRES_HOST"], port=int(pg_config["POSTGRES_PORT"]),
        dbname=pg_config["POSTGRES_DB"],
        user=pg_config["POSTGRES_ADMIN_USER"], password=pg_config["POSTGRES_ADMIN_PASSWORD"],
    ))
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def pg_qasource(pg_config, compose_lock):
    """Postgres qasource connection (owns the qasource schema)."""
    import psycopg2

    _ensure_up("postgres", compose_lock)
    conn = _connect_retry(lambda: psycopg2.connect(
        host=pg_config["POSTGRES_HOST"], port=int(pg_config["POSTGRES_PORT"]),
        dbname=pg_config["POSTGRES_DB"],
        user=pg_config["POSTGRES_SOURCE_USER"], password=pg_config["POSTGRES_SOURCE_PASSWORD"],
    ))
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def pg_qatarget(pg_config, compose_lock):
    """Postgres qatarget connection (owns the qatarget schema)."""
    import psycopg2

    _ensure_up("postgres", compose_lock)
    conn = _connect_retry(lambda: psycopg2.connect(
        host=pg_config["POSTGRES_HOST"], port=int(pg_config["POSTGRES_PORT"]),
        dbname=pg_config["POSTGRES_DB"],
        user=pg_config["POSTGRES_TARGET_USER"], password=pg_config["POSTGRES_TARGET_PASSWORD"],
    ))
    yield conn
    conn.close()


def _oracle_dsn(cfg: dict) -> str:
    return f'{cfg["ORACLE_HOST"]}:{int(cfg["ORACLE_PORT"])}/{cfg["ORACLE_SERVICE"]}'


@pytest.fixture(scope="session")
def ora_qasource(ora_config, compose_lock):
    """Oracle QASOURCE connection. python-oracledb in default THIN mode (pure Python,
    no Instant Client needed) -- same as scripts/live/livetest/oraadmin.py."""
    import oracledb

    _ensure_up("oracle", compose_lock)
    conn = _connect_retry(lambda: oracledb.connect(
        user=ora_config["ORACLE_SOURCE_USER"], password=ora_config["ORACLE_SOURCE_PASSWORD"],
        dsn=_oracle_dsn(ora_config),
    ), attempts=45, delay=5.0)  # Oracle's first boot is slow (1-2 min); wider budget than Postgres
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def ora_qatarget(ora_config, compose_lock):
    """Oracle QATARGET connection."""
    import oracledb

    _ensure_up("oracle", compose_lock)
    conn = _connect_retry(lambda: oracledb.connect(
        user=ora_config["ORACLE_TARGET_USER"], password=ora_config["ORACLE_TARGET_PASSWORD"],
        dsn=_oracle_dsn(ora_config),
    ), attempts=45, delay=5.0)
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def spanner_client(spanner_config, compose_lock):
    """Spanner admin client, pointed at the emulator. Brought up the same way as
    pg_admin/ora_qasource, except opt-in: `_require_service_opt_in` skips unless
    INT_SPANNER=1 (or INT_EMULATORS=1) is set -- Spanner's emulator
    redirect is JVM-global on the cluster and affects non-Spanner tests too.

    `SPANNER_EMULATOR_HOST` is process-global and first-writer-wins (`setdefault`):
    this fixture's `spanner_config` (`SPANNER_HOST`/`SPANNER_GRPC_PORT`) and the YAML
    pipeline's `${SPANNER_*}` tokens (`INT_SPANNER_HOST`/`INT_SPANNER_PORT` overrides)
    are two independently-resolved sources for the same value -- whichever code path
    runs first in this process pins the endpoint for the rest of it. Harmless as long
    as both resolve to the same emulator (the common case), but an env override that
    only one path honors won't take effect if the other path wins the race."""
    _require_service_opt_in("spanner")
    _ensure_up("spanner", compose_lock)
    os.environ.setdefault(
        "SPANNER_EMULATOR_HOST",
        f"{spanner_config['SPANNER_HOST']}:{spanner_config['SPANNER_GRPC_PORT']}",
    )
    from google.cloud import spanner

    client = spanner.Client(project=spanner_config["SPANNER_PROJECT"])
    yield client


@pytest.fixture(scope="session")
def spanner_admins(spanner_config, compose_lock):
    """{"spanner-google": SpannerAdmin, "spanner-postgres": SpannerAdmin}, instance +
    both databases ensured. The fixture-side counterpart of
    pg_qasource/ora_qasource, for standalone tests (tests/test_spanner_live.py) and
    `cleanup_spanner` below; the YAML `requires:` pipeline instead calls
    `spanneradmin.admins_for` directly from `IntYamlItem.runtest` (step 4b below),
    since it works off `self.tokens`, not this fixture's legacy `_spanner_tokens()`
    dict. Opt-in: same `_require_service_opt_in` gate as `spanner_client`."""
    _require_service_opt_in("spanner")
    _ensure_up("spanner", compose_lock)
    from . import spanneradmin as spanneradmin_mod

    admins = spanneradmin_mod.admins_for(spanner_config)
    return {route: admin for admin, route in admins}


# ============================================================================
# 7. FIXTURES: Schema names
# ============================================================================


@pytest.fixture
def pg_schema(test_id) -> str:
    """A dedicated per-test Postgres schema name (e.g. slt_test_lookup_by_key),
    for a test that wants full isolation beyond TID-prefixed table names inside the
    fixed qasource/qatarget schemas. A test that wants this schema must CREATE it
    itself (via `pg_admin`, which has the privilege qasource/qatarget lack) --
    `cleanup_postgres` only guarantees the DROP, so creating-and-not-using it is a
    harmless no-op cleanup."""
    return f"slt_{test_id}"


@pytest.fixture
def ora_schema(tid_oracle) -> str:
    """A per-test Oracle TABLE-NAME PREFIX (e.g. T3F9A2B1C4_), not a real Oracle user.
    QASOURCE/QATARGET (sql/oracle/init.sql) are not granted CREATE USER/DROP USER ANY,
    and no SYSTEM/admin credentials are wired into services/oracle/service.yaml, so a
    real per-test Oracle schema/user isn't provisionable yet (Phase 2 TODO: add an
    admin connection once that's needed). Until then this mirrors
    sql/oracle/templates.sql's ${TID_ORACLE} convention: prepend it to a table name
    inside the fixed QASOURCE/QATARGET schemas, and `cleanup_oracle` drops anything
    under that prefix from both."""
    return f"{tid_oracle}_"


# ============================================================================
# 8. Pass/fail tracking (so cleanup fixtures can skip on failure, for debugging)
# ============================================================================


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Stash each phase's report on the item as rep_<when> (the standard pytest
    recipe) so a fixture's teardown can check request.node.rep_call/.rep_setup."""
    outcome = yield
    rep = outcome.get_result()
    setattr(item, f"rep_{rep.when}", rep)


def _test_failed(request) -> bool:
    for when in ("setup", "call"):
        rep = getattr(request.node, f"rep_{when}", None)
        if rep is not None and rep.failed:
            return True
    return False


def _keep_resources(request) -> bool:
    if _test_failed(request):
        return True  # keep on failure, for debugging
    if request.config.getoption("--slt-keep-resources", False):
        return True
    if os.environ.get("INT_KEEP_RESOURCES"):
        return True
    return False


def pytest_addoption(parser):
    parser.addoption(
        "--slt-keep-resources", action="store_true", default=False,
        help="keep per-test Postgres schemas / Oracle tables after the run, "
             "for manual inspection (also settable via INT_KEEP_RESOURCES=1)",
    )
    parser.addoption(
        "--perf", action="store_true", default=False,
        help="run tests in performance mode (PERF_SPEC.md) -- only tests under "
             "scripts/integration/perf/ (a test.yaml's disabled: key still skips it, "
             "same as in integration mode)",
    )
    parser.addoption(
        "--perf-reverse", action="store_true", default=False,
        help="§115.6 ORDERING CONTROL: run this case's variants/matrix runs in REVERSE "
             "declaration order. Same runs, same run ids, opposite sequence -- so a second "
             "pass pairs against the first and a difference that changes sign was the running "
             "order, not the change under test. Adjacent permutations have differed by 4-10%% "
             "on order alone, so any result under ~10%% needs this",
    )
    parser.addoption(
        "--perf-json", action="store", default=None, metavar="PATH",
        help="write the performance JSON report to PATH "
             "(if omitted, no JSON report is written -- only the console block prints)",
    )


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _check_ident(name: str) -> str:
    if not _IDENT_RE.match(name):
        raise ValueError(f"unsafe identifier: {name!r}")
    return name


# ============================================================================
# 9. FIXTURES: Auto-cleanup
#
# Both are globally autouse but marker-gated internally (via
# request.node.get_closest_marker + request.getfixturevalue) so a test that never
# touches Postgres/Oracle never forces those services up just to run an unmarked
# no-op teardown -- only a test carrying @pytest.mark.postgres / .oracle pays the
# cost of actually connecting.
# ============================================================================


@pytest.fixture(autouse=True)
def cleanup_postgres(request):
    """Drop this test's dedicated schema (`pg_schema`) after a @pytest.mark.postgres
    test, unless it failed (kept for debugging) or --slt-keep-resources/
    INT_KEEP_RESOURCES was set. A no-op if the test never created that schema.

    pg_admin/pg_schema are resolved with request.getfixturevalue() during SETUP
    (before the yield), not at teardown: pytest deprecates (and pytest 10 will
    remove) resolving a not-yet-requested fixture from inside a teardown. Gating the
    getfixturevalue() calls on the marker check, done here in setup where markers are
    already known, is what keeps an unmarked test from paying for a Postgres
    connection at all -- postgres-marked tests eagerly grab both now; unmarked tests
    never touch either fixture."""
    if not request.node.get_closest_marker("postgres"):
        yield
        return
    admin = request.getfixturevalue("pg_admin")
    schema = request.getfixturevalue("pg_schema")
    yield
    if _keep_resources(request):
        return
    schema = _check_ident(schema)
    with admin.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    admin.commit()


def _drop_oracle_tables_with_prefix(conn, prefix: str) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT table_name FROM user_tables WHERE table_name LIKE :p", [f"{prefix}%"])
        names = [row[0] for row in cur.fetchall()]
        for name in names:
            try:
                cur.execute(f'DROP TABLE "{name}" CASCADE CONSTRAINTS PURGE')
            except Exception as e:  # noqa: BLE001 - one bad drop must not orphan the rest
                print(f"[integration] WARNING: failed to drop Oracle table {name}: {e!r}")
    conn.commit()


@pytest.fixture(autouse=True)
def cleanup_oracle(request):
    """Drop every QASOURCE/QATARGET table prefixed with this test's `ora_schema`
    after a @pytest.mark.oracle test (see `ora_schema` for why this drops TABLES, not
    a USER, in Phase 1), unless it failed or resources were asked to be kept.

    Same setup-time (pre-yield) getfixturevalue() pattern as `cleanup_postgres` --
    see its docstring for why: it's the non-deprecated way to conditionally resolve a
    fixture only for oracle-marked tests."""
    if not request.node.get_closest_marker("oracle"):
        yield
        return
    prefix = request.getfixturevalue("ora_schema")
    qasource = request.getfixturevalue("ora_qasource")
    qatarget = request.getfixturevalue("ora_qatarget")
    yield
    if _keep_resources(request):
        return
    prefix = _check_ident(prefix.rstrip("_"))
    _drop_oracle_tables_with_prefix(qasource, prefix)
    _drop_oracle_tables_with_prefix(qatarget, prefix)


@pytest.fixture(autouse=True)
def cleanup_spanner(request):
    """Drop this test's `${TID}`-prefixed tables (both dialect databases) after a
    @pytest.mark.spanner test, unless it failed or resources were asked to be kept.
    Spanner is `isolation: none` (one shared instance/databases) -- there
    is no per-test schema/user to drop, so this prefixes by `test_id` instead,
    mirroring `cleanup_oracle`'s prefix-based table drop rather than
    `cleanup_postgres`'s whole-schema drop.

    Same setup-time (pre-yield) getfixturevalue() pattern as cleanup_postgres/
    cleanup_oracle -- see cleanup_postgres's docstring for why."""
    if not request.node.get_closest_marker("spanner"):
        yield
        return
    admins = request.getfixturevalue("spanner_admins")
    prefix = request.getfixturevalue("test_id")
    yield
    if _keep_resources(request):
        return
    for admin in admins.values():
        admin.drop_test_tables(prefix)


# ============================================================================
# 9b. IMPERATIVE DB ISOLATION FOR IntYamlItem (SPEC §13, §17 #4)
#
# IntYamlItem (section 11 below) is a raw pytest.Item, not pytest.Function -- pytest
# fills NO fixtures for it, so cleanup_postgres/cleanup_oracle (section 9 above)
# never fire for a YAML test. This section revives that cleanup intent
# IMPERATIVELY: IntYamlItem.runtest() calls these helpers directly (no fixture
# system involved) and its own try/finally decides whether to actually drop
# anything, using the same _keep_resources-style gate (failed / --slt-keep-resources
# / INT_KEEP_RESOURCES).
#
#   - Postgres: fixed qasource/qatarget schemas (pgclient.PgAdmin.ensure_setup,
#     idempotent and safe under concurrent callers -- docs/internals/INTEGRATION-ENGINE.md#token-isolation
#     §4.1), objects distinguished by a per-test `${TID}` table-name prefix rather
#     than a per-test schema -- the same model scripts/live's own Postgres uses,
#     and the same model this tier already uses for Oracle/Spanner below. Serial:
#     ensure_setup + a whole-schema reset (PgAdmin.reset_schemas) gives a clean
#     slate; parallel: that whole-schema DROP would clobber a sibling worker's
#     tables, so wipe only THIS test's `${TID}`-prefixed tables
#     (PgAdmin.reset_test_objects). ddl:/seed: keep running through
#     dbroutes.run_sql_file's existing `SET search_path` (unchanged) --
#     ${POSTGRES_SOURCE_SCHEMA}/${POSTGRES_TARGET_SCHEMA} are no longer overridden
#     per-test; they stay at their fixed qasource/qatarget defaults.
#   - Oracle: no CREATE SCHEMA/CREATE USER grant is available (see `ora_schema`'s
#     docstring above), so ddl:/seed: keep running in the route user's own
#     QASOURCE/QATARGET schema, table-name-prefixed with `${TID_ORACLE}` --
#     `${ORACLE_SOURCE_SCHEMA}.${TID_ORACLE}CUSTOMERS`, the same convention
#     services/oracle/sql/templates.sql and scripts/live already use. `${TID_ORACLE}`
#     keeps its documented SPEC §6 serial-empty/parallel-nonempty contract: a serial
#     run's table stays unprefixed (same as every other requires:-tagged fixture run
#     serially, self-healing via DROP TABLE IF EXISTS in ddl:), and teardown only
#     drops (revives `_drop_oracle_tables_with_prefix` above) a PARALLEL run's
#     `${TID_ORACLE}`-prefixed tables -- mirroring Spanner's identical serial-empty/
#     parallel-nonempty prefix-drop below, and now Postgres's too.
#
# All three only run when the test's requires: actually names the service -- a
# smoke/no-ddl test with no requires: touches neither, doing nothing DB-side.
# ============================================================================


def _pg_admin(tokens: dict, role: str = "source") -> pgclient_mod.PgAdmin:
    """Builds a `pgclient.PgAdmin` from the per-test token table -- the shared
    construction site for `IntYamlItem`/`PerfYamlItem`'s setup and teardown, so
    the two Item classes cannot drift on how they build it."""
    return pgclient_mod.PgAdmin(pgclient_mod.dsn_from_tokens(tokens), role=role)


def _ensure_spanner_databases(tokens: dict) -> None:
    """Ensure the Spanner instance + both dialect databases exist for this test's
    `${SPANNER_*}` tokens. Unlike Postgres (fixed qasource/qatarget
    schemas + per-test `${TID}` table prefix) and Oracle (per-test table
    prefix), Spanner is `isolation: none` -- one fixed instance +
    two fixed-dialect databases. Must run even for a test with no `ddl:`/`seed:`
    (the operator's own `${SPANNER_*_URL}` JDBC connection still needs the
    database to exist, and `dbroutes.run_sql_text` never runs for such a test),
    so this is called unconditionally from `runtest()` step 4b rather than lazily
    from `_run_spanner`.

    Deliberately NOT memoized (unlike an earlier version of this function): a
    memo keyed on token identity would go stale the moment an emulator is torn
    down and restarted within one process (`ensure_up_for_test` can do exactly
    that), reporting "already provisioned" for an instance that no longer exists.
    `admins_for` against an already-provisioned emulator is a handful of cheap
    local RPCs -- `_teardown_db_isolation` below already calls it on every
    teardown for the same reason, so this is consistent with that call, not an
    added cost relative to it."""
    from . import spanneradmin as spanneradmin_mod

    spanneradmin_mod.admins_for(tokens)


def _ensure_gcs_bucket(tokens: dict) -> None:
    """Ensure this test's `${GCS_BUCKET}` exists. Like Spanner's instance + databases, GCS
    is `isolation: none` -- one fixed bucket, not per-test -- so this must run even
    for a test with no `ddl:`/`seed:`: a real upload needs the bucket to exist
    first, and fake-gcs-server does not auto-create one on first PUT (verified
    against `scripts/live/livetest/gcsadmin.py::GcsAdmin.ensure_bucket`, which the
    live tier calls explicitly for the same reason). Deliberately NOT memoized,
    for the same reason `_ensure_spanner_databases` isn't: a memo would go stale if
    the emulator is torn down and restarted within one process. `ensure_bucket` is
    a single cheap existence check once the bucket already exists (the common
    case after the first test), so this is not a real added cost."""
    from . import gcsadmin as gcsadmin_mod

    gcsadmin_mod.ensure_bucket(tokens)


def _run_label(variant, overrides) -> str:
    """`[postgres, UseUpsert=true]` — which run a failure came from.

    With variants and permutations composed, a case can execute thirty-two times behind ONE
    expectation. A mismatch that does not say which engine and which flags produced it sends the
    reader off to re-run them by hand, and the whole construct is that only one of them differs.
    """
    parts = []
    if variant is not None:
        parts.append(variant.name)
    parts.extend(f"{k}={v}" for k, v in (overrides or {}).items())
    return "" if not parts else " [" + ", ".join(parts) + "]"


def _id_safe(value) -> str:
    """Escapes the characters `_run_id` uses as separators, so a value containing one cannot
    forge a different run's id. `~` is the escape because it is not a separator here and is
    safe in both a pytest node id and a filename."""
    return (str(value).replace("~", "~7e").replace("-", "~2d").replace("=", "~3d")
            .replace("[", "(").replace("]", ")"))


def _run_id(variant, overrides) -> str:
    """`[postgres-UseUpsert=false]` — the node-id suffix naming one perf run.

    Built from the parts, NOT by reformatting `_run_label`'s display string: a property
    VALUE may itself contain the separators that form would have to strip (`BatchPolicy:
    'EventCount:20, Interval:600'` is the ordinary spelling), and rewriting them inside the
    value makes two different runs read as the same node.

    Empty for a case using neither axis, which is what keeps every pre-existing perf case's
    node id byte-identical to what it was before the two axes arrived.

    ⚠ MUST BE INJECTIVE, and an earlier version was not. It joined `k=v` parts with `-` and
    said "uniqueness comes from runs() yielding each pair once" -- true of the PAIRS, false of
    their RENDERINGS: a value may itself contain `-` and `=`, so
    `matrix: {A: ['x', 'x-B=y'], B: ['z', 'y-B=z']}` produced two items with the identical id
    `[A=x-B=y-B=z]`. Identical node ids cannot be addressed by node-id selection or
    `--deselect`, and `report_key_for` builds on this, so the two runs also collided on report
    filename whenever they finished in the same second -- exactly the loss the run id exists to
    prevent. `_id_safe` escapes the separators so the rendering is injective too.
    """
    parts = []
    if variant is not None:
        parts.append(variant.name)
    # `[`/`]` would nest inside pytest's own id brackets; `-` separates because a value may
    # contain `,` and a name may not contain `-`... which is not guaranteed, so this is a
    # READABILITY aid. Uniqueness comes from runs() yielding each pair once.
    parts.extend(f"{k}={_id_safe(v)}" for k, v in (overrides or {}).items())
    return "" if not parts else "[" + "-".join(parts) + "]"


def _jsonable_rows(rows: list) -> list:
    """Normalize driver-returned rows to values a JSON `match` fixture can state exactly.

    Every conversion here is chosen so the fixture stays LOSSLESS, because this tier's job is
    to catch what the writer handed the database being wrong:

    * `Decimal` becomes a STRING, never a float. A writer that refuses a value not fitting the
      column's scale is asserting about exact digits, and `float(Decimal("1.10"))` throws away
      both the trailing zero and, past 17 digits, the value.
    * `datetime`/`date`/`time` become ISO 8601, keeping any offset. A temporal case compares the
      value READ BACK against the value written, so an offset silently dropped here would hide
      exactly the class of defect the round trip exists to find.
    * `bytes` become lowercase hex, so a fixture can state them without an encoding guess.
    """
    import datetime as _dt
    import decimal

    def value(v):
        if v is None or isinstance(v, (bool, int, float, str)):
            return v
        if isinstance(v, decimal.Decimal):
            return str(v)
        if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
            return v.isoformat()
        if isinstance(v, _dt.timedelta):
            return str(v)
        if isinstance(v, memoryview):
            v = v.tobytes()
        if isinstance(v, (bytes, bytearray)):
            return bytes(v).hex()
        if isinstance(v, (list, tuple)):
            return [value(item) for item in v]
        return str(v)

    return [[value(cell) for cell in row] for row in rows]


def _prune_gcs_objects(tokens: dict, keys: list) -> None:
    """Delete the object keys an `assert.gcs_objects:` block is about to check, BEFORE
    the operator runs. This is a correctness requirement, not hygiene:
    `services/gcs/README.md`'s bucket lifecycle keeps ONE fixed bucket that is never
    cleared, and `${TID}` is EMPTY in a serial run (`tokens.py`'s `isolation_tokens`),
    so `${TID}`/`FolderName` isolation makes a key unique across CONCURRENT xdist
    workers but NOT across sequential runs of the same case. Without this prune, a
    green previous run leaves a real object at exactly the key this test asserts, and
    the post-run existence check would pass even if THIS run uploaded nothing -- or
    uploaded to a different key. Only the named keys are deleted (never a prefix,
    never the bucket), so no other test's objects are affected. Runs regardless of
    any keep-resources flag: those govern teardown, and nothing here deletes anything
    after the test, so a failing run's objects stay for inspection."""
    from . import gcsadmin as gcsadmin_mod

    gcsadmin_mod.delete_objects(tokens, keys)


def _assert_gcs_objects(tokens: dict, name: str, keys: list) -> None:
    """`assert.gcs_objects:`: fail unless every named key now exists in
    `${GCS_BUCKET}`. Deliberately independent of the emitted WAEvent -- a writer op
    computes its `userdata` path string from the same local variable it MEANT to
    upload to, so the two can disagree (e.g. recording one path while uploading to
    another) and only reading the bucket back catches it. Existence only; object
    bytes are never compared."""
    from . import gcsadmin as gcsadmin_mod

    missing = gcsadmin_mod.missing_objects(tokens, keys)
    if missing:
        pytest.fail(
            f"{name}: assert.gcs_objects -- bucket {tokens['GCS_BUCKET']!r} has no object "
            f"at key(s) {missing}. The emitted userdata may still NAME them: this check "
            f"reads the bucket, not the event.")


def jmx_mismatches(spec, snapshot: dict) -> list:
    """`assert.jmx:` against the runner's snapshot (JmxSnapshot.java): one line per failure,
    empty when every attribute matches.

    A snapshot carrying `error` fails every attribute, so a bean that could not be built or
    registered can never pass vacuously. An attribute absent from the snapshot fails too -- a
    typo would otherwise assert nothing. `bool` is compared as `bool` only: Python's True == 1
    would otherwise let `Hits: true` pass on one hit."""
    if not isinstance(snapshot, dict) or snapshot.get("error"):
        reason = snapshot.get("error") if isinstance(snapshot, dict) else snapshot
        return [f"no MBean snapshot: {reason}"]
    actual = snapshot.get("attributes") or {}
    errors = snapshot.get("attributeErrors") or {}
    out = []
    for name, want in spec.attributes.items():
        if name in errors:
            out.append(f"{name}: getter threw {errors[name]}")
            continue
        if name not in actual:
            out.append(f"{name}: not an attribute of {snapshot.get('objectName')} "
                       f"(has {sorted(actual)})")
            continue
        got = actual[name]
        if isinstance(want, dict):
            numeric = isinstance(got, (int, float)) and not isinstance(got, bool)
            if (not numeric or ("min" in want and got < want["min"])
                    or ("max" in want and got > want["max"])):
                out.append(f"{name}: expected {want}, got {got!r}")
        elif isinstance(want, bool) or isinstance(got, bool):
            if type(want) is not type(got) or want != got:
                out.append(f"{name}: expected {want!r}, got {got!r}")
        elif want != got:
            out.append(f"{name}: expected {want!r}, got {got!r}")
    return out


def _drop_oracle_test_prefix(prefix: str) -> None:
    """Drop every QASOURCE/QATARGET table under `prefix` (e.g. ${TID_ORACLE}),
    reviving `_drop_oracle_tables_with_prefix`/`cleanup_oracle` above imperatively.
    `prefix` may carry the trailing "_" separator (as ${TID_ORACLE} does) -- it is
    stripped before the LIKE match, same as `cleanup_oracle` does for `ora_schema`.
    Only called with a non-empty prefix (`_oracle_prefix` stays `None` in a serial
    run, see runtest() step 4b), so this never needs a Spanner-style ""-means-drop-
    everything guard."""
    import oracledb

    prefix = _check_ident(prefix.rstrip("_"))
    cfg = _oracle_tokens()
    dsn = _oracle_dsn(cfg)
    for direction in ("SOURCE", "TARGET"):
        conn = _connect_retry(lambda d=direction: oracledb.connect(
            user=cfg[f"ORACLE_{d}_USER"], password=cfg[f"ORACLE_{d}_PASSWORD"], dsn=dsn,
        ), attempts=5, delay=2.0)
        try:
            _drop_oracle_tables_with_prefix(conn, prefix)
        finally:
            conn.close()


# ============================================================================
# 10. PYTEST CONFIGURATION
# ============================================================================


def pytest_configure(config):
    """Register markers (pytest.ini already lists them under --strict-markers; this
    makes conftest.py self-sufficient too) and enforce the SLT_PARALLEL opt-in for
    parallel execution -- mirrors scripts/live/livetest/plugin.py's identical guard:
    per-test objects are tokenized (${TID}/${TID_ORACLE}) so parallel runs are
    supported, but stay opt-in."""
    os.environ.update(paths_mod.effective_env(os.environ))
    config.addinivalue_line("markers", "integration: integration test (requires Docker services)")
    config.addinivalue_line("markers", "postgres: requires the Postgres integration service")
    config.addinivalue_line("markers", "oracle: requires the Oracle integration service")
    config.addinivalue_line("markers", "spanner: requires the Spanner integration service")
    config.addinivalue_line("markers", "gcs: requires the GCS integration service")
    config.addinivalue_line("markers", "perf: performance test (PERF_SPEC.md, run under --perf)")

    nprocs = getattr(config.option, "numprocesses", None)
    dist = getattr(config.option, "dist", None)
    if nprocs or (dist and dist != "no"):
        if config.getoption("--perf"):
            raise pytest.UsageError(
                "performance mode (--perf) runs SERIALLY, unconditionally (PERF_SPEC.md "
                "§9 'Sequencing') -- concurrent execution would contend for CPU/memory "
                "bandwidth/database connections and invalidate every measurement. "
                "pytest-xdist (-n/--dist) is not allowed under --perf, even with "
                "SLT_PARALLEL=1."
            )
        if not os.environ.get("SLT_PARALLEL"):
            raise pytest.UsageError(
                "the integration-test suite runs SERIALLY by default. Per-test object "
                "names are tokenized (${TID}/${TID_ORACLE}) so parallel runs are "
                "supported, but you must opt in explicitly: set SLT_PARALLEL=1 to allow "
                "pytest-xdist (-n/--dist)."
            )

    # Refuse a second concurrent session against this checkout -- see the docstring, which
    # records what two of them actually do to each other. AFTER the parallel guard above so
    # a misuse of -n still reports the more specific message.
    _acquire_session_lock(config)

    # Clear the ensure_provisioned_once registry once per run, on the CONTROLLER
    # only (xdist sets PYTEST_XDIST_WORKER in workers, not the controller, and the
    # controller's pytest_configure runs before any worker spawns) -- mirrors
    # scripts/live/livetest/plugin.py's identical guard. A prior run's stale
    # "postgres-setup" record must not make a worker skip ensure_setup against a
    # container that was torn down and recreated since (services.ensure_up_for_test
    # does that routinely). Unconditional serial runs never touch this registry at
    # all (the parallel branch is the only caller of ensure_provisioned_once), so
    # this is a no-op there beyond deleting a file that likely doesn't exist.
    if os.environ.get("SLT_PARALLEL") and not os.environ.get("PYTEST_XDIST_WORKER"):
        if _docker_mod is not None:
            _docker_mod.clear_provision_registry()


# ============================================================================
# 11. YAML TEST DISCOVERY AND EXECUTION
#
# Discover test.yaml files via inttest.manifest.load_manifest (SPEC §3) and execute
# the SPEC §5 execution pipeline per test:
#   1. skip gates (disabled)                     -- IntYamlItem.__init__ (collection time)
#   2. provision requires: services               -- runtest(), services.ensure_up
#   3. build/reuse the operator jar                -- runtest(), opartifacts.build_jar
#   4. assemble the ${...} token table             -- runtest(), tokens.build_tokens
#   5. run ddl: then seed:                         -- runtest(), dbroutes.run_sql_file
#   6. assertions: data entries drive IntegrationProcessor (harness.drive) and
#      semantically compare its emitted WAEvents against the `match` fixture
#      (waevent.compare); smoke passes if steps 2-5 completed without failure/skip
#
#   (7. compare -- folded into step 6 above.)
#   8. cleanup -- the existing cleanup_postgres/cleanup_oracle autouse fixtures
#      (section 9 above), unchanged.
# ============================================================================


def _provision_requires(name: str, requires: list, lock: FileLock) -> None:
    """Bring up every `requires:` service, filelock-serialized (SPEC §5 step 2 /
    PERF_SPEC.md §5 step 1). Shared by `IntYamlItem` and `PerfYamlItem`."""
    for svc in requires:
        if svc not in _docker_mod.SUPPORTED_SERVICES:
            pytest.skip(
                f"{name}: requires unsupported service {svc!r} -- only "
                f"{sorted(_docker_mod.SUPPORTED_SERVICES)} are provisionable today"
            )
        _require_service_opt_in(svc)
        # This case requires svc, so its host-side pre_up hook may run first
        # (inttest.services.run_pre_up says when it does not). A failed hook is an error,
        # not a skip: the files the case needs could not be fetched.
        try:
            _docker_mod.run_pre_up(svc)
        except Exception as e:
            if type(e).__name__ != "PreUpError":
                raise
            pytest.fail(f"{name}: service {svc!r}: {e}", pytrace=False)
        why = _docker_mod.unavailable(svc)
        if why:
            if _docker_mod._service_spec(svc).get("unavailable_policy") == "fail":
                pytest.fail(f"{name}: service {svc!r} unavailable: {why}", pytrace=False)
            pytest.skip(f"{name}: service {svc!r} unavailable: {why}")
        spec = _load_service_yaml(svc)
        strict = _docker_mod._service_spec(svc).get('unavailable_policy') == 'fail'
        if spec and not spec.get("compose") and not spec.get("container"):
            # Connection only (the shipped teradata): nothing to bring up. The case runs against
            # the instance its settings name (live_override_env and live_env), or skips.
            gate = spec.get("live_override_env")
            if gate and (os.environ.get(gate) or "").strip():
                continue
            message = f"{name}: no container ships for service {svc!r}: set {gate} and its settings to your own instance"
            if strict:
                pytest.fail(message, pytrace=False)
            pytest.skip(message)
        try:
            _docker_mod.ensure_up(svc, lock)
        except FileNotFoundError as e:
            # No docker / no compose file: this checkout cannot run the case at all. A skip,
            # as for an unsupported service -- the case is not applicable here.
            message = f"{name}: cannot bring up service {svc!r}: {e}"
            if strict:
                pytest.fail(message, pytrace=False)
            pytest.skip(message)
        except subprocess.CalledProcessError as e:
            # ⚠ A FAILURE, not a skip. Docker is here and the service is
            # declared; `compose up` returning non-zero is the environment breaking, and a
            # skip folds it into a green run as one `s` in the dots -- the perf tier's first
            # item went that way on 2026-09-15 and the run read 82 passed, 1 skipped. §88's
            # rule: a green run is not evidence that it ran.
            pytest.fail(f"{name}: `docker compose up` failed for service {svc!r}: {e}")


def _build_operator_jar(name: str, op_jar_ref: str, report=None):
    """Build/reuse the operator jar for the detected Striim release (SPEC §5 step 3 /
    PERF_SPEC.md §5 step 2). Building needs a real Striim install (STRIIM_HOME);
    RUNNING this suite must not require it, so a missing STRIIM_HOME is a skip, not a
    failure. Shared by `IntYamlItem` and `PerfYamlItem`.

    `report`, when given, is passed straight through to `opartifacts.build_jar` --
    `PerfYamlItem` uses it to record whether this run rebuilt the jar (PERF_SPEC.md
    §12's "whether the jar was rebuilt during this run or reused"); `IntYamlItem`
    leaves it `None`, unchanged from before."""
    if not os.environ.get("STRIIM_HOME"):
        pytest.skip(
            f"{name}: STRIIM_HOME is not set -- building {op_jar_ref!r} needs a "
            f"Striim install (running the fast unit suite does not)"
        )
    release = releases_mod.resolve_release(os.environ)
    # Serialise the build per module, as the live tier's build_modules does. Under xdist every
    # worker reaches here for the same module at once, and concurrent `mvn package` (or `clean`)
    # in one target/ deletes or tears another's output -- which surfaced as the skip below.
    lock = FileLock(str(_op_build_lock_path(op_jar_ref)))
    try:
        with lock:
            return opartifacts_mod.build_jar(op_jar_ref, release, report=report)
    except opartifacts_mod.OpBuildFailed as e:
        # A FAILURE, not a skip: the build ran, so the module itself is broken, and a skip
        # would let a run that tested nothing of it read green.
        pytest.fail(f"{name}: operator jar {op_jar_ref!r} failed to build: {e}")
    except (opartifacts_mod.OpArtifactError, FileNotFoundError) as e:
        pytest.skip(f"{name}: cannot build operator jar {op_jar_ref!r}: {e}")


def _terminal_emit(config, text: str) -> None:
    """Prints `text` (the PERF_SPEC.md §11 console report) to the real terminal from
    inside `runtest()`, where pytest's default fd-level capture would otherwise
    swallow a plain `print` until the whole run finished (same mechanism/precedent as
    `scripts/live/livetest/plugin.py`'s `_make_progress`: suspend capturemanager's
    global capture around the write, resume after). Falls back to plain `print` when
    the terminal reporter plugin isn't active (e.g. `-p no:terminal`). Never raises --
    a reporter quirk must not fail a performance run whose measurements already
    succeeded."""
    try:
        tr = config.pluginmanager.getplugin("terminalreporter")
        if tr is None:
            print(text)
            return
        capman = config.pluginmanager.getplugin("capturemanager")
        if capman is not None:
            capman.suspend_global_capture(in_=False)
        try:
            # pytest's test-header line (the "path::name" prefix) is still open with
            # no trailing newline at this point -- an unconditional bare newline
            # first, same as _make_progress, so the report starts on its own line.
            tr._tw.write("\n")  # noqa: SLF001
            for line in text.splitlines():
                tr.write_line(line)
            try:
                tr._tw.flush()  # noqa: SLF001
            except Exception:  # noqa: BLE001
                pass
        finally:
            if capman is not None:
                capman.resume_global_capture()
    except Exception as e:  # noqa: BLE001
        print(f"[integration] WARNING: failed to emit performance report to terminal: {e!r}")
        print(text)


def render_config_file(path_str: str, tokens: dict, dest_dir: Path | None = None) -> str:
    """Token-renders a ConfigFile's content onto a per-test temp copy, returning the
    copy's absolute path. Mirrors scripts/live/livetest/plugin.py::upload_op_uploads's
    identical read-decode-render-encode step for uploaded files, so a config-driven
    OP can isolate a literal identifier (e.g. a
    table name) embedded in its own JSON the same way ddl:/seed: SQL already can via
    dbroutes -- previously the ONLY property value ever left unrendered (see
    harness.drive's docstring), since `properties:`/`input`/`match` already go
    through `tokens_mod.render`.

    Returns `path_str` UNCHANGED when the file carries no ${...} tokens at all (the
    overwhelming majority of fixtures), so an untokenized config's path -- and its
    directory, for any relative sibling reference inside it (e.g. a bootstrap CSV
    path, resolved by the operator against the subprocess's cwd, never against
    ConfigFile's own directory) -- is untouched; no temp file is created. `path_str`
    is expected to already be absolute (as rendered from a `${TEST_DIR}/...`
    property value by the caller), independent of this process's own cwd. Raises
    `tokens_mod.SubstitutionError` if the content references a token missing from
    `tokens`, exactly as a `properties:`/`input`/`match` render would.

    `dest_dir`, if given, is used instead of a fresh `tempfile.mkdtemp` -- for a
    caller (`inttest.perf.run_measured_iteration`) that already owns a per-
    iteration scratch directory it tears down itself, so this doesn't leak one
    extra temp dir per iteration alongside it."""
    original = Path(path_str)
    text = original.read_text(encoding="utf-8")
    rendered = tokens_mod.render(text, tokens)
    if rendered == text:
        return path_str
    tmp_dir = dest_dir if dest_dir is not None else Path(tempfile.mkdtemp(prefix="inttest-configfile-"))
    dest = tmp_dir / original.name
    dest.write_text(rendered, encoding="utf-8")
    return str(dest)


def pytest_collect_file(parent, file_path):
    """Discover test.yaml files (SPEC §3, PERF_SPEC.md §14).

    A test.yaml under perf/ is fully self-contained (PERF_SPEC.md §3 --
    `performance:` in place of `assert:`, otherwise identical to a regression
    test.yaml) -- dispatched by location, since testpaths only restricts the *default*
    no-args collection root.
    """
    if file_path.name != "test.yaml":
        return None
    if file_path.is_relative_to(_PERF_DIR):
        return PerfYamlFile.from_parent(parent, path=file_path)
    return IntYamlFile.from_parent(parent, path=file_path)


class IntYamlFile(pytest.File):
    """A pytest.File subclass for test.yaml manifests (SPEC §3). Parsing/normalization
    is entirely `inttest.manifest.load_manifest`'s job; this class only turns a parse
    failure into a clear `pytest.fail` and wraps a successful load in one IntYamlItem."""

    def collect(self):
        try:
            test_manifest = manifest_mod.load_manifest(self.path)
        except manifest_mod.ManifestError as e:
            pytest.fail(str(e))
        yield IntYamlItem.from_parent(
            self,
            name=test_manifest.name,
            manifest_path=self.path,
            test_manifest=test_manifest,
        )


class IntYamlItem(pytest.Item):
    """A pytest.Item representing a single test.yaml manifest (SPEC §3/§5).

    `__init__` (collection time) applies service markers (postgres/oracle/spanner,
    driving the existing cleanup_postgres/cleanup_oracle autouse fixtures the same way
    the retired stub did). `runtest()` executes SPEC §5 steps 1 (disabled gate) and 2-6.

    Note: the `disabled:` gate is a `pytest.skip()` call at the top of `runtest()`,
    NOT a `pytest.mark.skip` marker applied here at collection time -- a raw
    `pytest.Item` (unlike `pytest.Function`) doesn't override `reportinfo()`, and
    pytest's marker-skip machinery needs that to return a real line number
    (`TestReport.from_item_and_call` asserts on it), so a collection-time
    `pytest.mark.skip` on this Item crashes with an INTERNALERROR. Calling
    `pytest.skip()` directly from `runtest()` (same as the STRIIM_HOME/service gates
    below) sidesteps that entirely and matches SPEC §5 step 1's own wording."""

    def __init__(self, *, manifest_path, test_manifest, **kw):
        super().__init__(**kw)
        self.manifest_path = manifest_path
        self.manifest = test_manifest

        # Per-test DB isolation state (section 9b) -- set during runtest() step 4b,
        # read back by runtest()'s own finally/_teardown_db_isolation. None means
        # "nothing was created for this service", the smoke/no-ddl no-op case.
        self._pg_admin = None
        self._oracle_prefix = None
        self._spanner_prefix = None

        self.add_marker(pytest.mark.integration)
        for svc in test_manifest.requires:
            if svc == "postgres":
                self.add_marker(pytest.mark.postgres)
            elif svc == "oracle":
                self.add_marker(pytest.mark.oracle)
            elif svc == "spanner":
                self.add_marker(pytest.mark.spanner)
            elif svc == "gcs":
                self.add_marker(pytest.mark.gcs)

    def runtest(self):
        """Execute SPEC §5 steps 1-6 for this test.yaml (step 8/cleanup is the
        existing autouse fixtures)."""
        m = self.manifest

        # --- 1. Skip gate: disabled: truthy skips before any provisioning/build work,
        # unless forced via SLT_RUN_DISABLED=1. ---
        if m.disabled and os.environ.get("SLT_RUN_DISABLED") != "1":
            reason = m.disabled if isinstance(m.disabled, str) else "disabled"
            pytest.skip(f"{m.name}: disabled ({reason}); set SLT_RUN_DISABLED=1 to force")

        # --- 2. Provision: bring up every requires: service, filelock-serialized. ---
        # Postgres/oracle/spanner also get an earlier, independent availability check
        # via the cleanup_postgres/cleanup_oracle/cleanup_spanner fixtures' own
        # _ensure_up call during fixture SETUP (before runtest() ever runs) -- this
        # call is what SPEC §5 step 2 itself asks for and is a harmless idempotent
        # no-op if that already happened.
        lock = FileLock(str(_compose_lock_path()))
        _provision_requires(m.name, m.requires, lock)

        # --- 3. Build/reuse the operator jar for the detected Striim release. ---
        # Stashed on the item for Phase 3 (IntegrationProcessor invocation needs the
        # built jar's path/name/op_name) -- unused by this slice's pipeline itself.
        self.artifact = artifact = _build_operator_jar(m.name, m.module_ref)

        # --- 4. Tokens: ${TEST_DIR}/${TID}/${TID_ORACLE} + every requires: service's
        # provides: map (SPEC §6). ${OP_JAR}/${OP_NAME} are assembled for parity/Phase 3
        # convenience even though no test.yaml key references them (SPEC §6 note). ---
        parallel = _parallel(os.environ)
        tok = _build_tokens(m.dir, m.requires, parallel=parallel)
        tok.setdefault("OP_JAR", str(artifact.path))
        tok.setdefault("OP_NAME", artifact.op_name)
        self.tokens = tok

        # --- 4b through 8, wrapped in one try/finally: per-test DB isolation setup
        # (4b), ddl/seed (5), assertions (6/7), and imperative cleanup (8) -- see
        # section 9b for why this must be imperative rather than fixture-driven.
        # `db_failed` mirrors `_test_failed`'s intent (section 8) but computed
        # locally: pytest's rep_call/rep_setup reports don't exist yet at this point
        # in the runtest protocol (they're built from THIS call's outcome, in
        # pytest_runtest_makereport, after runtest() returns/raises), so failure has
        # to be tracked by catching the exception ourselves. `pytest.skip.Exception`
        # (e.g. the harness-jar/`java`-not-found skips below) is deliberately NOT a
        # failure -- an environment gap isn't something to keep resources around
        # for -- only a real ddl/seed/assertion problem is. ---
        db_failed = False
        try:
            # --- 4b. Per-test DB isolation (SPEC §13/§17 #4). A smoke/no-ddl test
            # whose requires: never names postgres/oracle does nothing here. ---
            if "postgres" in m.requires:
                # Fixed qasource/qatarget schemas, per-test isolation by ${TID}
                # table prefix -- same model as Oracle/Spanner in this tier and as
                # scripts/live's own Postgres. Serial: ensure_setup + whole-schema
                # reset gives a clean slate; ensure_setup runs unconditionally,
                # once per test, since a serial run has no concurrent sibling to
                # contend with. Parallel: that whole-schema DROP would clobber a
                # sibling worker's tables, so wipe only THIS test's ${TID}-prefixed
                # tables -- and run ensure_setup through ensure_provisioned_once
                # (docs/internals/INTEGRATION-ENGINE.md#token-isolation) rather than
                # unconditionally: still safe without it (pgclient.PgAdmin.
                # ensure_setup's retry makes uncoordinated concurrent callers
                # correct), but with N xdist workers all calling it once per test
                # this removes the GRANT CONNECT contention (and its retries)
                # instead of just absorbing it.
                pg = _pg_admin(tok, role="source")
                if parallel:
                    _docker_mod.ensure_provisioned_once("postgres-setup", pg.ensure_setup)
                    pg.reset_test_objects(tok["TID"])
                else:
                    pg.ensure_setup()
                    pg.reset_schemas()
                self._pg_admin = pg  # only recorded once setup succeeded
            if "oracle" in m.requires:
                # ${TID_ORACLE}, NOT a separate always-non-empty token: matches
                # services/oracle/sql/templates.sql's and scripts/live's own
                # convention (${ORACLE_SOURCE_SCHEMA}.${TID_ORACLE}<name>), and its
                # documented serial-empty contract is fine here -- a serial run's
                # prefix-drop is `None`/no-op below, same as Spanner, relying on
                # ddl:'s own DROP TABLE IF EXISTS for serial-mode self-healing.
                self._oracle_prefix = tok["TID_ORACLE"] or None
            if "spanner" in m.requires:
                # Unlike Postgres/Oracle (a real admin connection, so a whole-
                # database/schema reset is available), Spanner is isolation: none --
                # one fixed instance + two fixed-dialect databases, provisioned
                # once, with no such reset available. This ensure must run even
                # for a test with no ddl:, since the OPERATOR's own
                # ${SPANNER_*_URL} JDBC connection needs the database to exist and
                # dbroutes never runs for such a test.
                _ensure_spanner_databases(tok)
                self._spanner_prefix = tok["TID"] or None
            if "gcs" in m.requires:
                # `isolation: none` like Spanner -- one fixed bucket, no per-test
                # reset (services/gcs/README.md's "Bucket lifecycle" section). Must
                # run even for a test with no ddl:/seed: (gcs never has either --
                # see that README), since a real upload needs the bucket to exist
                # first.
                _ensure_gcs_bucket(tok)

            # --- 5. DDL then seed, in that order, each against its own db: route. ---
            for spec in m.ddl:
                try:
                    dbroutes_mod.run_sql_file(spec.db, spec.path, tok)
                except Exception as e:
                    pytest.fail(f"{m.name}: ddl {spec.file!r} against {spec.db!r} failed: {e}")
            # ⚠ A `source: {seed_when: post_start}` case holds its seed back to here-plus-later:
            # a change stream captures only commits made after its start timestamp, so seeding now
            # would be invisible to it and the case would sit at zero events looking like an
            # operator bug. Those seeds run in `run_post_start_seed` below, called by the harness
            # while the reader is blocked after its first tick.
            post_start_seed = (m.source is not None
                               and getattr(m.source, "seed_when", "pre_start") == "post_start")

            # ⚠ Once per CASE, not once per `assert.data` entry. The callback below is handed to
            # every drive() in the assertion loop, so without this latch a second data assertion
            # would replay the seed SQL and hit ALREADY_EXISTS -- silently restricting a
            # post_start case to exactly one assertion, with nothing saying so.
            post_start_seed_done = []

            def run_post_start_seed():
                if post_start_seed_done:
                    return
                post_start_seed_done.append(True)
                for spec in m.seed:
                    try:
                        dbroutes_mod.run_sql_file(spec.db, spec.path, tok)
                    except Exception as e:
                        pytest.fail(
                            f"{m.name}: post-start seed {spec.file!r} against {spec.db!r} "
                            f"failed: {e}")

            if not post_start_seed:
                for spec in m.seed:
                    try:
                        dbroutes_mod.run_sql_file(spec.db, spec.path, tok)
                    except Exception as e:
                        pytest.fail(f"{m.name}: seed {spec.file!r} against {spec.db!r} failed: {e}")

            # --- 6. Assertions. ---
            # assert.expect_error: the drive must FAIL, and the failure must name the reason.
            # This is what makes "must refuse" testable at all -- §6.1's structural variants
            # include "source wider than target must refuse" (§28.1), which has no output to
            # compare and no rows to read back.
            if m.assert_.expect_error is not None:
                for i, (variant, overrides) in enumerate(m.runs()):
                    if variant is not None or i > 0:
                        self._reset_fixtures(m, tok, overrides, variant)
                    self._expect_error(m, overrides, variant)
                return


            # A target emits no events, so it takes its own branch entirely: the driver returns a
            # run REPORT rather than emitted events, and the database it wrote is read back and
            # compared here.
            if m.target is not None:
                # ONE case, every permutation of its `matrix:` flags, ONE assert block (§6.1a).
                # Absent `matrix:` this is a single pass with no overrides -- the existing
                # behaviour, expressed rather than special-cased.
                for i, (variant, overrides) in enumerate(m.runs()):
                    # A VARIANTS case has no case-level `ddl:` -- step 5 created nothing -- so
                    # every run including the FIRST must lay down its own engine's tables. A
                    # non-variant case had its DDL run in step 5, so it only needs the reset
                    # BETWEEN runs.
                    if variant is not None or i > 0:
                        self._reset_fixtures(m, tok, overrides, variant)
                    self._run_target_case(m, overrides, variant)
                return

            # assert.data drives IntegrationProcessor (SPEC §7) per {input, match} entry
            # and semantically compares its emitted WAEvents against the `match` fixture
            # (SPEC §11). If both assert.data and assert.smoke are set, data subsumes
            # smoke (steps 2-5 already had to pass to get here).
            if m.assert_.data:
                # Running the harness needs a JRE; building it (mvn package, below) needs
                # a JDK -- neither is guaranteed merely because STRIIM_HOME was set in
                # step 3 (that gate is about building the OP jar), so an
                # environment/availability problem here is a skip, matching the
                # STRIIM_HOME skip above, not a failure.
                try:
                    harness_mod.ensure_harness_jar()
                except harness_mod.HarnessError as e:
                    pytest.skip(f"{m.name}: cannot build the inttest harness jar: {e}")

                # properties: values may themselves carry ${...} tokens (SPEC §3), same
                # as every other test.yaml string.
                props = {k: tokens_mod.render(v, self.tokens) for k, v in m.properties.items()}

                # ConfigFile's file CONTENTS may ALSO carry ${...} tokens (e.g. a table
                # name that needs the same per-test isolation ddl:/seed: SQL already gets
                # via dbroutes) -- mirrors scripts/live/livetest/plugin.py::
                # upload_op_uploads's identical read-decode-render-encode step for
                # uploaded files, so a config-driven OP
                # can isolate a literal table name embedded in its own JSON the same way
                # a TQL app already can. Rendered onto a per-test temp copy, never in
                # place, so the checked-in fixture stays byte-identical across runs;
                # `cwd=m.dir` below is unaffected -- a config's own relative file
                # references (e.g. a bootstrap CSV path) resolve against the subprocess's
                # cwd, not against ConfigFile's own directory (CsvSource.open's
                # Path.of(location)), so relocating the rendered copy elsewhere is safe.
                if "ConfigFile" in props:
                    props["ConfigFile"] = render_config_file(props["ConfigFile"], self.tokens)

                # assert.gcs_objects: resolved once, but PRUNED PER PERMUTATION below.
                gcs_keys = [tokens_mod.render(k, self.tokens) for k in m.assert_.gcs_objects]

                # ONE case, every permutation of its `matrix:` block, ONE assert block
                # (§6.1a). Absent `matrix:` this is a single pass with no overrides -- the
                # existing behaviour, expressed rather than special-cased.
                base_props = dict(props)
                for perm_index, overrides in enumerate(m.permutations()):
                    if perm_index > 0:
                        self._reset_fixtures(m, tok, overrides)
                    # ⚠ INSIDE the loop, not before it. The prune exists so a prior run's object
                    # cannot make the post-run check vacuous -- and with a matrix, the "prior run"
                    # is the PREVIOUS PERMUTATION. Pruning once would let permutation 2's check
                    # pass on permutation 1's objects, which is the same vacuous assertion the
                    # prune was written to prevent, arriving by a new route.
                    if gcs_keys:
                        _prune_gcs_objects(self.tokens, gcs_keys)
                    # Rebuilt from the base each time, so one permutation's values cannot
                    # leak into the next. load_manifest guarantees these keys are NOT in
                    # `properties:`, so this adds rather than overrides.
                    props = dict(base_props)
                    props.update(overrides or {})
                    # Named in every failure, so a mismatch says WHICH run made it.
                    at = _run_label(None, overrides)
                    for spec in m.assert_.data:
                        if m.source is None:
                            raw_input = Path(spec.input_path).read_text()
                            rendered_input = tokens_mod.render(raw_input, self.tokens)
                            input_events = waevent_mod.load(rendered_input)
                        else:
                            # A `source:` case has no input fixture -- the events come from
                            # ticking the reader, not from a file (load_manifest rejects an
                            # `input:` here rather than letting one sit unread).
                            input_events = []

                        try:
                            # test.yaml's `types:` block (SPEC §3, §17 #1 RESOLVED) carries source
                            # schemas keyed by TableName; forward it verbatim to
                            # IntegrationProcessor -- schemas are not `${...}`-templated.
                            # `password_properties:` names the `properties` keys (e.g. `Password`,
                            # `BootstrapPassword`) that must be wrapped in the harness's mock
                            # Password before construction -- also not `${...}`-templated.
                            # `cwd=m.dir` runs the operator subprocess from the test's own
                            # directory, so a config.json (also never `${...}`-templated) can
                            # reference a sibling data file -- e.g. a bootstrap CSV path -- by
                            # plain relative filename.
                            # ⚠ The sink is passed on THIS path too, not only the target one.
                            # An assert key that is honoured on one drive path and silently
                            # ignored on the other is R2's defect exactly -- declared and inert
                            # (§126) -- and it would be invisible, because a case whose log
                            # assertion is never evaluated simply passes.
                            data_log_sink: list = []
                            jmx_sink: list = []
                            emitted = harness_mod.drive(
                                self.artifact.path, props, input_events, types=(m.types or None),
                                password_properties=(m.password_properties or None),
                                udf=m.udf, source=m.source,
                                on_source_started=(run_post_start_seed if post_start_seed else None),
                                timeout=m.timeout, cwd=m.dir, log_sink=data_log_sink,
                                jmx=m.assert_.jmx, jmx_sink=jmx_sink,
                            )
                        except harness_mod.HarnessError as e:
                            if "no `java` executable found" in str(e):
                                pytest.skip(f"{m.name}: {e}")
                            raise  # a real driver/operator failure -- let it fail the test

                        self._assert_expect_log(m, at, data_log_sink)

                        raw_expected = Path(spec.match_path).read_text()
                        rendered_expected = tokens_mod.render(raw_expected, self.tokens)
                        expected = waevent_mod.load(rendered_expected)

                        # WAEventMismatch subclasses AssertionError -- propagates as the test
                        # failure, naming the first differing path (SPEC §11). ignore_fields/
                        # project narrow the comparison for an op that stamps something
                        # non-deterministic; both default to empty, so a case that declares
                        # neither compares exactly as it always did.
                        try:
                            waevent_mod.compare(emitted, expected,
                                                ignore_fields=spec.ignore_fields,
                                                project=spec.project,
                                                sort_by=spec.sort_by)
                        except AssertionError as mismatch:
                            # Re-raised with the permutation named. With N runs behind one
                            # expectation, a diff that does not say which combination produced it
                            # sends the reader off to re-run them by hand.
                            raise type(mismatch)(f"{m.name}{at}: {mismatch}") from None

                        # After the WAEvent comparison, so the richer diff reports first. Once per
                        # drive: every data entry and permutation is its own JVM and its own counters.
                        if m.assert_.jmx is not None:
                            snapshot = jmx_sink[0] if jmx_sink else {
                                "error": "the harness returned no JMX snapshot"}
                            failures = jmx_mismatches(m.assert_.jmx, snapshot)
                            if failures:
                                pytest.fail(
                                    f"{m.name}{at}: assert.jmx failed for {spec.input}:\n  "
                                    + "\n  ".join(failures)
                                    + "\n--- snapshot ---\n" + json.dumps(snapshot, indent=2,
                                                                            sort_keys=True))

                    # Side-effect assertion, after the WAEvent comparison so the richer
                    # WAEventMismatch reports first when both are wrong.
                    if gcs_keys:
                        _assert_gcs_objects(self.tokens, m.name, gcs_keys)
                return

            # assert.smoke: steps 2-5 completing without failure/skip IS the assertion.
            # manifest.load_manifest guarantees assert.smoke or assert.data is set, and
            # the branch above already handled assert.data, so reaching here means
            # smoke: true.
            assert m.assert_.smoke
        except pytest.skip.Exception:
            raise  # an environment gap, not a test failure -- see db_failed docstring above
        except BaseException:
            db_failed = True
            raise
        finally:
            self._teardown_db_isolation(db_failed)

    def _expect_error(self, m, overrides: dict | None = None, variant=None) -> None:
        """Drive the module and require it to FAIL with the message the case names.

        A refusal is a first-class outcome for this writer -- it refuses a decimal that does not
        fit its column's scale, a temporal value that would be fabricated, a table name that
        matches two tables. Until this existed those refusals could only be pinned by unit tests,
        which see what is handed to the driver and not what the database would have done.

        The failure is matched by SUBSTRING, and `load_manifest` refuses a substring broad enough
        to match any failure: a case that cannot tell the refusal it is testing from the harness
        failing to start is worse than no case.
        """
        try:
            harness_mod.ensure_harness_jar()
        except harness_mod.HarnessError as e:
            pytest.skip(f"{m.name}: cannot build the inttest harness jar: {e}")

        at = _run_label(variant, overrides)
        tok = self._variant_tokens(variant)
        props = {k: tokens_mod.render(v, tok) for k, v in m.properties.items()}
        props.update(overrides or {})
        if "ConfigFile" in props:
            props["ConfigFile"] = render_config_file(props["ConfigFile"], tok)

        if m.target is not None:
            raw_input = Path(m.target.input_path).read_text()
        elif m.assert_.data:
            raw_input = Path(m.assert_.data[0].input_path).read_text()
        else:
            raw_input = "[]"
        input_events = waevent_mod.load(tokens_mod.render(raw_input, tok))

        try:
            harness_mod.drive(
                self.artifact.path, props, input_events, types=(m.types or None),
                password_properties=(m.password_properties or None),
                udf=m.udf, source=m.source, target=m.target, timeout=m.timeout, cwd=m.dir,
                on_mid_run=self._mid_run_callback(m, tok, variant))
        except harness_mod.HarnessError as e:
            if "no `java` executable found" in str(e):
                pytest.skip(f"{m.name}: {e}")
            # ⚠ §84. RENDERED with this run's tokens, so a variant can supply its own expected
            # text. Matched raw, this pinned a whole class of cases to one engine: a case that
            # asserts a DRIVER's message can never fan out, because every engine words the same
            # condition differently -- PostgreSQL says "null value in column", SQL Server says
            # "Cannot insert the value NULL into column", Oracle raises ORA-01400, and there is no
            # common sqlState either (23502 / 23000 / 23000). Rendering costs nothing for a case
            # with no tokens in its expect_error, which is all of them today.
            expected = tokens_mod.render(m.assert_.expect_error, self._variant_tokens(variant))
            if expected in str(e):
                # Refused, for the stated reason. Now check what the failure LEFT BEHIND: a
                # rollback is only proved by the database, not by the exception.
                self._assert_target_rows(m, at, variant)
                return
            pytest.fail(
                f"{m.name}{at}: the drive failed as expected, but not for the stated reason.\n"
                f"  expect_error: {expected!r}\n"
                f"  actual failure did not contain it:\n{e}")
        pytest.fail(
            f"{m.name}{at}: expected the drive to be REFUSED with a failure containing "
            f"{tokens_mod.render(m.assert_.expect_error, self._variant_tokens(variant))!r}, "
            f"but it SUCCEEDED. A refusal that stopped happening is a "
            f"behaviour change, and silently writing what this case says must be rejected is the "
            f"outcome it exists to prevent.")

    def _variant_tokens(self, variant) -> dict:
        """This run's token table: the case's own, plus the variant's, each rendered.

        A variant's values may themselves carry `${...}` -- `V_URL: '${POSTGRES_URL}'` is the
        ordinary form -- so they are rendered against the base table before being added to it.
        """
        if variant is None:
            return self.tokens
        merged = dict(self.tokens)
        for key, value in variant.tokens.items():
            merged[key] = tokens_mod.render(value, self.tokens)
        return merged

    def _reset_fixtures(self, m, tok, overrides, variant=None) -> None:
        """Re-run this run's `ddl:` and `seed:` between runs.

        A run that inherited the previous one's rows would make the shared expectation assert the
        UNION of every run rather than what each produced -- the construct defeated. Called only
        BETWEEN runs: step 5 already ran the first.

        For a VARIANT case the DDL comes from the variant, through the variant's own route, so
        each engine's tables are created by the connection that will write to them.
        """
        ddl = variant.ddl if variant is not None else m.ddl
        tok = self._variant_tokens(variant) if variant is not None else tok
        for kind, specs in (("ddl", ddl), ("seed", m.seed)):
            for spec in specs:
                try:
                    dbroutes_mod.run_sql_file(spec.db, spec.path, tok)
                except Exception as e:
                    pytest.fail(f"{m.name}: {kind} {spec.file!r} against {spec.db!r} failed "
                                f"before permutation {overrides}: {e}")

    def _assert_expect_log(self, m, at: str, log_sink: list) -> None:
        """`assert.expect_log:` -- every named substring must appear in what the operator LOGGED.

        ⚠ THIS IS THE ONLY ASSERTION THAT CAN SEE A WARNING ON A SUCCESSFUL RUN. `expect_error:`
        reads the failure text, so it needs the drive to fail; `target:` reads the database, which
        a warning never touches. Three capabilities have no other observable behaviour --
        VendorConfiguration (§142.3), useBulkCopyForBatchInsert on a non-SQL-Server engine (§133),
        and the set-based back-out warning (§122.5).

        ⚠ The failure message quotes what WAS logged, truncated. A "not found" that does not show
        the haystack sends the reader back to re-run the case by hand with the driver's output
        unredirected, which is exactly what this key exists to avoid.
        """
        if not m.assert_.expect_log:
            return
        logged = "".join(log_sink)
        missing = [want for want in m.assert_.expect_log if want not in logged]
        if not missing:
            return
        tail = logged[-1500:] if logged else "(the operator logged nothing at all)"
        pytest.fail(
            f"{m.name}{at}: assert.expect_log did not find {missing!r} in what the operator "
            f"logged.\n--- last 1500 chars of the operator's output ---\n{tail}")

    def _run_target_case(self, m, overrides: dict | None = None, variant=None) -> None:
        """Drive a `target:` case: feed the writer, then assert the database it wrote.

        A Target's three observable outputs are split across two places, and that split is
        structural rather than tidy. What it acked and the position it holds durable live inside
        the JVM and come back in the driver's run report; the target DATABASE is read straight
        back out of the real database, which is the entire reason this tier exists for a writer.
        """
        try:
            harness_mod.ensure_harness_jar()
        except harness_mod.HarnessError as e:
            pytest.skip(f"{m.name}: cannot build the inttest harness jar: {e}")

        # The variant's tokens are what make ONE `properties:` block serve every engine: the
        # block names ${V_URL}/${V_PROVIDER}/..., and the variant supplies their values.
        tok = self._variant_tokens(variant)
        props = {k: tokens_mod.render(v, tok) for k, v in m.properties.items()}
        # The permutation's own values. load_manifest guarantees these keys are NOT in
        # `properties:`, so this adds rather than overrides -- nothing an author wrote is
        # silently replaced.
        props.update(overrides or {})
        if "ConfigFile" in props:
            props["ConfigFile"] = render_config_file(props["ConfigFile"], tok)

        # Named in every failure below. With eight permutations sharing one expectation, a
        # mismatch that does not say WHICH combination produced it sends the reader to re-run
        # them by hand -- and the whole point of the matrix is that only one of them differs.
        at = _run_label(variant, overrides)
        rendered_input = tokens_mod.render(Path(m.target.input_path).read_text(), tok)
        input_events = waevent_mod.load(rendered_input)

        log_sink: list = []
        try:
            report = harness_mod.drive(
                self.artifact.path, props, input_events, types=(m.types or None),
                password_properties=(m.password_properties or None),
                target=m.target, timeout=m.timeout, cwd=m.dir,
                on_mid_run=self._mid_run_callback(m, tok, variant),
                log_sink=log_sink,
            )
        except harness_mod.HarnessError as e:
            if "no `java` executable found" in str(e):
                pytest.skip(f"{m.name}: {e}")
            # Re-raised WITH the run label. A drive that fails under one permutation and not
            # another is the most valuable thing a matrix can find, and an unlabelled traceback
            # makes it the hardest to act on -- the reader cannot tell which of eight runs broke.
            raise harness_mod.HarnessError(f"{m.name}{at}: {e}") from None

        self._assert_expect_log(m, at, log_sink)
        self._assert_target_rows(m, at, variant, report)

        # Checked whatever the case asserts: a writer without the marker is never called back by
        # the platform, so it silently loses whatever it had not committed when the checkpoint
        # advanced. From the ack count alone that is indistinguishable from the legitimate
        # no-recovery path, which is exactly why it is reported separately rather than inferred.
        if not report.get("acknowledgeable", True):
            pytest.fail(
                f"{m.name}{at}: the writer does not implement com.webaction.recovery.Acknowledgeable, "
                f"so the platform would never inject a receipt callback and would never pin its "
                f"positions. It is an EMPTY marker interface, so nothing about dropping it fails "
                f"to compile and nothing warns at run time.\n"
                f"  writer report: {json.dumps(report)}")

        # The restart accounting, BEFORE the ack count: a recovery case that silently stopped
        # restarting would otherwise report an ack mismatch, which points at the writer's ack path
        # rather than at the restart that never happened.
        for key, field, what in (("restarts", "restarts", "restart(s)"),
                                 ("replayed", "eventsReplayed", "replayed event(s)")):
            expected = getattr(m.assert_, key)
            if expected is not None and report.get(field) != expected:
                pytest.fail(
                    f"{m.name}{at}: assert.{key} expected {expected} {what} but the run reported "
                    f"{report.get(field)}.\n"
                    f"  writer report: {json.dumps(report)}\n"
                    f"  A restart case asserts the same rows and the same ack count as one with no "
                    f"restart, so these two counts are what make it about recovery at all.")

        # What the platform is SHOWN. §43.24: it saw no throughput at all for a whole live run
        # because publishMonitorEvents was never overridden, and nothing in any tier noticed.
        published = report.get("monitor") or {}
        for name, raw_expected in (m.assert_.monitor or {}).items():
            # §156. Rendered through the case's tokens, like every other assertion here. Without
            # it TABLE_INFO -- whose value contains the TARGET TABLE NAME -- could only be asserted
            # by a single-engine case, because Oracle folds it upper and Spanner leaves it
            # unqualified. The metric names themselves are a closed registry, so only the VALUE
            # can carry a token.
            if isinstance(raw_expected, list):
                # A SHAPE metric: the keys are asserted, the values are clocks.
                want = sorted(tokens_mod.render(k, self._variant_tokens(variant))
                              for k in raw_expected)
                actual_raw = published.get(name)
                try:
                    got = json.loads(actual_raw) if actual_raw is not None else None
                except (TypeError, ValueError):
                    got = None
                bad = (not isinstance(got, dict) or sorted(got) != want
                       or any(not isinstance(v, int) or isinstance(v, bool) or v < 0
                              for v in got.values()))
                if bad:
                    pytest.fail(
                        f"{m.name}{at}: assert.monitor.{name} expected a lag for exactly "
                        f"{want} (non-negative millis each) but the writer published "
                        f"{actual_raw!r}.\n"
                        f"  published: {json.dumps(published)}\n"
                        f"  A lag is measured from the events' own source commit time; a table "
                        f"missing here had no source time, or the field was never published.")
                continue
            expected = tokens_mod.render(raw_expected, self._variant_tokens(variant))
            actual = published.get(name)
            if actual != expected:
                pytest.fail(
                    f"{m.name}{at}: assert.monitor.{name} expected {expected!r} but the writer "
                    f"published {actual!r}.\n"
                    f"  published: {json.dumps(published)}\n"
                    f"  This is the figure a person reads off the monitor page to decide whether "
                    f"a flow is progressing. A writer that writes correctly and reports nothing "
                    f"looks dead (§43.24).")

        if m.assert_.acked is not None and report.get("ackedEvents") != m.assert_.acked:
            pytest.fail(
                f"{m.name}{at}: assert.acked expected {m.assert_.acked} acknowledged event(s) but "
                f"the writer acked {report.get('ackedEvents')}.\n"
                f"  writer report: {json.dumps(report)}\n"
                f"  An ack releases the checkpoint pin, so acking too many events is data loss "
                f"on the next restart and acking too few stalls the app checkpoint.")

        if m.assert_.exception_store is not None:
            # The harness's NotifyExceptionStore records what the writer handed over,
            # each event named by its input ordinal; a case asserts the exact notifications.
            got = [entry.get("events", []) for entry in report.get("exceptionStore", [])]
            if got != m.assert_.exception_store:
                pytest.fail(
                    f"{m.name}{at}: assert.exception_store expected the writer to hand "
                    f"{m.assert_.exception_store} to the exception store (input ordinals, one "
                    f"list per notification) but it handed {got}.\n"
                    f"  notifications: {json.dumps(report.get('exceptionStore', []))}\n"
                    f"  A skipped row that never reaches the store is unrecoverable; a clean row "
                    f"that does is reported as a failure it did not have.")


    def _mid_run_callback(self, m, tok, variant=None):
        """The `mid_run:` SQL runner, or None when the case declares none.

        ⚠ SHARED BY BOTH DRIVE PATHS ON PURPOSE. A target case is driven from two places -- the
        ordinary one and `_expect_error` -- and the first version of this hook wired only the
        ordinary one. The driver then blocked at a gate nobody was listening to, and the failure
        surfaced as the Java side's "no gate directory was supplied", which is a good message for a
        harness bug and a confusing one for a case author. Extracting it is what stops the two
        paths drifting apart a second time.
        """
        if m.target is None or not m.target.mid_run:
            return None

        def _run(ordinal: int) -> None:
            for step in m.target.mid_run:
                if step.after != ordinal:
                    continue
                # Rendered through the same token table as ddl:/seed:, so ${TID} and the schema
                # tokens mean the same thing here as everywhere else in the case.
                sql = tokens_mod.render(Path(step.path).read_text(), tok)
                # ⚠ §84. Follow the VARIANT's route unless the case named one. Resolving this at
                # parse time sent every variant's mid-run SQL to PostgreSQL while the writer wrote
                # elsewhere -- silently, because the SQL succeeded against the wrong database.
                route = step.db
                if not step.db_explicit and variant is not None and variant.db:
                    route = variant.db
                dbroutes_mod.run_sql_text(route, sql, tok)

        return _run

    def _assert_target_rows(self, m, at, variant, report=None) -> None:
        """Compare every `assert.target:` query against the database.

        Shared by the success path and the `expect_error:` path: after a refusal the database is
        the ONLY evidence, and "nothing landed, the checkpoint did not move" is the whole of what
        a rollback means.
        """
        tok = self._variant_tokens(variant)
        for spec in m.assert_.target:
            # Rendered here as well as inside query_rows, purely so a failure can print the query
            # that actually ran. A message naming ${POSTGRES_TARGET_SCHEMA}.${TID}customers is one
            # nobody can paste into psql, which is the first thing anyone does with it.
            # A variant supplies the route its assertions read through, so one authored query
            # reaches whichever engine this run is against.
            route = variant.db if variant is not None else spec.db
            shown = " ".join(tokens_mod.render(spec.query, tok).split())
            try:
                rows = dbroutes_mod.query_rows(route, spec.query, tok)
            except Exception as e:
                pytest.fail(f"{m.name}{at}: assert.target query against {route!r} failed: {e}\n"
                            f"  query: {shown}")
            raw_expected = tokens_mod.render(Path(spec.match_path).read_text(), tok)
            expected = json.loads(raw_expected)
            actual = _jsonable_rows(rows)
            if actual != expected:
                pytest.fail(
                    f"{m.name}{at}: assert.target mismatch against {route!r}\n"
                    f"  query:    {shown}\n"
                    f"  expected: {json.dumps(expected)}\n"
                    f"  actual:   {json.dumps(actual)}"
                    # The run report only exists when the drive SUCCEEDED. On the expect_error
                    # path there is none, and the rows are the whole of the evidence.
                    + (f"\n  writer report: {json.dumps(report)}" if report is not None else ""))

    def _teardown_db_isolation(self, failed: bool) -> None:
        """SPEC §5 step 8 / §13: drop whatever per-test DB isolation state
        `runtest()` step 4b created, unless the test failed (kept for debugging) or
        --slt-keep-resources/INT_KEEP_RESOURCES was passed (same gate
        `_keep_resources` applies for the fixture-based cleanup_postgres/
        cleanup_oracle above -- reimplemented here without a `request` fixture,
        since this Item never gets one). A no-op for a smoke/no-ddl test (`_pg_admin`/
        `_oracle_prefix`/`_spanner_prefix` all stay None, per step 4b)."""
        keep = (
            failed
            or self.config.getoption("--slt-keep-resources", False)
            or bool(os.environ.get("INT_KEEP_RESOURCES"))
        )
        if keep:
            return
        if self._pg_admin is not None:
            # Wipe this test's own Postgres data NOW rather than deferring to the
            # NEXT test's setup-time reset -- a passing test with no keep-flag
            # should leave nothing behind for a standalone/one-off run to confuse
            # manual debugging with. Symmetric with the setup-time reset above,
            # which stays in place too as defense-in-depth (e.g. cleaning up
            # after a kept run).
            try:
                if _parallel(os.environ):
                    self._pg_admin.reset_test_objects(self.tokens["TID"])
                else:
                    self._pg_admin.reset_schemas()
            except Exception as e:  # noqa: BLE001 - teardown must not mask the real outcome
                print(f"[integration] WARNING: failed to reset Postgres test objects "
                      f"for {self.manifest.name}: {e!r}")
        if self._oracle_prefix is not None:
            try:
                _drop_oracle_test_prefix(self._oracle_prefix)
            except Exception as e:  # noqa: BLE001
                print(f"[integration] WARNING: failed to drop Oracle tables prefixed "
                      f"{self._oracle_prefix!r}: {e!r}")
        if self._spanner_prefix is not None:
            # Only drops this test's ${TID}-prefixed tables (mirrors scripts/live's
            # _DDL_TEARDOWN_PREFIX_TOKENS behavior for spanner) -- deliberately NOT
            # live's serial-mode drop-everything fallback (prefix=""): ${TID} is
            # empty in serial runs by design (tokens.py), and a blanket drop of every
            # user table in a long-lived INT_KEEP_SERVICES emulator is a footgun with
            # no upside here. Accepted consequence: a serial run drops nothing, so
            # spanner DDL fixtures must be self-healing (DROP TABLE IF EXISTS).
            try:
                from . import spanneradmin as spanneradmin_mod

                for admin, _route in spanneradmin_mod.admins_for(self.tokens):
                    admin.drop_test_tables(self._spanner_prefix)
            except Exception as e:  # noqa: BLE001 - teardown must not mask the real outcome
                print(f"[integration] WARNING: failed to drop Spanner tables prefixed "
                      f"{self._spanner_prefix!r}: {e!r}")


# ============================================================================
# 12. PERFORMANCE-MODE YAML DISCOVERY AND EXECUTION (PERF_SPEC.md)
#
# Collected from scripts/integration/perf/**/test.yaml -- pytest_collect_file's
# location dispatch above routes THESE files to PerfYamlFile/PerfYamlItem instead of
# IntYamlFile/IntYamlItem, in BOTH pytest modes (collection itself does not know
# about --perf; a malformed perf test.yaml is a collection-time error either way,
# PERF_SPEC.md §2). pytest_collection_modifyitems below is what then makes `--perf`
# SELECT only these and makes plain `pytest` DESELECT all of them (PERF_SPEC.md
# §14) -- dispatch and selection are two separate hooks. A perf test.yaml that
# isn't ready to run yet uses `disabled:` (checked at runtest() time, below),
# not a selection-time flag -- there is no `performance.enabled` in this shape.
#
# runtest() drives PERF_SPEC.md §5's protocol: for each run_size (ascending, already
# sorted by perfmanifest.py) and each measured iteration, call inttest.perf's I/O orchestration
# functions (preflight once, then run_measured_iteration per iteration) -- this module
# owns none of that logic, only the loop, pytest lifecycle, and steps 1-3 (disabled
# gate / service provisioning / operator jar build), reused verbatim from IntYamlItem
# via the _provision_requires/_build_operator_jar helpers above since both item
# classes need them identically.
#
# Reporting (PERF_SPEC.md §11/§12 -- console layout, JSON report, reproducibility
# metadata): runtest() builds the report and writes the JSON before the final
# pytest.fail check, so a failing run still gets a report (§10). Raw per-iteration
# results are also stashed on the item (perf_run_size_reports/perf_unrecreatable_error)
# for anything else that wants them without re-running.
# ============================================================================


def _forget_raw_result(iteration):
    """Drop an iteration's `raw_result` before it is retained for the session.

    `IterationResult.raw_result` is the operator subprocess's full parsed JSON. It is
    needed only while `run_measured_iteration` is scoring that iteration; nothing
    downstream reads it (`perfreport._iteration_dict` does not emit it). Retaining it
    grew a `--perf` session's footprint with tests x run_sizes x measured_runs. The
    field stays on the dataclass -- `run_measured_iteration`'s own return value is
    unchanged -- only the retained copy is cleared.
    """
    if iteration is None or iteration.raw_result is None:
        return iteration
    return dataclasses.replace(iteration, raw_result=None)


def _run_all_iterations(*, manifest, performance, preflight_result, tokens, java_bin,
                         harness_jar, op_jar, scratch_dir, literal_property_keys=None):
    """Drive PERF_SPEC.md §5 across every `run_size` (ascending) x `measured_runs`
    for one perf test, calling `inttest.perf.run_measured_iteration` once per
    iteration. Extracted out of `PerfYamlItem.runtest()` so the `not_run`/
    `db_failed` bookkeeping around an `UnrecreatableEnvironmentError` (§10 --
    every not-yet-attempted run_size/iteration is recorded not_run rather than
    silently missing) is independently testable without a real JVM/DB.

    Returns (run_size_reports, unrecreatable_error, db_failed).
    """
    run_size_reports = []
    unrecreatable_error = None
    db_failed = False
    for run_size in performance.run_sizes:
        if unrecreatable_error is not None:
            run_size_reports.append({"run_size": run_size, "iterations": [], "not_run": True})
            continue
        iterations = []
        for _ in range(performance.measured_runs):
            try:
                iteration = perf_mod.run_measured_iteration(
                    manifest=manifest, performance=performance, preflight_result=preflight_result,
                    run_size=run_size, tokens=tokens,
                    java_bin=java_bin, harness_jar=harness_jar, op_jar=op_jar,
                    scratch_dir=scratch_dir, literal_property_keys=literal_property_keys,
                )
            except perf_mod.UnrecreatableEnvironmentError as e:
                unrecreatable_error = e
                db_failed = True
                # A surviving-process error carries the failed IterationResult for
                # the iteration that was actually in flight (§10: partial results
                # are always written and reported) -- append it before this run
                # size, and every later one, is marked not_run, so it isn't
                # silently dropped the way raising bare would leave it.
                if e.partial_iteration_result is not None:
                    iterations.append(_forget_raw_result(e.partial_iteration_result))
                break
            if iteration.status == "failed":
                db_failed = True
            iterations.append(_forget_raw_result(iteration))
        run_size_reports.append({
            "run_size": run_size,
            "iterations": iterations,
            "not_run": unrecreatable_error is not None and len(iterations) < performance.measured_runs,
        })
    return run_size_reports, unrecreatable_error, db_failed


class PerfYamlFile(pytest.File):
    """A pytest.File subclass for perf-tree test.yaml manifests (PERF_SPEC.md §3/§14).

    A perf test.yaml is fully self-contained -- structurally identical to a
    regression test.yaml except `performance:` replaces `assert:` (PERF_SPEC.md
    §3) -- so parsing/normalization is entirely `inttest.perfmanifest.
    load_perf_manifest`'s job; nothing here resolves a separate regression
    manifest or merges anything.
    """

    def collect(self):
        try:
            perf_manifest = perfmanifest_mod.load_perf_manifest(self.path)
        except perfmanifest_mod.PerfManifestError as e:
            pytest.fail(str(e))

        # ONE ITEM PER (VARIANT, PERMUTATION) -- the engine axis and the property axis
        # composed by TestManifest.runs(), exactly as the regression tier composes them.
        #
        # ⚠ Unlike the regression tier, which loops both axes INSIDE a single item, each run
        # is its own item here. A perf run's product is a NUMBER: twenty runs behind one item
        # would have to average or pick between twenty of them, and the difference between
        # two of them one property apart is the entire deliverable (§69.3). It also removes
        # §88's complaint that a fanned-out case reports `1 passed` whether it ran one run or
        # twenty -- here twenty runs are twenty reported items.
        m = perf_manifest.test_manifest
        for variant, overrides in m.runs():
            yield PerfYamlItem.from_parent(
                self,
                name=m.name + _run_id(variant, overrides),
                perf_manifest=perf_manifest,
                test_manifest=m,
                variant=variant,
                overrides=overrides,
            )


class PerfYamlItem(pytest.Item):
    """A pytest.Item representing one performance test.yaml (PERF_SPEC.md §3/§5).

    Like `IntYamlItem`, this is a raw `pytest.Item` (no fixtures filled), and uses
    `pytest.skip()`/`pytest.fail()` directly from `runtest()` rather than markers or
    exceptions, for the same `reportinfo()` reason documented on `IntYamlItem`.
    """

    def __init__(self, *, perf_manifest, test_manifest, variant=None, overrides=None, **kw):
        super().__init__(**kw)
        self.perf_manifest = perf_manifest
        # perf_manifest.test_manifest -- a perf test.yaml is fully self-contained
        # (PERF_SPEC.md §3), so op:/properties:/requires:/ddl:/seed:/types:/
        # password_properties: all come from THIS file, not a regression case.
        #
        # This item is ONE run of the two axes: the variant's DDL replaces the case-level
        # `ddl:` the loader refuses, and the permutation's values overlay `properties:`.
        # Both are folded in HERE rather than at each use, so everything downstream --
        # `_reset_once`, `preflight`, `run_measured_iteration`, the report -- works unchanged
        # off a manifest that already describes this run.
        self.variant = variant
        self.overrides = overrides or {}
        if variant is not None:
            test_manifest = dataclasses.replace(test_manifest, ddl=variant.ddl)
        if self.overrides:
            # ⚠ This can only ADD a key, never overlay one: `_normalize_matrix` REFUSES a name
            # present in both `properties:` and `matrix:` ("which wins is not something an
            # author should have to know"), so the two sets are disjoint by the time we get
            # here. Folded in so the JSON report's `configuration.operator.properties`
            # describes the permutation actually measured.
            test_manifest = dataclasses.replace(
                test_manifest, properties={**test_manifest.properties, **self.overrides})
        self.manifest = test_manifest

        self.add_marker(pytest.mark.integration)
        self.add_marker(pytest.mark.perf)
        for svc in test_manifest.requires:
            if svc == "postgres":
                self.add_marker(pytest.mark.postgres)
            elif svc == "oracle":
                self.add_marker(pytest.mark.oracle)
            elif svc == "spanner":
                self.add_marker(pytest.mark.spanner)
            elif svc == "gcs":
                self.add_marker(pytest.mark.gcs)

    def runtest(self):
        """Execute PERF_SPEC.md §5 for every `run_size × measured_runs` of this
        performance test."""
        pm = self.perf_manifest
        m = self.manifest
        performance = pm.performance

        # --- 1. Skip gate: disabled: truthy skips before any provisioning/build work,
        # same as IntYamlItem step 1 -- this perf test.yaml's own `disabled:` key
        # (PERF_SPEC.md §3: structurally identical to a regression test.yaml). ---
        if m.disabled and os.environ.get("SLT_RUN_DISABLED") != "1":
            reason = m.disabled if isinstance(m.disabled, str) else "disabled"
            pytest.skip(f"{m.name}: disabled ({reason}); set SLT_RUN_DISABLED=1 to force")

        # (PERF_SPEC.md §12): this run's start, and whether the jar build
        # below rebuilds or reuses -- both feed the JSON report's reproducibility
        # metadata once the run finishes.
        started_at = datetime.now(timezone.utc)
        rebuild_reasons = []

        # --- 2-3. Provision services, build the operator jar -- identical to
        # IntYamlItem, off this perf test.yaml's own manifest. ---
        lock = FileLock(str(_compose_lock_path()))
        _provision_requires(m.name, m.requires, lock)
        self.artifact = artifact = _build_operator_jar(m.name, m.module_ref, report=rebuild_reasons.append)

        try:
            harness_jar = harness_mod.ensure_harness_jar()
        except harness_mod.HarnessError as e:
            pytest.skip(f"{m.name}: cannot build the inttest harness jar: {e}")
        try:
            java_bin = harness_mod._resolve_java(None)
        except harness_mod.HarnessError as e:
            pytest.skip(f"{m.name}: {e}")

        # --- Tokens: same assembly as IntYamlItem step 4, off this perf
        # test.yaml's own manifest's dir/requires -- ${...} tokens in `properties:`
        # (rendered inside
        # inttest.perf.run_measured_iteration) resolve identically to integration
        # mode. Assembled BEFORE pre-flight (below) so pre-flight can render a
        # performance fixture's ${...} tokens too, the same way IntYamlItem already
        # renders assert.data[].input/match -- a performance fixture referencing e.g.
        # ${TID} must not silently reach the operator as the literal token text. ---
        parallel = _parallel(os.environ)
        tok = _build_tokens(m.dir, m.requires, parallel=parallel)
        tok.setdefault("OP_JAR", str(artifact.path))
        tok.setdefault("OP_NAME", artifact.op_name)
        # A variant's values may themselves carry `${...}` (`V_URL: '${POSTGRES_URL}'` is the
        # ordinary form), so they render against the base table before joining it -- the same
        # rule as IntYamlItem._variant_tokens, which is what lets one `properties:` block
        # naming ${V_URL}/${V_PROVIDER} serve every engine.
        # ⚠ `self.tokens` STAYS THE BASE TABLE; the variant's additions live in a separate
        # merged copy, exactly as IntYamlItem keeps `self.tokens` base and returns a merge from
        # `_variant_tokens`. That split is load-bearing rather than stylistic:
        # `_teardown_db_isolation` reads `self.tokens` for `TID`, `TID_ORACLE` and the Postgres
        # DSN, so folding variant values in would let a variant token NAMED `TID` or
        # `POSTGRES_URL` point the final drop at the wrong database.
        self.tokens = dict(tok)
        if self.variant is not None:
            # Rendered against the BASE table, then added -- never against a table this loop is
            # mutating. Rendering into `tok` as we went would let one variant token resolve
            # another, so `{V_PREFIX: 'qa_', V_TABLE: '${V_PREFIX}t'}` would resolve here and
            # raise SubstitutionError in the regression tier, and swapping the two YAML keys
            # would change this tier's own answer.
            base = dict(tok)
            for key, value in self.variant.tokens.items():
                tok[key] = tokens_mod.render(value, base)

        # --- Pre-flight (PERF_SPEC.md §3 stage 2): once per test, before the first
        # warmup replay -- a configuration/data problem here stops execution before
        # any performance run begins (§10), so this is a failure, not a skip. ---
        try:
            preflight_result = perf_mod.preflight(performance, tokens=tok, source=m.source)
        except perf_mod.PerfPreflightError as e:
            pytest.fail(f"{m.name}: performance pre-flight failed: {e}")
        self.perf_preflight = preflight_result

        # --- PERF_SPEC.md §5: for each run_size (ascending) and each measured
        # iteration, reset + launch + measure. ---
        scratch_dir = Path(tempfile.mkdtemp(prefix="inttest-perf-"))
        # ⚠ BOUND BEFORE THE TRY, because the `finally` reads it. `_run_all_iterations` catches
        # only UnrecreatableEnvironmentError, so anything else out of it -- a SubstitutionError
        # from an unresolvable ${...} in `properties:`, say -- left this name unassigned and the
        # `finally` raised UnboundLocalError, which both masked the real cause and skipped
        # teardown, leaking the run's ${TID} tables.
        db_failed = False
        try:
            run_size_reports, unrecreatable_error, db_failed = _run_all_iterations(
                manifest=m, performance=performance, preflight_result=preflight_result,
                tokens=tok, java_bin=java_bin, harness_jar=harness_jar, op_jar=artifact.path,
                scratch_dir=scratch_dir, literal_property_keys=frozenset(self.overrides),
            )
        finally:
            shutil.rmtree(scratch_dir, ignore_errors=True)
            self._teardown_db_isolation(db_failed)

        self.perf_run_size_reports = run_size_reports
        self.perf_unrecreatable_error = unrecreatable_error

        # --- (PERF_SPEC.md §11/§12): build + write the JSON report and emit
        # the console block -- BEFORE the pytest.fail below, so a failing run still
        # gets its report (§10: "partial results are always written and reported").
        # Broadly guarded: a bug in metadata collection/report assembly must not
        # itself abort the test and swallow the pytest.fail summary below, which is
        # the only place a failed iteration's reason otherwise surfaces.
        #
        # Writing the JSON report to disk is opt-in: only when --perf-json PATH is
        # given explicitly. Without it, the report is still built and the console
        # block still prints -- only the disk write (and its "Report: <path>"
        # console line, which format_console only adds when given a path) are
        # skipped, so a bare `pytest --perf` run doesn't silently accumulate one
        # JSON file per test under .perf-results/ every time. ---
        perf_json_option = self.config.getoption("--perf-json")
        write_json = perf_json_option is not None
        report = None
        json_path = None
        try:
            metadata = perfreport_mod.collect_metadata(
                test_dir=m.dir, artifact=artifact,
                release=releases_mod.resolve_release(os.environ),
                rebuild_reason=(rebuild_reasons[0] if rebuild_reasons else None),
                striim_home=os.environ.get("STRIIM_HOME"), java_bin=java_bin, timestamp=started_at,
            )
            report = perfreport_mod.build_report(
                perf_manifest=pm, test_manifest=m, preflight_result=preflight_result,
                run_size_reports=run_size_reports, unrecreatable_error=unrecreatable_error,
                metadata=metadata,
                variant=(self.variant.name if self.variant is not None else None),
                permutation=self.overrides,
            )
            if write_json:
                # The FINAL selection (after -k/-m/other pytest_collection_modifyitems
                # hooks, which may run before or after this plugin's own) -- read from
                # session.items rather than counting inside this plugin's own
                # pytest_collection_modifyitems, which only sees pre-`-k` counts and
                # would otherwise miscount a `--perf -k <name>` single-test selection
                # as "multiple", turning a requested --perf-json FILE into a directory.
                selected_perf_count = sum(1 for i in self.session.items if isinstance(i, PerfYamlItem))
                # The perf CASE's full path relative to perf/ (flattened, perfreport.
                # report_key_for), not test_manifest.name, keys the report filename --
                # see that function's docstring for why `.name` alone still collides.
                # The JSON's own "test.name" field (build_report, from
                # test_manifest.name) still carries the authored display name.
                # ⚠ The run id is part of the key, not decoration: ONE perf_dir now
                # produces one report PER RUN, so the path alone stopped identifying a
                # report and only the second-granularity timestamp would separate two of
                # them -- see report_key_for's docstring.
                report_key = perfreport_mod.report_key_for(
                    pm.dir, _PERF_DIR, _run_id(self.variant, self.overrides))
                json_path = perfreport_mod.resolve_report_path(
                    perf_json_option, _PERF_RESULTS_DIR, report_key, started_at,
                    multiple_tests=selected_perf_count > 1,
                )
        except Exception as e:  # noqa: BLE001
            print(f"[integration] WARNING: failed to build the performance report for {m.name}: {e!r}")

        if write_json and report is not None and json_path is None:
            # Defensive: report and json_path are assigned in the same try block
            # above, so this should be unreachable -- but a bare `if report is not
            # None:` below would otherwise pass json_path=None straight into
            # write_report(), which raises TypeError (not OSError), escaping the
            # except clause below and this whole runtest() call, silently losing
            # the pytest.fail summary that is the only place a failed iteration's
            # reason otherwise surfaces.
            print(f"[integration] WARNING: performance report for {m.name} was built but its "
                  f"path could not be resolved; not written.")
            report = None

        if report is not None:
            write_error = None
            if write_json:
                try:
                    perfreport_mod.write_report(report, json_path)
                except OSError as e:
                    write_error = e
                    print(f"[integration] WARNING: failed to write performance JSON report to {json_path}: {e!r}")
            wrote_ok = write_json and write_error is None
            self.perf_report = report
            self.perf_report_path = json_path if wrote_ok else None
            console_text = perfreport_mod.format_console(
                report, report_path=(json_path if wrote_ok else None))
            if write_json and write_error is not None:
                console_text += f"\n  (JSON report write to {json_path} FAILED: {write_error!r})"
            _terminal_emit(self.config, console_text)

        failed_iterations = [
            it for r in run_size_reports for it in r["iterations"] if it.status == "failed"]
        not_run_sizes = [r for r in run_size_reports if r["not_run"]]
        if unrecreatable_error is not None or failed_iterations or not_run_sizes:
            lines = [f"{m.name}{_run_label(self.variant, self.overrides)}: performance run FAILED"]
            if unrecreatable_error is not None:
                lines.append(
                    f"  environment became unrecreatable "
                    f"({unrecreatable_error.failure_class}): {unrecreatable_error}")
            for it in failed_iterations:
                lines.append(f"  run_size={it.run_size_authored}: {it.failure_reason}")
            if not_run_sizes:
                lines.append(
                    "  not run: " + ", ".join(r["run_size"].authored for r in not_run_sizes))
            lines.append(f"  JSON report: {json_path}" if json_path is not None
                         else "  JSON report: (not written -- see warnings above)")
            pytest.fail("\n".join(lines))

    def _teardown_db_isolation(self, failed: bool) -> None:
        """Drop this test's per-test Postgres/Oracle test objects after the
        performance run, unless it failed (kept for debugging) or
        --slt-keep-resources/INT_KEEP_RESOURCES was passed -- same gate as
        `IntYamlItem._teardown_db_isolation`.

        Unlike `IntYamlItem` (which creates its isolation objects once), every
        `run_measured_iteration` call drops-and-recreates the SAME Postgres
        reset target / Oracle prefix (PERF_SPEC.md §9, perf.py::_reset_once) --
        nothing else drops what the LAST iteration left behind, so this is that
        final drop, not a repeat of per-iteration reset.

        ⚠ SPANNER IS DROPPED HERE TOO, and this docstring previously gave a reason not to that
        was wrong: "Spanner is `isolation: none` ... nothing per-test to drop". The INSTANCE is
        shared, but a case still creates `${TID}`-prefixed TABLES in it, and
        `IntYamlItem._teardown_db_isolation` has always dropped those -- which is why the
        regression every-engine case does not leak. Nothing in the perf tier did, and the first
        perf case with Spanner variants would have leaked `t_upsert`/`chkpoint` into the shared
        emulator on every parallel run, on SUCCESS as well as failure.

        ⚠ SQL SERVER IS DROPPED BY NEITHER TIER -- `perf._reset_once` has no `sqlserver` branch
        either. Both rely on each case's own `DROP TABLE IF EXISTS` DDL, which self-heals a
        re-run of the SAME `${TID}` but leaves a parallel run's tables behind. Stated rather
        than fixed here: it is the tier's shape rather than this item's, and changing it
        belongs with the reset path."""
        keep = (
            failed
            or self.config.getoption("--slt-keep-resources", False)
            or bool(os.environ.get("INT_KEEP_RESOURCES"))
        )
        if keep:
            return
        m = self.manifest
        if "postgres" in m.requires and hasattr(self, "tokens"):
            # Fixed qasource/qatarget schemas, ${TID}-prefixed objects -- same
            # model as IntYamlItem's Postgres branch (docs/internals/INTEGRATION-ENGINE.md#token-isolation
            # §4.5). _reset_once already reset before every measured iteration;
            # this is the final drop so a passing run leaves nothing behind.
            try:
                pg = _pg_admin(self.tokens, role="source")
                if _parallel(os.environ):
                    pg.reset_test_objects(self.tokens["TID"])
                else:
                    pg.reset_schemas()
            except Exception as e:  # noqa: BLE001 - teardown must not mask the real outcome
                print(f"[integration] WARNING: failed to reset Postgres test objects "
                      f"for {m.name}: {e!r}")
        if "oracle" in m.requires and hasattr(self, "tokens"):
            # ${TID_ORACLE}, NOT ${TID_ORACLE_DB} (removed -- see tokens.isolation_tokens):
            # empty in a serial run (SPEC §6), same as IntYamlItem's _oracle_prefix, so
            # skip the call entirely rather than passing _drop_oracle_test_prefix an
            # empty prefix it would reject.
            tid_oracle = self.tokens.get("TID_ORACLE")
            if tid_oracle:
                try:
                    _drop_oracle_test_prefix(tid_oracle)
                except Exception as e:  # noqa: BLE001
                    print(f"[integration] WARNING: failed to drop Oracle tables for {m.name}: {e!r}")
        if "spanner" in m.requires and hasattr(self, "tokens") and self.tokens.get("TID"):
            # Mirrors IntYamlItem's Spanner branch exactly, including its choice NOT to fall
            # back to a blanket prefix="" drop in serial runs: ${TID} is empty there by design,
            # and wiping every user table in a long-lived INT_KEEP_SERVICES emulator is a
            # footgun. Accepted consequence, same as the regression tier's: a serial run drops
            # nothing, so Spanner DDL must stay self-healing (DROP TABLE IF EXISTS).
            try:
                from . import spanneradmin as spanneradmin_mod

                for admin, _route in spanneradmin_mod.admins_for(self.tokens):
                    admin.drop_test_tables(self.tokens["TID"])
            except Exception as e:  # noqa: BLE001 - teardown must not mask the real outcome
                print(f"[integration] WARNING: failed to drop Spanner tables prefixed "
                      f"{self.tokens['TID']!r} for {m.name}: {e!r}")


def pytest_collection_modifyitems(config, items):
    """Symmetric selection filtering between the two collection trees (PERF_SPEC.md
    §14): `perf/` is a real `testpaths` root now, so `PerfYamlItem`s exist in the
    collected tree in BOTH modes -- this hook is what makes each mode see only its
    own kind.

    Under `--perf`: deselect everything except `PerfYamlItem`s; `pytest.UsageError`
    if nothing remains, naming what was searched. A perf test.yaml that isn't ready
    to run yet uses `disabled:` (checked at `runtest()` time, same as
    `IntYamlItem`), not a selection-time flag -- there is no `performance.enabled`
    to filter on here (removed; see PERF_SPEC.md §3). Without `--perf`: deselect
    every `PerfYamlItem` outright -- they are never valid as ordinary integration
    tests (no `assert:` of their own).

    Also where `--perf-reverse` (§115.6) applies: the selected list is reversed here, once,
    because this is the only place that sees BOTH the matrix permutations and the case
    sequence the finding names.
    """
    if config.getoption("--perf"):
        selected = [item for item in items if isinstance(item, PerfYamlItem)]
        deselected = [item for item in items if item not in selected]
        if not selected:
            searched = list(_PERF_DIR.rglob("test.yaml")) if _PERF_DIR.is_dir() else []
            raise pytest.UsageError(
                f"--perf selected no performance tests (searched {_PERF_DIR} for "
                f"'test.yaml' files; found {len(searched)} perf test.yaml file(s) total)"
            )
    else:
        selected = [item for item in items if not isinstance(item, PerfYamlItem)]
        deselected = [item for item in items if isinstance(item, PerfYamlItem)]

    if config.getoption("--perf-reverse"):
        # ⚠ §115.6, AND IT IS ONE MECHANISM ON PURPOSE. Reversing the selected list reverses
        # BOTH things that finding names: adjacent `matrix:` permutations, which are contiguous
        # within a file, and separate cases that "run in sequence in one session and were never
        # order-controlled" -- §69.3's five hand-written pairs. Reversing in `runs()` INSTEAD
        # would cover only the first; reversing in BOTH would reverse permutations twice and
        # leave them in forward order while the files moved, which looks like a control and is
        # not one.
        #
        # Item NAMES are untouched, so a reversed report pairs against a forward one by run id.
        if not config.getoption("--perf"):
            raise pytest.UsageError("--perf-reverse is an ordering control for the performance "
                                    "tier and does nothing without --perf")
        selected = list(reversed(selected))

    if deselected:
        config.hook.pytest_deselected(items=deselected)
    items[:] = selected
