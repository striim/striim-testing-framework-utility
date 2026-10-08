"""Python-side driver for the Java `IntegrationProcessor` (docs/INTEGRATION-TESTS.md).

Two responsibilities:

- `ensure_harness_jar()` -- build-or-reuse the shaded harness jar
  (`scripts/integration/java/target/inttest-harness-*.jar`), mirroring
  `opartifacts`'s mtime-staleness spirit but far simpler: there is exactly one
  jar, one module, no release-fingerprint dimension (the harness never links
  Striim, so it is not release-specific).
- `drive()` -- write the request.json contract, shell out to
  `java -cp harness.jar:opJar com.striim.testing.inttest.IntegrationProcessor request.json`,
  and return the parsed emitted-events JSON.

Nothing at import time touches Java, `mvn`, or the filesystem beyond this
module's own path arithmetic -- both entry points are only exercised when a
caller actually drives an operator.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import tempfile
from pathlib import Path

from . import manifest as manifest_mod
from . import paths


def _extra_jars() -> list:
    """JDBC drivers the op jar cannot bundle, for the classpath.

    ⚠ The writer shades PostgreSQL, Spanner and the rest INTO its jar, so every engine's driver
    normally arrives with the op. Teradata's does not: it is not redistributable, so the pom
    carries no dependency on it and the live tier gets it from the Striim install instead. Without
    this the Teradata variant fails at init with "No suitable driver found for jdbc:teradata://",
    which reads like a configuration error rather than a missing jar.

    Sourced from the live tier's Striim image deps -- the one copy already in the tree, which is
    how the live container gets it too. Absent, the list is empty and nothing changes: a case that
    needs it then fails with the driver error, naming what to fetch, rather than this silently
    substituting something else. INT_EXTRA_CLASSPATH appends to it for a driver kept elsewhere.
    """
    import os as _os
    from pathlib import Path as _Path
    jars = []
    deps = (_Path(__file__).resolve().parents[3]
            / "scripts/live/services/striim/images/striim/deps")
    if deps.is_dir():
        jars += [str(j) for j in sorted(deps.glob("terajdbc-*.jar"))]
        # Vertica's, for the same reason: a Vertica writer module compiles against it as a
        # system jar and does not shade it.
        jars += [str(j) for j in sorted(deps.glob("vertica-jdbc-*.jar"))]
    extra = _os.environ.get("INT_EXTRA_CLASSPATH", "").strip()
    if extra:
        jars += [p for p in extra.split(_os.pathsep) if p]
    return jars

_JAVA_DIR = paths.shim_dir()
_JAVA_SRC = _JAVA_DIR / "src"
_JAVA_POM = _JAVA_DIR / "pom.xml"


class HarnessError(Exception):
    """Raised when the IntegrationProcessor subprocess fails (non-zero exit)."""


def _files_under(directory: Path) -> list[Path]:
    return [p for p in directory.rglob("*") if p.is_file()] if directory.is_dir() else []


def _candidate_jars() -> list[Path]:
    # The pom pins <finalName>${project.artifactId}</finalName> (no version suffix), so
    # the shaded jar lands at target/inttest-harness.jar; `original-inttest-harness.jar`
    # is the shade plugin's pre-shade backup and must be excluded. The -sources/-javadoc
    # pair is excluded too: this pom does not bind those plugins today, but OP reference template
    # §10 now requires the binding fleet-wide, and _build() below hard-fails unless this
    # returns exactly one jar.
    target = _JAVA_DIR / "target"
    if not target.is_dir():
        return []
    return sorted(
        p for p in target.glob("inttest-harness*.jar")
        if not p.name.startswith("original-")
        and not p.name.endswith(("-sources.jar", "-javadoc.jar")))


def _is_stale(jar: Path) -> bool:
    jar_mtime = jar.stat().st_mtime
    sources = _files_under(_JAVA_SRC)
    if _JAVA_POM.exists():
        sources.append(_JAVA_POM)
    return any(p.stat().st_mtime > jar_mtime for p in sources)


def ensure_harness_jar() -> Path:
    """Locate the built harness jar, (re)building it with `mvn package` when
    it is missing or older than the harness's own Java sources/pom.

    Returns the absolute path to the shaded jar. Raises `HarnessError` if the
    build fails or produces no unambiguous `inttest-harness-*.jar`."""
    existing = _candidate_jars()
    if len(existing) == 1 and not _is_stale(existing[0]):
        return existing[0]
    # Under xdist every worker finds the jar stale at once after a harness source change, and
    # concurrent `mvn package` in one target/ fails or tears the jar. Build under a lock and
    # re-check inside it, so the first worker builds and the rest reuse its jar.
    from filelock import FileLock
    with FileLock(str(_JAVA_DIR / ".int-harness-build.lock")):
        existing = _candidate_jars()
        if len(existing) == 1 and not _is_stale(existing[0]):
            return existing[0]
        return _build_harness_jar()


def _build_harness_jar() -> Path:
    r = subprocess.run(
        ["mvn", "-q", "-f", str(_JAVA_POM), "package"],
        cwd=str(_JAVA_DIR), capture_output=True, text=True)
    if r.returncode != 0:
        out = (r.stderr or r.stdout or "")[-4000:]
        raise HarnessError(f"mvn package failed building the inttest harness in {_JAVA_DIR}:\n{out}")

    built = _candidate_jars()
    if len(built) != 1:
        raise HarnessError(
            f"harness build succeeded but no unambiguous inttest-harness-*.jar found in "
            f"{_JAVA_DIR / 'target'} (found: {[p.name for p in built]})")
    return built[0]


def _resolve_java(java: str | None) -> str:
    if java:
        return java
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        candidate = Path(java_home) / "bin" / "java"
        if candidate.exists():
            return str(candidate)
    found = shutil.which("java")
    if found:
        return found
    raise HarnessError(
        "no `java` executable found -- pass java=, set JAVA_HOME, or put java on PATH")


def _run_with_post_start_seed(argv, *, cwd, timeout, tempdir, on_source_started, op_jar,
                              on_mid_run=None):
    """Run the driver, and commit the case's seed while it waits after its first tick.

    The two processes rendezvous on two one-way files in `tempdir`: the driver writes `seed.ready`
    once the source has started and blocks on `seed.go`; this side runs `on_source_started` and
    then writes `seed.go`. Files rather than a socket because the driver is already a subprocess
    sharing this directory, and a file either exists or does not -- it cannot half-arrive.

    ⚠ If the driver dies before signalling, this returns its result and lets the normal non-zero
    exit path report it. Raising a handshake error here would bury the actual failure, which is
    the mistake that makes a harness problem read as an operator bug.
    """
    ready = tempdir / "seed.ready"
    go = tempdir / "seed.go"

    # The MID-RUN gates use the same two-one-way-files rendezvous, keyed by event ordinal, so one
    # run can stop several times. `midrun.N.ready` appears when the driver has fed N events and is
    # waiting; this side runs that step's SQL and writes `midrun.N.go`. Discovered by globbing
    # rather than driven from a list, so this loop needs to know nothing about which ordinals the
    # case chose -- the driver is the only thing that decides when it stops.
    mid_run_done: set[str] = set()

    # ⚠ FILES, NOT PIPES, and this is not a style choice -- it is the classic Popen deadlock.
    # `stdout=PIPE` with a loop that polls instead of reading blocks the CHILD in write() as soon
    # as it fills the ~64 KiB pipe buffer, while this side sees poll() is None and spins to its
    # deadline. Measured on this machine: 65,000 bytes of stderr completes in 0.11s, 70,000 bytes
    # deadlocks. It bites hardest exactly where it hurts most -- a JVM stack trace with a deep
    # gRPC/Spanner cause chain is the largest output the driver ever produces, and it is produced
    # on the FAILURE path, so a real operator failure would be converted into "the harness timed
    # out". `subprocess.run` (the pre_start path) never had this because communicate() drains
    # concurrently; only this loop can, so only this loop needs the redirect.
    out_path = tempdir / "driver.stdout"
    err_path = tempdir / "driver.stderr"
    seeded = False
    deadline = time.monotonic() + timeout
    with open(out_path, "w") as out_f, open(err_path, "w") as err_f:
        proc = subprocess.Popen(argv, cwd=(str(cwd) if cwd is not None else None),
                                stdout=out_f, stderr=err_f, text=True)
        try:
            while True:
                if not seeded and ready.exists():
                    if on_source_started is not None:
                        on_source_started()
                    go.write_text("")       # release the driver
                    seeded = True
                if on_mid_run is not None:
                    for signal in sorted(tempdir.glob("midrun.*.ready")):
                        if signal.name in mid_run_done:
                            continue
                        ordinal = int(signal.name.split(".")[1])
                        # ⚠ Marked done BEFORE the callback and released in a `finally`, so a
                        # step is never retried and the driver is never left blocked on a gate
                        # that will not open. If the SQL raises, the exception unwinds to the
                        # handler below, which kills the driver and re-raises -- so the operator
                        # sees the SQL error rather than a timeout. Writing the go file first
                        # costs nothing and means no unwind path can strand the subprocess.
                        mid_run_done.add(signal.name)
                        try:
                            on_mid_run(ordinal)
                        finally:
                            (tempdir / f"midrun.{ordinal}.go").write_text("")
                if proc.poll() is not None:
                    break
                if time.monotonic() > deadline:
                    proc.kill()
                    proc.wait()
                    raise subprocess.TimeoutExpired(
                        argv, timeout,
                        output=_read_text(out_path), stderr=_read_text(err_path))
                time.sleep(_POST_START_POLL_SECONDS)
        except BaseException:
            proc.kill()
            proc.wait()
            raise
    return subprocess.CompletedProcess(argv, proc.returncode,
                                       _read_text(out_path), _read_text(err_path))


def _read_text(path) -> str:
    """The driver's captured output, or "" if it never wrote any."""
    try:
        return path.read_text()
    except OSError:
        return ""


#: How often this side looks for the driver's ready signal. Bounds a handshake, never an
#: assertion -- the tick budget is still the only thing that decides a case's outcome.
_POST_START_POLL_SECONDS = 0.05


def drive(op_jar, properties: dict, input_events, types: dict | None = None,
          password_properties: list[str] | None = None, *,
          udf=None, source=None, target=None, on_source_started=None, on_mid_run=None,
          java: str | None = None, timeout: int = 120, cwd=None, log_sink: list | None = None,
          jmx=None, jmx_sink: list | None = None):
    """Drive `op_jar` with `input_events`, returning the emitted WAEvent-JSON dicts -- or, for
    a `target:` case, the run report (see `target` below), since a target emits nothing.

    `op_jar` is a path (str or Path) to a built operator jar. `properties` is
    the operator's property map (already token-substituted by the caller).
    `input_events` may be a path to a WAEvent-JSON fixture file, OR an
    already-loaded list of event dicts -- either is accepted (docs/INTEGRATION-TESTS.md).
    `types` is the source-schema map from `test.yaml`'s `types:` block (docs/INTEGRATION-TESTS.md) -- source table name -> ordered source column names -- passed
    through verbatim as `request.types`. `password_properties` is the list of
    `properties` keys from `test.yaml`'s `password_properties:` block that must
    be wrapped in the harness's mock `Password` before construction (e.g. a
    JDBC pool's `Password`/`BootstrapPassword`), passed through verbatim as
    `request.passwordProperties`. `cwd` is the operator subprocess's working
    directory (typically the test's own directory), so a config file (e.g. an
    OP's `config.json` bootstrap CSV path) can reference a sibling
    data file by plain relative path -- the harness's own request/input/output/jar
    paths are all absolute and unaffected by this. Note `properties["ConfigFile"]`
    may itself already be a per-test TEMP COPY of the fixture's checked-in config
    file: `plugin.py`'s caller pre-renders any ${...} tokens in that file's
    CONTENT (not just its path) onto a throwaway copy before calling `drive()`, so
    e.g. a table name embedded in the JSON can carry ${TID}-style isolation the
    same way ddl:/seed: SQL already can -- `drive()` itself is unaware of this and
    just opens whatever path `properties["ConfigFile"]` names.

    `udf`, when given (a `manifest.UdfSpec`), drives `op_jar` as a bare UDF pipeline
    (Java-side `UdfCore`) instead of as an OpenProcessor `Processor` -- `None` (the
    default) is the existing OP path, unchanged.

    `source`, when given (a `manifest.SourceSpec`), drives `op_jar` as a READER (Java-side
    `SourceCore`): it is ticked `source.max_ticks` times instead of being fed, so
    `input_events` must be empty or `None` -- a reader pulls from its source rather than
    consuming a stream. `None` (the default) is the existing OP path, unchanged.

    `target.timezone`, when set, runs the driver JVM at that `-Duser.timezone`. The live
    stack runs UTC, so a temporal defect that only appears at an offset zone -- a
    `timestamptz` stored nine hours out, a DATE moved a day -- is invisible everywhere
    else; this tier is the only one that can fork per zone cheaply.

    `target`, when given (a `manifest.TargetSpec`), drives `op_jar` as a TARGET (Java-side
    `TargetCore`): a `RetriableWriter` that is fed `input_events` and emits nothing. The
    return value is then a `TargetReport` DICT rather than a list of emitted events -- what
    the writer acked and the position it reports durable -- because a target's other output,
    the database it wrote, is asserted by the caller against the database itself.

    `on_source_started` is called ONCE, after the reader's first tick, while the subprocess is
    blocked waiting for it -- the hook a `seed_when: post_start` case needs. A change stream only
    captures commits made after its start timestamp, so a streaming case has to commit its data
    while the reader is already running; seeding beforehand is invisible to the stream by design,
    and seeding from a racing thread cannot know whether the stream query is open yet. It is only
    consulted when `source.seed_when == "post_start"`.

    `jmx`, when given (a `manifest.JmxSpec`), asks the runner to build, register and snapshot
    the op's MBean after the last event (Java-side `JmxSnapshot`). The snapshot dict is appended
    to `jmx_sink` -- a sidecar, like `log_sink`, so the return value keeps its shape. A missing
    snapshot file is appended as `{"error": ...}`, never skipped.

    Raises `HarnessError` on any non-zero IntegrationProcessor exit, with the
    subprocess's captured stderr included in the message.
    """
    op_jar = Path(op_jar)
    java_bin = _resolve_java(java)
    harness_jar = ensure_harness_jar()

    tempdir = Path(tempfile.mkdtemp(prefix="inttest-harness-"))
    try:
        input_path = Path(input_events) if isinstance(input_events, (str, Path)) else None
        if input_path is not None:
            input_file = input_path
        else:
            input_file = tempdir / "input.json"
            # A `source:` case has no input fixture at all; the Java side still wants a
            # readable inputFile, and asserts the list it holds is empty.
            input_file.write_text(json.dumps([] if input_events is None else list(input_events)))

        output_file = tempdir / "output.json"
        request = {
            "opJar": str(op_jar),
            "properties": {k: str(v) for k, v in (properties or {}).items()},
            "inputFile": str(input_file),
            "outputFile": str(output_file),
            "types": types,
            "passwordProperties": password_properties,
            "udf": manifest_mod.udf_to_wire(udf),
            "source": manifest_mod.source_to_wire(source),
            "target": manifest_mod.target_to_wire(target),
        }
        jmx_file = tempdir / "jmx.json"
        if jmx is not None:
            # Only when asked: every other request stays byte-identical to what it was.
            request["jmx"] = {**manifest_mod.jmx_to_wire(jmx), "outputFile": str(jmx_file)}
        request_file = tempdir / "request.json"
        request_file.write_text(json.dumps(request))

        post_start = (source is not None
                      and getattr(source, "seed_when", "pre_start") == "post_start")
        # A target case gates mid-run only when it asked to. Both hooks share one runner, because
        # both are the same rendezvous and a case could in principle use either.
        mid_run = on_mid_run is not None and target is not None and getattr(
            target, "mid_run", ())
        if post_start:
            # Same temp dir the request already lives in: the subprocess can reach it, and it is
            # removed with everything else in the finally below.
            request["source"]["seedGateDir"] = str(tempdir)
            # ⚠ Derived from THIS call's timeout, not hardcoded. The Java side must give up first
            # so its diagnostic ("the reader is fine; the handshake is not") is the one the
            # operator sees -- and a case may set `timeout:` freely, so a constant tuned against
            # the 120s default silently reinstates the bug it was meant to fix on any case that
            # lowers it.
            request["source"]["seedGateTimeoutMillis"] = max(1000, int(timeout * 1000 * 0.5))
            request_file.write_text(json.dumps(request))

        if mid_run:
            # Same reasoning as seedGateTimeoutMillis above: HALF the caller's budget, derived
            # rather than constant, so this side always expires first and its diagnostic is the one
            # the operator sees.
            request["target"]["midRunGateDir"] = str(tempdir)
            request["target"]["midRunTimeoutMillis"] = max(1000, int(timeout * 1000 * 0.5))
            request_file.write_text(json.dumps(request))

        classpath = os.pathsep.join([str(harness_jar), str(op_jar), *_extra_jars()])
        # -Duser.timezone must be a JVM ARGUMENT, not an env var or a later
        # TimeZone.setDefault: a driver reads the default zone during its own class
        # initialisation, so setting it after the JVM is up is already too late for some.
        # This is the whole reason a target case can ask for a zone at all -- a temporal
        # defect that only appears off UTC is invisible to every other tier, and the live
        # stack runs UTC. Defaults to UTC when unspecified to guarantee deterministic
        # execution matching the live stack and protecting against host-environment
        # timezone drift (e.g. macOS US/Pacific rejected by PostgreSQL 16).
        tz = target.timezone if (target is not None and getattr(target, "timezone", None)) else "UTC"
        zone = [f"-Duser.timezone={tz}"]
        argv = ([java_bin] + zone
                + ["-cp", classpath, "com.striim.testing.inttest.IntegrationProcessor",
                   str(request_file)])
        try:
            if post_start or mid_run:
                r = _run_with_post_start_seed(
                    argv, cwd=cwd, timeout=timeout, tempdir=tempdir,
                    on_source_started=on_source_started, op_jar=op_jar,
                    on_mid_run=(on_mid_run if mid_run else None))
            else:
                r = subprocess.run(argv, cwd=(str(cwd) if cwd is not None else None),
                                    capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise HarnessError(
                f"IntegrationProcessor timed out after {timeout}s driving {op_jar}\n"
                f"stdout:\n{e.stdout or ''}\nstderr:\n{e.stderr or ''}") from e

        if r.returncode != 0:
            raise HarnessError(
                f"IntegrationProcessor exited {r.returncode} driving {op_jar}\n"
                f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}")

        if not output_file.is_file():
            raise HarnessError(
                f"IntegrationProcessor exited 0 but wrote no outputFile ({output_file})\n"
                f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}")

        # ⚠ THE SUCCESS PATH USED TO DISCARD THIS. A failing drive puts both streams into the
        # HarnessError, so `expect_error:` can read them; a SUCCEEDING one returned the report and
        # dropped everything the operator said. That is why three capabilities had no case: their
        # only observable behaviour is a WARNING on a run that otherwise succeeds -- and a
        # `target:` assertion reads the database, which a warning never touches.
        #
        # A sink rather than a second return value: every existing caller keeps its signature and
        # its return type, and one that does not care cannot accidentally depend on this.
        if log_sink is not None:
            log_sink.append((r.stdout or "") + (r.stderr or ""))

        if jmx is not None and jmx_sink is not None:
            jmx_sink.append(json.loads(jmx_file.read_text()) if jmx_file.is_file() else {
                "error": f"IntegrationProcessor exited 0 but wrote no JMX snapshot ({jmx_file})"})

        return json.loads(output_file.read_text())
    finally:
        shutil.rmtree(tempdir, ignore_errors=True)
