"""Performance-mode runner (PERF_SPEC.md). Holds both the pure-logic pieces
(compute_sample_indices, aggregation) and the I/O orchestration half:
per-iteration DB reset, operator subprocess launch + psutil sampling,
metric attribution, and correctness dispatch for ONE measured iteration.

This module deliberately stops at "run one measured iteration, return a structured
result" -- looping across `run_sizes x measured_runs`, provisioning services,
building the op jar, and all pytest wiring (collection, selection, --perf) is
inttest/plugin.py. Nothing in this module imports pytest or knows what
a pytest Item is.

Circularity note: `reset_environment`/`_probe_once`/`run_measured_iteration` import
`inttest.plugin` LAZILY (inside the function body, not at module load time) because
plugin.py's `PerfYamlItem` will import THIS module -- a top-level import
here would be circular.
"""
from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import dbroutes as dbroutes_mod
from . import manifest as manifest_mod
from . import pgclient as pgclient_mod
from . import tokens as tokens_mod
from . import waevent as waevent_mod

# §8: "stable" band for a run-size-to-run-size median-throughput ratio.

# §7: sampling interval and priming-sample convention.
_SAMPLE_INTERVAL_SECONDS = 0.1
_MIN_SAMPLES_FOR_METRICS = 3

# §10: reset+probe retry delay, and how many attempts total (initial + 1 retry).
_RESET_RETRY_DELAY_SECONDS = 5
_RESET_ATTEMPTS = 2


def compute_sample_indices(n: int) -> list[int]:
    """The deterministic sample index set `S` for 'sampled' correctness mode
    (PERF_SPEC.md §6), computed once from `n = len(expected)`:

        if n <= 1000: S = {0, 1, ..., n-1}                      # everything
        else:
            head   = {0, ..., 99}                                # first 100
            tail   = {n-100, ..., n-1}                            # last 100
            m      = min(800, n - 200)                            # middle budget
            middle = {100 + floor(j * (n - 200) / m) for j in 0..m-1}
            S      = head | middle | tail

    Returns the indices sorted ascending. `len(result) == min(n, 200 + m) <= 1000`.
    """
    if n <= 1000:
        return list(range(n))
    head = set(range(100))
    tail = set(range(n - 100, n))
    m = min(800, n - 200)
    middle = {100 + (j * (n - 200)) // m for j in range(m)}
    return sorted(head | middle | tail)


@dataclass(frozen=True)
class MetricAggregate:
    """min/max/mean/median/sample-stdev for one metric over a run_size's successful
    measured iterations (PERF_SPEC.md §8). `stdev` is `None` when fewer than two
    values are present (sample stdev, ddof=1, is undefined for n<2)."""
    min: float | None
    max: float | None
    mean: float | None
    median: float | None
    stdev: float | None


_EMPTY_AGGREGATE = MetricAggregate(min=None, max=None, mean=None, median=None, stdev=None)


def aggregate_metric(values: list) -> MetricAggregate:
    """Aggregates one metric's values across a run_size's SUCCESSFUL measured
    iterations only -- callers exclude failed iterations before calling this
    (PERF_SPEC.md §8: "failed iterations are excluded from the aggregates but remain
    in the report with status failed"). An empty `values` list (every iteration for
    this run_size failed) returns every field as `None`."""
    if not values:
        return _EMPTY_AGGREGATE
    return MetricAggregate(
        min=min(values),
        max=max(values),
        mean=statistics.mean(values),
        median=statistics.median(values),
        stdev=statistics.stdev(values) if len(values) >= 2 else None,
    )


@dataclass(frozen=True)
class RunSizeAggregate:
    """The §8 aggregate pair for one run_size: duration and input throughput,
    each a `MetricAggregate` over that run_size's successful measured iterations."""
    duration_seconds: MetricAggregate
    input_throughput: MetricAggregate


def aggregate_run_size(durations_seconds: list, input_throughputs: list) -> RunSizeAggregate:
    """`durations_seconds`/`input_throughputs` are parallel lists (same length, same
    iteration order) over one run_size's SUCCESSFUL measured iterations only."""
    if len(durations_seconds) != len(input_throughputs):
        raise ValueError(
            f"durations_seconds and input_throughputs must be parallel lists of the same "
            f"length, got {len(durations_seconds)} and {len(input_throughputs)}")
    return RunSizeAggregate(
        duration_seconds=aggregate_metric(durations_seconds),
        input_throughput=aggregate_metric(input_throughputs),
    )


# ============================================================================
# Pre-flight (PERF_SPEC.md §3 stage 2)
# ============================================================================


class PerfPreflightError(Exception):
    """Raised when performance pre-flight validation fails (PERF_SPEC.md §3 stage 2)."""


@dataclass(frozen=True)
class PreflightResult:
    events_in_file: int
    input_events: list  # parsed (and, when `tokens` is given, ${...}-rendered) -- what the operator actually replays
    expected_events: list | None
    sample_indices: list | None  # precomputed once from len(expected); only for 'sampled' mode


def _load_rendered(path: Path, tokens: dict | None) -> list:
    text = path.read_text()
    if tokens is not None:
        text = tokens_mod.render(text, tokens)
    return waevent_mod.load(text)


def preflight(performance, tokens: dict | None = None, source=None) -> PreflightResult:
    """Validates `performance.input`/`performance.expected` existence, readability,
    and non-empty parse, plus `psutil` availability -- `psutil` is a required
    dependency of performance mode outright (PERF_SPEC.md §14), not just when
    `capture_cpu`/`capture_memory` are enabled: the process-tree kill/survivor check
    (§10) needs it unconditionally too. Raises `PerfPreflightError` naming what's
    wrong; never silently reports zeros or disables anything.

    `tokens`, when given, is rendered through `${...}` substitution before parsing --
    same as `IntYamlItem.runtest()` already does for `assert.data[].input`/`match` --
    so a performance fixture can reference e.g. `${TID}`/`${POSTGRES_SOURCE_SCHEMA}`
    exactly like a regression one, rather than silently feeding the literal token text
    to the operator. `input_events` on the result is what `run_measured_iteration`
    actually hands the Java driver (via a freshly-written temp file), not
    `performance.input_path` directly -- consistent with how `harness.drive()` already
    receives pre-rendered, pre-parsed events rather than a raw path."""
    try:
        import psutil  # noqa: F401
    except ImportError as e:
        raise PerfPreflightError(
            "psutil is required for performance mode but is not installed "
            "(PERF_SPEC.md §14)") from e

    if performance.input_path is None:
        # A `source:` (reader) case: its replay is `source.max_ticks` ticks, so there is no input
        # fixture to find, render, or parse. The loader has already rejected one being declared.
        input_events = []
        if source is None:
            raise PerfPreflightError(
                "performance.input is absent but no 'source:' block was supplied -- a case with "
                "neither has nothing to replay")
    else:
        if not performance.input_path.is_file():
            raise PerfPreflightError(f"performance.input does not exist or is not a file: {performance.input_path}")
        try:
            input_events = _load_rendered(performance.input_path, tokens)
        except Exception as e:  # noqa: BLE001 - any parse/render failure is a pre-flight failure
            raise PerfPreflightError(f"performance.input at {performance.input_path} failed to parse: {e}") from e
        if not input_events:
            raise PerfPreflightError(f"performance.input is empty: {performance.input_path}")

    expected_events = None
    sample_indices = None
    if performance.correctness.mode != "disabled":
        if performance.expected_path is None or not performance.expected_path.is_file():
            raise PerfPreflightError(f"performance.expected does not exist or is not a file: {performance.expected_path}")
        try:
            expected_events = _load_rendered(performance.expected_path, tokens)
        except Exception as e:  # noqa: BLE001
            raise PerfPreflightError(f"performance.expected at {performance.expected_path} failed to parse: {e}") from e
        if not expected_events:
            raise PerfPreflightError(f"performance.expected is empty: {performance.expected_path}")
        if performance.correctness.mode == "sampled":
            sample_indices = compute_sample_indices(len(expected_events))

    return PreflightResult(
        # For a reader the replay unit is a TICK, not a fixture record, so this carries
        # `max_ticks`. It is what `check_correctness` multiplies by `run_size` to verify
        # `measuredInputEvents`, so getting it from `len(input_events)` (zero, for a reader)
        # would fail every reader iteration with "expected 0, got N".
        events_in_file=source.max_ticks if source is not None else len(input_events),
        input_events=input_events,
        expected_events=expected_events,
        sample_indices=sample_indices,
    )


# ============================================================================
# Per-iteration environment reset + liveness probe (PERF_SPEC.md §9, §10)
# ============================================================================


class UnrecreatableEnvironmentError(Exception):
    """Raised when the environment could not be made recreatable (PERF_SPEC.md §10):
    reset+probe failed twice, or the operator process tree survived termination.
    `.failure_class` names which unrecreatable-environment class fired, for the
    caller to report; remaining work for the test is `not_run`, not just this
    iteration `failed`.

    `.partial_iteration_result`, when not `None`, is a `failed` `IterationResult`
    for the iteration that was IN PROGRESS when this was raised (only the
    surviving-process case can produce one -- a reset/probe failure happens
    before any iteration starts). Per §10, "partial results are always written
    and reported": the caller must still record this iteration, not just the
    ones that came before it and the `not_run` ones that come after."""

    def __init__(self, message: str, failure_class: str, partial_iteration_result=None):
        super().__init__(message)
        self.failure_class = failure_class
        self.partial_iteration_result = partial_iteration_result


def _reset_once(manifest, tokens: dict, gcs_prune_prefix: str | None = None) -> dict:
    """One reset attempt (no retry): reset Postgres's fixed qasource/qatarget
    schemas (whole-schema in serial, ${TID}-prefixed objects only in parallel --
    docs/internals/INTEGRATION-ENGINE.md#token-isolation), drop Oracle prefix tables, ensure Spanner
    databases exist and the gcs bucket exists (whichever the manifest's
    `requires:` names), then run `ddl:` then `seed:`. Returns an updated tokens
    dict -- a defensive copy; Postgres no longer needs a schema-token override
    (POSTGRES_SOURCE_SCHEMA/TARGET_SCHEMA stay at their fixed service.yaml
    defaults)."""
    from . import plugin as plugin_mod  # lazy -- see module docstring

    tokens = dict(tokens)
    if "postgres" in manifest.requires:
        pg = pgclient_mod.PgAdmin(pgclient_mod.dsn_from_tokens(tokens), role="source")
        pg.ensure_setup()
        if tokens.get("TID"):
            pg.reset_test_objects(tokens["TID"])
        else:
            pg.reset_schemas()
    if "oracle" in manifest.requires:
        # ${TID_ORACLE}, NOT ${TID_ORACLE_DB} (removed -- see tokens.isolation_tokens):
        # empty in a serial run, same as IntYamlItem's _oracle_prefix, so skip the
        # call entirely rather than passing _drop_oracle_test_prefix an empty prefix
        # it would reject.
        if tokens.get("TID_ORACLE"):
            plugin_mod._drop_oracle_test_prefix(tokens["TID_ORACLE"])
    if "spanner" in manifest.requires:
        plugin_mod._ensure_spanner_databases(tokens)
    if "gcs" in manifest.requires:
        # `isolation: none` like Spanner -- one fixed bucket, no per-test reset
        # (services/gcs/README.md's "Bucket lifecycle"). A real upload needs the
        # bucket to exist first and fake-gcs-server does not auto-create one on
        # first PUT, so this must run even though gcs cases never declare ddl:/seed:.
        plugin_mod._ensure_gcs_bucket(tokens)
        # The bucket is shared and never reset, so objects a writer case put there in the
        # previous iteration would still be there: a case that declares its folder has it
        # emptied here, rendered with this iteration's tokens (${TID} is empty in a serial run,
        # which is why the prefix is declared rather than derived).
        if gcs_prune_prefix:
            from . import gcsadmin as gcsadmin_mod
            gcsadmin_mod.delete_prefix(tokens, tokens_mod.render(gcs_prune_prefix, tokens))

    for spec in manifest.ddl:
        dbroutes_mod.run_sql_file(spec.db, spec.path, tokens)
    for spec in manifest.seed:
        dbroutes_mod.run_sql_file(spec.db, spec.path, tokens)

    return tokens


def _probe_once(manifest, tokens: dict, tmp_dir: Path) -> None:
    """Liveness probe (§10): each declared service accepts a connection and
    Postgres's qasource/qatarget schemas are reachable, and the temp dir is
    writable. Any failure (a raised exception of any kind) means the probe
    failed -- the caller treats reset and probe identically for retry purposes.

    Deliberately NOT an "and is empty" check: `reset_environment` calls
    `_reset_once` (reset THEN run ddl:/seed:) BEFORE `_probe_once`, so by the
    time this runs, ddl:/seed: have already (deliberately) created and
    populated this iteration's tables -- confirmed by test: an earlier version
    of this probe checked for zero tables here and failed every real Postgres
    perf iteration, since the table it was complaining about was the one ddl:
    had just created on purpose. What's worth confirming is reachability
    (schema exists under the fixed qasource/qatarget names this test's
    ConnectionUrl actually points at), the same thing the pre-migration probe
    checked, just translated from a per-test schema name to the fixed ones."""
    from . import plugin as plugin_mod  # lazy -- see module docstring

    if "postgres" in manifest.requires:
        pg = pgclient_mod.PgAdmin(pgclient_mod.dsn_from_tokens(tokens), role="source")
        for which in ("source", "target"):
            schema = tokens[f"POSTGRES_{which.upper()}_SCHEMA"]
            _, rows = pg._query(  # noqa: SLF001 - perf.py is harness-internal, same convention as tests/
                "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
                params=(schema,), which="admin")
            if not rows:
                raise RuntimeError(f"Postgres schema {schema!r} does not exist after reset")

    if "oracle" in manifest.requires:
        import oracledb

        dsn = f'{tokens["ORACLE_HOST"]}:{int(tokens["ORACLE_PORT"])}/{tokens["ORACLE_SERVICE"]}'
        conn = oracledb.connect(user=tokens["ORACLE_SOURCE_USER"], password=tokens["ORACLE_SOURCE_PASSWORD"], dsn=dsn)
        conn.close()

    if "spanner" in manifest.requires:
        plugin_mod._ensure_spanner_databases(tokens)  # cheap idempotent RPC; doubles as a reachability probe

    if "gcs" in manifest.requires:
        plugin_mod._ensure_gcs_bucket(tokens)  # cheap idempotent HEAD; doubles as a reachability probe

    probe_file = tmp_dir / ".perf-probe"
    probe_file.write_text("ok")
    probe_file.unlink()


def reset_environment(manifest, tokens: dict, tmp_dir: Path, gcs_prune_prefix: str | None = None) -> tuple:
    """Full per-iteration reset (§9) + liveness probe (§10), with one 5s-delayed
    retry if either raises. Returns `(updated_tokens, reset_duration_seconds)` on
    success (from whichever attempt succeeded). Raises `UnrecreatableEnvironmentError`
    if both attempts fail -- the caller stops the whole test, not just this iteration.

    A test with no `requires:` and no `ddl:`/`seed:` does no DB work at all (§9) and
    this call is correspondingly cheap: just the temp-dir-writable check."""
    last_error = None
    for attempt in range(_RESET_ATTEMPTS):
        start = time.monotonic()
        try:
            updated_tokens = _reset_once(manifest, tokens, gcs_prune_prefix)
            _probe_once(manifest, updated_tokens, tmp_dir)
            return updated_tokens, time.monotonic() - start
        except Exception as e:  # noqa: BLE001 - any reset/probe failure triggers the retry
            last_error = e
            if attempt < _RESET_ATTEMPTS - 1:
                time.sleep(_RESET_RETRY_DELAY_SECONDS)
    raise UnrecreatableEnvironmentError(
        f"environment reset/probe failed {_RESET_ATTEMPTS} times: {last_error}", "reset-or-probe-failed") from last_error


# ============================================================================
# Operator subprocess launch + psutil sampling (PERF_SPEC.md §5, §7)
# ============================================================================


@dataclass(frozen=True)
class Sample:
    """One process-tree CPU/RSS sample (PERF_SPEC.md §7)."""
    window_start_epoch_millis: int
    window_end_epoch_millis: int
    cpu_percent: float  # summed across the tree, percent of ONE core (PERF_SPEC.md §7 CPU normalization)
    rss_bytes: int      # summed across the tree


def _sample_process_tree(pid: int, samples: list, stop: threading.Event,
                          interval_seconds: float = _SAMPLE_INTERVAL_SECONDS) -> None:
    """Runs in a background thread: samples `psutil.Process(pid)` + all descendants
    every `interval_seconds` until `stop` is set or the root process is gone.
    Appends `Sample`s to the shared `samples` list (plain `list.append` is safe under
    the GIL for a single producer thread).

    A `Sample`'s window is `[end of the PREVIOUS iteration's readings, end of THIS
    iteration's readings]` -- NOT `[start of this iteration, end of this iteration]`.
    This matters: `cpu_percent(interval=None)` returns the CPU delta since the
    PREVIOUS call on that same Process object, i.e. it measures exactly the previous
    interval, not the few milliseconds this iteration's own read-loop takes. Stamping
    the reading with "now" as its window start would make `attribute_samples`'
    entirely-inside-the-measured-window filter (§7) systematically admit readings
    that are mostly pre-window activity near the boundary, and drop the reading that
    actually covers the window's true tail. The FIRST in-tree reading is a priming
    read (psutil's own `cpu_percent()` convention) and is never turned into a Sample.

    Process objects are cached by pid across iterations (not recreated every loop) so
    `cpu_percent(interval=None)` returns a meaningful delta for every process, not
    just the root -- a freshly-discovered child is itself primed on the loop
    iteration it first appears, same convention as the tree as a whole."""
    import psutil

    try:
        root = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return

    tracked = {pid: root}
    primed = False
    prev_window_end = None
    while not stop.is_set():
        try:
            if not root.is_running():
                break
            current_pids = {root.pid} | {c.pid for c in root.children(recursive=True)}
        except psutil.NoSuchProcess:
            break

        for new_pid in current_pids - tracked.keys():
            try:
                tracked[new_pid] = psutil.Process(new_pid)
            except psutil.NoSuchProcess:
                pass
        for stale_pid in list(tracked.keys() - current_pids - {root.pid}):
            tracked.pop(stale_pid, None)

        cpu_total = 0.0
        rss_total = 0
        for proc_obj in list(tracked.values()):
            try:
                cpu_total += proc_obj.cpu_percent(interval=None)
                rss_total += proc_obj.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        window_end = int(time.time() * 1000)

        if primed:
            samples.append(Sample(prev_window_end, window_end, cpu_total, rss_total))
        primed = True
        prev_window_end = window_end
        stop.wait(interval_seconds)


def _kill_process_tree(pid: int) -> bool:
    """Terminates, then (after a grace period) kills, `pid` and every descendant
    (PERF_SPEC.md §10's timeout-expiry handling). Returns True if anything is still
    alive after the kill + grace period -- PERF_SPEC.md §10's "surviving process"
    unrecreatable-environment class. Best-effort: a process that's already gone is
    not an error.

    Survivor detection is checked against Process objects captured in a snapshot
    BEFORE any signal is sent, never via a fresh `psutil.Process(pid)` lookup
    afterward. Once a process is reaped, a fresh lookup by pid raises `NoSuchProcess`
    immediately, and `root.children()` can no longer be walked at all -- so a
    genuinely surviving descendant (re-parented once its immediate parent died) would
    be invisible to a post-mortem query. A `psutil.Process` handle obtained while its
    target was alive stays safely queryable (`is_running()`) after the target exits,
    so the pre-kill snapshot is the only reliable way to check this.

    `pid` itself (the caller's own `subprocess.Popen` child) is signaled here
    (`terminate`/`kill` don't consume its exit status) but its death is polled via
    `is_running()`/`status()`, NEVER reaped via `psutil.Process.wait()`: for a
    process's own child, psutil's `wait()` reaps it the same way
    `subprocess.Popen.wait()`/`communicate()` would (both end up calling
    `os.waitpid()`), and only one caller can ever win that race. The caller
    (`launch_and_measure`) is responsible for reaping `pid` itself, via its own
    `communicate()`/`wait()` -- losing that race silently fabricates
    `returncode == 0` on the `subprocess.Popen` side (a known stdlib corner case)."""
    import psutil

    try:
        root = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return False

    def _descendants() -> dict:
        try:
            return {p.pid: p for p in root.children(recursive=True)}
        except psutil.NoSuchProcess:
            return {}

    descendants = _descendants()
    for p in list(descendants.values()) + [root]:
        try:
            p.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    # Wait on DESCENDANTS only -- waiting on `root` would race the caller's own reap
    # of its direct child (see docstring).
    if descendants:
        psutil.wait_procs(list(descendants.values()), timeout=5)

    # A terminated descendant may have forked its own child during the grace period.
    for new_pid, p in _descendants().items():
        descendants.setdefault(new_pid, p)
    alive_descendants = [p for p in descendants.values() if p.is_running()]
    for p in alive_descendants:
        try:
            p.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    if alive_descendants:
        psutil.wait_procs(alive_descendants, timeout=5)

    # Poll (never wait()) for root's own death, escalating to SIGKILL if it's still
    # around after the grace period.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            if root.status() == psutil.STATUS_ZOMBIE or not root.is_running():
                break
        except psutil.NoSuchProcess:
            break
        time.sleep(0.05)
    else:
        try:
            root.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        time.sleep(0.2)

    try:
        root_survives = root.is_running() and root.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        root_survives = False

    return root_survives or any(p.is_running() for p in descendants.values())


def build_perf_request(*, op_jar: Path, properties: dict, input_file: Path | None, result_file: Path,
                        types: dict | None, namespace: str | None, source_name: str | None,
                        password_properties: list | None, run_size: int, warmup_runs: int,
                        correctness_mode: str, sample_indices: list | None, udf=None,
                        source=None, target=None) -> dict:
    """Builds the on-disk `PerformanceProcessor.PerfRequest` JSON contract (Java
    side: `com.striim.testing.inttest.PerformanceProcessor`, PERF_SPEC.md §4). `udf`,
    when given (a `manifest.UdfSpec`), drives `op_jar` as a bare UDF pipeline instead
    of as an OpenProcessor `Processor` -- `None` (the default) is the existing OP
    path, unchanged. `source`, when given (a `manifest.SourceSpec`), replays `op_jar`
    as a READER: one replay is `source.max_ticks` ticks rather than one pass over
    `input_file`, which a reader has none of (`inputFile` is then `None`)."""
    return {
        "opJar": str(op_jar),
        "properties": {k: str(v) for k, v in (properties or {}).items()},
        "inputFile": None if input_file is None else str(input_file),
        "resultFile": str(result_file),
        "types": types,
        "namespace": namespace,
        "sourceName": source_name,
        "passwordProperties": password_properties,
        "runSize": run_size,
        "warmupRuns": warmup_runs,
        "correctnessMode": correctness_mode,
        "sampleIndices": sample_indices,
        "udf": manifest_mod.udf_to_wire(udf),
        "source": manifest_mod.source_to_wire(source),
        "target": manifest_mod.target_to_wire(target),
    }


def jvm_args(jvm) -> list[str]:
    """The effective JVM command-line options for `jvm:` (PERF_SPEC.md §3), in
    command-line order: -Xms, -Xmx, then jvm.args verbatim. Extracted so the
    reporting layer (perfreport.py) can record the exact args the JVM received,
    rather than reconstructing them a second time and risking drift."""
    args = []
    if jvm.min_heap:
        args.append(f"-Xms{jvm.min_heap}")
    if jvm.max_heap:
        args.append(f"-Xmx{jvm.max_heap}")
    args.extend(jvm.args)
    return args


def launch_and_measure(*, java_bin: str, harness_jar: Path, op_jar: Path, request: dict,
                        request_file: Path, result_file: Path, cwd: Path, jvm, timeout: int,
                        sample: bool = True) -> dict:
    """Launches `java <jvm args> -cp harness_jar:op_jar
    com.striim.testing.inttest.PerformanceProcessor request_file` (PERF_SPEC.md §14
    "Operator subprocess"), `cwd` = `manifest.dir` (the perf `test.yaml`'s own
    directory -- a perf test.yaml is fully self-contained, PERF_SPEC.md §3), so
    relative config-file references resolve, matching integration mode. Waits up
    to `timeout` seconds; on expiry, kills the whole process tree and confirms no
    descendant survives.

    `sample` gates whether the background psutil sampling thread runs at all -- when
    neither `capture_cpu` nor `capture_memory` is enabled, PERF_SPEC.md's own metrics
    section says the point of disabling them is to avoid the sampler perturbing the
    measurement; starting the thread anyway and merely discarding its output would
    defeat that. `samples` is `[]` when `sample=False`.

    Returns a plain dict (not a dataclass -- this is an internal handoff to
    `run_measured_iteration`, not a public result shape):
    `{"result": <parsed PerfResult dict, or None>, "samples": list[Sample],
    "returncode": int, "timed_out": bool, "survivor": bool, "stdout": str,
    "stderr": str}`.
    """
    request_file.write_text(json.dumps(request))

    # Same extra drivers the regression tier adds -- see harness._extra_jars. The perf tier
    # launches its OWN processor, so a driver added only there never reaches this one: the
    # Teradata variant failed here with "No suitable driver found" after the regression variant
    # already passed.
    from inttest.harness import _extra_jars
    classpath = os.pathsep.join([str(harness_jar), str(op_jar), *_extra_jars()])
    argv = [java_bin, *jvm_args(jvm), "-cp", classpath, "com.striim.testing.inttest.PerformanceProcessor", str(request_file)]

    proc = subprocess.Popen(argv, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    samples: list = []
    stop = threading.Event()
    sampler = None
    if sample:
        sampler = threading.Thread(target=_sample_process_tree, args=(proc.pid, samples, stop), daemon=True)
        sampler.start()

    timed_out = False
    survivor = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        # _kill_process_tree signals + polls (never reaps) `proc.pid` -- WE reap it,
        # below, via our own communicate(). Its return value is the definitive §10
        # survivor verdict; the two bounded communicate() retries after it are just
        # about getting stdout/stderr without blocking forever if some descendant
        # still holds the inherited pipes open (which IS the survivor case).
        survivor = _kill_process_tree(proc.pid)
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""
            survivor = True
    finally:
        stop.set()
        if sampler is not None:
            sampler.join(timeout=5)

    result = None
    if result_file.is_file():
        try:
            result = json.loads(result_file.read_text())
        except (OSError, ValueError):
            result = None

    return {
        "result": result,
        "samples": samples,
        "returncode": proc.returncode,
        "timed_out": timed_out,
        "survivor": survivor,
        "stdout": stdout,
        "stderr": stderr,
    }


# ============================================================================
# Metric attribution (PERF_SPEC.md §7)
# ============================================================================


@dataclass(frozen=True)
class AttributedMetrics:
    cpu_percent_avg: float | None
    cpu_percent_peak: float | None
    cpu_percent_avg_normalized: float | None
    cpu_percent_peak_normalized: float | None
    cpu_count: int
    memory_rss_peak_bytes: int | None
    sample_count: int
    insufficient_samples: bool


def attribute_samples(samples: list, measured_start_epoch_millis: int, measured_end_epoch_millis: int,
                       capture_cpu: bool = True, capture_memory: bool = True) -> AttributedMetrics:
    """Filters `samples` to those whose window falls ENTIRELY inside
    `[measured_start_epoch_millis, measured_end_epoch_millis]` (PERF_SPEC.md §7) and
    computes avg/peak CPU (raw + normalized by host core count) and peak RSS.

    `capture_cpu`/`capture_memory` independently null out just their own metric
    family (PERF_SPEC.md §3 documents them as two separate toggles, "Enable CPU
    sampling" / "Enable resident-memory sampling" -- neither implies the other).
    Both families are still sampled together regardless (the same background
    thread/loop collects both in one pass, `run_measured_iteration` only runs the
    sampler at all when at least one is enabled), so disabling one doesn't skip
    computing `sample_count`/`insufficient_samples`/`cpu_count` from the samples
    that WERE collected.

    Fewer than 3 in-window samples -> every metric field `None` and
    `insufficient_samples=True` (duration/throughput, computed elsewhere from the
    Java result directly, are unaffected -- only these sample-derived fields are
    unreliable at that window size)."""
    import psutil

    in_window = [
        s for s in samples
        if s.window_start_epoch_millis >= measured_start_epoch_millis
        and s.window_end_epoch_millis <= measured_end_epoch_millis
    ]
    cpu_count = psutil.cpu_count() or 1

    if len(in_window) < _MIN_SAMPLES_FOR_METRICS:
        return AttributedMetrics(
            cpu_percent_avg=None, cpu_percent_peak=None,
            cpu_percent_avg_normalized=None, cpu_percent_peak_normalized=None,
            cpu_count=cpu_count, memory_rss_peak_bytes=None,
            sample_count=len(in_window), insufficient_samples=True,
        )

    cpu_values = [s.cpu_percent for s in in_window]
    rss_values = [s.rss_bytes for s in in_window]
    avg_cpu = sum(cpu_values) / len(cpu_values) if capture_cpu else None
    peak_cpu = max(cpu_values) if capture_cpu else None
    return AttributedMetrics(
        cpu_percent_avg=avg_cpu,
        cpu_percent_peak=peak_cpu,
        cpu_percent_avg_normalized=(avg_cpu / cpu_count) if capture_cpu else None,
        cpu_percent_peak_normalized=(peak_cpu / cpu_count) if capture_cpu else None,
        cpu_count=cpu_count,
        memory_rss_peak_bytes=max(rss_values) if capture_memory else None,
        sample_count=len(in_window),
        insufficient_samples=False,
    )


# ============================================================================
# Correctness dispatch (PERF_SPEC.md §6)
# ============================================================================


@dataclass(frozen=True)
class CorrectnessOutcome:
    passed: bool
    reason: str | None
    mode: str | None = None
    records_compared: int | None = None


def check_correctness(perf_result: dict, events_in_file: int, run_size: int, correctness_mode: str,
                       sample_indices: list | None, expected_events: list | None,
                       returncode: int = 0, stderr: str = "") -> CorrectnessOutcome:
    """The §6 mandatory checks (every mode, every measured iteration) plus the
    mode-specific record comparison. `perf_result` is the Java driver's parsed
    `PerfResult` JSON for this iteration. `returncode` is the operator subprocess's
    exit code -- §6 mandatory checks include "no ... nonzero subprocess exit", so a
    JVM that writes a clean result and then dies nonzero during shutdown (a
    shutdown-hook throw, a non-daemon-thread crash) must still fail the iteration,
    not pass silently just because the result file happened to parse.

    `mode`/`records_compared` on the returned outcome are for reporting:
    `records_compared` is `len(sample_indices)` for 'sampled', `len(expected_events)`
    for 'full', `0` for 'disabled', and `None` when the run failed before a
    comparison was attempted."""
    if returncode != 0:
        # ⚠ WITH THE SUBPROCESS'S OWN LAST WORDS. This used to report the exit code ALONE, so a
        # JVM that failed to start, threw in init, or could not reach the database all looked
        # identical -- "exited 1" -- and the operator's actual stack trace was discarded. Found
        # while adding target: support (T3), where every early failure surfaced that way.
        lines = (stderr or "").strip().splitlines()
        # ⚠ The HEAD, not the tail. A Java stack trace carries its exception type and message on
        # the FIRST line and frames after it, so a tail slice shows twelve `at ...` lines and hides
        # the one sentence that says what went wrong. Learned by printing the tail first.
        detail = "\n    ".join(lines[:15]) if lines else "(the subprocess wrote nothing to stderr)"
        return CorrectnessOutcome(
            False, f"operator subprocess exited {returncode}:\n    {detail}",
            mode=correctness_mode)

    if perf_result.get("errorMessage"):
        return CorrectnessOutcome(False, f"operator error: {perf_result['errorMessage']}", mode=correctness_mode)

    expected_measured_input = events_in_file * run_size
    if perf_result.get("measuredInputEvents") != expected_measured_input:
        return CorrectnessOutcome(
            False,
            f"measuredInputEvents mismatch: expected {expected_measured_input}, "
            f"got {perf_result.get('measuredInputEvents')}", mode=correctness_mode)

    # A MISSING field fails loudly rather than defaulting to "stable" -- this is a
    # mandatory §4 result field; treating its absence as success would silently
    # disable the emission-count-stability check on any result-schema drift.
    stable = perf_result.get("perReplayOutputEventsStable")
    if stable is None:
        return CorrectnessOutcome(
            False, "result is missing required field 'perReplayOutputEventsStable'", mode=correctness_mode)
    if not stable:
        idx = perf_result.get("firstUnstableReplayIndex")
        return CorrectnessOutcome(
            False, f"emission-count instability first detected at replay index {idx}", mode=correctness_mode)

    if correctness_mode == "disabled":
        return CorrectnessOutcome(True, None, mode=correctness_mode, records_compared=0)

    compared_records = perf_result.get("comparedRecords") or []
    compared_count = perf_result.get("comparedReplayOutputEvents", 0)

    if expected_events is not None and compared_count != len(expected_events):
        return CorrectnessOutcome(
            False, f"length mismatch: expected {len(expected_events)}, got {compared_count}", mode=correctness_mode)

    if correctness_mode == "full":
        try:
            waevent_mod.compare(compared_records, expected_events)
        except waevent_mod.WAEventMismatch as e:
            return CorrectnessOutcome(False, str(e), mode=correctness_mode)
        return CorrectnessOutcome(True, None, mode=correctness_mode, records_compared=len(expected_events))

    if correctness_mode == "sampled":
        # The driver is handed `sampleIndices` as an INPUT (PERF_SPEC.md §4) and is
        # expected to retain exactly that many records, in the same order -- a
        # length mismatch here (a driver-side sampling bug/truncation) must fail
        # loudly rather than `zip` silently comparing only the shorter prefix and
        # the report claiming more records were checked than actually were.
        expected_sample_count = len(sample_indices or [])
        if len(compared_records) != expected_sample_count:
            return CorrectnessOutcome(
                False,
                f"sampled comparedRecords length mismatch: expected {expected_sample_count} "
                f"(len(sample_indices)), got {len(compared_records)}", mode=correctness_mode)
        pairs = list(zip(sample_indices or [], compared_records))
        try:
            waevent_mod.compare_indexed(pairs, expected_events)
        except waevent_mod.WAEventMismatch as e:
            return CorrectnessOutcome(False, str(e), mode=correctness_mode)
        return CorrectnessOutcome(True, None, mode=correctness_mode, records_compared=len(pairs))

    return CorrectnessOutcome(False, f"unknown correctness mode: {correctness_mode!r}", mode=correctness_mode)


# ============================================================================
# Top-level orchestrator: one measured iteration end to end
# ============================================================================


@dataclass(frozen=True)
class IterationResult:
    status: str  # "success" | "failed"
    run_size_authored: str
    run_size_value: int
    duration_seconds: float | None
    input_throughput: float | None
    output_throughput: float | None
    measured_input_events: int | None
    measured_output_events: int | None
    warmup_input_events: int | None
    warmup_output_events: int | None
    metrics: AttributedMetrics | None
    reset_duration_seconds: float | None
    failure_reason: str | None
    raw_result: dict | None
    correctness: CorrectnessOutcome | None = None


def run_measured_iteration(
    *,
    manifest,                     # TestManifest (inttest.perfmanifest.load_perf_manifest result's .test_manifest)
    performance,                  # PerformanceSpec (inttest.perfmanifest)
    preflight_result: PreflightResult,
    run_size,                     # RunSize (inttest.perfmanifest) -- .authored, .value
    tokens: dict,
    java_bin: str,
    harness_jar: Path,
    op_jar: Path,
    scratch_dir: Path,
    literal_property_keys=None,   # `matrix:` permutation keys -- see the render call below
) -> IterationResult:
    """Runs ONE measured iteration end to end (PERF_SPEC.md §5 steps 1-7): reset the
    environment, launch a fresh operator subprocess, sample it, validate
    correctness, and return a structured result.

    Raises `UnrecreatableEnvironmentError` ONLY for environment-level failures (§10)
    that should stop the whole test (remaining work `not_run`), not just fail this
    iteration: reset/probe failing twice, or a surviving process after a timeout
    kill. Every other failure (operator exception, correctness mismatch, a timeout
    whose kill succeeded cleanly) comes back as an `IterationResult` with
    `status="failed"` -- the caller resets and continues to the next iteration."""
    updated_tokens, reset_duration = reset_environment(
        manifest, tokens, scratch_dir, gcs_prune_prefix=performance.gcs_prune_prefix)

    # ⚠ `literal_property_keys` are the `matrix:` permutation's own values, and they are NOT
    # rendered -- the regression tier applies its overrides AFTER rendering
    # (`props.update(overrides)`, plugin.py), so they stay literal there. Rendering them here
    # would make a `matrix:` value mean two different things in the two tiers, and one holding
    # a `${NAME}` that is not a token would raise SubstitutionError straight out of this
    # function -- which the caller only catches for UnrecreatableEnvironmentError, so it would
    # escape runtest() as an error rather than a reported failure.
    literal = literal_property_keys or frozenset()
    rendered_properties = {
        k: (v if k in literal else tokens_mod.render(v, updated_tokens))
        for k, v in manifest.properties.items()
    }

    # PERF_SPEC.md §9: the per-test temp directory (request/result/scratch files) is
    # reset before every measured iteration -- a fresh, empty subdirectory per
    # iteration, removed once this iteration's launch is fully processed, rather than
    # accumulating request/result files under one shared scratch_dir for the whole
    # run_sizes × measured_runs matrix.
    iter_dir = scratch_dir / f"iter-{run_size.value}-{time.time_ns()}"
    iter_dir.mkdir(parents=True, exist_ok=True)

    # Gates whether the sampler thread runs AT ALL -- an OR, since sampling produces
    # both CPU and RSS readings together in one pass (launch_and_measure's `sample`
    # docstring). Which of the two families actually appears in the result is a
    # SEPARATE, independent decision made below at attribute_samples(), not here.
    capture_metrics = performance.metrics.capture_cpu or performance.metrics.capture_memory
    try:
        # ConfigFile's file CONTENTS may ALSO carry ${...} tokens (e.g. a table
        # name needing the same per-test isolation ddl:/seed: SQL already gets
        # via dbroutes) -- same step IntYamlItem.runtest() performs for
        # integration mode (plugin.render_config_file). Rendered into its own
        # subdirectory of this iteration's own directory (never in place, so
        # the checked-in fixture stays byte-identical, and never directly into
        # iter_dir, which also holds request.json/result.json/input.json below
        # -- a ConfigFile that happened to share one of those names would
        # otherwise be silently overwritten before the operator read it).
        # Inside the try so a render failure (e.g. a token missing from
        # `tokens`) still reaches iter_dir's cleanup in the finally below,
        # instead of leaking iter_dir on a raise before the try even opened.
        # cwd stays manifest.dir (the launch_and_measure call below), so a
        # config's own relative sibling references still resolve the same way
        # integration mode's do. render_config_file returns the path unchanged
        # when the file has no tokens, so an untokenized config (the common
        # case) costs nothing here.
        if "ConfigFile" in rendered_properties:
            from . import plugin as plugin_mod  # lazy: plugin.py imports this module at load time
            cfg_dir = iter_dir / "config"
            cfg_dir.mkdir()
            rendered_properties["ConfigFile"] = plugin_mod.render_config_file(
                rendered_properties["ConfigFile"], updated_tokens, dest_dir=cfg_dir)

        request_file = iter_dir / "request.json"
        result_file = iter_dir / "result.json"
        # The operator replays preflight's already-parsed, already-${...}-rendered
        # input events -- written fresh here, not performance.input_path directly --
        # mirroring how harness.drive() hands the Java driver pre-rendered events
        # rather than a raw path (see preflight()'s docstring: a fixture referencing
        # e.g. ${TID} must not silently reach the operator as the literal token text).
        if manifest.source is None:
            input_file = iter_dir / "input.json"
            input_file.write_text(json.dumps(preflight_result.input_events))
        else:
            input_file = None   # a reader is ticked, not fed

        request = build_perf_request(
            op_jar=op_jar, properties=rendered_properties, input_file=input_file,
            result_file=result_file, types=(manifest.types or None), namespace=None, source_name=None,
            password_properties=(manifest.password_properties or None), run_size=run_size.value,
            warmup_runs=performance.warmup_runs, correctness_mode=performance.correctness.mode,
            sample_indices=preflight_result.sample_indices, udf=manifest.udf,
            source=manifest.source,
            target=manifest.target,
        )

        launch = launch_and_measure(
            java_bin=java_bin, harness_jar=harness_jar, op_jar=op_jar, request=request,
            request_file=request_file, result_file=result_file, cwd=manifest.dir,
            jvm=performance.jvm, timeout=performance.timeout, sample=capture_metrics,
        )
    finally:
        # launch_and_measure has already parsed result_file into memory (launch["result"])
        # before returning, so it's safe to remove the whole iteration directory now.
        shutil.rmtree(iter_dir, ignore_errors=True)

    if launch["survivor"]:
        # This iteration genuinely ran (and its process tree still isn't fully
        # reaped) -- §10 says partial results are always written and reported,
        # so it must not simply vanish the way raising bare would leave it. The
        # caller (PerfYamlItem.runtest()'s loop) appends
        # `.partial_iteration_result` before treating the rest of this run size,
        # and every later one, as not_run.
        survivor_result = IterationResult(
            status="failed", run_size_authored=run_size.authored, run_size_value=run_size.value,
            duration_seconds=None, input_throughput=None, output_throughput=None,
            measured_input_events=None, measured_output_events=None,
            warmup_input_events=None, warmup_output_events=None,
            metrics=None, reset_duration_seconds=reset_duration,
            failure_reason="operator subprocess tree survived termination + kill after a timeout",
            raw_result=None,
        )
        raise UnrecreatableEnvironmentError(
            "operator subprocess tree survived termination + kill after a timeout", "surviving-process",
            partial_iteration_result=survivor_result)

    perf_result = launch["result"]
    if perf_result is None:
        reason = (
            f"timed out after {performance.timeout}s" if launch["timed_out"]
            else f"process exited {launch['returncode']} with no result file "
                 f"(stderr: {launch['stderr'][-2000:]!r})"
        )
        return IterationResult(
            status="failed", run_size_authored=run_size.authored, run_size_value=run_size.value,
            duration_seconds=None, input_throughput=None, output_throughput=None,
            measured_input_events=None, measured_output_events=None,
            warmup_input_events=None, warmup_output_events=None,
            metrics=None, reset_duration_seconds=reset_duration,
            failure_reason=reason, raw_result=None,
        )

    # A parseable-but-unexpected result shape (missing keys, wrong top-level JSON
    # type) must not crash out of this function -- an uncaught exception here would
    # propagate straight out of the caller's run_size loop, escaping §10's `not_run`
    # bookkeeping entirely ("partial results are always written and reported").
    # Treated as a §6 mandatory-check failure ("valid, parseable result output").
    try:
        metrics = None
        if capture_metrics:
            metrics = attribute_samples(
                launch["samples"], perf_result["measuredStartEpochMillis"], perf_result["measuredEndEpochMillis"],
                capture_cpu=performance.metrics.capture_cpu, capture_memory=performance.metrics.capture_memory)

        correctness = check_correctness(
            perf_result, preflight_result.events_in_file, run_size.value, performance.correctness.mode,
            preflight_result.sample_indices, preflight_result.expected_events, launch["returncode"],
            launch.get("stderr", ""),
        )

        duration_seconds = (
            perf_result["measuredDurationNanos"] / 1e9 if perf_result.get("measuredDurationNanos") is not None else None
        )
        input_throughput = (
            perf_result["measuredInputEvents"] / duration_seconds if duration_seconds else None
        )
        output_throughput = (
            perf_result["measuredOutputEvents"] / duration_seconds if duration_seconds else None
        )
    except (KeyError, TypeError, AttributeError) as e:
        return IterationResult(
            status="failed", run_size_authored=run_size.authored, run_size_value=run_size.value,
            duration_seconds=None, input_throughput=None, output_throughput=None,
            measured_input_events=None, measured_output_events=None,
            warmup_input_events=None, warmup_output_events=None,
            metrics=None, reset_duration_seconds=reset_duration,
            failure_reason=f"result file did not match the expected PerfResult shape: {e!r}",
            raw_result=perf_result if isinstance(perf_result, dict) else None,
        )

    # check_correctness's own first check (after the mandatory returncode check)
    # already covers errorMessage, so correctness.passed alone determines status.
    status = "success" if correctness.passed else "failed"
    failure_reason = None if correctness.passed else correctness.reason

    return IterationResult(
        status=status, run_size_authored=run_size.authored, run_size_value=run_size.value,
        duration_seconds=duration_seconds, input_throughput=input_throughput, output_throughput=output_throughput,
        measured_input_events=perf_result.get("measuredInputEvents"),
        measured_output_events=perf_result.get("measuredOutputEvents"),
        warmup_input_events=perf_result.get("warmupInputEvents"),
        warmup_output_events=perf_result.get("warmupOutputEvents"),
        metrics=metrics, reset_duration_seconds=reset_duration,
        failure_reason=failure_reason, raw_result=perf_result, correctness=correctness,
    )
