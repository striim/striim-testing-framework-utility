from __future__ import annotations
from dataclasses import dataclass
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock

from livetest import paths, service_healing
from livetest import lockdir, stack
from livetest import striim_provision
from livetest.registry import load_service, ServiceDef

_PROVISION_REGISTRY = ".slt-provision-registry.json"
_PROVISION_LOCK = ".slt-provision-registry.lock"

def _state_dir() -> Path:
    # SLT_STATE_DIR (default scripts/live) — the same coordination dir opregistry uses,
    # gitignored per §C.4.
    return paths.state_dir()

def _provision_state_paths(state_dir=None, env=None) -> tuple[Path, Path]:
    # Prefix-scoped per stack (SLT_STACK_PREFIX): two stacks run from the SAME checkout, so
    # each needs its own registry+lock — a shared file would let one stack's provision
    # records (container names, postgres-setup/mssql-setup keys) leak into the other's run.
    # The REGISTRY stays with the caller's state dir (it records this run's provisioning),
    # but the LOCK is machine-wide -- see stack.lock_path(). Containers are a per-machine
    # resource, so two checkouts bringing up the same stack must serialise even though their
    # registries are separate.
    d = Path(state_dir) if state_dir is not None else _state_dir()
    return (d / stack.state_name(_PROVISION_REGISTRY, env),
            stack.lock_path(_PROVISION_LOCK, env))

def clear_provision_registry(state_dir=None) -> None:
    """Delete the service-provision registry (controller, session start). A prior run's stale
    record must not make a worker skip bringing up a container that is no longer running."""
    reg, _ = _provision_state_paths(state_dir)
    try:
        reg.unlink()
    except FileNotFoundError:
        pass

def container_running(container: str, run=None) -> bool:
    """True iff `container` exists and is running, straight from `docker inspect`.

    Deliberately not a port/health probe: the question a provision record raises is whether
    the thing it recorded still EXISTS, not whether it is ready to serve. Docker missing or
    the container unknown both read as "not running" rather than raising -- a caller deciding
    whether a record is stale doesn't need to tell those apart."""
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    try:
        out = run(["docker", "container", "inspect", "-f", "{{.State.Running}}", container])
    except OSError:
        return False
    return (getattr(out, "stdout", "") or "").strip() == "true"

def container_exists(container: str, run=None) -> bool:
    """True iff a container by that NAME exists, in ANY state.

    Distinct from `container_running`, and the right question after `compose down`: Docker
    names are unique across every state, so an `exited` leftover reserves the name exactly as
    hard as a running one and the next `up` fails on it either way. `container_running` asks
    the other question -- "is the thing this record describes still serving?" -- which is what
    a stale provision record needs."""
    # `docker container inspect`, not bare `docker inspect`: the bare form also resolves
    # images, volumes and networks, and volumes/networks carry `.Name` -- so a VOLUME named
    # slt-postgres would read as a surviving container, failing `stop live postgres` with a
    # `docker rm -f` that cannot clear it.
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    try:
        out = run(["docker", "container", "inspect", "-f", "{{.Name}}", container])
    except OSError:
        return False
    return getattr(out, "returncode", 1) == 0

# --------------------------------------------------------------------------------------------
# What this checkout believes it owns
#
# One question, asked the same way by every teardown path (services AND the Striim cluster):
# "which of the container names our own compose files declare are still held by something?"
# Each is a compose project we renamed on this branch, so `docker compose down` -- which is
# project-scoped -- silently misses anything created under the old project and exits 0. This
# was found and patched three separate times, once per rename, before being made one function.
# --------------------------------------------------------------------------------------------

# `${SLT_STACK_PREFIX:+${SLT_STACK_PREFIX}-}` and friends: one level of nesting is all the
# compose files use, and stripping the whole expression leaves the literal base name that
# stack.prefixed() then re-derives from the CURRENT environment.
_INTERPOLATION = re.compile(r"\$\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}")

# current container name -> names it USED to have. A rename is invisible to both `compose down`
# (the old container is in no project the new file names) and to a check that reads only
# today's compose file, so the old name has to be carried explicitly. Drop an entry once no
# checkout can plausibly still be holding that container.
_RENAMED_CONTAINERS = {
    "slt-token": ["slt-gcs-token"],
    "slt-node": ["slt-striim-node"],
    "slt-agent": ["slt-striim-agent"],
}


def declared_containers(compose_paths, env=None) -> list[str]:
    """Every container name `compose_paths` declare, prefix-scoped, plus their former names.

    Read from the compose files rather than from service.yaml's `container:` (which names only
    the primary, missing the gcs/kafka sidecars) or a hand-maintained tuple (which drifts):
    the compose file IS the declaration. Unreadable files contribute nothing."""
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
            for name in [value, *_RENAMED_CONTAINERS.get(value, [])]:
                name = stack.prefixed(name, env)
                if name not in names:
                    names.append(name)
    return names


def orphaned_containers(compose_paths, env=None) -> list[str]:
    """Declared containers that still EXIST -- what a `compose down` left behind.

    Existence, not running-ness: Docker names are unique across every state, so an `exited`
    leftover reserves the name and blocks the next `up` exactly as a running one does."""
    return [c for c in declared_containers(compose_paths, env) if container_exists(c)]


def service_compose_path(defn: ServiceDef):
    """The service's compose file, or None when the definition names none."""
    if not getattr(defn, "dir", None) or not getattr(defn, "compose", None):
        return None
    return Path(defn.dir) / defn.compose

def forget_provisioned(keys, state_dir=None) -> None:
    """Drop `keys` from the provision registry — the de-provision counterpart of
    `ensure_provisioned_once`, for a caller that tears a container down OUTSIDE a session
    boundary. Workers never call this (spec §C.4's asymmetry is about workers); an explicit
    `livetest.cli stop <service>` must, or its record outlives the container and the next
    bring-up finds the key, skips `docker compose up`, and reports success over nothing."""
    reg, lock = _provision_state_paths(state_dir)
    with FileLock(str(lock), mode=lockdir.file_mode()):
        try:
            state = json.loads(reg.read_text())
        except (FileNotFoundError, ValueError):
            return
        if not isinstance(state, dict):
            return          # junk registry: nothing to forget, and not this function's to repair
        for key in keys:
            state.pop(key, None)
        reg.write_text(json.dumps(state))

def ensure_provisioned_once(container: str, do_provision, state_dir=None) -> bool:
    """Bring a shared service container up exactly once across xdist workers (spec §C.4).

    Under -n each worker is its own process with its own `started` set, so >1 worker would
    otherwise race `docker compose up` on the SAME container (the observed
    'container name "/slt-oracle" is already in use' conflict) AND re-run a disruptive
    post_up (e.g. Oracle's archivelog restart) under a sibling's already-running tests. A
    filelock + JSON registry makes the first worker provision while the rest block, then find
    the record and skip. Workers never de-provision (spec §C.4 asymmetry). Returns True if
    THIS call performed the provisioning."""
    reg, lock = _provision_state_paths(state_dir)
    context = service_healing.current_context()
    timeout = context.timeout() if context else -1
    with FileLock(str(lock), mode=lockdir.file_mode(), timeout=timeout):
        if context:
            context.check()
        try:
            state = json.loads(reg.read_text())
        except (FileNotFoundError, ValueError):
            state = {}
        if container in state:
            return False
        do_provision()
        state[container] = "1"
        reg.write_text(json.dumps(state))
        return True

class ServiceError(Exception):
    pass

class ProvisionRefused(ServiceError):
    """Provisioning was refused by an ownership, admission, or cancellation guard."""


class DockerUnavailable(ServiceError):
    pass

@dataclass
class ResolvedService:
    name: str
    mode: str          # "live" | "docker"
    base: dict
    started: bool

def derive_per_test_base(svc_name: str, base: dict, tid: str, env=None) -> dict:
    """Return a copy of `base` with per-test object names derived from `tid` -- the
    hashed per-test TID (plugin._tid_oracle: "T"+9 hex, lowercased at the call site),
    the same identity the ${TID} token renders in parallel runs minus that token's
    trailing "_" separator; passed un-gated here so serial runs keep per-test isolation
    too (spec §A.3). kafka: src_topic=f"slt_{tid.lower()}_src", tgt likewise. gcs:
    src_bucket=f"slt-{t}-src" (t = tid lowercased, "_"->"-"), tgt likewise -- placement
    is now CONSISTENT between kafka and gcs: the tid sits BETWEEN "slt" and "src"/"tgt".
    The hash keeps bucket names bounded-length (18 chars + ns, far under GCS's 63-char
    cap -- the old slug-keyed names grew with test-name length). Every other service:
    unchanged copy.

    Under a stack prefix (SLT_STACK_PREFIX) the gcs buckets carry it too --
    f"slt-{tid}-{ns}-src" -- because buckets live in ONE shared emulator/project per
    host: unlike containers, a compose network cannot isolate them, so two stacks
    running the same test would otherwise seed and diff the SAME bucket. The ns (stack
    prefix) trails the tid to match the estate's per-test object convention
    (${TID}${NS}_<name>; tid first, then ns), hyphenated because GCS bucket names forbid
    "_". Kafka topics are network-isolated per stack, so they are left alone. The old
    tid-as-suffix rationale -- keep the literal "slt-src"/"slt-tgt" head visible to
    enforcement's _FIXED_NAME_RE lookahead -- is obsolete: with the tid interposed the
    bare literal never occurs in a derived name (as was always true for kafka), and
    enforcement still flags truly bare "slt-src"/"slt-tgt"."""
    out = dict(base)
    if svc_name == "kafka":
        t = tid.lower()
        out["src_topic"] = f"slt_{t}_src"
        out["tgt_topic"] = f"slt_{t}_tgt"
    elif svc_name == "gcs":
        t = tid.lower().replace("_", "-")
        # stack.prefix() is the one accessor: it validates+normalizes the ns to [a-z0-9-],
        # already inside the GCS bucket charset (an "_"-bearing prefix fails loud there, so
        # it can never reach bucket naming). Trails the tid, hyphen-joined.
        ns = stack.prefix(env)
        t = f"{t}-{ns}" if ns else t
        out["src_bucket"] = f"slt-{t}-src"
        out["tgt_bucket"] = f"slt-{t}-tgt"
    return out

def _classify_compose_phase(line: str):
    """Map a `docker compose up` progress line to a coarse phase word, or None."""
    l = line.lower()
    if "pulling" in l or "downloading" in l or "extracting" in l or "pull complete" in l:
        return "pulling image"
    if "building" in l or "load build" in l or l.strip().startswith("step "):
        return "building image"
    if "waiting" in l or "healthy" in l or "health" in l:
        return "waiting for healthy"
    if "creating" in l or "created" in l:
        return "creating container"
    if "starting" in l or "started" in l or "running" in l:
        return "starting container"
    return None

def _compose_env() -> dict:
    """The environment for a `docker compose` subprocess: os.environ enriched with any Striim
    license/cluster vars (COMPANY_NAME/CLUSTER_NAME/PRODUCT_KEY/LICENCE_KEY) derivable from
    $STRIIM_HOME/conf/startUp.properties when they are unset/blank (see
    striim_provision.enrich_license_env). A `docker compose` subprocess otherwise inherits
    os.environ and, for a striim bring-up with no license env, warns "variable is not set.
    Defaulting to a blank string" and the cluster is doomed. Harmless for the non-striim service
    composes brought up here — they don't interpolate those vars, so the extra keys are inert;
    an explicit env value always wins. os.environ itself is never mutated."""
    from livetest import paths
    enriched, _ = striim_provision.enrich_license_env(paths.effective_env(os.environ))
    return enriched

def _compose_reset(compose) -> None:
    """Best-effort `down -v` before a bring-up, removing any containers AND persistent
    volumes left by a prior run so each service starts from clean state — otherwise a
    stale volume (Oracle datafiles, Postgres data dir, Kafka logs, a CDC replication
    slot) leaks across runs and quietly poisons the test. A no-op when nothing is up."""
    try:
        subprocess.run(["docker", "compose", "-f", str(compose), "down", "-v"],
                       capture_output=True, text=True, env=_compose_env())
    except FileNotFoundError as e:
        raise DockerUnavailable(f"docker not found: {e}") from e


_NOT_RUNNING = ("created", "exited", "paused", "restarting", "dead")


def _has_stopped_containers(compose, cenv) -> bool:
    """True when the service's compose project has a container that is not running: a stack an
    earlier run left stopped, or another checkout that shares the project name (the project is
    named by the compose file's `name:`, not by its path)."""
    argv = ["docker", "compose", "-f", str(compose), "ps", "-a", "-q"]
    for status in _NOT_RUNNING:
        argv += ["--status", status]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, env=cenv)
    except FileNotFoundError as e:
        raise DockerUnavailable(f"docker not found: {e}") from e
    return r.returncode == 0 and bool((r.stdout or "").strip())


def _preflight_profile(defn: ServiceDef) -> None:
    """Before any reset or up command the service's whole profile -- definition, its DECLARED
    compose file, bind-mounted init files, build contexts and COPY sources -- must be present
    in the one origin it was loaded from (no fall-through to another root)."""
    from livetest import resource_profiles
    try:
        resource_profiles.select_profile("live", defn.name, services_dir=Path(defn.dir).parent)
    except resource_profiles.ProfileError as e:
        raise ServiceError(f"preflight failed for {defn.name}: {e}") from e


def _default_compose_up(defn: ServiceDef, progress=None) -> None:
    context = service_healing.current_context()
    if context:
        context.check()
    _preflight_profile(defn)
    compose = defn.dir / defn.compose
    cenv = _compose_env()   # os.environ + any STRIIM_HOME-derived license vars (striim bring-up)
    if not os.environ.get("SLT_KEEP_SERVICES") and not (context and defn.name == "kafka"):
        # Wipe stale volumes so a fresh bring-up starts from clean state. Under SLT_KEEP_SERVICES
        # (parallel runs / fast re-runs, spec §C.6) we must NOT `down -v` first — that destroys the
        # shared emulator volumes a sibling worker or a prior kept run depends on; reuse them as-is
        # (the up below is a no-op against an already-healthy service).
        _compose_reset(compose)
    elif defn.name == "kafka" and _has_stopped_containers(compose, cenv):
        # Kept services are reused as they are, but a STOPPED Kafka stack is not reusable: its
        # ZooKeeper reloads the stopped broker's registration, which expires only 18 s after
        # ZooKeeper starts, and a broker started inside that window exits 1 with
        # NodeExistsException at registerBroker ("dependency failed to start: container
        # slt-kafka exited (1)"; seen with a stack another framework checkout left stopped).
        # Nothing can be using a stopped Kafka, so it starts from clean state instead.
        _compose_reset(compose)
    # --build: `up` alone builds ONLY when the image is absent, so a service with a `build:`
    # stanza (postgres, mssql, oracle) silently keeps a stale image after its Dockerfile or
    # init scripts change -- on any machine that built it once. Measured cost of the rebuild
    # check when nothing changed: postgres 0.74s, oracle 1.05s (20 KB contexts), because
    # Docker's own layer cache does the detection and skips every unchanged step. Services
    # with no `build:` (gcs, kafka, spanner) are unaffected: there is nothing to build.
    #
    # NOT applied to the Striim cluster, which builds through striim_provision.ensure_image:
    # its context carries deps/ (15 GB), so the same no-op check costs ~152s on a cold BuildKit
    # context (1.8s warm, but which you get is not predictable) -- see that module for how
    # staleness is detected there instead.
    argv = ["docker", "compose", "-f", str(compose), "up", "-d", "--wait", "--build"]
    since = datetime.now(timezone.utc)
    if context and defn.name == "kafka":
        r = _run_preflight_compose(argv, cenv, context, defn.name, progress)
        if r.returncode:
            _recover_compose_failure(context, defn, cenv, since, r.stdout.strip())
        return
    if progress is None:
        try:
            r = subprocess.run(argv, capture_output=True, text=True, env=cenv)
        except FileNotFoundError as e:
            raise DockerUnavailable(f"docker not found: {e}") from e
        if r.returncode != 0:
            if not os.environ.get("SLT_KEEP_SERVICES"):
                # Under SLT_KEEP_SERVICES (parallel runs, spec §C.6) a transient up-failure
                # must NOT `down -v` — that destroys the shared emulator volumes sibling
                # workers depend on. Leave them standing; the operator/next serial run cleans up.
                subprocess.run(["docker", "compose", "-f", str(compose), "down", "-v"],
                               capture_output=True, text=True, env=cenv)
            raise ServiceError(f"docker compose up failed for {defn.name}: {r.stderr.strip()}")
        return
    # Streaming variant: surface live phase feedback while `up --wait` blocks (cold start).
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, env=cenv)
    except FileNotFoundError as e:
        raise DockerUnavailable(f"docker not found: {e}") from e
    tail, last = [], None
    for line in proc.stdout:
        tail.append(line)
        if len(tail) > 40:
            tail.pop(0)
        phase = _classify_compose_phase(line)
        if phase and phase != last:
            last = phase
            progress(defn.name, phase)
    proc.wait()
    if proc.returncode != 0:
        if not os.environ.get("SLT_KEEP_SERVICES"):
            # See the non-streaming path above: skip the volume-nuking `down -v` cleanup
            # under SLT_KEEP_SERVICES so a worker's up-failure doesn't wipe shared emulators.
            subprocess.run(["docker", "compose", "-f", str(compose), "down", "-v"],
                           capture_output=True, text=True, env=cenv)
        raise ServiceError(f"docker compose up failed for {defn.name}: {''.join(tail).strip()[-500:]}")

def _run_preflight_compose(argv, env, context, name, progress):
    """Bound the initial Kafka attempt, with the same failure path in both APIs."""
    import selectors
    import signal
    import time
    # Unix host/container execution (Mac and Linux); own only this command group.
    context.deadline = context.clock() + 900
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env=env, start_new_session=True)
    except FileNotFoundError as exc:
        raise DockerUnavailable(f"docker not found: {exc}") from exc
    tail, partial, last = b"", b"", None
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while selector.get_map() or proc.poll() is None:
                context.check()
                for key, _ in selector.select(min(.25, context.deadline-context.clock())):
                    data = os.read(key.fileobj.fileno(), 4096)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    tail = (tail + data)[-65536:]
                    partial = (partial + data)[-65536:]
                    lines = partial.split(b"\n")
                    partial = lines.pop()
                    for line in lines:
                        phase = _classify_compose_phase(line.decode(errors="replace"))
                        if progress and phase and phase != last:
                            last = phase
                            progress(name, phase)
                if not selector.get_map() and proc.poll() is None:
                    time.sleep(.05)
        proc.wait()
        context.check()
        return subprocess.CompletedProcess(argv, proc.returncode, tail.decode(errors="replace"), "")
    except service_healing.Refused as exc:
        context.original_error = service_healing._redact(tail.decode(errors="replace"))
        context.emit("cancelled" if context.cancelled() else "exhausted", "initial compose", str(exc))
        context.summary(False, str(exc))
        raise ServiceError(f"Kafka initial compose interrupted: {exc}") from exc
    finally:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait()
        proc.stdout.close()


def _recover_compose_failure(context, defn, env, since, error):
    if context is None or defn.name != "kafka":
        return
    try:
        if env.get("SLT_KAFKA_HOST"):
            raise service_healing.Refused("live override cannot heal")
        service_healing.recover_kafka(
            context, service_healing.DockerEvidence(context, defn, env), since, error)
    except service_healing.Refused as exc:
        raise ServiceError(f"docker compose up failed for {defn.name}: "
                           f"{service_healing._redact(error)}; recovery refused: {exc}") from exc


def _default_post_up(defn: ServiceDef) -> None:
    # Run the service's post-up script inside the (now-healthy) container. Used for
    # steps that are unsafe during container startup — e.g. Oracle's archivelog
    # mount-restart (see services/oracle/postup/enable_archivelog.sh).
    r = subprocess.run(
        ["docker", "exec", defn.container, "bash", defn.post_up],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise ServiceError(
            f"post_up failed for {defn.name} ({defn.post_up}): "
            f"{(r.stderr or r.stdout).strip()[-500:]}")

def compose_down(defn: ServiceDef, compose_run=None) -> None:
    """Tear the service's compose project down, raising ServiceError if docker refuses.

    The returncode check is load-bearing, not defensive: callers key "did this come down?"
    off whether we raised. Swallowing a non-zero exit (daemon down, in-use volume, compose
    error) makes a failed teardown look successful -- so `stop` reports 0 over a container
    that is still running, and `_take_down_services` goes on to drop its provision record,
    which then lets the next bring-up re-run a disruptive `post_up` (Oracle's archivelog
    restart) against it. That record is exactly what ensure_provisioned_once exists to hold."""
    run = compose_run or (lambda args: subprocess.run(args, capture_output=True, text=True, env=_compose_env()))
    compose = defn.dir / defn.compose
    r = run(["docker", "compose", "-f", str(compose), "down", "-v"])
    # A stub compose_run (tests, the console) may return None rather than a CompletedProcess;
    # only a real, non-zero returncode is a failure.
    if getattr(r, "returncode", 0):
        raise ServiceError(f"docker compose down failed for {defn.name}: "
                           f"{(getattr(r, 'stderr', '') or '').strip()[-500:]}")

def resolve(name: str, env: dict, started: set, compose_up=None, post_up=None, progress=None) -> ResolvedService:
    defn = load_service(name)
    from livetest.service_env import require_env, RegistryError
    try:
        require_env(name, getattr(defn, "required_env", ()), env)
    except RegistryError as exc:
        raise ServiceError(str(exc)) from exc
    up = compose_up or (lambda d: _default_compose_up(d, progress=progress))
    run_post_up = post_up or _default_post_up
    if defn.live_override_env and env.get(defn.live_override_env):
        base = {}
        for key, envname in defn.live_env.items():
            base[key] = env.get(envname, (getattr(defn, "live_defaults", None) or {}).get(key, defn.docker_defaults.get(key)))
            if base[key] is None:
                # Unset, with no default (an OAuth client in a basic-auth test): its token renders
                # empty, never as the text "None".
                base[key] = ""
        # Host the Striim APP uses to reach a LIVE service. SLT_STRIIM_VIEW_HOST exists for a
        # database on this machine seen from a containerised Striim (host.docker.internal); a
        # remote host is reachable by the app at its own address, and routing it through the
        # docker gateway sends the app to the wrong machine -- measured against a Cloud SQL
        # instance, where every case failed at START with "TCP/IP connection to
        # host.docker.internal". SLT_<NAME>_VIEW_HOST is the per-service override for a host the
        # app must reach some other way (a VPN name the container cannot resolve, say).
        host = base.get("host") or "localhost"
        per_service = (env.get(f"SLT_{name.upper()}_VIEW_HOST") or "").strip()
        if per_service:
            base["view_host"] = per_service
        elif host in ("localhost", "127.0.0.1", "::1"):
            base["view_host"] = env.get("SLT_STRIIM_VIEW_HOST", host)
        else:
            base["view_host"] = host
        return ResolvedService(name=name, mode="live", base=base, started=False)
    # docker mode
    base = dict(defn.docker_defaults)
    # Honor the same env vars compose.yaml interpolates for published host ports. Without this the
    # emulator moves and the admin client does not: SLT_SPANNER_GRPC_HOST_PORT=9110 publishes on
    # 9110 while SpannerAdmin keeps dialing 9010, and every requires: [spanner] test errors.
    for key, envname in defn.docker_env.items():
        override = (env.get(envname) or "").strip()
        if override:
            base[key] = override
    # The service container publishes its port on the DOCKER HOST. When the test process itself runs
    # in a container (a containerized test runner), docker_defaults' "localhost"
    # is that test container — not the host — so the admin/pytest connection is refused (DPY-6005 /
    # ECONNREFUSED). SLT_SERVICES_HOST (e.g. host.docker.internal) is the docker-host gateway the
    # containerized console already uses to reach Striim; honor it for the admin base host too.
    # Unset (pytest running ON the docker host) leaves the localhost default unchanged.
    svc_host = (env.get("SLT_SERVICES_HOST") or "").strip()
    if svc_host and base.get("host") in (None, "localhost", "127.0.0.1"):
        base["host"] = svc_host
    # Host the Striim APP uses to reach this service: localhost for a native/host
    # Striim, host.docker.internal for a containerized Striim (SLT_STRIIM_VIEW_HOST).
    base["view_host"] = env.get("SLT_STRIIM_VIEW_HOST", base.get("host", "localhost"))
    first = defn.container not in started
    if first:
        def _do_up():
            up(defn)
            if defn.post_up:
                run_post_up(defn)
        if env.get("PYTEST_XDIST_WORKER") or env.get("SLT_PARALLEL"):
            # Parallel run (xdist workers OR the console's N single-test subprocesses, spec
            # §D.3.2): serialize the one-time bring-up across processes so they don't race
            # `docker compose up` / re-run post_up on the shared container (spec §C.4). The
            # console pre-flight populates this registry first, so its subprocesses find the
            # container already recorded and skip the bring-up.
            ensure_provisioned_once(defn.container, _do_up)
        else:
            _do_up()
        started.add(defn.container)
    return ResolvedService(name=name, mode="docker", base=base, started=first)
