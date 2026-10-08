from __future__ import annotations
import dataclasses
import hashlib
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from filelock import FileLock

from livetest import lockdir
from livetest.manifest import load_manifest, TestManifest, ManifestError, VALID_DEPTHS, xfail_for_release
from livetest.substitute import render
from livetest.striim import StriimClient, probe_reachable, TERMINAL_STATUSES, POLL_TIMEOUT
from livetest.assertions import AssertionFailed, ExpectedAssertionFailure
from livetest.resultschema import write_sidecar
from livetest.assertions.smoke import assert_smoke
from livetest.assertions.data import assert_data, parse_data_specs
from livetest.assertions.diff import assert_diff, parse_diff_specs
from livetest.assertions.file import assert_file, parse_file_specs
from livetest import exactdata as _slt_exact, inputs as _slt_inputs   # C8
from livetest.assertions.gcs import assert_gcs, parse_gcs_specs
from livetest.assertions.json import assert_json, parse_json_specs
from livetest.assertions.monitor import assert_monitor, parse_monitor_specs
from livetest.assertions.checkpoint_history import (
    assert_checkpoint_history, parse_checkpoint_history_spec,
)
from livetest.assertions.jmx import JmxSpecError, assert_jmx, parse_jmx_specs, render_row_keys
from livetest.assertions.halt import assert_halt, accept_expected_halt
from livetest import recovery
from livetest import outage
from livetest import appactions
from livetest.striimfile import (
    read_server_files, clear_server_files, clear_server_dir, clear_op_checkpoints, ensure_server_dir,
    place_server_file,
)
from livetest.registry import load_service, unavailable as _registry_unavailable
from livetest import prestart as _prestart
from livetest.services import (
    resolve, compose_down, ServiceError, DockerUnavailable, derive_per_test_base,
    clear_provision_registry, ensure_provisioned_once, forget_provisioned,
)
from livetest.pgclient import PgAdmin
from livetest.oraadmin import OraAdmin
from livetest.mssqladmin import MssqlAdmin
from livetest import drivers as _drivers
from livetest.spanneradmin import SpannerAdmin
from livetest.mysqlclient import MySQLAdmin
from livetest.teradataadmin import TeradataAdmin
from livetest.verticaadmin import VerticaAdmin
from livetest.gcsadmin import GcsAdmin
from livetest.kafkaadmin import KafkaAdmin
from livetest.isolation import schema_for
from livetest.topology import parse_deployment_groups, topology_satisfies, Topology
from livetest import paths
from livetest import stack
from livetest import striim_provision as _sp
from livetest import opartifacts
from livetest import opregistry
from livetest import releases as _releases
from livetest import infra as _slt_infra   # infrastructure ownership, C7.1
from livetest import lifecycle as _slt_lifecycle   # lifecycle witnesses, C7.2-C7.3
from livetest import evidence as _slt_evidence     # evidence envelope, C7.4
from livetest import ownership as _slt_ownership   # ownership ledger, C7.6

_STATE_DIR = paths.state_dir()
INTERRUPT_TEARDOWN_TIMEOUT = 90.0


def _interrupt_teardown(run, item):
    """Let the case's existing finally clean owned resources after TERM/INT."""
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    previous_alarm = signal.getsignal(signal.SIGALRM)
    interrupted = None

    def interrupt(sig, frame):
        nonlocal interrupted
        # A second group signal must not interrupt stop/undeploy halfway through.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt(
            "live-test teardown timed out")))
        signal.setitimer(signal.ITIMER_REAL, INTERRUPT_TEARDOWN_TIMEOUT)
        interrupted = KeyboardInterrupt(f"live test interrupted by {signal.Signals(sig).name}")
        if not item._slt_cleanup_active:
            raise interrupted

    try:
        for sig in previous:
            signal.signal(sig, interrupt)
        result = run()
        if interrupted is not None:
            raise interrupted
        return result
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_alarm)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def pytest_report_header(config, start_path):
    """Name (never print the value of) every listed key taken from .env, and every
    shipped service a consumer root overrides (servicesRoots). The header is the
    place pytest shows even for a green run; stderr from a passing test is dropped."""
    from livetest import registry
    return _dotenv_header() + registry.overrides()


def _dotenv_header():
    import os
    taken = []
    values = paths.dotenv_values()
    for key, value in values.items():
        if key.startswith("SLT_INT_"):        # integration-only: the live engine never reads them
            continue
        group = next((g for g in paths._ALIASES.values() if key in g), (key,))
        if not value.strip() or any((os.environ.get(n) or "").strip() for n in group):
            continue
        if next(n for n in group if (values.get(n) or "").strip()) == key:   # not a shadowed alias
            taken.append(key)
    return [f"livetest: from {paths.dotenv_path()}: {', '.join(sorted(taken))}"] if taken else []


def _collected_live_cases() -> Path:
    """The case root collection reads. striim-test collects SLT_LIVE_CASES (project_io hands
    paths.live_cases() to the tier child), so a set key is the root pre-flight reads too.
    Unset, it is this engine's own regression/ (pyproject testpaths), whatever
    SLT_FRAMEWORK_HOME says (same rule as the integration engine's _collected_int_cases)."""
    if paths._lookup("SLT_LIVE_CASES")[1]:
        return paths.live_cases()
    return Path(__file__).resolve().parents[1] / "regression"


_LIVE_CASES = _collected_live_cases()
# The other roots SLT_LIVE_CASES lists (an os.pathsep list; paths.live_case_roots). Collecting
# the primary root collects these too (_collect_every_case_root); pre-flight scans them all.
_EXTRA_LIVE_CASES = paths.live_case_roots()[1:] if paths._lookup("SLT_LIVE_CASES")[1] else []


def _collect_every_case_root(config) -> None:
    """A run that collects the whole primary case root (or a folder holding it) collects every
    extra root too; a run naming cases or folders inside a root collects only those."""
    if not _EXTRA_LIVE_CASES:
        return
    bases = (Path(config.invocation_params.dir), config.rootpath)

    def covered(arg):
        p = Path(arg.split("::")[0]).expanduser()
        for base in bases:
            cand = (p if p.is_absolute() else base / p).resolve()
            if cand.exists():
                return cand
        return None
    named = [c for c in map(covered, config.args) if c is not None]
    if not any(c == _LIVE_CASES or c in _LIVE_CASES.parents for c in named):
        return
    for root in _EXTRA_LIVE_CASES:
        if not any(c == root or c in root.parents for c in named):
            config.args.append(str(root))

# Guards `mvn package` for one module. Concurrent builds tear target/<jar> on disk, which
# no amount of upload locking can fix -- see build_modules.
_BUILD_LOCK = ".slt-op-build.lock"

def _cluster_provision_lock() -> Path:
    # xdist: one worker provisions (§C.4). Prefix-scoped (SLT_STACK_PREFIX) so two parallel
    # stacks provisioning from one checkout coordinate per stack, not against each other.
    return stack.lock_path(".slt-provision.lock")


def _restore_then_post_recover(restore, run_files, seeds, settle, last,
                                sleep=time.sleep, now=time.monotonic, hb=None, report=None):
    """Restore one recovery cycle and seed after the last restore when configured."""
    _post_rec = seeds if last else []
    restore(0.0 if (_post_rec or not last) else settle)
    if not _post_rec:
        return

    if report is not None:
        report("recover: seeding data (post-recover)")
    if hb is None:
        hb = lambda _label: lambda _elapsed, _total: None

    _pr0 = now()
    for _db, _f, _after in _post_rec:
        _hb_pr = hb(f"waiting to seed {_f} after the restore")
        while now() - _pr0 < _after:
            _hb_pr(now() - _pr0, _after)
            sleep(max(0.0, min(2.0, _after - (now() - _pr0))))
        run_files([(_db, _f)])
    _hb_s = hb("recover: settling after the post-recover seed")
    _st0 = now()
    while now() - _st0 < settle:
        _hb_s(now() - _st0, settle)
        sleep(max(0.0, min(2.0, settle - (now() - _st0))))


# Collection is driven by pytest's own testpaths + pytest_collect_file (LiveYamlFile) below,
# NOT by a manual scan — see pyproject `testpaths`.
_STRIIM_DIR = paths.services_dir() / "striim"


def _striim_dir():
    """Alias of striim_provision.striim_dir() (call time, unlike _STRIIM_DIR)."""
    return _sp.striim_dir()

# Services a manifest can name in `requires:`. Each becomes a pytest MARKER on every test
# that requires it, so a runner can select or deselect by what a test actually uses:
#
#     -m "live and not spanner"     everything that does not touch the Spanner emulator
#     -m "live and spanner"         only the tests that do
#
# This exists because name matching cannot answer the question. `-k "not spanner"`, which
# README.md prescribed for parallel runs, misses every test that USES Spanner without saying
# so in its path or name -- ten of them today, including a whole suite, which reach
# SpannerWriter through a directory not named after Spanner. Selecting on `requires:` is exact.
# Registered in pyproject.toml alongside `live`, not here: pytest_configure is called with a
# bare fake config in seven hermetic tests, and marker registration is static configuration
# anyway -- it does not belong in a hook.
_SERVICE_MARKERS = ("postgres", "oracle", "mssql", "spanner", "gcs", "kafka", "mysql", "teradata",
                    "vertica", "servicenow")


def _registered_markers(config) -> set:
    """The marker names the run's ini registers: a consumer registers one per service of its own."""
    try:
        return {line.split(":", 1)[0].strip() for line in config.getini("markers")}
    except (AttributeError, ValueError):
        return set()


def pytest_addoption(parser):
    parser.addoption(
        "--parallel",
        nargs="?",
        const=3,
        type=int,
        default=None,
        metavar="N",
        help="Run live tests in parallel with N workers (default: 3). Automatically sets SLT_PARALLEL=1 and configures pytest-xdist.",
    )


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config):
    parallel_opt = getattr(config.option, "parallel", None)
    # An xdist worker receives the controller's options, --parallel included. Only the controller turns
    # it into xdist options: a worker that did the same started N workers of its own, recursively.
    if hasattr(config, "workerinput"):
        return
    if parallel_opt is not None:
        if parallel_opt > 1:
            os.environ["SLT_PARALLEL"] = "1"
            config.option.numprocesses = parallel_opt
            if getattr(config.option, "dist", "no") == "no":
                config.option.dist = "load"
            config.option.tx = ["popen"] * parallel_opt
        else:
            os.environ.pop("SLT_PARALLEL", None)
            config.option.numprocesses = 0
            config.option.dist = "no"
            config.option.tx = []


def _activate_project() -> None:
    """The project manifest named by GOLD_TARGETS, activated as livetest.cli and striim-test's
    guard do, so a plain `pytest -p livetest.plugin` sees its servicesRoots too (console impact
    F1). A project already active (striim-test's guard) is left as it is."""
    from livetest import project
    if not (os.environ.get("GOLD_TARGETS") or "").strip() or project._ACTIVE is not None:
        return
    try:
        project.load_and_activate()
    except Exception as e:     # ProjectError, PathConfigError, LayoutError
        raise pytest.UsageError(f"livetest: cannot activate GOLD_TARGETS "
                                f"{os.environ['GOLD_TARGETS']}: {e}")


def pytest_configure(config):
    _activate_project()
    _collect_every_case_root(config)
    from livetest import paths
    os.environ.update(paths.effective_env(os.environ))
    # Serial guard. The live suite runs SERIALLY unless SLT_PARALLEL=1: tests share
    # fixed-name DB objects (Oracle QASOURCE.*, MSSQL dbo.*, one Spanner instance) and one
    # shared Striim cluster. Phase-1 tokenized every per-test object name
    # (${TID}/${TID_ORACLE}, kafka/gcs derived names) so parallel runs are now supported,
    # but they stay opt-in until the phase-2 exit criteria pass (spec §C.1). Without
    # SLT_PARALLEL=1, running under pytest-xdist would silently bypass the concurrency
    # safeguards, so keep the hard error. Fail fast with a clear message if -n/--dist is
    # set without the opt-in.
    nprocs = getattr(config.option, "numprocesses", None)
    dist = getattr(config.option, "dist", None)
    if (nprocs or (dist and dist != "no")) and not os.environ.get("SLT_PARALLEL"):
        raise pytest.UsageError(
            "the live-test suite runs SERIALLY by default — it shares one Striim cluster "
            "and shared DB service containers. Per-test object names are now tokenized "
            "(${TID}/${TID_ORACLE}, kafka/gcs derived names) so parallel runs are supported, "
            "but you must opt in explicitly: set SLT_PARALLEL=1 to allow pytest-xdist "
            "(-n/--dist). Without it the guard stays on.")
    # A malformed STRIIM_API_TIMEOUT is a configuration error: refuse the run now, not when the
    # first client is built after provisioning, where it would read as a missing cluster.
    from livetest.striim import striim_api
    try:
        striim_api.parse_timeout(os.environ.get("STRIIM_API_TIMEOUT"))   # effective_env merged it above
    except ValueError as e:
        raise pytest.UsageError(f"livetest: {e}") from e
    # The OP register-once registry is NO LONGER WIPED here, and that is the point.
    #
    # It used to be, on the CONTROLLER only, because the registry JSON lives on the host while
    # UploadedFiles/ lives in the container: `cluster_down` (or any `docker compose down -v`)
    # destroys the loaded jar and leaves the record claiming it is there, so the next run
    # would skip both the upload and the LOAD and every OP test would fail at deploy against
    # a jar the server has never seen. Wiping was the safe direction for that.
    #
    # But it also meant the content-hash diff could never span runs. The registry started
    # empty EVERY run, so the first test needing each OP jar always re-registered it --
    # byte-identical or not -- and each re-registration was an UNLOAD + LOAD OPEN PROCESSOR,
    # i.e. a destroy-then-recreate of state shared by every app on the cluster. That reload is
    # the operation that wedges the OP loader, and nothing ever asked for it.
    #
    # The stale-record hazard is now closed directly instead: ensure_registered takes a
    # `verify` callback, and _loaded_jar_probe answers it from LIST LIBRARIES -- the cluster's
    # own account of which JARS it has loaded, OP and UDF alike, keyed on the filename these
    # records already use. A record the cluster no longer backs is re-registered; one it does
    # back is trusted and nothing is touched.
    #
    # SLT_RUN_EPOCH is stamped here, on the controller, before any worker spawns, so every
    # xdist worker agrees which run it is in. It scopes remembered registration FAILURES
    # (opregistry._current_run), and is the fallback scope for records on a cluster whose
    # restarts cannot be detected (_registry_key).
    # C5: every run has an invocation id, which the evidence envelope records. striim-test sets one per
    # tier child; a direct pytest run gets one here, before any xdist worker spawns (workers inherit it).
    os.environ.setdefault("SLT_INVOCATION_ID", uuid.uuid4().hex)
    if not os.environ.get("PYTEST_XDIST_WORKER"):
        # Controller of a parallel run: clear per-run coordination state so a prior run's
        # stale records can't make a worker skip a service bring-up (§C.4).
        # Skipped entirely under SLT_OPS_PRELOADED: the console pre-flight
        # (livetest.preflight) OWNS the coordination registries — it clears them once up front
        # and populates them; each fan-out subprocess ALSO matches this branch (it is its own
        # xdist-less controller), so it must NOT wipe what pre-flight registered.
        if not os.environ.get("SLT_OPS_PRELOADED"):
            os.environ.setdefault("SLT_RUN_EPOCH", uuid.uuid4().hex[:12])
            clear_provision_registry(_STATE_DIR)
    # the xdist controller collects no live item, so a distributed live run
    # declares ownership here, before any worker starts; a refusal is the C7.1 usage error (rc 4) with its message.
    _slt_infra.declare_if_distributed_live(config)


@dataclass
class StriimContext:
    url: str; user: str; password: str
    mode: str; topology: Topology; view_host: str; groups: dict

def striim_group_tokens(ctx) -> dict:
    return {"APP_GROUP": ctx.groups["app"], "SOURCE_GROUP": ctx.groups["source"]}

def striim_web_url(ctx) -> str:
    """The Striim web API URL as seen from INSIDE a cluster container.

    This is NOT ``ctx.url`` and NOT ``view_host``. Both of those point at the server from
    the HOST; an OP deployed into the agent (``SourceFlow IN Agents``) that calls back into
    the server's REST API needs the opposite direction. ``localhost`` inside the agent
    container is the AGENT, so an OP left on its own default posts DDL into a closed port
    and the app dies mid-flow. In docker mode the server answers on its container name over
    the shared compose network; the port is the container's own 9080, never the published
    host port, which is why it is a literal here rather than parsed off ``ctx.url``.
    A native single-node run has no server/agent split, so the harness URL is already right.
    """
    if ctx.mode == "docker":
        return f"http://{stack.striim_container()}:9080"
    return ctx.url

def striim_url_tokens(ctx) -> dict:
    return {"STRIIM_WEB_URL": striim_web_url(ctx)}

def _striim5_running() -> bool:
    r = subprocess.run(["docker", "ps", "--filter", f"name=^{stack.striim_container()}$", "-q"],
                       capture_output=True, text=True)
    return bool(r.stdout.strip())

def _running_striim_version() -> str | None:
    # The version of the running cluster, read from its version-tagged image. The tag carries
    # the stack prefix (`<prefix>-slt-striim:<version>`, striim_provision.image_ref) so two
    # checkouts cannot overwrite each other's; rsplit(":", 1) below takes the version whether or
    # not a prefix is present. None if not inspectable.
    r = subprocess.run(["docker", "inspect", stack.striim_container(),
                        "--format", "{{.Config.Image}}"],
                       capture_output=True, text=True)
    img = r.stdout.strip()
    if r.returncode != 0 or ":" not in img:
        return None
    return img.rsplit(":", 1)[1]

def _resolve_release(config) -> dict:
    # Resolve the release once per pytest session and cache it — every OP/UDF build and
    # the cluster's image tag key off it. It is DETECTED from STRIIM_HOME's install
    # (livetest.releases.resolve_release; default 5.4.2 when STRIIM_HOME is unset), so the
    # version we build against is the version we test. A ReleaseError (e.g. no Platform-*.jar
    # under STRIIM_HOME/lib) is a hard configuration error and propagates, not caught into a skip.
    if not hasattr(config, "_slt_release"):
        rel = _releases.resolve_release(os.environ)
        config._slt_release = rel
        # Surface the auto-detected release so it's visible which version we build + test.
        # Best-effort observability only — never let a reporter quirk break resolution.
        try:
            home = os.environ.get("STRIIM_HOME")
            src = f"detected from STRIIM_HOME={home}" if home else "STRIIM_HOME unset — using default"
            tr = config.pluginmanager.getplugin("terminalreporter")
            if tr is not None:
                tr.write_line(f"[slt] release: STRIIM_VERSION={rel['STRIIM_VERSION']} "
                              f"STRIIM_SERIES={rel['STRIIM_SERIES']} JAVA_RELEASE={rel['JAVA_RELEASE']} "
                              f"({src})")
        except Exception:
            pass
    return config._slt_release

def _redeploy_reason(running_version, want_version, striim_dir, ownership: str = "") -> str | None:
    """Why a reachable Docker cluster must be torn down and redeployed, or None to reuse it.

    A separate function so the decision is testable: it was inline, and the tests for it
    monkeypatched both inputs and then asserted the mocks returned what they were set to --
    deleting the whole check left them green.

    Two reasons. A version mismatch would make the test invalid outright. A matching version
    but different BUILD SOURCES is the case the tag cannot see (it is
    `<prefix>-slt-striim:<version>` and does not change when entrypoint.sh does), and the reuse
    path is the one flow that never
    reaches ensure_image -- so without this an edit stays invisible exactly where a developer
    is most likely to have just made one."""
    if running_version and want_version and running_version != want_version:
        return f"running version {running_version} != required {want_version}"
    if ownership != "shared" and want_version and not _sp.image_inputs_match(want_version, striim_dir):
        return "the running cluster's image was built from different sources"
    return None


def _slt_infra_guard(session, exitstatus):
    """A live run that executed nothing because the infrastructure was absent must not exit 0.

    **The failure mode this closes is a silent pass.** With the cluster down, every live item
    skips, pytest reports ``39 skipped`` and exits **0**, and a caller -- a person, a CI job, a
    sweep table in a tracker -- reads that as green. It is worse than a red run: it retroactively
    makes every green claim ever sourced from this tier unfalsifiable, because nobody can tell
    afterwards which runs executed anything.

    Fires only when BOTH hold: at least one live item was skipped for an infrastructure reason
    (``LiveItem.runtest`` sets ``_slt_infra_skip`` at the three sites that mean "we could not
    test this"), and **no live item executed**. A partial run keeps its exit status -- that is an
    ordinary skip, and failing it would train people to ignore this, which is how a guard stops
    working. Per-case skips (``disabled:``, an opt-in service gate, a topology mismatch) are not
    marked and never fire it.

    ``SLT_ALLOW_NO_CLUSTER=1`` opts out, for deliberately probing without a stack.
    """
    tr = session.config.pluginmanager.get_plugin("terminalreporter")
    if tr is None:                       # no reporter at all (-p no:terminal, an embedding host)
        return exitstatus
    stats = getattr(tr, "stats", {}) or {}

    def _live(key):
        return [r for r in stats.get(key, []) if getattr(r, "_slt_live", False)]

    blocked = [r for r in _live("skipped") if getattr(r, "_slt_infra_skip", False)]
    if not blocked:
        return exitstatus
    causes = {getattr(r, "_slt_infra_skip", "") for r in blocked}

    # The opt-out is about ABSENT INFRASTRUCTURE, so it is refused when a build failure is in
    # the mix. Checking it here rather than at the top of the function is the whole point: an
    # earlier version tested the variable before the causes were known, so a developer who had
    # exported it during a cluster outage -- on this banner's own advice -- kept a green run
    # afterwards when their `mvn package` broke. You cannot declare a compile error absent.
    if os.environ.get("SLT_ALLOW_NO_CLUSTER") and not causes & {"build"}:
        return exitstatus

    # "error" as well as "failed": an item that dies in setup or teardown lands there, and it
    # still executed.
    if _live("passed") or _live("failed") or _live("error"):
        return exitstatus

    first = blocked[0]
    print("")
    print("[slt] LIVE TIER PROVED NOTHING: %d live case(s) could not be run, and none "
          "executed." % len(blocked))
    print("[slt] Reporting this as a FAILURE rather than a pass -- an all-skipped live run that "
          "exits 0 reads as green and is not.")
    print("[slt] First skipped: %s" % getattr(first, "nodeid", "?"))
    print("[slt]   because: %s" % _slt_skip_reason(first))
    # Advertise the opt-out only where it would actually work -- i.e. exactly the condition the
    # gate above applies -- and name the subsystem that was missing rather than always the
    # cluster. A docker-only run used to be told to run "without a cluster" while the cluster
    # was healthy.
    if not causes & {"build"}:
        if "cluster" in causes:
            print("[slt] Set SLT_ALLOW_NO_CLUSTER=1 if you meant to run without a cluster.")
        else:
            print("[slt] Docker was unavailable; SLT_ALLOW_NO_CLUSTER=1 opts out of this check.")
    return 1 if exitstatus == 0 else exitstatus


def _resolve_striim(config):
    if hasattr(config, "_slt_striim"):
        return config._slt_striim
    import os, socket, time
    from urllib.parse import urlparse
    url = paths.setting("STRIIM_URL") or ""
    if not url:
        svc_host = (os.environ.get("SLT_SERVICES_HOST") or "").strip() or "localhost"
        url = f"http://{svc_host}:9080"
    else:
        svc_host = (os.environ.get("SLT_SERVICES_HOST") or "").strip()
        if svc_host and "localhost" in url:
            url = url.replace("localhost", svc_host)
    user = paths.setting("STRIIM_USER") or "admin"
    # Default matches the Docker image's baked test-only credential (Dockerfile/entrypoint:
    # sksConfig -a striim => admin user password "striim"). A native/real Striim uses a
    # different password — override with STRIIM_PASS (STRIIM_PASSWORD is accepted too). admin/admin
    # was wrong: the harness's own provisioned cluster would 401 on the post-provision reachability wait.
    pw = paths.setting("STRIIM_PASS") or "striim"
    parsed = urlparse(url if "://" in url else "http://" + url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 9080

    def _fail(reason):
        config._slt_striim = None
        config._slt_striim_reason = reason
        return None

    def _port_open(h, p):
        try:
            with socket.create_connection((h, p), timeout=3):
                return True
        except OSError:
            return False

    def _probe_retry(attempts=3, delay=2.0):
        # Retry the reachability probe: a SINGLE transient blip on the first live item
        # must not cache "unreachable" for the whole session (every later item would then
        # skip spuriously). A genuinely-down Striim still fails fast (~a few seconds) and
        # falls through to the provision/skip path, whose result IS cached.
        for i in range(attempts):
            if probe_reachable(url, user, pw):
                return True
            if i < attempts - 1:
                time.sleep(delay)
        return False

    report, _clear_progress = (lambda *a, **k: None), (lambda: None)
    provisioned = False

    def _provision(rel):
        # Build the image (cached per version) + bring the cluster up, then wait for it to answer.
        # Returns True; on failure calls _fail() and returns False. Used for a first provision AND
        # a wrong-version redeploy.
        nonlocal report, _clear_progress
        report, _clear_progress = _make_progress(config)
        try:
            _sp.ensure_deps(_STRIIM_DIR, rel, progress=report)
            _sp.ensure_image(_STRIIM_DIR, rel, progress=report)
            _sp.cluster_up(_STRIIM_DIR, rel, progress=report)
            # Record that WE started the cluster as soon as it's up, so it is torn down at
            # session end even if a later step below returns via _fail().
            config._slt_striim_provisioned = True
        except Exception as e:
            _clear_progress(); _fail(f"could not provision Docker cluster: {e}"); return False
        # Containers are up; server + node + agent take time to become healthy (minutes on a cold
        # cluster). Keep the status line live through the wait, don't go silent — report elapsed
        # time each poll so the message actually changes (a static message would get deduped).
        wait_budget = 300
        started = time.monotonic()
        deadline = started + wait_budget
        while not probe_reachable(url, user, pw):
            elapsed = time.monotonic() - started
            if elapsed >= wait_budget:
                _clear_progress()
                # Keep the nodes' own logs: the cause (a full Docker disk, a license refusal)
                # is only there, and `stop` removes the containers.
                xml = getattr(config.option, "xmlpath", None)
                saved = xml and _sp.save_cluster_logs(Path(xml).parent / "cluster-logs",
                                                      _STRIIM_DIR)
                where = f"; node logs: {saved}" if saved else ""
                _fail(f"provisioned cluster did not become reachable in time{where}"); return False
            report("cluster", f"waiting for containers to be healthy ({elapsed:.0f}s/{wait_budget}s)")
            time.sleep(10)
        return True

    # xdist provisioning coordination (spec §C.4): serialize the up/down-and-provision
    # DECISION across workers with a filelock. The first worker to arrive provisions (or
    # reuses) while the others block; because the reachability re-check (`_probe_retry`)
    # sits INSIDE the lock, every later worker that acquires it finds the cluster already
    # up and falls through the fast reuse path — only ONE worker ever waits/provisions, so
    # this is not "the lock around the whole reachability wait" the spec warns against.
    # Uncontended (serial) it acquires instantly, so serial behavior is unchanged.
    # C7.1: an exclusive run never adopts a reachable or occupied
    # endpoint. Shared and undeclared callers are unchanged here.
    _slt_why = _slt_infra.before_cluster_resolution(config, url, host, port,
                                                    lambda: probe_reachable(url, user, pw), _port_open)
    if _slt_why:
        return _fail(_slt_why)
    native_only = paths.setting("SLT_STRIIM_NATIVE_ONLY") == "1"
    with FileLock(str(_cluster_provision_lock()), mode=lockdir.file_mode()):
        if _probe_retry():
            if not native_only and _striim5_running():
                mode = "docker"
                # STRICT: the reused Docker cluster MUST be the version we build + test against
                # (detected from STRIIM_HOME). A leftover cluster of a different version would make the
                # test invalid, so tear it down and redeploy the right version (the image is cached, so
                # it's a container swap, not a rebuild).
                rel = _resolve_release(config)
                # With SLT_STRIIM_DEPS_MANIFEST set, verify + stage TODAY's manifest before
                # comparing, so a retained identity record is never treated as current verification.
                try:
                    candidate = _sp.verify_current_inputs(_STRIIM_DIR, rel)
                except Exception as e:
                    return _fail(f"cannot reuse the Docker cluster: {e}")
                reason = _redeploy_reason(_running_striim_version(), rel.get("STRIIM_VERSION"),
                                          _STRIIM_DIR, getattr(_slt_infra.of(config), "ownership", ""))
                if not reason and candidate:
                    # A tag matching today's inputs is not enough: the running container must run
                    # that exact image (immutable image ID).
                    reason = _sp.running_image_reason(
                        stack.striim_container(),
                        rel.get("STRIIM_VERSION", _sp._DEFAULT_STRIIM_VERSION))
                if reason and _slt_infra.forbid_redeploy(config, reason):
                    return None
                if reason:
                    report, _clear_progress = _make_progress(config)
                    report("cluster", f"{reason} — redeploying")
                    try:
                        _sp.cluster_down(_STRIIM_DIR, rel)
                    except Exception:
                        pass
                    if not _provision(rel):
                        return None
                    provisioned = True
            else:
                # Native (a local Striim outside Docker) IS the STRIIM_HOME install, so it is
                # consistent with the detected version by construction — nothing to redeploy.
                mode = "native"
        else:
            if native_only:
                raise RuntimeError("SLT_STRIIM_NATIVE_ONLY=1: existing native Striim did not authenticate; "
                                   "check STRIIM_URL/STRIIM_USER/STRIIM_PASS (Docker provisioning disabled)")
            if _striim5_running():
                # Our own docker cluster is up but not answering the auth probe YET — it is still
                # booting (a Striim server opens :9080 before it can authenticate) or was just
                # (re)started by a prior step. WAIT for it rather than skip: the occupied-port guard
                # below is for a FOREIGN service on :9080, not our own mid-boot cluster. Without this,
                # a runner (or a console) would skip the whole suite the instant a freshly
                # provisioned cluster's port opens but before it can log in — "it didn't wait".
                report, _clear_progress = _make_progress(config)
                started = time.monotonic()
                budget = float(os.environ.get("SLT_STRIIM_BOOT_WAIT", "300"))
                primary = stack.striim_container()
                while not probe_reachable(url, user, pw):
                    elapsed = time.monotonic() - started
                    if elapsed >= budget:
                        _clear_progress()
                        return _fail(f"{primary} is running but did not authenticate within {budget:.0f}s "
                                     f"(check STRIIM_USER/STRIIM_PASS and `docker logs {primary}`)")
                    report("cluster", f"waiting for {primary} to finish booting ({elapsed:.0f}s/{budget:.0f}s)")
                    time.sleep(5)
                _clear_progress()
                mode = "docker"
                # With SLT_STRIIM_DEPS_MANIFEST set, a booting cluster is checked too: verify + stage
                # TODAY's manifest, then apply the warm branch's version/identity comparison.
                if _sp.candidate_mode():
                    rel = _resolve_release(config)
                    try:
                        _sp.verify_current_inputs(_STRIIM_DIR, rel)
                    except Exception as e:
                        return _fail(f"cannot reuse the Docker cluster: {e}")
                    reason = _redeploy_reason(_running_striim_version(), rel.get("STRIIM_VERSION"),
                                              _STRIIM_DIR, getattr(_slt_infra.of(config), "ownership", ""))
                    if not reason:
                        reason = _sp.running_image_reason(
                            stack.striim_container(),
                            rel.get("STRIIM_VERSION", _sp._DEFAULT_STRIIM_VERSION))
                    if reason and _slt_infra.forbid_redeploy(config, reason):
                        return None
                    if reason:
                        report, _clear_progress = _make_progress(config)
                        report("cluster", f"{reason} — redeploying")
                        try:
                            _sp.cluster_down(_STRIIM_DIR, rel)
                        except Exception:
                            pass
                        if not _provision(rel):
                            return None
                        provisioned = True
                # C7.1: a declared run compares the booted cluster's version as the warm branch does,
                # and never adopts or redeploys a mismatched one (an undeclared caller is unchanged).
                elif _slt_infra.of(config) is not None:
                    rel = _resolve_release(config)
                    reason = _redeploy_reason(_running_striim_version(), rel.get("STRIIM_VERSION"),
                                              _STRIIM_DIR, getattr(_slt_infra.of(config), "ownership", ""))
                    if reason and _slt_infra.forbid_redeploy(config, reason):
                        return None
            elif _port_open(host, port):
                return _fail(f"Striim on :{port} did not authenticate (check STRIIM_USER/STRIIM_PASS); "
                             f"not provisioning over an occupied port")
            else:
                if not _provision(_resolve_release(config)):
                    return None
                mode = "docker"
                provisioned = True

    try:
        client = StriimClient.from_url(url, user, pw)
        if mode == "docker":
            # Any docker Striim (provisioned OR reused) may still be forming its
            # 2nd node + agent. Wait so we don't read a half-formed topology and
            # skip cluster tests spuriously. (native single Striim: no wait.)
            #
            # ALWAYS get a real reporter here (not just when WE provisioned this session):
            # a REUSED cluster can be just as incomplete (a node that never joined, an agent
            # that crashed) as a freshly-provisioned one, and silently passing progress=None
            # in that case turns a real problem into an opaque wait — up to this function's
            # own 300s budget, which exceeds the console's default per-run watchdog (240s),
            # so it reads as "hung" with zero output rather than "cluster is broken".
            report, _clear_progress = _make_progress(config)
            try:
                _sp.wait_cluster_ready(client, progress=report)
            except Exception as e:
                _clear_progress()
                _, node_c, agent_c = stack.cluster_containers()
                xml = getattr(config.option, "xmlpath", None)
                saved = xml and _sp.save_cluster_logs(Path(xml).parent / "cluster-logs",
                                                      _STRIIM_DIR)
                where = f"; node logs saved in {saved}" if saved else ""
                return _fail(f"Striim cluster is not fully formed (2nd node/agent never joined): {e} "
                             f"— check `docker logs {node_c}` / `docker logs {agent_c}`, "
                             f"or re-provision the cluster from the console's Settings page{where}")
            # A cluster that JUST reached full membership isn't deploy-ready yet
            # (node join settles asynchronously); a brief settle avoids a flaky
            # first deploy. Once per session (resolution is cached).
            if provisioned:
                report("cluster", "settling (node join)")
            time.sleep(float(os.environ.get("SLT_CLUSTER_SETTLE", "20")))
            try:
                client.api.post_tungsten_line("ALTER CLUSTER DISABLE RESOURCE_LIMIT_POLICY;")
            except Exception:
                pass
        _clear_progress()
        topo = parse_deployment_groups(client.list_deployment_groups())
    except Exception as e:
        _clear_progress()
        return _fail(f"could not read Striim topology: {e}")

    view = "host.docker.internal" if mode == "docker" else "localhost"
    ctx = StriimContext(url=url, user=user, password=pw, mode=mode, topology=topo,
                        view_host=view, groups={"app": "default", "source": "Agents"})
    config._slt_striim = ctx
    config._slt_striim_provisioned = provisioned   # framework brought up the cluster -> we tear it down
    # C7.1: record the cluster's ownership status; an exclusive run
    # also requires its allocated container to publish the endpoint port.
    _slt_why = _slt_infra.bind_striim(config, ctx, provisioned, stack.striim_container())
    if _slt_why:
        return _fail(_slt_why)
    return ctx

_NODE_LOG = "/var/log/striim/striim.server.log"

def _node_log_marks(ctx, run=None) -> dict:
    """Current size in bytes of each app node's server log: a per-test start mark.

    `_node_log_tail` reads a FIXED last-N-bytes window of a log that is shared by the whole
    cluster. Under SLT_PARALLEL three tests write to it at once, so a test's own halt reason
    can be pushed out of that window by its siblings' output before the assertion reads it --
    the app halts exactly as expected and the evidence is simply gone. Observed on
    spanner-json-parent-child-orphan-no-upsert and spanner-json-missing-intermediate: HALT
    every time, but expect_halt_contains intermittently missing one or both of its required
    substrings, and WHICH one varied with where the 20 KB boundary happened to fall.

    Marking the log at test start and reading forward from there gives each test exactly its
    own slice, whatever the siblings do.
    """
    if getattr(ctx, "mode", None) != "docker":
        return {}
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    marks = {}
    for node in stack.app_nodes():
        try:
            r = run(["docker", "exec", node, "sh", "-c", f"wc -c < {_NODE_LOG} 2>/dev/null || echo 0"])
            marks[node] = int((getattr(r, "stdout", "") or "0").strip() or 0)
        except Exception:
            marks[node] = 0
    return marks


def _node_log_since(ctx, marks: dict, run=None, nbytes: int = 20000) -> str:
    """Each app node's server log from `marks` onward, falling back to the fixed tail.

    Falls back when there is no mark for a node, or when the log is now SHORTER than the mark
    -- it rotated or was truncated mid-test, so the offset is meaningless and a fixed tail is
    the best available answer rather than nothing.
    """
    if getattr(ctx, "mode", None) != "docker":
        return ""
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    out = []
    for node in stack.app_nodes():
        mark = marks.get(node)
        try:
            if mark:
                # +N is 1-based, so mark+1 starts at the first byte written after the mark.
                r = run(["docker", "exec", node, "sh", "-c",
                         f"if [ $(wc -c < {_NODE_LOG}) -ge {mark} ]; "
                         f"then tail -c +{mark + 1} {_NODE_LOG}; "
                         f"else tail -c {nbytes} {_NODE_LOG}; fi"])
            else:
                r = run(["docker", "exec", node, "tail", "-c", str(nbytes), _NODE_LOG])
            out.append(getattr(r, "stdout", "") or "")
        except Exception:
            pass
    return "\n".join(out)


def _node_log_tail(ctx, run=None, nbytes: int = 20000) -> str:
    # Concatenated tail of each app node's server log — used only to classify a failed OP
    # deploy (poisoned class-loader vs a genuine failure). Docker mode only; native single
    # Striim returns "" (the loader-poisoning cascade is a cluster phenomenon).
    if getattr(ctx, "mode", None) != "docker":
        return ""
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    out = []
    for node in stack.app_nodes():
        try:
            r = run(["docker", "exec", node, "tail", "-c", str(nbytes), _NODE_LOG])
            out.append(getattr(r, "stdout", "") or "")
        except Exception:
            pass
    return "\n".join(out)

def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")

def _tid_oracle(name: str) -> str:
    """Short, deterministic, collision-resistant per-test identifier for Oracle object
    names (spec §A.1a). The readable test-name slug (see _slug, used for NS/APP) is too
    long once combined with a table name -- Oracle's CDC/LogMiner layer emits an
    unparseable "UNSUPPORTED" redo record for a <table_name>${TID_ORACLE} combined
    length past 30 bytes (confirmed via a live spanner-json-feedback-submission repro
    across all 8 of its tables: 30 chars OK, 31 chars CRASH -- lines up exactly with
    Oracle's classic 30-byte unquoted-identifier limit, even though extended 128-char
    identifiers are otherwise enabled on the target release). With the 11-char
    ${TID_ORACLE} token ("T" + 9 hex + "_"), that leaves table names a ~19-char budget.
    A "T" prefix guarantees the result starts with a letter
    (Oracle identifiers can't start with a digit); already uppercase, so it also serves
    as the case-matching form (no separate _UPPER needed) for fields string-matched
    against a live event's metadata.TableName."""
    return "T" + hashlib.sha256(name.encode()).hexdigest()[:9].upper()

def testid_to_ns_app(m: TestManifest) -> tuple[str, str]:
    slug = _slug(m.name)
    ns = f"SLT_{slug}"
    return ns, f"{ns}.{slug}App"

# Not a test: pytest's default `python_functions = ["test"]` prefix would
# otherwise collect this helper as a test item wherever it's imported
# (e.g. into tests/test_discovery.py), causing a spurious fixture error.
testid_to_ns_app.__test__ = False

def diff_kwargs(m) -> dict:
    """The manifest-derived arguments for `assert_diff`.

    ⚠ A SEAM, so the wiring is testable. `diff_poll` reaching the assertion is invisible when it
    works and silent when it does not: delete it and every measurement quietly reverts to the 2.0s
    default, `elapsed_s` becomes quantisation, and §85.3's comparison reports numbers that look
    fine. Nothing else in the tier would fail. Same hazard as `exact:` being dropped in transit.
    """
    return {"timeout": m.timeout, "poll": m.diff_poll}


def substitute_targets(specs, tokens) -> list:
    out = []
    for s in specs:
        c = dict(s)
        if "target" in c:
            c["target"] = render(c["target"], tokens)
        if "source" in c:
            c["source"] = render(c["source"], tokens)
        out.append(c)
    return out

def _run_admin_sql(entry, sql) -> None:
    # Every admin now runs DDL/seed in its own FIXED schema — Oracle QASOURCE/QATARGET,
    # Postgres/MSSQL qasource/qatarget via the source/target role's connection — so
    # run_sql takes just the SQL (no per-test schema argument). entry["schema"] is kept
    # in the dict for shape compatibility but is always None.
    entry["admin"].run_sql(sql)

def _spanner_ensure(admin, attempts: int = 20, delay: float = 1.5) -> None:
    # The emulator has no healthcheck and needs a moment after the container starts;
    # retry the first instance/database creation until it's reachable.
    import time
    last = None
    for _ in range(attempts):
        try:
            admin.ensure()
            return
        except Exception as e:      # noqa: BLE001 — re-raised after retries
            last = e
            time.sleep(delay)
    raise last

def _spanner_admins(base) -> list:
    # One admin per dialect, both backed by the single emulator instance. The
    # framework reaches the emulator at the base host:port (localhost:9010); the
    # Striim app uses the ${SPANNER_*_URL} tokens (view_host) instead.
    emu = f"{base['host']}:{base['port']}"
    common = {"project": base["project"], "instance": base["instance"], "emulator_host": emu}
    google = SpannerAdmin({**common, "database": base["gsql_db"], "dialect": "google_standard_sql"})
    pg = SpannerAdmin({**common, "database": base["pg_db"], "dialect": "postgresql"})
    _spanner_ensure(google)   # creates the instance + the GoogleSQL database
    _spanner_ensure(pg)       # instance already exists; creates the PostgreSQL database
    return [(google, "spanner-google"), (pg, "spanner-postgres")]

_FAKE_KEY_NAME = "fake-gcp-key.json"

def _make_fake_gcp_key(token_uri: str) -> str:
    # A throwaway service-account JSON with a locally-generated, structurally-valid
    # RSA key that authenticates to nothing. Spanner needs only a *parseable* key
    # (SPANNER_EMULATOR_HOST skips auth). GCS always fetches a token from `token_uri`,
    # so we point it at the services/gcs fake token server (which returns a dummy
    # bearer token the emulator ignores). Never a real credential.
    import json, subprocess
    r = subprocess.run(
        ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048"],
        capture_output=True, text=True)
    pem = r.stdout
    if r.returncode != 0 or not pem.strip():
        # An empty PEM yields a structurally-invalid key JSON whose failure surfaces deep
        # in the Spanner/GCS SDK's key parser with no hint of the real cause — fail here.
        raise RuntimeError(
            f"openssl genpkey failed (rc={r.returncode}, stderr={r.stderr.strip()!r}); "
            "cannot build the throwaway GCP service-account key. Ensure 'openssl' is on PATH.")
    return json.dumps({
        "type": "service_account", "project_id": "test-project",
        "private_key_id": "0000000000000000000000000000000000000000",
        "private_key": pem,
        "client_email": "fake-sa@test-project.iam.gserviceaccount.com",
        "client_id": "000000000000000000000",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": token_uri,
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_x509_cert_url": "https://www.googleapis.com/robot/v1/metadata/x509/fake-sa%40test-project.iam.gserviceaccount.com",
        "universe_domain": "googleapis.com"}, indent=2)

def ensure_fake_gcp_key(config, ctx) -> None:
    # Upload the throwaway GCP key into the Striim server's UploadedFiles so the
    # spanner/gcs tests' `ServiceAccountKey: 'UploadedFiles/fake-gcp-key.json'`
    # resolves — no manual make-fake-key.sh step. Once per session.
    if getattr(config, "_slt_gcp_key_done", False):
        return
    import tempfile, subprocess, os as _os
    # token_uri -> the services/gcs fake token server, reachable from the Striim
    # server (host.docker.internal for the Docker cluster, localhost for native).
    token_host = "host.docker.internal" if ctx.mode == "docker" else "localhost"
    # The port was hardcoded here while compose publishes ${SLT_GCS_TOKEN_HOST_PORT:-4444}, so
    # remapping a busy 4444 moved the token server and left this key pointing at the old port --
    # the same "container moves, client does not" bug docker_env fixes elsewhere.
    token_port = _os.environ.get("SLT_GCS_TOKEN_HOST_PORT", "").strip() or "4444"
    content = _make_fake_gcp_key(f"http://{token_host}:{token_port}/token")
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        f.write(content)
        path = f.name
    _os.chmod(path, 0o644)   # tempfiles are 0600; the in-container Striim user must read it
    try:
        if ctx.mode == "docker":
            # The single-topology apps deploy on the `default` group (primary + node).
            for node in stack.app_nodes():
                subprocess.run(["docker", "cp", path, f"{node}:/opt/striim/UploadedFiles/{_FAKE_KEY_NAME}"],
                               capture_output=True, text=True)
        else:  # native Striim
            home = _os.environ.get("STRIIM_HOME")
            if home:
                import shutil
                shutil.copy(path, _os.path.join(home, "UploadedFiles", _FAKE_KEY_NAME))
    finally:
        _os.unlink(path)
    config._slt_gcp_key_done = True

def _gcs_ensure(admin, bucket, attempts: int = 20, delay: float = 1.5) -> None:
    # fake-gcs-server has no healthcheck and needs a moment after the container starts;
    # retry the first bucket creation until it's reachable.
    import time
    last = None
    for _ in range(attempts):
        try:
            admin.ensure_bucket(bucket)
            return
        except Exception as e:      # noqa: BLE001 — re-raised after retries
            last = e
            time.sleep(delay)
    raise last

def _gcs_admin(base):
    # One admin against the emulator; seeds go to the source bucket, the diff reads
    # both buckets. The framework reaches the emulator at the base host:port
    # (localhost:4443); the Striim app reaches it via PrivateServiceConnectEndpoint
    # (an IP -- see _gcs_endpoint_ip) plus the fake token server.
    endpoint = f"http://{base['host']}:{base['port']}"
    admin = GcsAdmin({"endpoint": endpoint, "project": base["project"],
                      "seed_bucket": base["src_bucket"]})
    _gcs_ensure(admin, base["src_bucket"])
    _gcs_ensure(admin, base["tgt_bucket"])
    # Start every test from empty buckets: a prior run's objects (especially a matching
    # target) would otherwise satisfy the diff before the app writes anything.
    admin.clear_bucket(base["src_bucket"])
    admin.clear_bucket(base["tgt_bucket"])
    return admin

def _gcs_endpoint_ip(ctx, run=None) -> str:
    # PrivateServiceConnectEndpoint -> StorageOptions.setHost() runs through Apache
    # UrlValidator, which rejects host.docker.internal / localhost (.internal is not a
    # valid TLD and local hostnames are disallowed) and mangles the endpoint into a
    # bogus "storage-http://..." host -> an opaque "storage-http" StorageException.
    # Feed it an IP instead. For the Docker cluster, resolve the host IP the Striim
    # container reaches host.docker.internal by; native Striim reaches it at 127.0.0.1.
    if ctx.mode != "docker":
        return "127.0.0.1"
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    # `getent ahostsv4` (IPv4-only): plain `getent hosts` can return an IPv6 ULA
    # (e.g. fdc4:...), which yields an unbracketed IPv6 endpoint the GCS client's
    # StorageOptions mangles into an opaque "storage-http" StorageException. The GCS
    # adapter needs an IPv4 dotted-quad; ahostsv4 gives that (first token of the output).
    primary = stack.striim_container()
    out = run(["docker", "exec", primary, "getent", "ahostsv4", "host.docker.internal"])
    parts = (getattr(out, "stdout", "") or "").split()
    if not parts:
        # Do NOT fall back to 127.0.0.1 here: inside the Striim container that is the
        # container's OWN loopback, not the host running the GCS emulator, so the GCS
        # client would silently connect to the wrong place and fail opaquely downstream.
        # Fail loud with the root cause instead.
        rc = getattr(out, "returncode", "?")
        err = (getattr(out, "stderr", "") or "").strip()
        raise RuntimeError(
            "could not resolve host.docker.internal from the Striim container "
            f"(getent rc={rc}, stderr={err!r}); the GCS emulator endpoint IP is required in "
            f"docker mode. Ensure the {primary!r} container is running and its image provides "
            "'getent' (glibc). Refusing to fall back to 127.0.0.1 (the container's own loopback).")
    return parts[0].strip()

def _kafka_clear(admin, topic, attempts: int = 20, delay: float = 1.5) -> None:
    # Kafka has no healthcheck exposed to us here; retry until the broker answers the
    # AdminClient. clear_topic delete+recreates so each test starts from an empty topic
    # (a prior run's messages, a matching target especially, would be a false pass).
    import time
    last = None
    for _ in range(attempts):
        try:
            admin.clear_topic(topic)
            return
        except Exception as e:      # noqa: BLE001 — re-raised after retries
            last = e
            time.sleep(delay)
    raise last

def _kafka_endpoints(base: dict) -> tuple[str, str]:
    """(broker, registry_url) for KafkaAdmin's OWN connection — not the Striim app's KAFKA_*
    tokens (those always use the DOCKER listener + view_host; see build_service_tokens).

    base['host'] stays the docker_defaults default ("localhost") unless services.resolve()
    overrode it via SLT_SERVICES_HOST (docker-out-of-docker execution, e.g. a containerized test
    runner) — same signal services.py's resolve() uses. The HOST listener's advertised
    address is hardcoded to "localhost" (kafka/compose.yaml): Kafka's broker-metadata redirect
    sends any client back to that address right after the initial connect, so a client running
    outside the docker host (inside a container) gets redirected to its OWN loopback and fails
    with ECONNREFUSED, no matter what host it originally dialed. The DOCKER listener
    (broker_port, advertised at view_host) doesn't have that problem — it's the exact listener
    the Striim app already uses — so KafkaAdmin uses it too whenever the base host was
    redirected.
    """
    out_of_docker = base.get("host") not in (None, "localhost", "127.0.0.1")
    if out_of_docker:
        return (f"{base['view_host']}:{base['broker_port']}",
                f"http://{base['view_host']}:{base['registry_port']}")
    return f"{base['host']}:{base['port']}", f"http://{base['host']}:{base['registry_port']}"


def _kafka_admin(base):
    # One admin against the broker + registry; seeds go to the source topic, the diff
    # reads both topics.
    broker, registry_url = _kafka_endpoints(base)
    admin = KafkaAdmin({
        "broker": broker,
        "registry_url": registry_url,
        "seed_topic": base["src_topic"],
    })
    _kafka_clear(admin, base["src_topic"])
    _kafka_clear(admin, base["tgt_topic"])
    return admin

def _spec_route(s) -> str:
    # A data/file spec routes to ONE admin. Accept the uniform source/target vocabulary
    # (source_db/target_db) or the older spec-wide db; default to the source route.
    return s.get("source_db") or s.get("target_db") or s.get("db") or "postgres-source"

def specs_by_db(specs) -> dict:
    # Group data/file specs by their routed database (default postgres-source) so each
    # group is asserted through the admin that owns it.
    groups: dict = {}
    for s in specs:
        groups.setdefault(_spec_route(s), []).append(s)
    return groups

def require_service_admin(admins: dict, db: str, test_name: str, what: str) -> dict:
    """Return the admins entry for db, or raise AssertionError naming what needs it."""
    if db not in admins:
        raise AssertionError(f"{test_name}: {what} needs service {db!r} in 'requires'")
    return admins[db]

def admin_groups(admins: dict, specs: list, test_name: str, kind: str) -> list:
    """Group data/file specs by db and pair each with its admin; fail fast on an unrequired db."""
    out = []
    for db, group in specs_by_db(specs).items():
        out.append((require_service_admin(admins, db, test_name, f"{kind} db")["admin"], group))
    return out

def teardown_derived_resources(gcs_cleanup: list, kafka_cleanup: list) -> None:
    """Best-effort delete of this test's own per-test GCS buckets / Kafka topics (spec
    §A.4) — mirrors the pg-schema teardown's own best-effort try/except. Never raises;
    one admin's failure must not block the others' cleanup."""
    for admin, bucket in gcs_cleanup:
        try:
            admin.delete_bucket(bucket)
        except Exception:
            pass
    for admin, topic in kafka_cleanup:
        try:
            admin.delete_topic(topic)
        except Exception:
            pass

# Non-Postgres routes whose ddl-created tables the teardown drops itself, mapped to the
# token carrying that service's per-test table-name prefix (parallel runs). Postgres is
# absent deliberately (the pg_admins teardown already resets its schemas), as are
# gcs/kafka (their per-test buckets/topics are deleted wholesale at teardown). mysql is
# absent because plain "TID" -- the .get() default below -- is already its prefix token;
# it is picked up here via MySQLAdmin.drop_test_tables, not skipped.
_DDL_TEARDOWN_PREFIX_TOKENS = {"oracle": "TID_ORACLE", "mssql": "TID", "spanner": "TID"}

def teardown_ddl_tables(admins: dict, ddl_files: list, tokens: dict, parallel: bool) -> None:
    """Best-effort drop of the tables this test's `ddl:` created, at teardown -- a test
    must not leave its DB objects behind once it finishes (the DROP+CREATE inside the
    ddl files themselves only cleans up at the NEXT run of the same test). Routes whose
    admin has no drop_test_tables (postgres/gcs/kafka -- cleaned elsewhere in teardown)
    are skipped. Serial runs drop every table in the admin's fixed test schema
    (mirroring the Postgres whole-schema reset); parallel runs drop only this test's
    prefix-isolated tables so a sibling worker's tables survive. Never raises --
    a cleanup failure must not fail a passed test; it warns instead."""
    seen = set()
    for db, _f in ddl_files:
        if db in seen:
            continue
        seen.add(db)
        entry = admins.get(db)
        drop = getattr(entry["admin"], "drop_test_tables", None) if entry else None
        if drop is None:
            continue
        token = _DDL_TEARDOWN_PREFIX_TOKENS.get(db.split("-", 1)[0], "TID")
        prefix = tokens.get(token, "") if parallel else ""
        try:
            drop(prefix)
        except Exception as e:
            print(f"[slt] WARNING: post-test table cleanup failed for {db}: {e!r}")

def teardown_file_outputs(file_spec, tokens: dict, clear) -> None:
    """Best-effort removal of this test's FileWriter output files (<path>* for each
    `assert.file` spec) at teardown. The pre-deploy clear only protects a RE-run from
    stale output -- without this, a finished test's output files sit on the server (or
    the native host's filesystem) forever. Never raises."""
    if not file_spec:
        return
    try:
        specs = parse_file_specs(file_spec)
    except Exception:
        return   # a malformed spec already failed the assertion phase loudly
    for fs in specs:
        try:
            clear(render(fs["path"], tokens))
        except Exception:
            pass

def teardown_unowned(admins: dict, ddl_files: list, tokens: dict, gcs_cleanup: list, kafka_cleanup: list,
                     file_spec, clear_files, upload_names: list, delete_uploads) -> dict:
    """The best-effort teardown of a case without a `lifecycle:` block, after the ledger cleanup.

    The ownership ledger deletes Postgres objects, the namespace, slots and exact file claims; the
    kinds it never deletes keep the teardown they had before the ledger: the non-Postgres `ddl:`
    tables (always by this run's ${TID} prefix, so another run's tables survive), the per-test
    GCS buckets and Kafka topics (incl. `kafka_cleanup_topics:`), the per-test op.upload files and
    the rolled FileWriter parts (<path>*). Never raises; returns what it attempted and what failed,
    as {"attempted": ["<kind> <name>", ...], "failed": {"<kind> <name>": "<error>"}}, the subjects
    the ledger's verification gaps use (kind: engine-tables, bucket, topic, upload, file-output)."""
    out = {"attempted": [], "failed": {}}

    def attempt(subject, fn, *args):
        out["attempted"].append(subject)
        try:
            fn(*args)
        except Exception as e:          # noqa: BLE001 - best-effort; recorded, never raised
            out["failed"][subject] = repr(e)

    seen = set()
    for db, _f in ddl_files:
        if db in seen or db.startswith("postgres"):
            continue
        seen.add(db)
        entry = admins.get(db)
        drop = getattr(entry["admin"], "drop_test_tables", None) if entry else None
        if drop is None:
            continue
        token = _DDL_TEARDOWN_PREFIX_TOKENS.get(db.split("-", 1)[0], "TID")
        attempt(f"engine-tables {db}", drop, tokens.get(token, ""))
    for admin, bucket in gcs_cleanup:
        attempt(f"bucket {bucket}", admin.delete_bucket, bucket)
    for admin, topic in kafka_cleanup:
        attempt(f"topic {topic}", admin.delete_topic, topic)
    if upload_names:
        subjects = [f"upload {n}" for n in upload_names]
        out["attempted"] += subjects
        try:
            delete_uploads(upload_names)
        except Exception as e:          # noqa: BLE001 - one call deletes them all; each is recorded
            out["failed"].update({s: repr(e) for s in subjects})
    if file_spec:
        try:
            specs = parse_file_specs(file_spec)
        except Exception:
            specs = []                  # a malformed spec already failed the assertion phase loudly
        for fs in specs:
            try:
                path = render(fs["path"], tokens)
            except Exception:
                continue
            attempt(f"file-output {path}*", clear_files, path)
    for subject, err in out["failed"].items():
        print(f"[slt] WARNING: post-test cleanup failed for {subject}: {err}")
    return out


def note_unowned_teardown(resources: dict, outcome: dict) -> dict:
    """The cleanup resources of a block-less case after `teardown_unowned`: each ledger gap that says an
    unowned kind is never deleted, and each rolled file part reported as preserved, now says it was
    deleted best-effort by the pre-ledger teardown; a failed delete is one gap naming its error."""
    attempted, failed = set(outcome.get("attempted", ())), outcome.get("failed", {})
    gaps = []
    for line in resources.get("verificationGaps") or []:
        subject = line.split(":", 1)[0]
        if "never deleted" in line and subject in attempted:
            gaps.append(f"{subject}: pre-ledger teardown failed: {failed[subject]}" if subject in failed
                        else f"{subject}: deleted best-effort by the pre-ledger teardown")
        else:
            gaps.append(line)
    noted = {line.split(":", 1)[0] for line in gaps}
    gaps += [f"{s}: pre-ledger teardown failed: {e}" for s, e in failed.items() if s not in noted]
    foreign = []
    for item in resources.get("foreign") or []:
        glob = next((a for a in attempted if a.startswith("file-output ")
                     and item.get("kind") == "file-output" and item.get("name", "").startswith(a[12:-1])), None)
        if glob and "matches the output glob" in item.get("note", ""):
            item = {**item, "note": (f"pre-ledger teardown failed ({glob}): {failed[glob]}" if glob in failed
                                     else f"deleted best-effort by the pre-ledger teardown ({glob[12:]})")}
        foreign.append(item)
    return {**resources, "verificationGaps": gaps, "foreign": foreign}

def should_keep_resources(env: dict, deploy_attempted: bool, succeeded: bool) -> bool:
    """True when the test's resources should be left standing for manual inspection.

    SLT_KEEP_RESOURCES keeps them regardless of outcome (stand up a test case and poke
    at it by hand); SLT_KEEP_RESOURCES_ON_ERROR keeps them only when a test that got as
    far as attempting an app deploy failed. Keyed on the ATTEMPT, not a successful
    deploy_tql return: the TQL import is one multi-statement post (CREATE NAMESPACE ...
    DEPLOY ... START), so an app that crashes AT deploy/start raises out of deploy_tql
    with real server-side state (namespace, half-imported app, DEPLOY_FAILED/CRASH
    status, logs) -- exactly what the flag exists to preserve. A failure BEFORE any
    deploy attempt (service resolution, DDL, seeding) still tears down: no app-side
    state exists yet."""
    if env.get("SLT_KEEP_RESOURCES"):
        return True
    return bool(env.get("SLT_KEEP_RESOURCES_ON_ERROR")) and deploy_attempted and not succeeded

def _skip_shared_teardown(env: dict) -> bool:
    """True when THIS process must NOT tear down the shared services/cluster: any xdist worker
    (another process owns shared-infra lifecycle under -n), or SLT_KEEP_SERVICES. Under -n,
    teardown is the operator's / next serial run's job, never a worker's — a worker finishing
    first would otherwise `compose down` the cluster + service containers out from under its
    still-running siblings (spec §C.4)."""
    return bool(env.get("PYTEST_XDIST_WORKER") or env.get("SLT_KEEP_SERVICES"))

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

def build_modules(m: TestManifest, release: dict, report=None) -> list:
    """Build every op:/udf: module's jar (opartifacts.build_jar) against `release`.
    Returns [(module_dict, BuiltArtifact), ...] in module order (see
    manifest._normalize_modules — op: entries first, then udf: entries). Raises
    opartifacts.OpArtifactError on a build failure; the caller turns that into a
    pytest.skip (kept as a caller-side try/except, not swallowed here, so an upload
    failure below is NOT accidentally skipped too).

    `report` (optional, a `(label, phase)` progress reporter like the run-level `_report`)
    surfaces WHY an automatic jar (re)build fired: build_jar calls back with the trigger
    reason and it is reported per module, tagged with the module's token."""
    builts = []
    for mod in m.modules:
        cb = None
        if report is not None:
            token = mod["token"]
            cb = lambda reason, token=token: report(m.name, f"rebuilding {token} jar: {reason}")
        # Serialise the build per module. Two runners doing `mvn package` in the same module
        # directory write target/<jar> concurrently, so the jar can be TORN ON DISK -- and a
        # perfectly serialized upload then copies the torn bytes faithfully. This is upstream
        # of the upload lock and was the missing half: guarding the copy cannot help if the
        # source file is being rewritten while it is read.
        # Hash INSIDE the lock, and carry the digest on the artifact. Building under the lock
        # and then hashing outside it is not enough: a cache-hit build releases the lock
        # immediately, a sibling runner whose sources moved then acquires it and rewrites
        # target/<jar> in place, and this runner's later read sees a half-rewritten file. The
        # digest is what decides whether the server needs a reload, so a torn read there means
        # registering a hash for bytes that were never shipped. An OP jar carries its content
        # fingerprint instead (opartifacts.content_addressed), taken and copied under this lock.
        with _module_build_lock(mod):
            built = opartifacts.build_jar(mod["jar"], release, report=cb)
            if mod["kind"] == "op":
                # Uploaded and loaded under a content name; see opartifacts.content_addressed.
                built = opartifacts.content_addressed(built)
            else:
                built = dataclasses.replace(built, sha256=opartifacts.file_sha256(built.path))
        builts.append((mod, built))
    return builts

def _module_build_lock(mod) -> FileLock:
    """The per-module build lock: held while a module's jar is built and read."""
    lock_path = stack.lock_path(_BUILD_LOCK.replace(
        ".lock", "-" + opartifacts.module_name(mod["jar"]) + ".lock"))
    return FileLock(str(lock_path), mode=lockdir.file_mode())


def _register_op_jar(ctx, client, built) -> None:
    """Upload and LOAD an OP jar once across processes (opregistry.ensure_registered).

    The record is keyed by OP (_op_key), not by jar name: Striim registers one build per OP, so
    a new build replaces the OP's record in the same write, and a sibling still holding another
    build's record sees the mismatch and re-registers. The exclusive lock waits for every
    running test before a jar swap; it is taken only when the record does not match, i.e. when
    the build changed. Every UNLOAD the load needs is made safe first (restore_loaded_copy)."""
    j, tag = built.name, built.content_tag

    def _register():
        with opregistry.exclusive_reload():
            opartifacts.upload_artifacts(ctx, [built.path], keep_existing=bool(tag))
            client.load_open_processor_idempotent(
                j, tag or None, lambda name: opartifacts.restore_loaded_copy(ctx, name))
        # Keep the probe memo truthful. Without this, a "not loaded" verdict would be re-served
        # from the memo for every remaining test and re-register the jar each time.
        _note_jar_loaded(j)
    # The record can outlive the run: confirm the cluster still has the jar instead of assuming.
    # The generation makes a JVM restart -- which leaves the library listed but its class loader
    # gone -- change the key, so it re-registers rather than trusting a dead record.
    opregistry.ensure_registered(None, _op_key(built), _registry_key(built), _register,
                                 verify=_loaded_jar_probe(client, j))


def _op_key(built) -> str:
    """The registry key for an OP jar: the OP's identity, the same for every build and every file
    name it is loaded under -- its Striim-Module-Name, else the name the build produced. The value
    (_registry_key) identifies the build, so a new build replaces the OP's record in one write."""
    module = getattr(built, "module_name", "")
    return f"op:{module}" if module else (getattr(built, "built_name", "") or built.name)


def _agent_name(built) -> str:
    """The name an OP jar goes on the agent under: the name the build produced."""
    return getattr(built, "built_name", "") or built.name


def set_module_tokens(builts: list, tokens: dict) -> None:
    """Set each module's ${<TOKEN>_JAR}/${<TOKEN>_NAME} tokens from its BuiltArtifact
    (mutates `tokens`). Split out of upload_modules so the SLT_OPS_PRELOADED worker
    path (spec §B.4) can derive these tokens from a cache-hit build WITHOUT re-uploading
    or re-loading the jars."""
    for mod, built in builts:
        tokens[f"{mod['token']}_JAR"] = built.name
        tokens[f"{mod['token']}_NAME"] = built.op_name


# The loaded-jar probe is a read-only LIST LIBRARIES that can run under the machine-wide registry
# lock: striim.POLL_TIMEOUT, long enough for a slow healthy cluster, since a timed-out probe answers
# "cannot tell" and then a jar the cluster has lost is not reloaded.
_PROBE_TIMEOUT = POLL_TIMEOUT


def _loaded_jar_probe(client, jar_name: str, cache: dict | None = None):
    """A zero-arg `verify` for opregistry.ensure_registered: does the cluster still have this
    jar loaded? True / False / None when the cluster could not be asked.

    Keyed on the JAR FILENAME, which is what `LOAD`/`UNLOAD` operate on and what the registry
    records, so there is no name-convention inference in the middle. Covers OP and UDF jars
    alike -- which is why neither kind needs its records scoped to a single run any more.

    A cluster that cannot be reached returns None ("cannot tell") rather than False, because
    reading an unreachable moment as "nothing is loaded" would reload every jar on the
    cluster -- the behaviour this path exists to remove. Retried once first, so a single blip
    while the nodes are still settling does not silently become a trusted stale record, and
    the give-up is LOGGED, because a cluster where this always fails degrades to "trust every
    record" and that must not be invisible.

    The answer is memoised per process (`cache`) because ensure_registered can call it under
    the machine-wide registry lock, and even with its shorter timeout (_PROBE_TIMEOUT; up to
    four calls per probe) a
    wedged-but-listening Striim could otherwise stall every worker on this cluster once per
    jar per test.

    A memo hit of ABSENT is never trusted -- it is re-probed once, fresh. The memo snapshots
    the WHOLE library list in one call, and `_note_jar_loaded` can only correct it for jars
    THIS process registers, so a jar another xdist worker loaded after our snapshot stays
    "absent" here forever. Measured: three live runs on one cluster, where gw0 registered
    one OP and gw2 -- whose snapshot predated it, taken while probing a different
    jar -- then read a matching registry record, asked its stale memo, got False, concluded
    the cluster had lost the jar and re-registered it. Three registrations of one jar in run 1
    where every other OP got exactly one, and that OP was the only one whose tests
    landed on two workers. Each of those is a destructive UNLOAD + LOAD, i.e. the operation
    that wedges the loader.

    Re-probing only on ABSENT is what makes this cheap and correct: absent is the sole verdict
    that triggers a reload, so it is the only one whose staleness costs anything. A stale
    PRESENT can only cause a skip, and a skip additionally requires the registry record and
    the restart generation to match, so it cannot mask a cluster that lost the jar. Paying one
    extra call per would-be reload is the right trade; the memo still absorbs every
    steady-state hit, which is the case that repeats hundreds of times per run.
    """
    want = jar_name.strip().lower()

    def _fetch(store):
        for attempt in (0, 1):
            try:
                store["names"] = client.loaded_libraries(timeout=_PROBE_TIMEOUT)
                return True
            except Exception as e:
                if attempt:
                    print(f"[slt] WARNING: could not ask the cluster which jars are "
                          f"loaded ({e!r}); trusting the recorded registrations. A jar "
                          f"the cluster has since lost will not be reloaded.")
                    return False     # cannot tell -- do NOT cache a failure
                time.sleep(2)
        return False

    def _verify():
        # Resolved per CALL, not per construction: SLT_STACK_PREFIX / STRIIM_URL are read at
        # call time, so the cluster a probe describes is only known when it runs.
        store = _jar_cache(cache)
        if "names" not in store and not _fetch(store):
            return None
        if want in store["names"]:
            return True
        # Memo says absent, and absent is the expensive answer. Confirm it against the
        # cluster before letting it cost a reload -- another worker may have loaded the jar
        # since this snapshot was taken.
        if not _fetch(store):
            return None
        return want in store["names"]
    return _verify


def _note_jar_loaded(jar_name: str, cache: dict | None = None) -> None:
    """Record in the probe memo that `jar_name` is now loaded, after a successful
    registration. Without this the memo keeps saying "absent" for the rest of the process, so
    every later test re-registers the same jar -- N destructive reloads per run instead of the
    one the wipe used to cost."""
    store = _jar_cache(cache)
    if "names" in store:
        store["names"].add(jar_name.strip().lower())


# Process-wide memo, keyed BY CLUSTER. The answer is cluster-specific and both env vars that
# identify a cluster (SLT_STACK_PREFIX, STRIIM_URL) are read at call time, so a single unkeyed
# dict served one stack's answer to another: with two prefixed stacks on one machine, stack
# "alt" would be told a jar was loaded that only the default stack had, and skip a load its own
# cluster never received. Not reachable while each process drives one stack, but it is the
# dangerous direction to leave latent.
#
# The cluster GENERATION is deliberately not memoised -- see _cluster_generation.
_LOADED_JARS_CACHE: dict = {}


def _cluster_key() -> str:
    """Identity of the cluster this process is addressing right now. Reuses the registry
    filename, which already encodes both the stack prefix and the resolved Striim address --
    so anything that separates two registries separates their memos too."""
    try:
        return str(opregistry.registry_path())
    except Exception:
        return ""


def _registry_key(built) -> str:
    """The registry key for a built jar: its content hash, scoped so that a cluster restart
    cannot leave the record trusted.

    Normally that scope is the app nodes' start-time generation. When it cannot be determined
    -- a native install, or a remote Striim reached through STRIIM_URL, where there are no
    local containers to inspect -- it falls back to the RUN, which is what the deleted
    session-start wipe used to provide: records simply do not outlive the run there, so a
    restart between runs can never be trusted through. Silently dropping the scope instead
    would leave every record trusted across a restart the probe cannot see, and LIST LIBRARIES
    would confirm them, because the MDR outlives the JVM.
    """
    generation = _cluster_generation()
    scope = generation or f"run-{opregistry._current_run()}"
    return f"{_op_jar_fingerprint(built)}@{scope}"


def _jar_cache(cache: dict | None) -> dict:
    """The loaded-jars memo for the current cluster. An explicitly supplied `cache` is used
    as-is (tests inject an isolated dict)."""
    if cache is not None:
        return cache
    return _LOADED_JARS_CACHE.setdefault(_cluster_key(), {})


def _cluster_generation() -> str:
    """A token that changes when an app node's JVM has restarted, for the registry key -- so a
    restart invalidates records instead of leaving them trusted-but-dead. Empty when it cannot
    be established for the cluster actually being addressed; the caller falls back to
    run-scoping then.

    DELIBERATELY NOT memoised. Caching it produced a cross-process ping-pong: the process that
    called restart_app_nodes kept its pre-restart token for the rest of the session and went on
    writing records keyed fp@G1, while any other process on the same cluster -- the
    two-checkout/worktree case registry_path exists to support -- computed fp@G2. Each then
    read the other's record as a mismatch and re-registered on EVERY test, an unbounded reload
    storm strictly worse than the once-per-run reload this work removes. Two `docker inspect`
    calls per registration decision is a rounding error against a 20-second live test.

    Only meaningful when the target IS this checkout's compose stack. app_nodes_generation
    inspects LOCAL containers by name and knows nothing about STRIIM_URL, so against a native
    install or a remote server it would either fail (silently disabling restart-detection) or,
    worse, fold an unrelated local stack's start times into the key -- restarting the wrong
    Striim would invalidate every record while restarting the real one invalidated none.
    opregistry.cluster_tag() is empty exactly when the target is the local default stack, so
    that is the gate.
    """
    try:
        if opregistry.cluster_tag():
            return ""            # not the local compose stack -- cannot inspect its nodes
        return _sp.app_nodes_generation()
    except Exception:
        return ""


def _op_jar_fingerprint(built) -> str:
    """A fingerprint of the built jar's CONTENTS, ignoring build timestamps, for the
    register-once registry.

    It has to ignore timestamps, because these builds are NOT reproducible: neither the jar
    nor the shade plugin is configured with project.build.outputTimestamp, so Maven stamps
    every zip entry with the moment of the build. Two `mvn clean package` runs over byte-identical
    sources therefore produce jars with different sha256 -- demonstrated: 93fbd087… then
    984fb186… then 42814d37… from one unchanged tree.

    A raw sha256 of the file is what this used to be, and that made the registry nearly
    useless for the case it exists to serve. Any rebuild at all -- a touched source file, a
    `mvn clean` from the unit tier, a fresh checkout -- moved the fingerprint, so the
    framework concluded "the jar changed", and a changed jar MUST be re-registered: an
    UNLOAD + LOAD OPEN PROCESSOR of cluster-wide state. Running the suite twice against one
    cluster reloaded every OP jar the second time even though nothing had changed, and that
    reload is what wedges Striim's OP loader.

    So digest the (name, CRC-32, uncompressed size) of every entry, sorted by name. That is
    stable across rebuilds of identical inputs (verified: 1ac99abb… from two separate builds)
    and still moves on any real change -- a one-character edit to a default value produced
    04d0c1f5… . CRCs come straight from the zip's central directory, so nothing is
    decompressed and a 50MB jar costs milliseconds.

    Not covered: a change that leaves every entry's name, CRC and size identical. For a jar
    of compiled classes that means no functional change. A dependency swapped for a different
    build of the same size AND colliding CRC would slip through, which is not a risk worth
    engineering against here.

    `built.sha256` (the raw digest build_modules takes for UDF jars under the build lock) is
    deliberately NOT used: it is the timestamp-sensitive value this replaces.

    A content-named OP jar carries the fingerprint content_addressed already computed.
    """
    return getattr(built, "fingerprint", "") or opartifacts.jar_content_fingerprint(built.path)


def upload_modules(m: TestManifest, builts: list, ctx, client, tokens: dict,
                   *, load: bool = True, upload: bool = True) -> tuple:
    """Upload every built module jar into UploadedFiles, set each module's
    ${<TOKEN>_JAR}/${<TOKEN>_NAME} tokens (mutates `tokens`), and -- when `load` is True --
    client.load_jar() the `udf:` modules as a global UDF. `upload:` files are handled
    separately by upload_op_uploads (spec §B.3 -- they are per-test, unlike the
    union-shared jars). Returns (files, jar_names): `files` is every uploaded jar path
    (reused verbatim for a poison-recovery re-upload); `jar_names` is the built jars'
    filenames (for the caller's UNLOAD OPEN PROCESSOR bookkeeping, one per module).

    `load=False` (the parallel/xdist path) suppresses the per-test global UDF load: a UDF
    jar is shared cluster-global state exactly like an OP jar, so under xdist the caller
    registers it ONCE across workers via the filelock registry (spec §B.4) instead. A
    per-test load_jar (which UNLOADs then LOADs) by one worker would yank the jar out from
    under a sibling worker's running app.

    `upload=False` (the parallel/xdist path) suppresses the jar upload for the same reason:
    a shared jar's bytes on the server are cluster-global state, so the caller uploads it
    inside the same register-once lock as the load. Tokens are still set here, because every
    worker needs its own ${<TOKEN>_JAR}/${<TOKEN>_NAME} regardless of who did the upload."""
    files = [built.path for _, built in builts]
    if upload:
        opartifacts.upload_artifacts(ctx, files)
    set_module_tokens(builts, tokens)
    jar_names = []
    for mod, built in builts:
        jar_names.append(built.name)
        if mod["kind"] == "udf" and load:
            client.load_jar(built.name)
    return files, jar_names

def _upload_dest_name(from_name: str, to_spec: str | None, tid: str, tokens: dict) -> str:
    # Shared by upload_op_uploads (upload) and the teardown delete below, so the two can
    # never drift apart. to_spec is None for the back-compat plain-string form -> the
    # original per-test f"{TID}<basename>" prefix; otherwise it's the author's `to`
    # template, token-rendered (e.g. "${NS}-<basename>").
    return f"{tid}{Path(from_name).name}" if to_spec is None else render(to_spec, tokens)

def upload_op_uploads(m: TestManifest, ctx, tokens: dict) -> tuple[list[Path], dict[str, str]]:
    """Per-test render+rename of `op.upload` files (spec §B.3): read each upload's bytes
    from source_dir (the case dir for a `local: true` entry); UTF-8-decodable content is
    token-rendered (so e.g. a ConfigFile JSON's embedded table names pick up ${TID});
    undecodable (binary) content is left byte-for-byte untouched. Each entry is either a plain string (back-compat: uploaded as
    f"{tokens['TID']}{original name}", the ${TID} value carrying its own trailing '_' when
    parallel and "" when serial) or a {from, to} mapping (the author's own `to`, token-
    rendered, fully determines the uploaded name -- see manifest._normalize_uploads).
    Either way, parallel tests sharing an upload's original filename don't clobber each
    other in UploadedFiles/. Returns (tmp_paths, renames): `tmp_paths` are the per-test temp
    paths actually uploaded (fold into the caller's op_files so poison-recovery re-upload
    re-uploads these too); `renames` maps each entry's original basename to its actual
    uploaded name, for _rendered_tql to rewrite a literal "UploadedFiles/<from>" reference
    in the TQL (a shipped example's untokenized ConfigFile) to the real per-test name."""
    tid = tokens["TID"]
    tmpdir = Path(tempfile.mkdtemp(prefix="slt-uploads-"))
    tmp_paths = []
    renames: dict[str, str] = {}
    for u in m.op_uploads:
        from_name, to_spec = u["from"], u["to"]
        data = m.file_path(from_name).read_bytes()
        try:
            data = render(data.decode("utf-8"), tokens).encode("utf-8")
        except UnicodeDecodeError:
            pass   # binary content: uploaded byte-for-byte, only the filename is renamed
        dest_name = _upload_dest_name(from_name, to_spec, tid, tokens)
        dest = tmpdir / dest_name
        dest.write_bytes(data)
        _slt_inputs.record_on(m, "upload", from_name, data, path=m.file_path(from_name))   # as uploaded
        tmp_paths.append(dest)
        renames[Path(from_name).name] = dest_name
    opartifacts.upload_artifacts(ctx, tmp_paths)
    return tmp_paths, renames

# Data GENERATORS for the manifest's `generate:` key (manifest._normalize_generate). A
# generator turns a small declarative workload.yaml into real source data (e.g. GoldenGate
# trail files + a schema.def) in a local tmp dir; the runner then places every file it
# produced onto the Striim server, so a test ships a ~30-line workload instead of committed
# binary fixtures. Keyed by the manifest's `kind:`, so a second generator plugs in here
# without another schema change. Each entry imports its implementation LAZILY (inside the
# function): the generator packages are optional, and neither the hermetic suite nor a test
# that declares no `generate:` should pay their import.

def _run_ggtrail(workload: Path, out_dir: Path) -> dict:
    from livetest.ggtrail.runner import generate_from_yaml
    return generate_from_yaml(workload, out_dir)

GENERATORS = {"ggtrail": _run_ggtrail}

def _generated_dest(dest: str, name: str) -> str:
    # A generate: `dest` is a server DIRECTORY (trailing slash optional); every produced
    # file lands in it under its own basename.
    return f"{dest.rstrip('/')}/{name}"

def _ensure_generate_dirs(m: TestManifest, tokens: dict, ctx) -> None:
    # Create every generate: dest dir on the server before deploy -- including post_start
    # specs -- so a FileReader watching it sees the directory at deploy time (the same
    # reason the server_files loop ensure_server_dir's every dest whatever its `when`).
    for spec in m.generate_specs:
        ensure_server_dir(ctx, _generated_dest(render(spec["dest"], tokens), ".keep"))

def _run_generate_specs(m: TestManifest, when: str, tokens: dict, ctx, report=None) -> list:
    # Run each generate: spec declared for THIS lifecycle point and place everything it
    # produced (the data files + the def file) into the token-rendered dest directory.
    # Returns the server-side paths placed (used by the hermetic test; handy for logs).
    placed = []
    for spec in m.generate_specs:
        if spec["when"] != when:
            continue
        gen = GENERATORS.get(spec["kind"])
        if gen is None:
            pytest.fail(f"{m.name}: unknown generate kind {spec['kind']!r} "
                        f"(known kinds: {sorted(GENERATORS)})")
        if report:
            report(m.name, f"generating {spec['kind']} data ({when})")
        out_dir = Path(tempfile.mkdtemp(prefix=f"slt-generate-{spec['kind']}-"))
        produced = gen(spec["workload"], out_dir)
        rdest = render(spec["dest"], tokens)
        files = list(produced.get("trail_files") or [])
        if produced.get("def_file"):
            files.append(produced["def_file"])
        for src in files:
            dest = _generated_dest(rdest, Path(src).name)
            ensure_server_dir(ctx, dest)
            place_server_file(ctx, src, dest)
            placed.append(dest)
    return placed

# A Java NPE raised by the SERVER while compiling one statement of a TQL import. Observed in
# parallel runs as `MetaInfo$User.isUserActive() ... because "u" is null` on CREATE SOURCE,
# with CREATE NAMESPACE / USE / CREATE APPLICATION all returning 200 on the SAME session
# moments earlier -- so the session was valid and the user object was momentarily not. The
# same test passes standalone and on re-run. Matching the NPE phrasing rather than the one
# observed method keeps the next variant of the same server-side race in scope, while a
# genuine TQL error (a syntax error, a bad verb, a missing column) never reads like this.
_TRANSIENT_IMPORT_NPE = re.compile(r'Cannot invoke .*? because ".*?" is null')


def _deploy_tql_once_retrying(client, tql: str, ns: str, name: str) -> None:
    """`deploy_tql`, retried ONCE when the server fails a statement with an NPE.

    The namespace is dropped before the retry: the failed import leaves the namespace and the
    application behind (both had already succeeded), and `CREATE NAMESPACE` is not idempotent,
    so a bare re-import would fail with "already exists" and hide the original error.

    Only this narrow signature retries. Anything else -- including a second NPE -- propagates,
    so a deterministic failure still fails, twice as slowly and no more quietly.
    """
    try:
        client.deploy_tql(tql)
        return
    except Exception as e:
        if not _TRANSIENT_IMPORT_NPE.search(str(e)):
            raise
        print(_slt_evidence.redact_text(
            f"[slt] {name}: TQL import hit a server-side NPE ({e}) — dropping the namespace "
            f"and retrying the import once", _slt_evidence.known_secrets(), home="", hostname=""))
    client.teardown_namespace(ns)
    client.deploy_tql(tql)


def _rendered_tql(m: TestManifest, tokens: dict, upload_renames: dict) -> str:
    # op.upload {from, to} entries let a shipped example's TQL keep an untokenized,
    # customer-realistic 'UploadedFiles/<from>' ConfigFile reference while each test
    # uploads its own per-test-unique <to> name -- rewrite that literal reference BEFORE
    # the usual ${...} token render (upload_renames' values are already fully rendered,
    # so re-rendering them here is a harmless no-op).
    text = appactions.apply_upload_renames((m.source_dir / m.tql).read_text(), upload_renames)
    _slt_out = render(text, tokens)
    _slt_inputs.record_on(m, "tql", m.tql, _slt_out, path=m.source_dir / m.tql)   # the TQL as sent
    return _slt_out

def build_service_tokens(defn, resolved, schema: str) -> dict:
    ctx = {**resolved.base, "schema": schema}
    try:
        return {k: str(v).format(**ctx) for k, v in defn.provides.items()}
    except (KeyError, IndexError) as e:
        raise ServiceError(f"{defn.name}: provides template references unknown key: {e}") from e

def _make_progress(config):
    """(report, clear) that show live provisioning status via the terminal reporter.

    The status is emitted from inside `runtest`, where pytest's default fd-level
    capture has redirected stdout — so a plain terminal-reporter write is swallowed
    and never reaches the screen. We suspend global capture (capturemanager) around
    each write so it lands on the real terminal, live.

    Each status update is written as its own newline-terminated line (no in-place
    repaint/spinner) so interactive and piped/non-interactive consumers behave the
    same — e.g. a console that drives the harness and reads its output with
    ``stdout.readline()``, which would otherwise block on a bare
    ``\\r``-prefixed repaint with no trailing newline until the whole provisioning
    phase (up to the 5-minute reachability deadline) completed. The very first
    write calls ``ensure_newline()`` first: pytest's test-header line (the
    ``path::name`` prefix) is still open with no trailing newline at that point,
    so without it the first status line would print appended to the end of the
    header instead of starting on its own line."""

    tr = config.pluginmanager.getplugin("terminalreporter")
    if tr is None:
        return (lambda *a: None), (lambda: None)
    capman = config.pluginmanager.getplugin("capturemanager")
    def _emit(fn):
        if capman is not None:
            capman.suspend_global_capture(in_=False)
        try:
            fn()
            try:
                tr._tw.flush()
            except Exception:
                pass
        finally:
            if capman is not None:
                capman.resume_global_capture()
    state = {"last": None, "started": False}
    def report(label, phase):
        msg = f"  {label}: {phase} …"
        if msg == state["last"]:
            return
        state["last"] = msg
        def _do():
            if not state["started"]:
                # Don't trust tr.ensure_newline()'s currentfspath tracking here — it
                # can be stale after capture suspend/resume, so it silently no-ops
                # and our first line lands appended to pytest's still-open test
                # header. Just emit a bare newline unconditionally.
                tr._tw.write("\n")
                state["started"] = True
            tr.write_line(msg)
        _emit(_do)
    def clear():
        state["last"] = None
    return report, clear

def pytest_collect_file(parent, file_path):
    if file_path.name != "test.yaml":
        return None
    return LiveYamlFile.from_parent(parent, path=file_path)

def _declared_services(path) -> list:
    """`requires:` read at COLLECTION time, for the per-service markers.

    Deliberately a raw yaml read rather than load_manifest: collection must not fail on a
    manifest that the loader would reject, because a broken manifest's error belongs at RUN
    time where it names the file and the reason. A file we cannot parse simply gets no service
    markers, which makes it run in the default (serial-safe) bucket rather than vanish.
    """
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        # Narrow on purpose. A broad `except Exception` here silently swallowed a missing
        # import while writing this, leaving every test unmarked and the selector matching
        # nothing -- a bug that looks exactly like "no spanner tests exist". Unparseable YAML
        # and unreadable files are the two conditions worth tolerating; anything else is a
        # defect and should surface.
        return []
    if not isinstance(raw, dict):
        return []
    return [s for s in (raw.get("requires") or []) if isinstance(s, str)]

def _declared_depth(path):
    """`depth:` read at COLLECTION time, mirroring `_declared_services` above.

    Same rationale: collection must not fail on a manifest `load_manifest` would reject. A
    file we cannot parse, or that omits/misspells `depth`, simply gets no depth marker rather
    than vanishing or crashing collection -- L1's lint gate is where a missing/invalid depth
    is a hard error, not here.
    """
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict):
        return None
    depth = raw.get("depth")
    return depth if isinstance(depth, str) else None

class LiveYamlFile(pytest.File):
    def collect(self):
        yield LiveItem.from_parent(self, name=self.path.parent.name, manifest_path=self.path,
                                   services=_declared_services(self.path),
                                   depth=_declared_depth(self.path))

def _prepare_service(defn, env, mode):
    why = _prestart.prepare_selected(defn, env, mode)
    if why:
        pytest.skip(f"service {defn.name} unavailable: {why}")


class LiveItem(pytest.Item):
    def __init__(self, *, manifest_path, services=(), depth=None, **kw):
        super().__init__(**kw)
        self.manifest_path = manifest_path
        self.add_marker(pytest.mark.live)
        consumer_markers = _registered_markers(self.config) if services else set()
        for svc in services:
            if svc not in _SERVICE_MARKERS and svc not in consumer_markers:
                # A consumer's own service: register its marker here, since a generated
                # configuration (striim-test's) carries no consumer marker list.
                try:
                    self.config.addinivalue_line(
                        "markers", f"{svc}: live test whose manifest requires the {svc} service")
                    consumer_markers.add(svc)
                except (AttributeError, ValueError):
                    continue
            self.add_marker(getattr(pytest.mark, svc))
        if depth in VALID_DEPTHS:
            self.add_marker(getattr(pytest.mark, f"depth_{depth}"))

    def runtest(self):
        self._slt_cleanup_active = False
        return _interrupt_teardown(self._runtest, self)

    def _runtest(self):
        self._slt_records = []          # per-assertion structured records for the .slt.json sidecar
        m = load_manifest(self.manifest_path)
        self._slt_topology = m.topology            # effective topology (Phase A: the manifest's)
        self._slt_services = list(m.requires)
        if m.disabled and not os.environ.get("SLT_RUN_DISABLED"):  # known-bug/quarantine: skip before
            pytest.skip(f"disabled: {m.disabled if isinstance(m.disabled, str) else 'disabled'}"  # provisioning
                        " (set SLT_RUN_DISABLED=1 to force-run)")
        if m.disabled_parallel and _parallel(os.environ) and not os.environ.get("SLT_RUN_DISABLED"):
            pytest.skip(f"disabled_parallel: {m.disabled_parallel if isinstance(m.disabled_parallel, str) else 'disabled_parallel'}"
                        " (set SLT_RUN_DISABLED=1 to force-run)")
        # C8.3: every referenced input is read once, after the disabled skips and
        # before any provisioning; exact assertions parse these bytes and never re-read a golden.
        self._slt_data = []             # slt-canon/1 comparison records
        # A case without an exact: block keeps its old outcome for a missing input: the send fails, or it skips first.
        self._slt_inputs = _slt_inputs.snapshot(m, self.manifest_path, strict=m.exact is not None)
        m._slt_inputs = self._slt_inputs   # send sites that hold only the manifest record through it
        ctx = _resolve_striim(self.config)
        if ctx is None:
            self._slt_infra_skip = "cluster"   # see pytest_runtest_makereport
            pytest.skip(getattr(self.config, "_slt_striim_reason", "no reachable Striim"))
        try:
            for svc in m.requires:
                _prestart.eligibility(load_service(svc), ctx.mode, m.topology, ctx.topology)
        except _prestart.PreUpError as e:
            pytest.fail(str(e), pytrace=False)
        ok, why = topology_satisfies(m.topology, ctx.topology)
        if not ok:
            pytest.skip(why)
        for _spec in m.action_specs:
            if _spec["type"] == "service_outage":
                _c, _why = outage.container_for(_spec["service"])
                if _c is None:
                    pytest.skip(_why)
        os.environ.setdefault("SLT_STRIIM_VIEW_HOST", ctx.view_host)
        gcs_ip = None
        if "gcs" in m.requires:
            # The GCS adapter's setHost() endpoint must be an IP (see _gcs_endpoint_ip).
            # Use the same IP for the emulator's -public-host so its resumable-upload
            # URLs are reachable and consistent with what the writer connects to.
            gcs_ip = _gcs_endpoint_ip(ctx)
            # HOST port: compose publishes ${SLT_GCS_HOST_PORT:-4443}:4443, so a stack using
            # the documented busy-port escape hatch needs that value, not the container's.
            gcs_port = os.environ.get("SLT_GCS_HOST_PORT") or "4443"
            os.environ.setdefault("SLT_GCS_PUBLIC_HOST", f"{gcs_ip}:{gcs_port}")
        # C7.5: resource identity is run + worker + case
        # (livetest.runident), not the case name alone. pytest_configure stamps SLT_RUN_EPOCH on
        # the controller; a process started without it (a console fan-out subprocess) is its own run.
        from livetest import runident as _slt_runident
        os.environ.setdefault("SLT_RUN_EPOCH", uuid.uuid4().hex[:12])
        ident = _slt_runident.derive(m.name, os.environ)
        self._slt_ident = ident
        ns, app = ident.ns, ident.app
        release = _resolve_release(self.config)
        # APP_BARE = the app name WITHOUT the namespace prefix (app minus "${NS}."). An
        # exception-store reader's includeApps matches either form; pass an ${APP}-based name to
        # read only this run's store, since ${APP_BARE} also matches the same case in another
        # namespace.
        app_bare = app.split(".", 1)[1]
        # TID / TID_UPPER / TID_ORACLE all derive from the SAME short hashed per-test id
        # (_tid_oracle: "T" + 9 hex chars) rather than the full test-name slug -- the full
        # slug run through Oracle's CDC/LogMiner layer crashes once <table_name>${TID_ORACLE}
        # exceeds 30 combined chars (spec §A.1a; see _tid_oracle's docstring for the confirmed
        # 30-OK/31-CRASH data), and a short hash sidesteps that everywhere instead of
        # special-casing only Oracle. The "T" prefix guarantees every one starts with a letter, always --
        # several engines (Oracle, Postgres unquoted identifiers) reject identifiers
        # beginning with a digit, and a hash's own leading character is otherwise
        # unconstrained (could be a digit).
        # TID is lowercase (existing convention -- ${TID}-prefixed identifiers in test
        # .sql/.tql files are written lowercase). TID_UPPER/TID_ORACLE are the uppercase
        # form (now identical in value -- both exist as separate tokens for call-site
        # clarity and backward compatibility with existing test files), for op-config
        # fields string-matched against a live event's metadata.TableName rather than sent
        # through a DB's own SQL parser -- Oracle (and other CI-but-declared-case dialects)
        # always report unquoted identifiers uppercased, so those specific fields need the
        # uppercase form; a value substituted into an actual SQL statement (DatabaseReader's
        # Tables:, a lookup's own query) doesn't, since the DB engine normalizes it either way.
        # Per-test object-name tokens (spec §A.1/§A.1a). The token's VALUE carries its own
        # trailing '_' separator, and the tokens are populated ONLY when the run is CONCURRENT
        # (_parallel: an xdist worker OR SLT_PARALLEL) -- a template like `${TID}users` then
        # renders `mytest_users`. A serial run leaves them "" so the same template renders the
        # plain `users` (no siblings to isolate from, no forced separator).
        # NB `slug` (un-gated, no separator, still the READABLE test-name slug) is what NS/APP
        # and per-test DB cleanup key on, below -- unrelated to the ${TID} token and unaffected
        # by the hashing above. (kafka/gcs name-derivation used to key on the slug too; it now
        # keys on the un-gated TID hash `per_test` below.) The slug's own digit-guard just
        # below is vestigial now (kept -- harmless, and NS/APP already prefix "SLT_"/the
        # namespace regardless) but no longer load-bearing for TID's alpha-start guarantee.
        # C7.5: ${TID}/${TID_UPPER}/${TID_ORACLE} are never empty -- a
        # serial run is isolated too -- and derive from the run-scoped hashed per-test id (the
        # Oracle 11-char budget is unchanged). `per_test` stays un-gated for kafka/gcs derivation.
        slug, per_test = ident.slug, ident.per_test
        tid, tid_upper, tid_oracle = ident.tid, ident.tid_upper, ident.tid_oracle
        tokens = {**os.environ, **release, **_slt_runident.tokens(ident),
                  **striim_group_tokens(ctx),
                  **striim_url_tokens(ctx),
                  # manifest `tokens:` last; load_manifest refused any name set above or by a
                  # service/module, so the order here is not what keeps them apart
                  **m.tokens}
        if not hasattr(self.config, "_slt_started"):
            self.config._slt_started = set()
            self.config._slt_defs = {}
        schema = ident.pg_slot   # slt_<per_test> (run-scoped, 14 chars)
        # case state exists before any failure path (the cleanup
        # record and the ownership ledger are filled in by increments 2-3).
        cleanup = None
        # C7.6: the ownership ledger is opened and persisted before any side effect.
        ledger = _slt_ownership.Ledger.open(ident)
        self._slt_ledger = ledger
        self._slt_lc = _slt_lifecycle.State.for_manifest(m)   # legacy records when no block
        self._slt_lc.record_input = self._slt_inputs.record   # the lifecycle sentinel SQL as sent
        admins = {}          # svc name -> {"admin":.., "schema": schema|None}
        pg_admins = []       # (admin, schema) to drop on teardown
        gcs_cleanup = []     # (admin, bucket) to delete on teardown (spec §A.4)
        kafka_cleanup = []   # (admin, topic) to delete on teardown (spec §A.4)
        client = None
        deployed = False
        deploy_attempted = False
        in_use_held = False
        succeeded = False
        # SLT_SKIP_VERIFY: run the whole test up to and including data flow (deploy + start
        # + pre/post-start seed) but skip every output-verification block and the "no
        # assertion ran" guard, then report the test as skipped. Independent of
        # SLT_KEEP_RESOURCES (whether the started app is kept) and SLT_RUN_DISABLED
        # (whether a disabled test runs at all) — this flag governs verification only.
        skip_verify = bool(os.environ.get("SLT_SKIP_VERIFY"))
        # Run-level progress reporter, live for the WHOLE test (not just service provisioning) so
        # the console log shows what the long, otherwise-silent deploy→wait→assert phase is doing.
        # `_report(label, phase)` streams one newline-terminated line via capture-suspend (deduped
        # by message). `_hb(label)` is a heartbeat callback for the long blocking waits — its phase
        # string carries the elapsed/timeout so each tick is a distinct (non-deduped) line.
        _report, _clear_progress = (lambda *a, **k: None), (lambda: None)

        def _hb(label):
            return lambda elapsed, total, *_: _report(m.name, f"{label} ({elapsed:.0f}s/{total:.0f}s)")

        try:
            _report, _clear_progress = _make_progress(self.config)
            for svc in m.requires:
                defn = load_service(svc)
                self.config._slt_defs[svc] = defn
                try:
                    _prepare_service(defn, os.environ, ctx.mode)
                except _prestart.PreUpError as e:
                    pytest.fail(f"service {svc}: {e}", pytrace=False)
                try:
                    resolved = _slt_infra.resolve_service(  # shared-service adapter
                        _slt_infra.of(self.config), svc, os.environ, self.config._slt_started,
                        resolve, progress=_report)
                except DockerUnavailable as e:
                    if getattr(defn, "unavailable_policy", "skip") == "fail":
                        pytest.fail(f"service {svc} unavailable (no Docker): {e}", pytrace=False)
                    self._slt_infra_skip = "docker"
                    pytest.skip(f"service {svc} unavailable (no Docker): {e}")
                # F5: the resolved credentials are known secrets before any setup or token construction
                # below can fail with one of them in its message
                _slt_evidence.register_secrets(getattr(resolved, "base", None) or {})
                # Per-test object identity for kafka/gcs (spec §A.3) -- BEFORE both the
                # framework's own admins below (_gcs_admin/_kafka_admin read base["src_bucket"]
                # etc.) and build_service_tokens (the app-facing ${GCS_*}/${KAFKA_*} tokens),
                # so both see the same derived names.
                # Kafka/GCS object names stay per-test in BOTH serial and parallel (harmless
                # when serial, required when concurrent), so they key on the un-gated hashed
                # per-test id `per_test` (the same value ${TID} carries when parallel, sans
                # separator) -- not the gated token (empty when serial), and not the readable
                # `slug` (unbounded length, and it isn't the TID the rest of the run's
                # objects carry).
                resolved.base = derive_per_test_base(svc, resolved.base, per_test)
                if svc == "postgres":
                    # Fixed qasource/qatarget schemas (like oracle): one admin per data
                    # role. source ops (`source_db: postgres-source`) run as qasource in
                    # the qasource schema; target ops (`target_db: postgres-target`) as
                    # qatarget in qatarget.
                    # ensure_setup creates the roles+schemas once. `schema` (schema_for) is
                    # reused only as the per-test replication-slot name for teardown.
                    # Serial: reset_schemas (whole-schema wipe) gives each test a clean slate.
                    # Parallel (xdist): that whole-schema DROP would clobber a sibling worker's
                    # tables, and the role-create would race — so run ensure_setup exactly ONCE
                    # across workers and wipe only THIS test's ${TID} tables (spec §C.4).
                    _pg_src = PgAdmin(resolved.base, role="source")
                    # no whole-schema wipe on any path; every run
                    # resets only its own run-scoped ${TID} objects (replaced by the ledger reset
                    # in increment 3).
                    if _parallel(os.environ):
                        ensure_provisioned_once("postgres-setup", _pg_src.ensure_setup)
                    else:
                        _pg_src.ensure_setup()
                    # no prefix reset here; the ledger resets this
                    # identity's recorded objects by exact name (H6).
                    _terminated = []   # no reset here, so no backend was terminated for it
                    # Design §4.4: a previous run's writer left `idle in transaction` holding a lock
                    # on what we are about to drop. Say so -- the alternative was a silent
                    # stall at the last progress line for as long as that transaction lived.
                    for _t in _terminated:
                        _report(m.name, f"postgres: terminated idle-in-transaction backend pid "
                                        f"{_t['pid']} ({_t['usename']}, {_t['application_name']!r}, "
                                        f"idle {_t['idle_s']}s, last query {_t['query']!r}) that was "
                                        f"blocking the reset -- design §4.4")
                    admins["postgres-source"] = {"admin": _pg_src, "schema": None}
                    admins["postgres-target"] = {"admin": PgAdmin(resolved.base, role="target"),
                                                 "schema": None}
                    pg_admins.append((_pg_src, schema))
                elif svc == "oracle":
                    admins["oracle-source"] = {"admin": OraAdmin(resolved.base), "schema": None}
                    # A second admin as the TARGET data user (qatarget) — for DDL/reads
                    # in QATARGET.* (the source user has CREATE TABLE only in its own
                    # schema). Route target ddl/data specs with `target_db: oracle-target`.
                    _ora_tgt = {**resolved.base,
                                "source_user": resolved.base["target_user"],
                                "source_password": resolved.base["target_password"]}
                    admins["oracle-target"] = {"admin": OraAdmin(_ora_tgt), "schema": None}
                elif svc == "mssql":
                    # Like oracle/postgres: qasource (source) + qatarget (target) accounts &
                    # schemas. ensure_setup (as sa) relaxes the sa password, creates qauser +
                    # CDC + the two data accounts. Route target specs with `target_db: mssql-target`.
                    # ensure_setup mutates shared-global state (ALTER LOGIN sa, CREATE LOGIN,
                    # sp_cdc_enable_db); under xdist run it exactly ONCE across workers so
                    # concurrent tests don't race it (spec §C.4). mssql tables are already
                    # ${TID}-prefixed + per-table DROP, so no whole-schema reset to gate.
                    _mssql = MssqlAdmin(resolved.base, role="source")
                    if _parallel(os.environ):
                        ensure_provisioned_once("mssql-setup", _mssql.ensure_setup)
                    else:
                        _mssql.ensure_setup()
                    admins["mssql-source"] = {"admin": _mssql, "schema": None}
                    admins["mssql-target"] = {"admin": MssqlAdmin(resolved.base, role="target"),
                                              "schema": None}
                elif svc == "spanner":
                    for admin, key in _spanner_admins(resolved.base):
                        admins[key] = {"admin": admin, "schema": None}
                elif svc == "gcs":
                    _gcs = _gcs_admin(resolved.base)
                    admins["gcs"] = {"admin": _gcs, "schema": None}
                    gcs_cleanup += [(_gcs, resolved.base["src_bucket"]),
                                    (_gcs, resolved.base["tgt_bucket"])]
                elif svc == "kafka":
                    _kafka = _kafka_admin(resolved.base)
                    admins["kafka"] = {"admin": _kafka, "schema": None}
                    kafka_cleanup += [(_kafka, resolved.base["src_topic"]),
                                      (_kafka, resolved.base["tgt_topic"])]
                    # App-created topics the framework didn't derive (manifest
                    # `kafka_cleanup_topics:` -- e.g. a persisted stream's derived
                    # <ns>_<streamName> data + _CHECKPOINT topics): token-render and
                    # register them for the SAME best-effort teardown delete as the
                    # derived src/tgt topics above. ${NS}/${APP}/${TID} are already in
                    # `tokens` here (framework tokens precede the requires loop); a
                    # missing token fails loud (render). An empty list -- every test
                    # that doesn't set the key -- appends nothing: a strict no-op.
                    kafka_cleanup += [(_kafka, render(t, tokens))
                                      for t in m.kafka_cleanup_topics]
                    # Auto-detect persistent streams from TQL and register their derived
                    # topics for cleanup. Each stream creates:
                    #   ${NS}_<streamName> (data topic)
                    #   ${NS}_<streamName>_CHECKPOINT (coordinator topic)
                    for stream_name in m.persistent_stream_names:
                        kafka_cleanup += [
                            (_kafka, render("${NS}_" + stream_name, tokens)),
                            (_kafka, render("${NS}_" + stream_name + "_CHECKPOINT", tokens))
                        ]
                elif svc == "mysql":
                    # Fixed qasource/qatarget schemas (like postgres): per-test table isolation.
                    # Admin user (root) creates and tears down schemas; qasource/qatarget users
                    # own their tables. Serial: reset_schemas (whole-schema wipe) gives each
                    # test a clean slate. Parallel (xdist): ensure_setup runs exactly ONCE
                    # across workers and reset_test_objects wipes only THIS test's ${TID}
                    # prefix tables (spec §C.4).
                    _mysql = MySQLAdmin(resolved.base, role="source")
                    # run-scoped ${TID} reset only, serial included.
                    if _parallel(os.environ):
                        ensure_provisioned_once("mysql-setup", _mysql.ensure_setup)
                    else:
                        _mysql.ensure_setup()
                    # The ledger does not own MySQL objects: reset this run's own ${TID} tables only
                    # (never the whole-schema wipe, which would reach another run's tables).
                    _mysql.reset_test_objects(tid)
                    admins["mysql-source"] = {"admin": _mysql, "schema": None}
                    admins["mysql-target"] = {"admin": MySQLAdmin(resolved.base, role="target"),
                                              "schema": None}
                elif svc == "teradata":
                    # Like mssql: qasource (source) + qatarget (target) users, each its own
                    # database. They are baked into the Docker disks; ensure_setup (as dbc)
                    # re-creates them if missing, once across xdist workers. Tables are
                    # ${TID}-prefixed + dropped per table at teardown (drop_test_tables).
                    _td = TeradataAdmin(resolved.base, role="source")
                    if _parallel(os.environ):
                        ensure_provisioned_once("teradata-setup", _td.ensure_setup)
                    else:
                        _td.ensure_setup()
                    admins["teradata-source"] = {"admin": _td, "schema": None}
                    admins["teradata-target"] = {"admin": TeradataAdmin(resolved.base, role="target"),
                                                 "schema": None}
                    admins["teradata-admin"] = {"admin": TeradataAdmin(resolved.base, role="admin"),
                                                "schema": None}
                elif svc == "vertica":
                    # Like teradata: qasource (source) + qatarget (target) users, each owning a
                    # same-named schema. The container's init.sql creates them; ensure_setup (as
                    # dbadmin) re-creates them if missing, once across xdist workers. Tables are
                    # ${TID}-prefixed + dropped per table at teardown (drop_test_tables).
                    _vt = VerticaAdmin(resolved.base, role="source")
                    if _parallel(os.environ):
                        ensure_provisioned_once("vertica-setup", _vt.ensure_setup)
                    else:
                        _vt.ensure_setup()
                    admins["vertica-source"] = {"admin": _vt, "schema": None}
                    admins["vertica-target"] = {"admin": VerticaAdmin(resolved.base, role="target"),
                                                "schema": None}
                    admins["vertica-admin"] = {"admin": VerticaAdmin(resolved.base, role="admin"),
                                               "schema": None}
                elif _drivers.hook(defn, "admins") or _drivers.hook(defn, "provision"):
                    # A service whose driver (livetest.drivers) supplies its connections, the
                    # setup compose cannot do, or both: provision runs once across xdist workers,
                    # per test otherwise (it is idempotent, and re-checking each test turns a
                    # broken service into a clear error). No admins hook: provision gets {}.
                    _drv_make_admins = _drivers.hook(defn, "admins")
                    _drv_admins = _drv_make_admins(resolved.base, defn) if _drv_make_admins else {}
                    _drv_provision = _drivers.hook(defn, "provision")
                    if _drv_provision:
                        _drv_client = StriimClient.from_url(ctx.url, ctx.user, ctx.password)
                        _drv_prepare = (lambda p=_drv_provision, a=_drv_admins, c=_drv_client:
                                        p(a, c, progress=_report))
                        if _parallel(os.environ):
                            ensure_provisioned_once(f"{svc}-setup", _drv_prepare)
                        else:
                            _drv_prepare()
                    for _key, _admin in _drv_admins.items():
                        admins[_key] = {"admin": _admin, "schema": None}
                tokens.update(build_service_tokens(defn, resolved, schema))
                if svc == "gcs" and gcs_ip:
                    # Override the host in the setHost() endpoint with the IP (UrlValidator
                    # rejects host.docker.internal); GcsAdmin still uses base host:port.
                    tokens["GCS_ENDPOINT"] = f"http://{gcs_ip}:{resolved.base['port']}"

            def _run_files(items):
                for db, fname in items:
                    entry = require_service_admin(admins, db, m.name, f"file {fname!r}")
                    _slt_sql = render(m.file_path(fname).read_text(), tokens)
                    _run_admin_sql(entry, _slt_sql)
                    # C4: the SQL exactly as sent
                    self._slt_inputs.record("ddl" if any(_f == fname for _d, _f in m.ddl_files) else "seed", fname,
                                            _slt_sql, path=m.file_path(fname))

            client = StriimClient.from_url(ctx.url, ctx.user, ctx.password)
            if ctx.mode == "docker":
                try:
                    client.api.post_tungsten_line("ALTER CLUSTER DISABLE RESOURCE_LIMIT_POLICY;")
                except Exception:
                    pass
            # Spanner/GCS writers need a parseable ServiceAccountKey in UploadedFiles;
            # upload the throwaway key so the test is self-contained.
            if "spanner" in m.requires or "gcs" in m.requires:
                ensure_fake_gcp_key(self.config, ctx)
            # Pre-clean BEFORE ddl: drop any leftover app(s)/namespace from a prior run whose
            # teardown didn't finish, so a zombie app can't hold locks on the tables ddl is
            # about to drop/create. teardown_namespace (not the single-app teardown) because a
            # test can create MULTIPLE apps in its namespace (e.g. "${APP}_producer" +
            # "${APP}_reader") — any left RUNNING would block DROP NAMESPACE CASCADE. Best-effort
            # — a no-op on a clean slate.
            # C7.6: reset only what a prior attempt of this identity recorded,
            # by exact name, then acquire the run-scoped namespace (refused if it already exists).
            _report(m.name, "resetting this identity's recorded resources")
            ledger.reset(client=client, admins=admins, ctx=ctx, tokens=tokens)
            ledger.acquire_namespace(client)
            # Also drop any OP resume checkpoint this namespace left on the server. Dropping
            # the app and namespace does NOT remove them -- they are plain files in Striim's
            # working dir -- so without this the next run's same-named app resumes from the
            # previous run's position. For a Spanner change-stream reader that is fatal, not merely stale:
            # the change stream's retention window has passed, so the first partition query
            # dies with OUT_OF_RANGE and the app goes TERMINATED. See clear_op_checkpoints.
            #
            # Gated on m.modules, like clear_server_files is gated on an assert_["file"]:
            # only a test with an OP can have written one, and the sweep is two `docker exec`s
            # per test -- ~410 process spawns per suite run to match nothing for the ~195
            # tests without a module. It also narrows the window in which a concurrent
            # sibling's live checkpoint is exposed to the delete.
            if m.modules:
                _slt_ownership.clear_own_checkpoints(ctx, ident.ns, ledger)   # listed, exact names only

            # DDL always runs before deploy (reader/writer need the tables to exist).
            _report(m.name, "loading DDL")
            # C7.6: one file at a time -- acquire (catalog pre-existence check),
            # run, confirm what now exists; a failed file keeps the objects it did create.
            ledger.run_ddl_files(m.ddl_files, admins=admins, tokens=tokens,
                                 render_file=lambda _f: render(m.file_path(_f).read_text(), tokens),
                                 run_one=lambda _db, _f: _run_files([(_db, _f)]))
            _pre_seed = [(db, f) for db, f, when, _a in m.seed_files if when == "pre_deploy"]
            if _pre_seed:
                _report(m.name, "seeding data")
                _run_files(_pre_seed)
            if m.lifecycle is not None:
                # The owned dir before the baseline: a `sink: file` case's baseline-landed
                # readiness.path must already be inside a confirmed owned-dir of this attempt.
                if _slt_ownership.needs_owned_dir(m):
                    ledger.allocate_owned_dir(ctx)   # framework-allocated ${OWNED_DIR}, refused if it exists
                # C7.2: the positive source baseline, recorded after the seed.
                _slt_lifecycle.record_baseline(self._slt_lc, m.lifecycle, admins, tokens, owned=ledger.owns)

            # op:/udf: modules: build the OpenProcessor/UDF jar(s) against the resolved
            # release (once, if missing/stale) and upload them + their shared config
            # JSON(s) into UploadedFiles so `LOAD OPEN PROCESSOR` / `ConfigFile` resolve
            # at deploy. uploads are example-relative (source_dir; the case dir for a
            # `local: true` entry). Each module's `jar`
            # is a repo-relative MODULE reference (its dir, or pom.xml) — never a
            # versioned jar path; the release fixes STRIIM_SERIES, which drives the
            # built jar's actual name. A single `op:`/`udf:` mapping normalizes in
            # manifest.py to one module with the default token "OP"/"UDF", so
            # ${OP_JAR}/${OP_NAME} (or ${UDF_JAR}/${UDF_NAME}) keep working unchanged;
            # a list builds N jars, each with its own ${<TOKEN>_JAR}/${<TOKEN>_NAME}.
            op_jar_names: list[str] = []
            op_files: list = []
            upload_renames: dict = {}
            if m.modules:
                try:
                    _report(m.name, "building OP/UDF jar(s)")
                    builts = build_modules(m, release, report=_report)
                except opartifacts.OpArtifactError as e:
                    # "build", not an infrastructure fault: OpArtifactError covers a failed
                    # `mvn package` in the module's OWN source as well as a missing JDK. The
                    # run still proved nothing, so it still counts -- but the banner must not
                    # blame the cluster or offer SLT_ALLOW_NO_CLUSTER, which would turn a
                    # broken build back into a green run.
                    self._slt_infra_skip = "build"
                    pytest.skip(f"{m.name}: {e}")
                preloaded = bool(os.environ.get("SLT_OPS_PRELOADED"))
                if preloaded:
                    # Console pre-flight (Phase 3) already built+uploaded+registered the OP
                    # union. Derive the ${*_JAR}/${*_NAME} tokens from the cache-hit build
                    # only, then do the per-test uploads (spec §B.3, still per-test).
                    set_module_tokens(builts, tokens)
                    op_jar_names = [b.name for _, b in builts]
                    op_files, upload_renames = upload_op_uploads(m, ctx, tokens)
                else:
                    _report(m.name, "uploading OP/UDF jar(s)")
                    # Under xdist, suppress BOTH the jar upload and the per-test global UDF
                    # load here; the register-once block below does each exactly once across
                    # workers. The upload has to be in there too: `docker cp` of the same jar
                    # to the same path from N workers x every app node interleaves, and a
                    # sibling's LOAD reading the half-written file fails with "ZipFile invalid
                    # LOC header". Widest window on the largest jar, so it looked random.
                    # Neither the upload nor the load happens here for EITHER mode now: the
                    # register-once block below does both inside the filelock, keyed on the
                    # jar's CONTENT hash. Serial used to upload unconditionally and
                    # unload+load on every test, outside any lock -- which is why two serial
                    # runners (one in one shell, a -k run in another) corrupted each
                    # other's jar just as reliably as xdist workers did. The bytes on the
                    # server are shared cluster state regardless of how many processes this
                    # run happens to use.
                    op_files, op_jar_names = upload_modules(
                        m, builts, ctx, client, tokens, load=False, upload=False)
                    _uploads_files, upload_renames = upload_op_uploads(m, ctx, tokens)
                    op_files = op_files + _uploads_files
                    # Recipe L (spec §B.2) stripped the in-TQL `LOAD OPEN PROCESSOR`
                    # statements, so the runner registers OP jars itself now.
                    # Register every shared jar exactly once, SERIAL OR PARALLEL, via the
                    # filelock registry keyed on the jar's content hash (spec §B.4):
                    #
                    #   1. diff  -- hash the built jar; if the registry already records
                    #               that hash for this name, the server already has these
                    #               exact bytes loaded, so do nothing at all;
                    #   2. guard  -- otherwise take the lock, so no other test can upload
                    #               or load while this one is mid-flight;
                    #   3. re-diff -- ensure_registered re-reads the registry INSIDE the
                    #               lock, so a sibling that registered while we waited is
                    #               honoured and we skip rather than re-upload.
                    #
                    # A jar can only be loaded once and must stay loaded while any other
                    # test's app uses it, so per-test unload+load is wrong in both modes:
                    # one runner's UNLOAD deregisters a jar another runner's running app
                    # still needs (two UDF jars colliding), and the reload
                    # window is what a concurrent reader sees as a corrupt zip.
                    for mod, built in builts:
                        j = built.name
                        # Upload INSIDE the lock, immediately before the load: the bytes
                        # on the server are shared cluster state exactly like the
                        # registration, and a concurrent re-upload corrupts what a
                        # sibling worker is loading.
                        # Reached ONLY when the content hash differs, i.e. the server
                        # does not already have these exact bytes. The exclusive lock
                        # waits for every running test to finish before swapping the
                        # artifact out from under it, and holds off new ones until the
                        # new bytes are loaded. Identical jars never get here, so in the
                        # normal case nothing ever blocks.
                        # LIST LIBRARIES reports OP and UDF jars alike, so both kinds are
                        # verifiable against the cluster and neither needs its records scoped
                        # to a single run. The only difference left is the load statement.
                        if mod["kind"] != "udf":
                            _register_op_jar(ctx, client, built)
                            continue

                        def _load(j=j):
                            client.load_jar(j)

                        def _register(j=j, b=built, _load=_load):
                            with opregistry.exclusive_reload():
                                opartifacts.upload_artifacts(ctx, [b.path])
                                _load()
                            # Keep the probe memo truthful. Without this, a "not loaded"
                            # verdict would be re-served from the memo for every remaining
                            # test and re-register the jar each time -- N reloads per run
                            # where the old session-start wipe cost exactly one.
                            _note_jar_loaded(j)
                        # The record can outlive the run: confirm the cluster still has the
                        # jar instead of assuming, which is what the start-of-run registry
                        # wipe used to stand in for. The generation makes a JVM restart --
                        # which leaves the library listed but its class loader gone -- change
                        # the key, so it re-registers rather than trusting a dead record.
                        opregistry.ensure_registered(
                            None, j, _registry_key(built), _register,
                            verify=_loaded_jar_probe(client, j))

                # `on_agent: true` modules: put the jar on the AGENT's classpath and restart
                # it. This is NOT part of the register-once block above, because it is not the
                # same operation: `LOAD OPEN PROCESSOR` is server-side and never reaches an
                # agent, so an agent-deployed flow whose source is an OP fails at DEPLOY with
                # ClassNotFoundException no matter how correctly the servers loaded it. See
                # opartifacts.place_on_agent for why a restart is the only way in.
                #
                # Outside the filelock and not gated on the `upload` flag on purpose: it is
                # idempotent (a content digest per jar) and touches the AGENT container only,
                # so the servers' class loaders and every opregistry record stay valid. The
                # cost is one agent restart per changed jar. Under xdist that restart is
                # visible to a sibling test using the same agent -- acceptable while this is
                # opt-in for `topology: cluster` cases, and the reason it is opt-in.
                # Under the BUILT name, not the content name: the agent's lib dir is a classpath,
                # so each build must replace the last one there, not sit beside it.
                _agent = [(built.path, _agent_name(built))
                          for mod, built in builts if mod.get("on_agent")]
                if _agent:
                    _report(m.name, "placing OP jar(s) on the agent")
                    opartifacts.place_on_agent(
                        ctx, [p for p, _ in _agent], client=client,
                        progress=lambda _label, msg: _report(m.name, msg),
                        names=[n for _, n in _agent])

            # Clear any prior FileWriter output on the server so a re-run's file
            # assertion doesn't see stale events (FileWriter appends across runs).
            # (a lifecycle case's owned dir was allocated before its baseline, above)
            if not _slt_ownership.needs_owned_dir(m) and m.assert_.get("file"):
                for _fs in parse_file_specs(m.assert_["file"]):
                    ledger.claim_exact_file(ctx, render(_fs["path"], tokens), "file-output")   # exact path
            # Same problem in reverse for a FileReader: it re-reads whatever is in the directory
            # it tails, so a previous run's input -- or a fixture since renamed -- is read again
            # and the golden will not match. Emptied BEFORE generate: runs, so generated files
            # placed afterwards survive. `load: true` entries are skipped: their dest is an
            # uploaded jar's NAME, not a server path.
            for _sf, _dest, _when, _load in m.server_files:
                if not _load and m.lifecycle is None:
                    # the exact rendered path, never a parent-directory wipe
                    ledger.claim_exact_file(ctx, render(_dest, tokens), "server-file")
            # Generated data (generate:) is produced + placed BEFORE this phase's
            # hand-placed server_files, so generated output can never race a file the test
            # drops itself. Every generate: dest dir is created now, whatever its `when`.
            _ensure_generate_dirs(m, tokens, ctx)
            _run_generate_specs(m, "pre_deploy", tokens, ctx, report=_report)
            # Place server files: ensure every dest's parent dir exists now (so a
            # FileReader sees the directory at deploy even for a post_start drop), and
            # copy the pre_deploy ones. post_start ones are dropped after RUNNING below.
            for _sf, _dest, _when, _load in m.server_files:
                _rdest = render(_dest, tokens)
                if _load:
                    # load: true doesn't place at a literal server path (`/opt/striim/...`
                    # only exists in the docker cluster, not a native install) -- it goes
                    # through opartifacts.upload_artifacts, the same docker-vs-native-aware
                    # helper op:/udf: modules use, then registers it via client.load_jar()
                    # (UNLOAD-first-best-effort-then-LOAD, safe on both a fresh cluster and
                    # a re-run against an already-loaded namespace).
                    # A private staging dir, removed whole afterwards with the staged jar and any
                    # fetched gs:// copy. `dest` is a bare jar name (enforced at load).
                    with opartifacts.jar_staging() as _jroot:
                        # The source may be outside the test dir: an absolute path, a ${...} one,
                        # or a gs:// object fetched first (a published older jar, say).
                        _jsrc = opartifacts.jar_source(render(_sf, tokens), m.source_dir, _jroot)
                        _jtmp = _jroot / Path(_rdest).name
                        # F6: the jar bytes are staged once into the file that is uploaded, and recorded from it
                        _slt_evidence.stage_on(m, "server-file", _sf, _jsrc, _jtmp)
                        # An OP jar is NOT registered by `LOAD '<path>'` -- that is the UDF form.
                        # LOAD OPEN PROCESSOR is what registers the @PropertyTemplate, without
                        # which `USING Global.<Name>` fails to resolve at deploy.
                        if _load == "open_processor":
                            # Named from the staged bytes, and registered once like any
                            # op: module jar (see _register_op_jar).
                            _register_op_jar(ctx, client, opartifacts.content_addressed_file(
                                _jtmp, release.get("STRIIM_SERIES")))
                        else:
                            opartifacts.upload_artifacts(ctx, [_jtmp])
                            client.load_jar(_jtmp.name)
                    continue
                ensure_server_dir(ctx, _rdest)
                if _when == "pre_deploy":
                    # F6: the staged bytes are the bytes placed and the bytes recorded
                    _staged = _slt_evidence.stage_on(m, "server-file", _sf, m.source_dir / _sf)
                    try:
                        place_server_file(ctx, _staged, _rdest)
                    finally:
                        _slt_evidence.unstage(_staged)
            # OP-loader poisoning recovery: a prior OP crash can corrupt the shared,
            # in-memory OP class-loader so every later OP deploy cascades to failure
            # ("invalid LOC header" / "File copying failed during dependency verification")
            # until a node restart. The poison can surface EITHER as deploy_tql raising (the
            # LOAD OPEN PROCESSOR / DEPLOY statement itself Fails) OR as the app never reaching
            # RUNNING — so the recovery must wrap BOTH the deploy and the readiness probe. If
            # either fails AND the node log shows the poison signature (NOT a genuine bad-TQL
            # failure), restart the app nodes, re-upload + re-unload, and retry the deploy ONCE.
            # Mark the shared node log before anything of ours reaches it. expect_halt reads
            # forward from here instead of taking a fixed 20 KB tail, so a sibling test's
            # output under SLT_PARALLEL cannot push this test's halt reason out of view.
            log_marks = _node_log_marks(ctx)
            # Same idea for a source reader whose service driver (livetest.drivers) can tell
            # when it is reading: its evidence may be shared by every app that ran before, so
            # readiness is what appears after this mark.
            reader_waits = []           # (service, driver, mode, mark)
            for _svc in m.requires:
                _drv = _drivers.load(getattr(self.config, "_slt_defs", {}).get(_svc))
                if _drv is None or not hasattr(_drv, "reader_mode"):
                    continue
                _mode = _drv.reader_mode(_rendered_tql(m, tokens, upload_renames))
                if _mode:
                    reader_waits.append((_svc, _drv, _mode, _drv.reader_mark()))
            _report(m.name, "deploying app")
            # Hold the in-use lock SHARED for the whole deployed lifetime of this test.
            # Any number of tests hold it at once, so this costs nothing in the normal
            # case -- but a jar reload takes it EXCLUSIVE, so it cannot swap the loaded
            # artifact out from under an app that is running on it. Released in the
            # `finally` below, after teardown.
            #
            # ONLY for tests that actually use a module. flock gives no writer preference:
            # a blocked LOCK_EX does not stop new LOCK_SH acquisitions, so readers can starve
            # a writer indefinitely. A test with no op:/udf: module has no stake in the loaded
            # artifact at all, and letting it join the reader crowd was pure starvation risk
            # for zero benefit -- worse because the writer blocks while holding the registry
            # lock, so one stalled reload freezes every module test on every worker.
            # A server_files `load: open_processor` jar is registered like a module jar, so its
            # test has the same stake in it.
            if m.modules or any(_ld == "open_processor" for *_x, _ld in m.server_files):
                _in_use = opregistry.in_use()
                _in_use.__enter__()
                in_use_held = True
            # Set BEFORE deploy_tql: a DEPLOY/START statement failure raises mid-import
            # with the namespace + app already on the server -- keep-on-error must still
            # apply (see should_keep_resources).
            deploy_attempted = True
            expect_halt_early = None

            def _accept_halt(exc):
                # accept_expected_halt RAISES AssertionFailed when expect_halt_contains
                # doesn't match. Both call sites below live inside the DEPLOY phase's
                # `except Exception` handlers -- OUTSIDE the `except AssertionFailed`
                # handler that wraps the assertion phase further down -- so nothing else
                # would fold a failed record into the sidecar. Catch, record, re-raise: the
                # test still fails (a propagating exception is the only pass/fail gate),
                # and .slt.json still shows why.
                try:
                    return accept_expected_halt(m.expect_halt, exc,
                                                contains=m.expect_halt_contains)
                except AssertionFailed as af:
                    self._slt_records.extend(af.records)
                    raise

            if m.modules:
                try:
                    _deploy_tql_once_retrying(
                        client, _rendered_tql(m, tokens, upload_renames), ns, m.name)
                    deployed = True
                    client.await_running(app, timeout=min(m.timeout, 90),
                                         progress=_hb("waiting for app RUNNING"))
                except Exception as e:
                    if _sp.op_loader_poisoned(_node_log_tail(ctx)):
                        if os.environ.get("SLT_PARALLEL"):
                            # Parallel (spec §C.5): do NOT restart_app_nodes here — it restarts
                            # the SHARED app nodes and would kill every other worker's running
                            # apps. Drop a marker beside the junit for the orchestrator (console
                            # post-flight) to serialise ONE restart + re-queue, and fail this
                            # test with a recognisable prefix.
                            marker = _op_poison_marker_path(
                                getattr(self.config.option, "xmlpath", None))
                            if marker is not None:
                                try:
                                    marker.write_text(m.name + "\n")
                                except Exception:
                                    pass
                            raise RuntimeError(
                                f"[slt-op-poisoned] {m.name}: OP class-loader poisoned by a prior "
                                f"OP crash (parallel mode: shared app-node restart deferred to the "
                                f"orchestrator)")
                        print(f"[slt] {m.name}: OP class-loader poisoned (prior OP crash) — "
                              f"restarting app nodes and retrying the deploy once")
                        _sp.restart_app_nodes(client)
                        client.teardown_namespace(ns)
                        # Re-upload + UNLOAD/LOAD is a jar swap like any other, so it needs
                        # the exclusive reload lock -- without it this path stayed the exact
                        # pre-fix corruption pattern: a `docker cp` of the shared jar plus a
                        # full reload while a sibling serial runner is mid-LOAD.
                        #
                        # The shared lock must be DROPPED first. _flock opens a fresh
                        # descriptor per call, and flock treats descriptors from separate
                        # open() calls independently even inside one process, so asking for
                        # EX while still holding SH here would block on ourselves forever.
                        # Safe to drop: the app failed to start, so nothing of ours is running
                        # against the artifact at this moment.
                        if in_use_held:
                            _in_use.__exit__(None, None, None)
                            in_use_held = False
                        # Per-test uploads first, under the exclusive lock like any shared-file swap;
                        # then each OP jar through the one registration path. restart_app_nodes
                        # wiped the registry, so each registers once more and records itself in
                        # the registry's own locked write -- without that the next test would find
                        # nothing, re-register, and do a SECOND full UNLOAD + LOAD on a loader just
                        # recovered from poisoning.
                        _op_jars = {_b.path for _mod, _b in builts if _mod["kind"] == "op"}
                        _uploads = [f for f in op_files if f not in _op_jars]
                        if _uploads:
                            with opregistry.exclusive_reload():
                                opartifacts.upload_artifacts(ctx, _uploads)
                        for _mod, _b in builts:
                            if _mod["kind"] == "op":
                                _register_op_jar(ctx, client, _b)
                        # Re-take it for the rest of the test: the retried deploy below runs
                        # an app against the artifact, so a sibling must not reload under it.
                        _in_use = opregistry.in_use()
                        _in_use.__enter__()
                        in_use_held = True
                        client.deploy_tql(_rendered_tql(m, tokens, upload_renames))
                        deployed = True
                    elif not deployed:
                        # deploy_tql itself failed for a NON-poison reason (a genuine hard
                        # deploy error). Re-raise loudly rather than fall through to a
                        # confusing assertion-timeout -- unless expect_halt accepts this
                        # deploy/startup-phase failure as the test's expected outcome. A
                        # non-poison readiness failure (app slow to start / terminal) is
                        # instead left to surface in the assertion step below, which polls
                        # with the full timeout.
                        expect_halt_early = _accept_halt(e)
                        if expect_halt_early is None:
                            raise
            else:
                try:
                    _deploy_tql_once_retrying(
                        client, _rendered_tql(m, tokens, upload_renames), ns, m.name)
                    deployed = True
                except Exception as e:
                    expect_halt_early = _accept_halt(e)
                    if expect_halt_early is None:
                        raise

            if expect_halt_early is not None:
                self._slt_records += expect_halt_early
                ran = True
            else:
                # `assert.smoke` is either `true` (this test deploys one app named exactly
                # `${NS}.${slug}App`) or a list of app-name suffixes for tests that deploy
                # several named sub-apps (e.g. a producer+reader pair) --
                # none of which is ever the bare name. smoke_apps is reused by the terminal
                # probe below so a HALT in ANY sub-app aborts long-running assertions too.
                smoke_spec = m.assert_.get("smoke")
                smoke_apps = [f"{app}{suffix}" for suffix in smoke_spec] if isinstance(smoke_spec, list) else [app]

                def _terminal_probe():
                    # Under expect_halt the halt is the EXPECTED end state, and a data/diff
                    # assertion is then a question about what the target holds after it --
                    # "did a fragment of the failed transaction land?" -- so the poll must
                    # observe the post-halt state rather than abort on it. The halt itself is
                    # asserted by the expect_halt step, which runs first.
                    if m.expect_halt:
                        return
                    for a in smoke_apps:
                        st = client.current_status(a)
                        if st in TERMINAL_STATUSES:
                            raise AssertionError(f"{m.name}: {a} entered terminal status {st!r} during assertion polling")

                ran = False

                def _await_running_for_seed() -> bool:
                    # Wait for RUNNING before a post-start seed — CDC readers capture only
                    # post-start commits, so the app must be RUNNING before rows land. Under
                    # SLT_SKIP_VERIFY a never-RUNNING app (often the very failure being
                    # debugged) is tolerated: return False so we skip the dependent seed and
                    # leave the app + its logs for inspection instead of raising. In a normal
                    # run it either returns True or raises exactly as before.
                    if m.lifecycle is not None and self._slt_lc.ready_satisfied():
                        return True   # RUNNING and readiness already proven, bounded (H3)
                    try:
                        # Wait on the app names this manifest ACTUALLY deploys, not the bare
                        # ${APP}.
                        #
                        # THE PREDICATE IS NOT "MULTI-APP". It is: NO APPLICATION NAMED EXACTLY
                        # ${APP} EXISTS, while a post-start wait needs one to wait on. The
                        # assert.smoke list form is how you tell -- and an EMPTY-STRING element
                        # in that list is how a multi-app manifest KEEPS a bare-named app.
                        # A manifest can do exactly that, deliberately: smoke: ["", "_reader"] resolves through the smoke_apps
                        # comprehension to [app, app + "_reader"], so the bare name is present and
                        # polling it never 404s. Do not restate this guard as "multi-app manifests
                        # have no bare app" -- that file is a standing counter-example.
                        #
                        # WHY IT SURVIVED: this closure is reached ONLY on the post_start paths,
                        # and until 2026-09-17 no test was both (a) reached here and (b) lacking a
                        # bare-named app. The multi-app reader cases seed
                        # PRE-start and never enter it; one gated-lookup case
                        # (since removed) entered it but kept a bare app via the empty suffix. The
                        # first multi-app drop/recreate cases were the first to be both, which is why the bare name survived here.
                        #
                        # WHY IT IS SAFE: smoke_apps degrades to [app] for `smoke: true` and for
                        # no smoke key at all, so those tests keep byte-identical behaviour. A
                        # test that both enters this closure and has a list, such as
                        # lookup-gated-children, waits for every listed app instead of
                        # [app] -- strictly MORE waiting, which cannot green a test early. Such
                        # cases time their seeds from the moment RUNNING is reached, so re-run
                        # them after changing this.
                        #
                        # ONE CHANGE COVERS FOUR CALLERS: post_start seed, post_start generate,
                        # post_start server files, and the terminal path all route through here.
                        for _a in smoke_apps:
                            client.await_running(_a, timeout=m.timeout, progress=_hb("waiting for app RUNNING"))
                        return True
                    except Exception as e:
                        if skip_verify:
                            _report(m.name, f"skip-verify: app not RUNNING ({e}); skipping post-start seed")
                            return False
                        raise

                # Each assert_* returns per-spec structured records (accumulated for the
                # sidecar); on failure it raises AssertionFailed carrying the records it built
                # before the raise, which we fold in so the failing assertion is still recorded.
                try:
                    # smoke is itself a verification (it waits for + asserts RUNNING), so it is
                    # skipped under SLT_SKIP_VERIFY along with every other assert_* below.
                    if m.lifecycle is not None and not skip_verify:
                        # C7.2: bounded RUNNING for every app (the smoke
                        # record), then the declared capture readiness, before any ordered change.
                        _report(m.name, "lifecycle: proving readiness")
                        self._slt_records += _slt_lifecycle.smoke_and_ready(
                            self._slt_lc, m.lifecycle, client=client, apps=smoke_apps, admins=admins,
                            tokens=tokens, ident=ident, owned=ledger.owns,
                            read_files=lambda _p, **_k: read_server_files(ctx, _p, **_k),
                            source_dir=m.source_dir)
                        ran = True
                    elif smoke_spec and not skip_verify:
                        _report(m.name, "asserting: smoke")
                        self._slt_records += assert_smoke(client, smoke_apps, timeout=m.timeout,
                                                           progress=_hb("smoke: waiting for RUNNING"))
                        ran = True

                    # CDC sources capture only post-start commits: seed after RUNNING. Each
                    # entry's `after` is an offset from the moment RUNNING was reached (not
                    # from the previous file), so entries at 10s and 30s fire 10s and 30s in.
                    _post_seed = [(db, f, a) for db, f, when, a in m.seed_files
                                  if when == "post_start"]
                    if _post_seed and _await_running_for_seed():
                        for _svc, _drv, _mode, _mark in reader_waits:
                            # RUNNING is not reading: StartPosition NOW misses a row committed
                            # before the reader's first read. `after:` offsets count from here.
                            _waited = _drv.wait_reader_ready(
                                _mode, _mark, m.timeout,
                                progress=lambda msg: _report(m.name, msg))
                            _report(m.name, f"{_svc} {_mode} reader is reading ({_waited:.0f}s after RUNNING)")
                        _report(m.name, "seeding data (post-start)")
                        _t0 = time.monotonic()
                        for _db, _f, _after in _post_seed:
                            if _after:
                                _hb_wait = _hb(f"waiting to seed {_f}")
                                while True:
                                    _elapsed = time.monotonic() - _t0
                                    _left = _after - _elapsed
                                    if _left <= 0:
                                        break
                                    # Heartbeat while sleeping: a silent multi-minute pause
                                    # is indistinguishable from a hung run in the console.
                                    _hb_wait(_elapsed, _after)
                                    time.sleep(min(2.0, _left))
                            _run_files([(_db, _f)])
                    # Generate post_start data once RUNNING, before the post_start
                    # server_files drop (same no-race ordering as the pre_deploy phase).
                    if (any(g["when"] == "post_start" for g in m.generate_specs)
                            and _await_running_for_seed()):
                        _run_generate_specs(m, "post_start", tokens, ctx, report=_report)
                    # Drop post_start server files after RUNNING (a FileReader tailing the dir
                    # picks up the newly-appeared file — the file-CDC analog of post_start seed).
                    if any(w == "post_start" for _, _, w, _ in m.server_files) and _await_running_for_seed():
                        for _sf, _dest, _when, _load in m.server_files:
                            if _when == "post_start":
                                # F6: the staged bytes are the bytes placed and the bytes recorded
                                _staged = _slt_evidence.stage_on(m, "server-file", _sf, m.source_dir / _sf)
                                try:
                                    place_server_file(ctx, _staged, render(_dest, tokens))
                                finally:
                                    _slt_evidence.unstage(_staged)

                    # RECOVERY PHASE (opt-in, `recover:`). Sits HERE, after every post_start
                    # step and before every assertion, because that ordering is the test: the
                    # data must be in flight when the app is interrupted, and the assertions
                    # must run against a target that has been given its chance to replay.
                    #
                    # Deliberately AFTER the seed rather than concurrent with it. Racing the
                    # interruption against an in-progress seed would give a different in-flight
                    # window every run, and a recovery test that reproduces intermittently is
                    # one nobody trusts. `recover.after` tunes the window explicitly instead.
                    if m.recover and not skip_verify:
                        _rc = m.recover
                        _ok, _why = recovery.supported(_rc["mode"], ctx)
                        if not _ok:
                            pytest.skip(_why)
                        # One interruption is often not enough to observe anything: the field
                        # report this phase exists to reproduce states that the first stop is
                        # usually clean and the divergence appears on the second or third. So
                        # the cycle repeats `times`, and only the LAST restore settles --
                        # settling between interruptions would just wait out the very in-flight
                        # window the next interruption needs.
                        for _i in range(_rc["times"]):
                            _wait = _rc["after"] if _i == 0 else _rc["every"]
                            _last = (_i == _rc["times"] - 1)
                            _tag = "" if _rc["times"] == 1 else f" [{_i + 1}/{_rc['times']}]"
                            if _wait:
                                _hb_r = _hb(f"waiting to {_rc['mode']} the app{_tag}")
                                _rt0 = time.monotonic()
                                while True:
                                    _el = time.monotonic() - _rt0
                                    if _el >= _wait:
                                        break
                                    _hb_r(_el, _wait)
                                    time.sleep(min(2.0, _wait - _el))
                            _report(m.name, f"recover: interrupting app ({_rc['mode']}){_tag}")
                            # verify_timeout tracks the manifest's own timeout instead of
                            # recovery.interrupt's 120s default: on an 80 000-event pipeline a
                            # STOP can legitimately sit in STOPPING for longer than that, and
                            # the default would report a WORKING stop as "not interrupted".
                            # Floored at the default so a short-timeout manifest cannot make
                            # verification stricter than the library intends.
                            recovery.interrupt(client, ctx, app, _rc["mode"],
                                               verify_timeout=max(120.0, float(m.timeout) / 2.0),
                                               report=lambda msg: _report(m.name, msg))
                            _report(m.name, f"recover: restoring app{_tag}")
                            # `when: post_recover` seeds: after the LAST
                            # restore reaches RUNNING, before the settle -- a parent that
                            # arrives only after the restart. Their `after` counts from RUNNING.
                            _post_rec = [(db, f, a) for db, f, when, a in m.seed_files
                                         if when == "post_recover"]
                            _restore_then_post_recover(
                                restore=lambda _settle: recovery.restore(
                                    client, ctx, app, _rc["mode"], timeout=m.timeout,
                                    settle=_settle,
                                    expect_running=_rc["expect_running"],
                                    report=lambda msg: _report(m.name, msg),
                                    progress=_hb(f"recover: waiting for app RUNNING{_tag}")),
                                run_files=_run_files,
                                seeds=_post_rec,
                                settle=_rc["settle"],
                                last=_last,
                                hb=_hb,
                                report=lambda msg: _report(m.name, msg))

                    # ACTION PHASE (opt-in, `action:`). Runs app lifecycle control operations
                    # (e.g., stop/start cycles) after seeding and recovery, before assertions.
                    # Optionally runs concurrent SQL operations in background while cycling.
                    if m.action_specs and not skip_verify:
                        def _start_concurrent(concurrent_ops):
                            """Start one background SQL loop per op; returns (stop_flag, threads)."""
                            _action_stop_flag = threading.Event()
                            _action_threads = []

                            def _run_concurrent_op(op_spec):
                                """Background thread: run SQL loop until stop_flag is set."""
                                _op_db = op_spec["db"]
                                _op_file = op_spec["file"]
                                _loop_interval = op_spec["loop_interval"]
                                _op_path = m.source_dir / _op_file
                                _op_sql = _op_path.read_text()
                                _op_admin_entry = admins.get(_op_db)
                                if not _op_admin_entry:
                                    _report(m.name, f"action: concurrent op db {_op_db} not found")
                                    return
                                _op_admin = _op_admin_entry["admin"]
                                try:
                                    while not _action_stop_flag.is_set():
                                        try:
                                            # ${TOKEN} form, like every other SQL file; str.format
                                            # cannot see it, which is why three cases hardcoded a schema.
                                            _op_admin.run_sql(render(_op_sql, tokens))
                                            _report(m.name, f"action: concurrent {_op_file} executed")
                                        except Exception as e:
                                            # Log but don't fail: concurrent ops are best-effort
                                            _report(m.name, f"action: concurrent {_op_file} error: {e}")
                                        if not _action_stop_flag.wait(_loop_interval):
                                            pass  # Timeout: loop again
                                except Exception as e:
                                    _report(m.name, f"action: concurrent thread error: {e}")

                            if concurrent_ops:
                                for _concurrent in concurrent_ops:
                                    _t = threading.Thread(target=_run_concurrent_op, args=(_concurrent,), daemon=False)
                                    _t.start()
                                    _action_threads.append(_t)
                                _report(m.name, f"action: started {len(_action_threads)} concurrent thread(s)")
                            return _action_stop_flag, _action_threads

                        def _stop_concurrent(_action_stop_flag, _action_threads):
                            if _action_threads:
                                _report(m.name, "action: stopping concurrent operations")
                                _action_stop_flag.set()
                                for _t in _action_threads:
                                    _t.join(timeout=10.0)
                                _report(m.name, f"action: stopped {len(_action_threads)} concurrent thread(s)")

                        def _wait_hb(seconds, label):
                            if not seconds:
                                return
                            _hb_w = _hb(label)
                            _w_start = time.monotonic()
                            while True:
                                _elapsed = time.monotonic() - _w_start
                                if _elapsed >= seconds:
                                    break
                                _hb_w(_elapsed, seconds)
                                time.sleep(min(2.0, seconds - _elapsed))

                        def _run_action_seeds(seeds, running_at, app_name):
                            # (db, file, after): `after` counts from the moment the action's
                            # app was RUNNING again, like a top-level seed's `after`.
                            for _db, _f, _after in seeds:
                                _wait_hb(running_at + _after - time.monotonic(),
                                         f"action: waiting to seed {_f} into {app_name}'s source")
                                _run_files([(_db, _f)])

                        for action_spec in m.action_specs:
                            if action_spec["type"] == "stop_start_cycle":
                                cycles = action_spec["cycles"]
                                delay = action_spec["delay_before_stop"]
                                duration = action_spec["stop_duration"]
                                concurrent_ops = action_spec.get("concurrent", [])

                                _report(m.name, f"action: stop_start_cycle ({cycles} cycles)" +
                                        (f" with {len(concurrent_ops)} concurrent op(s)" if concurrent_ops else ""))

                                _action_stop_flag, _action_threads = _start_concurrent(concurrent_ops)

                                try:
                                    for cycle in range(1, cycles + 1):
                                        _tag = "" if cycles == 1 else f" [{cycle}/{cycles}]"

                                        if delay:
                                            _hb_wait = _hb(f"action: waiting to stop app{_tag}")
                                            _t_start = time.monotonic()
                                            while True:
                                                _elapsed = time.monotonic() - _t_start
                                                if _elapsed >= delay:
                                                    break
                                                _hb_wait(_elapsed, delay)
                                                time.sleep(min(2.0, delay - _elapsed))

                                        _report(m.name, f"action: stopping app{_tag}")
                                        client.stop_app(app)

                                        if duration:
                                            _hb_sleep = _hb(f"action: app stopped, waiting to restart{_tag}")
                                            _s_start = time.monotonic()
                                            while True:
                                                _elapsed = time.monotonic() - _s_start
                                                if _elapsed >= duration:
                                                    break
                                                _hb_sleep(_elapsed, duration)
                                                time.sleep(min(2.0, duration - _elapsed))

                                        _report(m.name, f"action: starting app{_tag}")
                                        client.start_app(app)
                                        client.await_running(app, timeout=m.timeout)
                                        _report(m.name, f"action: app running{_tag}")
                                finally:
                                    _stop_concurrent(_action_stop_flag, _action_threads)
                            elif action_spec["type"] == "drop_recreate_app":
                                # Stop -> force-drop -> rebuild the named app from its own block
                                # of the rendered TQL -> deploy -> start. This is the source-
                                # recreation event a multi-app topology may need to survive,
                                # and FORCE (via _force_drop) is the only drop that survives a
                                # wedged adapter (G-15).
                                _dr_app = render(action_spec["app"], tokens)
                                _dr_delay = action_spec["delay_before_stop"]
                                _dr_wait = action_spec["recreate_wait"]
                                _report(m.name, f"action: drop_recreate_app ({_dr_app})")
                                if _dr_delay:
                                    _hb_d1 = _hb(f"action: waiting to stop {_dr_app}")
                                    _d_start = time.monotonic()
                                    while True:
                                        _elapsed = time.monotonic() - _d_start
                                        if _elapsed >= _dr_delay:
                                            break
                                        _hb_d1(_elapsed, _dr_delay)
                                        time.sleep(min(2.0, _dr_delay - _elapsed))
                                try:
                                    client.stop_app(_dr_app)
                                    _report(m.name, f"action: {_dr_app} stopped")
                                except Exception as _de:
                                    # Best-effort: a HALTED or already-stopped app still drops.
                                    _report(m.name, f"action: stop {_dr_app} best-effort failed: {_de}")
                                if action_spec["capture"] or action_spec["stopped_seed"]:
                                    # What is read or written "while stopped" must not race a
                                    # STOP that has been accepted and not yet taken effect.
                                    recovery.await_left_running(client, _dr_app, timeout=min(300, m.timeout))
                                    appactions.run_captures(
                                        client, action_spec["capture"], ns, tokens, m.timeout,
                                        report=lambda msg: _report(m.name, msg))
                                    if action_spec["stopped_seed"]:
                                        _report(m.name, f"action: seeding while {_dr_app} is stopped "
                                                        f"({len(action_spec['stopped_seed'])} file(s))")
                                        _run_files(action_spec["stopped_seed"])
                                try:
                                    client.api.undeploy_application(_dr_app)
                                    _report(m.name, f"action: {_dr_app} undeployed")
                                except Exception as _ude:
                                    _report(m.name, f"action: undeploy {_dr_app} best-effort failed: {_ude}")
                                _dropped = client._force_drop(_dr_app)
                                if _dropped is None:
                                    raise RuntimeError(
                                        f"action: drop_recreate_app: DROP of {_dr_app} timed out "
                                        f"and may still be running on the server")
                                if not _dropped:
                                    raise RuntimeError(
                                        f"action: drop_recreate_app could not force-drop {_dr_app}")
                                _report(m.name, f"action: {_dr_app} dropped (FORCE)")
                                if _dr_wait:
                                    _hb_d2 = _hb(f"action: {_dr_app} dropped, waiting to recreate")
                                    _d_start = time.monotonic()
                                    while True:
                                        _elapsed = time.monotonic() - _d_start
                                        if _elapsed >= _dr_wait:
                                            break
                                        _hb_d2(_elapsed, _dr_wait)
                                        time.sleep(min(2.0, _dr_wait - _elapsed))
                                # `tokens:` overrides apply to this render only (a StartPosition
                                # that carries a captured position, say).
                                _full_tql = _rendered_tql(
                                    m, appactions.override_tokens(action_spec["tokens"], tokens)
                                    if action_spec["tokens"] else tokens, upload_renames)
                                _blk = re.search(
                                    r"CREATE\s+OR\s+REPLACE\s+APPLICATION\s+" + re.escape(_dr_app)
                                    + r"\b.*?END\s+APPLICATION\s+" + re.escape(_dr_app) + r"\s*;",
                                    _full_tql, flags=re.S | re.I)
                                if not _blk:
                                    raise RuntimeError(
                                        f"action: drop_recreate_app found no "
                                        f"'CREATE OR REPLACE APPLICATION {_dr_app} ... END' block "
                                        f"in the rendered TQL to recreate it from")
                                _dep = re.search(
                                    r"DEPLOY\s+APPLICATION\s+" + re.escape(_dr_app) + r"\b[^;]*;",
                                    _full_tql, flags=re.I)
                                _dep_cmd = _dep.group(0) if _dep else f"DEPLOY APPLICATION {_dr_app};"
                                _recreate_marks = [(_svc, _drv, _mode, _drv.reader_mark())
                                                   for _svc, _drv, _mode, _ in reader_waits]
                                client.deploy_tql(
                                    f"USE {ns};\n{_blk.group(0)}\n"
                                    f"{_dep_cmd}\n"
                                    f"START APPLICATION {_dr_app};\n")
                                client.await_running(_dr_app, timeout=min(300, m.timeout))
                                _dr_running_at = time.monotonic()
                                _report(m.name, f"action: {_dr_app} recreated and RUNNING")
                                _dr_seeds = action_spec.get("seed", [])
                                if _dr_seeds:
                                    for _svc, _drv, _mode, _mark in _recreate_marks:
                                        _waited = _drv.wait_reader_ready(
                                            _mode, _mark, m.timeout,
                                            progress=lambda msg: _report(m.name, msg))
                                        _report(m.name, f"{_svc} {_mode} reader ready after recreation ({_waited:.0f}s)")
                                    _report(m.name, f"action: seeding post-recreation data ({len(_dr_seeds)} file(s))")
                                    _run_action_seeds(_dr_seeds, _dr_running_at, _dr_app)
                            elif action_spec["type"] == "capture":
                                # Bind a value read now (DESCRIBE / MON) to ${token} for every
                                # later render: TQL, SQL, assertion values.
                                _wait_hb(action_spec["delay_before"],
                                         f"action: waiting to capture ${{{action_spec['token']}}}")
                                appactions.capture(client, action_spec, ns, tokens, m.timeout,
                                                   report=lambda msg: _report(m.name, msg))
                            elif action_spec["type"] == "alter_recompile":
                                # The in-place upgrade: the app, and so its checkpoint, survives.
                                _ar_app = render(action_spec["app"], tokens)
                                _report(m.name, f"action: alter_recompile ({_ar_app})")
                                _wait_hb(action_spec["delay_before_stop"],
                                         f"action: waiting to stop {_ar_app}")
                                client.stop_app(_ar_app)
                                recovery.await_left_running(client, _ar_app, timeout=min(300, m.timeout))
                                _report(m.name, f"action: {_ar_app} stopped")
                                appactions.run_captures(
                                    client, action_spec["capture"], ns, tokens, m.timeout,
                                    report=lambda msg: _report(m.name, msg))
                                if action_spec["stopped_seed"]:
                                    _report(m.name, f"action: seeding while {_ar_app} is stopped "
                                                    f"({len(action_spec['stopped_seed'])} file(s))")
                                    _run_files(action_spec["stopped_seed"])
                                _ar_path = m.source_dir / action_spec["file"]
                                # the same UploadedFiles/<from> -> <to> rewrite the app's TQL gets
                                _ar_frag = render(appactions.apply_upload_renames(
                                    _ar_path.read_text(), upload_renames), tokens)
                                self._slt_inputs.record("tql-fragment", action_spec["file"], _ar_frag, path=_ar_path)
                                _ar_tql = appactions.alter_tql(
                                    ns, _ar_app, _ar_frag, appactions.deploy_statement(
                                        _rendered_tql(m, tokens, upload_renames), _ar_app))
                                client.deploy_tql(_ar_tql)
                                client.await_running(_ar_app, timeout=min(300, m.timeout))
                                _ar_running_at = time.monotonic()
                                _report(m.name, f"action: {_ar_app} altered, recompiled and RUNNING")
                                if action_spec["seed"]:
                                    _report(m.name, f"action: seeding after the recompile "
                                                    f"({len(action_spec['seed'])} file(s))")
                                    _run_action_seeds(action_spec["seed"], _ar_running_at, _ar_app)
                            elif action_spec["type"] == "service_outage":
                                _so_svc = action_spec["service"]
                                _so_cycles = action_spec["cycles"]
                                _so_c, _so_why = outage.container_for(_so_svc)
                                if _so_c is None:
                                    # Checked before provisioning, so the environment changed.
                                    raise outage.OutageError(_so_why)
                                _so_graceful, _so_restart = outage.hooks_for(
                                    _so_svc, action_spec["signal"])
                                _so_ops = action_spec.get("concurrent", [])
                                _report(m.name, f"action: service_outage {_so_svc} "
                                                f"({_so_cycles} cycles, {action_spec['signal']}"
                                                f"{', graceful_stop' if _so_graceful else ''})")
                                _so_flag, _so_threads = _start_concurrent(_so_ops)
                                try:
                                    for cycle in range(1, _so_cycles + 1):
                                        _tag = "" if _so_cycles == 1 else f" [{cycle}/{_so_cycles}]"
                                        _wait_hb(action_spec["delay_before"],
                                                 f"action: waiting to "
                                                 f"{'restart' if _so_restart else 'stop'} "
                                                 f"{_so_svc}{_tag}")
                                        outage.cycle(
                                            _so_c, action_spec["signal"], action_spec["down_for"],
                                            action_spec["ready_timeout"],
                                            wait=lambda s, _t=_tag: _wait_hb(
                                                s, f"action: {_so_svc} down, waiting to restart{_t}"),
                                            report=lambda msg: _report(m.name, msg),
                                            progress=_hb(f"action: {_so_svc} "
                                                         f"{'restart' if _so_restart else 'stop/start'}"
                                                         f"{_tag}"),
                                            graceful=_so_graceful, restart=_so_restart)
                                        _report(m.name, f"action: {_so_svc} back{_tag}; "
                                                        f"watching app {action_spec['settle']:.0f}s")
                                        _st = outage.watch_app(client, app, action_spec["settle"],
                                                               report=lambda msg: _report(m.name, msg))
                                        _report(m.name, f"action: app {_st}{_tag}")
                                finally:
                                    _stop_concurrent(_so_flag, _so_threads)
                        ran = True

                    # SLT_SKIP_VERIFY: the app is now deployed, started, and seeded — stop before
                    # any output verification and report the test as skipped. pytest.skip raises a
                    # Skipped (a BaseException), so it bypasses the `except AssertionFailed` below
                    # and the `not ran` guard, and still runs the finally (teardown-vs-keep is
                    # governed independently by SLT_KEEP_RESOURCES).
                    if skip_verify:
                        pytest.skip("SLT_SKIP_VERIFY: app started + seeded; output not verified")

                    # expect_halt: the correct outcome IS a terminal HALT (a raise-path test).
                    # Runs FIRST among the assertions (the post_start seed that triggers the
                    # halt has already been applied): the data tiers below then observe the
                    # POST-halt state -- "did a fragment of the failed transaction land?" --
                    # rather than passing on a count that was true before anything arrived.
                    if m.expect_halt:
                        _report(m.name, "asserting: expect_halt")
                        records = assert_halt(
                            client, smoke_apps, timeout=m.timeout,
                            progress=_hb("expect_halt: waiting for terminal HALT"),
                            contains=m.expect_halt_contains,
                            log_tail=lambda: _node_log_since(ctx, log_marks))
                        self._slt_records += records
                        for rec in records:
                            if rec.get("status") == "passed" and m.expect_halt_contains:
                                # Extract just the verification part
                                detail = rec['detail']
                                if "expect_halt_contains verified:" in detail:
                                    verified_part = detail.split("expect_halt_contains verified:")[1].strip()
                                    _report(m.name, f"  ✓ {verified_part}")
                        ran = True
                    if m.lifecycle is not None:
                        # C7.2: positive completion and stability before any assertion.
                        _report(m.name, "lifecycle: proving completion")
                        _slt_lifecycle.complete(
                            self._slt_lc, m.lifecycle, client=client, apps=smoke_apps, admins=admins,
                            tokens=tokens, ident=ident, owned=ledger.owns,
                            read_files=lambda _p, **_k: read_server_files(ctx, _p, **_k),
                            source_dir=m.source_dir)
                    if m.assert_.get("data"):
                        _report(m.name, "asserting: data")
                        # C8: exact specs compare slt-canon/1 rows; the rest stay legacy.
                        _slt_raw, _slt_exact_specs = _slt_exact.partition(m.assert_["data"], m, "data")
                        if _slt_exact_specs:
                            self._slt_records += _slt_exact.assert_exact_data(
                                admins, _slt_exact_specs, m, tokens=tokens, owned=ledger.owns,
                                lifecycle=m.lifecycle is not None, inputs=self._slt_inputs,
                                collector=self._slt_data, status_probe=_terminal_probe)
                        specs = substitute_targets(parse_data_specs(_slt_raw), tokens)
                        for admin, group in admin_groups(admins, specs, m.name, "data"):
                            self._slt_records += assert_data(
                                admin, group, m.dir, timeout=m.timeout, poll=m.diff_poll,
                                status_probe=_terminal_probe, db=_spec_route(group[0]),
                                progress=_hb("asserting data"), tokens=tokens)
                        ran = True
                    if m.assert_.get("diff"):
                        _report(m.name, "asserting: diff")
                        # C8: block-gated diffs compare slt-canon/1 rows, the source as expected.
                        _slt_raw, _slt_exact_specs = _slt_exact.partition(m.assert_["diff"], m, "diff")
                        if _slt_exact_specs:
                            self._slt_records += _slt_exact.assert_exact_diff(
                                admins, _slt_exact_specs, m, tokens=tokens, owned=ledger.owns,
                                lifecycle=m.lifecycle is not None, inputs=self._slt_inputs,
                                collector=self._slt_data, status_probe=_terminal_probe)
                        specs = substitute_targets(parse_diff_specs(_slt_raw), tokens)
                        admin_map = {db: e["admin"] for db, e in admins.items()}
                        # a legacy diff.exact true is also recorded as a legacy-text/1 comparison.
                        self._slt_records += _slt_exact.legacy_diff(
                            assert_diff, admin_map, specs, collector=self._slt_data, **diff_kwargs(m),
                            status_probe=_terminal_probe, progress=_hb("asserting diff"))
                        ran = True
                    if m.assert_.get("file"):
                        _report(m.name, "asserting: file")
                        # C8: exact file specs compare slt-canon/1 events (docker mode).
                        _slt_raw, _slt_exact_specs = _slt_exact.partition(m.assert_["file"], m, "file")
                        if _slt_exact_specs:
                            self._slt_records += _slt_exact.assert_exact_file(
                                _slt_exact_specs, m, tokens=tokens, owned=ledger.owns,
                                lifecycle=m.lifecycle is not None, inputs=self._slt_inputs, mode=ctx.mode,
                                collector=self._slt_data, status_probe=_terminal_probe)
                        fspecs = parse_file_specs(_slt_raw)
                        for _fs in fspecs:
                            _fs["path"] = render(_fs["path"], tokens)
                        self._slt_records += assert_file(
                            lambda p: read_server_files(ctx, p), fspecs, m.dir, timeout=m.timeout,
                            status_probe=_terminal_probe, progress=_hb("asserting file"), tokens=tokens)
                        ran = True
                    if m.assert_.get("gcs"):
                        _report(m.name, "asserting: gcs")
                        gspecs = parse_gcs_specs(m.assert_["gcs"])
                        for _gs in gspecs:
                            _gs["bucket"] = render(_gs["bucket"], tokens)
                            _gs["object"] = render(_gs["object"], tokens)
                        gcs_entry = require_service_admin(admins, "gcs", m.name, "assert.gcs")
                        self._slt_records += assert_gcs(
                            gcs_entry["admin"], gspecs, timeout=m.timeout, status_probe=_terminal_probe,
                            progress=_hb("asserting gcs"))
                        ran = True
                    if m.assert_.get("json"):
                        _report(m.name, "asserting: json")
                        jspecs = substitute_targets(parse_json_specs(m.assert_["json"]), tokens)
                        for admin, group in admin_groups(admins, jspecs, m.name, "json"):
                            self._slt_records += assert_json(
                                admin, group, m.dir, timeout=m.timeout, status_probe=_terminal_probe,
                                db=_spec_route(group[0]), progress=_hb("asserting json"), tokens=tokens)
                        ran = True
                    if m.assert_.get("monitor"):
                        # T4-MON: what the platform's monitor SHOWS for the target, read the
                        # way the console reads it. After the data tiers, so a figure that is
                        # merely late (MON republishes per snapshot) is polled for, not raced.
                        _report(m.name, "asserting: monitor")
                        mspecs = parse_monitor_specs(m.assert_["monitor"])
                        self._slt_records += assert_monitor(
                            client, app, mspecs, timeout=m.timeout, status_probe=_terminal_probe,
                            progress=_hb("asserting monitor"), tokens=tokens)
                        ran = True
                    if m.assert_.get("checkpoint_history") is not None:
                        # Direct proof a checkpoint was (or was not) RECORDED, by name, rather
                        # than inferred from row counts surviving a restart.
                        _report(m.name, "asserting: checkpoint_history")
                        cp_expected = parse_checkpoint_history_spec(m.assert_["checkpoint_history"])
                        self._slt_records += assert_checkpoint_history(
                            client, app, cp_expected, timeout=m.timeout, status_probe=_terminal_probe,
                            progress=_hb("asserting checkpoint_history"))
                        ran = True
                    if m.assert_.get("jmx"):
                        # A plugin's MBean attributes, read from the stack's Prometheus JMX
                        # exporters. `component` is token-rendered, then qualified by ${NS}.
                        _report(m.name, "asserting: jmx")
                        if ctx.mode != "docker":
                            pytest.fail(f"{m.name}: assert.jmx reads the Docker stack's Prometheus JMX "
                                        f"exporters; a {ctx.mode} Striim has none")
                        xspecs = parse_jmx_specs(m.assert_["jmx"])
                        for _xs in xspecs:
                            _xs["bean"]["component"] = render(_xs["bean"]["component"], tokens)
                            try:
                                render_row_keys(_xs, lambda k: render(k, tokens))
                            except JmxSpecError as e:
                                pytest.fail(f"{m.name}: {e}")
                        self._slt_records += assert_jmx(
                            xspecs, ns, timeout=m.timeout, status_probe=_terminal_probe,
                            progress=_hb("asserting jmx"))
                        ran = True
                except AssertionFailed as e:
                    self._slt_records.extend(e.records)
                    raise
            if not ran:
                pytest.fail(f"{m.name}: no supported assertion ran (supported: smoke, data, diff, file, gcs, json, monitor, checkpoint_history, jmx)")
            succeeded = True
        finally:
            self._slt_cleanup_active = True
            # Stop the run-level progress line before teardown emits its own output.
            _clear_progress()
            # C7.6: cleanup deletes only this identity's confirmed ledger
            # entries, by exact name, re-reads them, and becomes part of the result. SLT_KEEP_RESOURCES
            # (always) / SLT_KEEP_RESOURCES_ON_ERROR (on failure) keep debugging resources;
            # only failure-only retention drops PostgreSQL slots, stopping apps on 55006.
            _slt_keep_reason = None
            if should_keep_resources(os.environ, deploy_attempted, succeeded):
                self.config._slt_keep_resources = True   # keep provisioned services at session end
                _slt_keep_reason = ("SLT_KEEP_RESOURCES is set" if os.environ.get("SLT_KEEP_RESOURCES")
                                    else f"{m.name} failed and SLT_KEEP_RESOURCES_ON_ERROR is set")
                note = (" [SLT_SKIP_VERIFY: app started + seeded, output NOT verified]"
                        if skip_verify else "")
                print(f"[slt] {_slt_keep_reason} — preserving "
                      f"{ns} (app {app} + schema {schema!r}) and the provisioned services for "
                      f"inspection at {ctx.url} (user {ctx.user!r}); remove them with "
                      f"python -m livetest.ownership replay {getattr(ledger, 'path', None)}.{note}")
            elif skip_verify:
                print("[slt] SLT_SKIP_VERIFY without SLT_KEEP_RESOURCES — the started app "
                      "will be torn down; set SLT_KEEP_RESOURCES=1 to inspect it.")

            def _slt_upload_names():
                try:
                    return [_upload_dest_name(u["from"], u["to"], tid, tokens) for u in m.op_uploads]
                except Exception:
                    return []

            def _slt_file_globs():
                # never raise inside the finally: an unrenderable spec already failed the case
                try:
                    if not m.assert_.get("file") or m.lifecycle is not None:
                        return []
                    return [render(_fs["path"], tokens) for _fs in parse_file_specs(m.assert_["file"])]
                except Exception:
                    return []

            cleanup = _slt_ownership.run_cleanup(
                ledger, client=client, admins=admins, ctx=ctx, tokens=tokens,
                gcs_cleanup=gcs_cleanup, kafka_cleanup=kafka_cleanup, upload_names=_slt_upload_names(),
                file_globs=_slt_file_globs(),
                checkpoint_ns=ident.ns if m.modules else None, keep_reason=_slt_keep_reason,
                keep_slots=bool(os.environ.get("SLT_KEEP_RESOURCES")))
            if _slt_keep_reason:
                if os.environ.get("SLT_KEEP_RESOURCES"):
                    print("[slt] PostgreSQL replication slots and app state kept for exploration.")
                else:
                    print(f"[slt] STOP sent to apps: {', '.join(cleanup.stopped_apps)}; "
                          "writes in flight may be discarded." if cleanup.stopped_apps
                          else "[slt] No apps stopped for slot cleanup.")
                    for entry in cleanup.owned:
                        if entry["kind"] == "pg-slot" and entry["state"] == "verified-absent":
                            print(f"[slt] Removed replication slot {entry['name']}. To restart: "
                                  f"SELECT pg_create_logical_replication_slot('{entry['name']}', 'wal2json'); "
                                  f"then START APPLICATION {app}; (and any other stopped apps).")
                    if cleanup.status == "failed":
                        print(f"[slt] PostgreSQL slot cleanup failed: {cleanup.detail}")
            self._slt_cleanup = cleanup.record()
            self._slt_resources = cleanup.resources()
            if m.lifecycle is None and _slt_keep_reason is None:
                # the kinds the ledger never deletes keep their pre-ledger teardown (no behaviour change
                # for a case without the block)
                try:
                    _unowned = teardown_unowned(admins, m.ddl_files, tokens, gcs_cleanup, kafka_cleanup,
                                                m.assert_.get("file"), lambda _p: clear_server_files(ctx, _p),
                                                _slt_upload_names(), lambda _n: opartifacts.delete_artifacts(ctx, _n))
                    self._slt_resources = note_unowned_teardown(self._slt_resources, _unowned)
                except Exception as _e:         # noqa: BLE001 - never replace the case's own outcome
                    print(f"[slt] WARNING: {m.name}: best-effort teardown failed: {_e!r}")

            # Release the shared in-use lock LAST, after teardown has finished.
            #
            # Ordering is the whole point and an earlier revision got it backwards: this block
            # sat at the TOP of the `finally`, so the entire teardown window -- undeploying an
            # app that still has the OP class loaded, dropping the namespace, deleting
            # artifacts -- ran with the lock released. A sibling's exclusive_reload could be
            # granted the moment assertions ended and issue UNLOAD OPEN PROCESSOR while this
            # app was still coming down, which is exactly the "yank the artifact out from
            # under a running app" case the lock exists to prevent.
            #
            # Nothing below this point may touch the loaded artifact.
            if in_use_held:
                try:
                    _in_use.__exit__(None, None, None)
                finally:
                    in_use_held = False
            # C7.4: a cleanup failure fails a run that had nothing else in
            # flight; an exception already propagating is never replaced.
            import sys as _slt_sys
            if _slt_sys.exc_info()[0] is None and cleanup is not None and cleanup.status == "failed":
                raise _slt_ownership.CleanupError(f"{m.name}: cleanup failed: {cleanup.detail}")

    def repr_failure(self, excinfo):
        # The failure text reaches the terminal and junit, outside the evidence redactor: a server error
        # can quote the rendered TQL, credentials included. Known secrets and URL userinfo are masked
        # here too (home and host name are kept: they are this machine's own report).
        return _slt_evidence.redact_text(f"[{self.name}] {excinfo.value}", _slt_evidence.known_secrets(),
                                         home="", hostname="")

    def reportinfo(self):
        return self.path, 0, f"live: {self.name}"

def _active_xfail(config, m: TestManifest) -> dict:
    # The manifest's xfail as it applies to THIS run's release: `xfail.releases` turns it off on
    # every release it does not name. The release is resolved only when a manifest asks. This runs
    # at collection, so a bad install or an unparseable version must not abort the session: it
    # turns the xfail off (unlisted => must pass), and the live test itself then fails on the install.
    if not m.xfail.get("releases"):
        return m.xfail
    try:
        return xfail_for_release(m.xfail, _resolve_release(config)["STRIIM_VERSION"])
    except _releases.ReleaseError:
        return {}


def pytest_collection_modifyitems(config, items):
    # Fail fast on a duplicate manifest `name:`. The name derives the Striim namespace AND
    # the Postgres schema (testid_to_ns_app / schema_for), so two tests sharing a name would
    # clobber each other's namespace/schema and be indistinguishable — the usual cause is a
    # copy-pasted test.yaml that kept the old name. Caught at collection, not mid-run.
    seen = {}
    dups = {}
    for it in items:
        if not isinstance(it, LiveItem):
            continue
        try:
            m = load_manifest(it.manifest_path)
            name = m.name
        except ManifestError:
            continue   # a malformed manifest surfaces as its own collection error
        path = str(it.manifest_path)
        if name in seen and seen[name] != path:
            dups.setdefault(name, {seen[name]}).add(path)
        else:
            seen[name] = path
        # Apply xfail marker if test is marked as expected to fail. `raises=` is the narrowing
        # (design §10.1): only an assertion failure on a tier the manifest's `xfail.tiers` names is
        # the expected failure -- runtest() re-types such a failure as ExpectedAssertionFailure.
        # Everything else (deploy error, provisioning, smoke, an unlisted tier, a spec problem)
        # fails the test the way it would without the marker.
        xf = _active_xfail(config, m)
        if xf:
            it.add_marker(pytest.mark.xfail(
                reason=xf.get("reason", ""),
                strict=xf.get("strict", False),
                raises=ExpectedAssertionFailure,
            ))
    if dups:
        detail = "; ".join(f"{n!r} -> {sorted(p)}" for n, p in sorted(dups.items()))
        raise pytest.UsageError(
            "duplicate live-test name(s): each manifest `name:` must be unique — it derives the "
            f"Striim namespace + Postgres schema, so collisions clobber state. {detail}")

# C7.1: infrastructure ownership is declared at the live
# execution boundary -- once live items are collected, in the controller and in every xdist
# worker -- and never for --collect-only or in pytest_configure. An undeclared run is a
# UsageError before any provisioning.
def pytest_collection_finish(session):
    if _slt_infra.declare_if_live(session, LiveItem) is not None and not hasattr(session.config, "workerinput"):
        _slt_report_leftovers(session.config)


def _slt_report_leftovers(config) -> None:
    """Once per live session (the controller): name the ownership ledgers earlier runs left
    unfinished, with the command that reclaims each. Nothing else ever reclaims them."""
    if getattr(config, "_slt_leftovers_reported", False):
        return
    config._slt_leftovers_reported = True
    try:
        from livetest import layout
        left = _slt_ownership.leftover_ledgers(layout.state_dir(), os.environ.get("SLT_RUN_EPOCH"))
    except Exception:                   # noqa: BLE001 - a notice only; never fails the run
        return
    for path, why in left:
        print(f"[slt] leftover ownership ledger: {_slt_ownership.leftover_notice(path, why)}")


# the xdist controller declares ownership from the executable selection a worker reports, before
# anything is scheduled; an empty or non-live selection needs no declaration.
@pytest.hookimpl(optionalhook=True)
def pytest_xdist_node_collection_finished(node, ids):
    config = getattr(node, "config", None)
    if _slt_infra.declare_for_distributed_selection(config, ids) is not None:
        _slt_report_leftovers(config)


def pytest_unconfigure(config):
    # Releases the exclusive endpoint lease or the shared marker, even after a session error.
    _slt_infra.release(config)


# C5 1.9.0: the invocation this session's JUnit belongs to, and the xdist
# controller's view of the evidence errors its workers recorded (folded at session finish).
def pytest_sessionstart(session):
    _slt_evidence.bind_invocation(session.config)


def pytest_runtest_logreport(report):
    _slt_evidence.note_report(report)


# ---------------------------------------------------------------------------
# Structured .slt.json results sidecar (schema_version 1). Written next to the
# junitxml the console names, so the console reads per-assertion structure by a
# path it derives itself (same stem, `.slt.json`). Keeps the JUnit XML unchanged.
# ---------------------------------------------------------------------------

def _slt_sidecar_path(config):
    """The sidecar path = the junitxml path with a `.slt.json` stem (its sibling), or
    None when no --junitxml was given (nothing to pair the sidecar with)."""
    xml = getattr(getattr(config, "option", None), "xmlpath", None)
    if not xml:
        return None
    p = Path(xml)
    return p.with_name(p.stem + ".slt.json")


def _op_poison_marker_path(xml_path):
    """The OP-poison marker path (sibling of the junit xml), or None when no --junitxml is
    set. In parallel mode a poisoned OP deploy drops this marker instead of restarting the
    shared app nodes; the console post-flight (Phase 3, §C.5) scans for these to serialise
    ONE app-node restart + a single re-queue of the poisoned test."""
    if not xml_path:
        return None
    p = Path(xml_path)
    return p.with_name(p.name + ".op-poisoned.flag")


def _slt_skip_reason(report):
    lr = getattr(report, "longrepr", None)
    reason = None
    if isinstance(lr, tuple) and len(lr) == 3:
        reason = lr[2]
    elif lr is not None:
        reason = str(lr)
    if reason and reason.startswith("Skipped: "):
        reason = reason[len("Skipped: "):]
    return reason


def _slt_collect_report(config, item, report):
    """Fold one pytest phase report for a LiveItem into config._slt_results[nodeid].

    The `call` phase decides passed/failed/skipped; a `setup` failure is an error (and a
    `setup` skip is a skip); a `teardown` failure downgrades an otherwise-passed test to
    error. Test-level topology/services/duration + the per-assertion records the item
    accumulated in runtest are captured here."""
    results = config.__dict__.setdefault("_slt_results", {})
    existing = results.get(item.nodeid)

    when = getattr(report, "when", "call")
    outcome = getattr(report, "outcome", "passed")
    if when == "call":
        status = {"passed": "passed", "failed": "failed", "skipped": "skipped"}.get(outcome, "error")
    elif when == "setup":
        if outcome == "skipped":
            status = "skipped"
        elif outcome == "failed":
            status = "error"
        else:
            return   # setup passed — wait for the call phase
    else:  # teardown
        if outcome == "failed" and (existing is None or existing.get("status") == "passed"):
            status = "error"
        else:
            return

    tr = {
        "name": getattr(item, "name", item.nodeid),
        "nodeid": item.nodeid,
        "status": status,
        "topology": getattr(item, "_slt_topology", "single"),
        "services": list(getattr(item, "_slt_services", [])),
        "duration": max(0.0, float(getattr(report, "duration", 0.0) or 0.0)),
        "skip_reason": _slt_skip_reason(report) if status == "skipped" else None,
        "assertions": list(getattr(item, "_slt_records", [])),
    }
    results[item.nodeid] = tr

    # Point CI dashboards at the sidecar via the junit <properties> block.
    path = _slt_sidecar_path(config)
    props = getattr(item, "user_properties", None)
    if path is not None and props is not None and not any(k == "slt_result_json" for k, _ in props):
        props.append(("slt_result_json", str(path)))


def _slt_write_sidecar(config):
    """Write the accumulated results to the sidecar. No-op (never raises) when there is no
    junitxml path or no results; a write failure warns but never masks the run outcome."""
    path = _slt_sidecar_path(config)
    results = getattr(config, "_slt_results", None)
    if not path or not results:
        return
    try:
        write_sidecar(path, list(results.values()))
    except Exception as e:
        print(f"[slt] WARNING: failed to write results sidecar {path}: {e!r}")
        _slt_evidence.sidecar_write_failed(config, path, e)   # F3: part of the session outcome


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    """xfail narrowing (design §10.1). For a LiveItem whose manifest is xfail'd, only an assertion
    failure on a tier named by `xfail.tiers` is the expected outcome: re-type that one as
    ExpectedAssertionFailure so the marker's `raises=` accepts it, and leave every other exception
    -- deploy error, provisioning, smoke, an unlisted tier, a record-less spec problem -- as it is,
    where the marker reports a real failure. A hookwrapper rather than a runtest() wrapper so
    runtest()'s own source stays what the infra-guard tests inspect."""
    outcome = yield
    if not isinstance(item, LiveItem):
        return
    exc = outcome.excinfo
    if exc is None or not isinstance(exc[1], AssertionFailed) or isinstance(exc[1], ExpectedAssertionFailure):
        return
    try:
        m = load_manifest(item.manifest_path)
    except Exception:
        return
    xf = _active_xfail(item.config, m)
    if not xf:
        return
    typed = ExpectedAssertionFailure.if_covered(exc[1], xf.get("tiers", ()))
    if typed is not exc[1]:
        outcome.force_exception(typed)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    if isinstance(item, LiveItem):
        rep = outcome.get_result()
        # Two facts _slt_infra_guard needs, carried as ATTRIBUTES rather than as text inside
        # the skip reason. xdist keeps them: reports serialise from report.__dict__ and are
        # restored with **extra, so both survive the trip to the controller.
        #
        # An earlier design put a "SLT-INFRA: " prefix on the reason instead. That reason is
        # read by three components which cannot import each other -- this plugin's sidecar,
        # a runner's results chart, and a console UI -- so each needed its own copy of the
        # literal and its own strip, and three separate review rounds each found the marker
        # leaking into one more of them. A flag that is not a string cannot leak.
        rep._slt_live = True
        # "" when the item was not blocked; otherwise the CAUSE ("cluster"/"docker"/"build").
        rep._slt_infra_skip = getattr(item, "_slt_infra_skip", "")
        _slt_collect_report(item.config, item, rep)
        _slt_evidence.finalize_from_report(item, rep)   # C7.4: one envelope per outcome


# C7.4: tryfirst, so a failed teardown of owned infrastructure is known
# before the junitxml report is written and becomes part of the run outcome.
@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    config = session.config
    _slt_write_sidecar(config)   # before any early-return below, so KEEP_SERVICES still emits it
    # Before the teardown early-returns below, for the same reason the sidecar is: a run that
    # proved nothing must say so whether or not services are being kept. Assigned only on a
    # CHANGE, which protects the NON-firing path: `exitstatus` is snapshotted before any impl
    # runs, so writing it back unconditionally would revert an escalation another plugin's
    # sessionfinish had made. When the guard does fire it still overwrites, so a hypothetical
    # earlier `session.exitstatus = 2` would be downgraded to 1 -- no plugin in this tree does
    # that, and the honest scope of the guard is stated here rather than overclaimed.
    decided = _slt_infra_guard(session, exitstatus)
    if decided != exitstatus:
        session.exitstatus = decided
    # C7.4: an evidence error fails the run, before any teardown early return.
    _slt_evidence.fold_evidence_errors(session, _slt_write_sidecar)
    # Keep services if asked explicitly, or if SLT_KEEP_RESOURCES was set, or if a
    # test failed under SLT_KEEP_RESOURCES_ON_ERROR (so the databases stay up for
    # inspection). Also skip teardown entirely for xdist workers (spec §C.4): under -n each
    # worker reaches sessionfinish independently, and the first to finish must not tear the
    # shared cluster + services out from under its siblings — teardown is deferred to the
    # operator / next serial run.
    if _skip_shared_teardown(os.environ) or getattr(config, "_slt_keep_resources", False):
        return
    started = getattr(config, "_slt_started", set())
    for defn in getattr(config, "_slt_defs", {}).values():
        if defn.container in started and defn.compose:
            try:
                compose_down(defn)
                # Forget what ensure_provisioned_once recorded, or the record outlives the
                # container: a later bring-up (this suite's next session, or an explicit
                # `livetest.cli start <svc>`) finds the key, skips `docker compose up`, and
                # reports success over nothing running. Only on success -- a container that
                # is still up must keep its record, so its post_up is not re-run.
                forget_provisioned([defn.container, f"{defn.name}-setup"], _STATE_DIR)
            except Exception as e:
                # Don't swallow silently — a failed teardown leaves the container running
                # with no signal, and they accumulate across CI runs.
                print(f"[slt] WARNING: failed to tear down service {defn.container!r}: {e!r} "
                      f"(container may still be running — 'docker compose down' manually)")
                _slt_infra.teardown_failed(config, f"service {defn.container}", e)
    # Tear down the Striim cluster too, but only if WE provisioned it (never a
    # reused native/pre-existing Striim). SLT_KEEP_SERVICES (early-return above) keeps it up.
    if getattr(config, "_slt_striim_provisioned", False):
        try:
            _sp.cluster_down(_STRIIM_DIR, getattr(config, "_slt_release", None))
        except Exception as e:
            print(f"[slt] WARNING: failed to tear down the Striim cluster: {e!r} "
                  f"(nodes may still be running — 'docker compose down' in services/striim)")
            _slt_infra.teardown_failed(config, "striim cluster", e)
    # C7.4: a failed teardown of owned infrastructure fails the run
    # (exit status, junit, the v1 sidecar and every v2 envelope), never only a warning.
    _slt_evidence.finalize_session(session, _slt_write_sidecar)
