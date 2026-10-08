"""Performance-mode reporting: console output (PERF_SPEC.md §11) and the JSON
report + reproducibility metadata (§11/§12).

`build_report` turns the raw pieces `inttest.plugin.PerfYamlItem.runtest()` already
has after running every `run_size x measured_runs` iteration (the loaded
`perfmanifest.PerfManifest`/`manifest.TestManifest`, the `perf.PreflightResult`, the
list of per-run-size `{"run_size", "iterations", "not_run"}` dicts it already builds,
and any `perf.UnrecreatableEnvironmentError`) into one JSON-serializable dict --
`schema_version = 1`. `format_console` renders that SAME dict as the §11 console
block, so console and JSON can never disagree about what happened. Everything else
here (`collect_git_info`/`collect_build_info`/`collect_environment`/
`collect_metadata`) gathers the §12 reproducibility metadata that gets folded into
that dict under `"metadata"`.

No re-running of anything: this module is a pure transform of data
`run_measured_iteration` (inttest.perf) already produced.
"""
from __future__ import annotations

import json
import platform
import re
import socket
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__ as _FRAMEWORK_VERSION
from . import opartifacts as opartifacts_mod
from . import perf as perf_mod

SCHEMA_VERSION = 1

# §12 metadata collectors shell out to git/java -- bounded so a stalled work tree
# (a stale index.lock, a stalled network/FUSE mount) or an NFS-mounted JAVA_HOME
# can't hang an otherwise-fully-measured run forever with no report ever written
# (the sibling of the unbounded-communicate() hang, PERF_SPEC.md §10).
_METADATA_SUBPROCESS_TIMEOUT_SECONDS = 10


# ============================================================================
# Report assembly (pure -- no I/O)
# ============================================================================


def _aggregate_dict(agg: perf_mod.MetricAggregate) -> dict:
    return {"min": agg.min, "max": agg.max, "mean": agg.mean, "median": agg.median, "stdev": agg.stdev}


def _correctness_dict(outcome: perf_mod.CorrectnessOutcome | None) -> dict | None:
    if outcome is None:
        return None
    return {
        "mode": outcome.mode,
        "passed": outcome.passed,
        "records_compared": outcome.records_compared,
        "detail": outcome.reason,
    }


def _iteration_dict(index: int, it: perf_mod.IterationResult) -> dict:
    metrics = it.metrics
    return {
        "index": index,
        "status": it.status,
        "duration_seconds": it.duration_seconds,
        "input_throughput": it.input_throughput,
        "output_throughput": it.output_throughput,
        "measured_input_events": it.measured_input_events,
        "measured_output_events": it.measured_output_events,
        "warmup_input_events": it.warmup_input_events,
        "warmup_output_events": it.warmup_output_events,
        "cpu_percent_avg": metrics.cpu_percent_avg if metrics else None,
        "cpu_percent_peak": metrics.cpu_percent_peak if metrics else None,
        "cpu_percent_avg_normalized": metrics.cpu_percent_avg_normalized if metrics else None,
        "cpu_percent_peak_normalized": metrics.cpu_percent_peak_normalized if metrics else None,
        "cpu_count": metrics.cpu_count if metrics else None,
        "memory_rss_peak_bytes": metrics.memory_rss_peak_bytes if metrics else None,
        "sample_count": metrics.sample_count if metrics else None,
        "insufficient_samples": metrics.insufficient_samples if metrics else None,
        "reset_duration_seconds": it.reset_duration_seconds,
        "correctness": _correctness_dict(it.correctness),
        "failure_reason": it.failure_reason,
    }


def _run_correctness_summary(iterations: list, mode: str) -> dict:
    """One correctness verdict per run size (PERF_SPEC.md §11's "Correctness:" line):
    PASS only if every iteration that reached a correctness check passed it.
    `of_records` (the denominator in "1,000 of 100,000 records") is the representative
    iteration's `measured_output_events` -- the total this run size actually emitted,
    not just the replay the comparison ran against (only replay 0 is ever compared,
    per §6)."""
    with_correctness = [it for it in iterations if it.correctness is not None]
    if not with_correctness:
        return {"mode": mode, "passed": None, "records_compared": None, "of_records": None, "detail": None}
    passed = all(it.correctness.passed for it in with_correctness)
    representative = next((it for it in with_correctness if it.correctness.passed), with_correctness[-1])
    detail = None
    if not passed:
        failing = next(it for it in with_correctness if not it.correctness.passed)
        detail = failing.correctness.reason
    return {
        "mode": representative.correctness.mode,
        "passed": passed,
        "records_compared": representative.correctness.records_compared,
        "of_records": representative.measured_output_events,
        "detail": detail,
    }


def _run_metrics_summary(successes: list) -> dict:
    """Per-run-size CPU/memory (PERF_SPEC.md §11's "CPU:"/"Memory:" lines) -- §8 only
    defines duration/throughput aggregation, so this picks a reading over the
    successful iterations that actually captured metrics: mean-of-per-iteration-avgs,
    max-of-per-iteration-peaks. `None` when no successful iteration has usable
    (non-insufficient-sample) metrics."""
    usable = [it for it in successes if it.metrics is not None and not it.metrics.insufficient_samples]
    if not usable:
        return {"avg_percent": None, "peak_percent": None, "cpu_count": None, "memory_rss_peak_bytes": None}

    # Either family may be switched off on its own (capture_cpu / capture_memory, §7), which
    # nulls that family's fields in every iteration: summarize each family only over the
    # readings it has, and report None for a family with none.
    def _mean(values):
        values = [v for v in values if v is not None]
        return statistics.mean(values) if values else None

    def _max(values):
        values = [v for v in values if v is not None]
        return max(values) if values else None

    return {
        "avg_percent": _mean(it.metrics.cpu_percent_avg for it in usable),
        "peak_percent": _max(it.metrics.cpu_percent_peak for it in usable),
        "cpu_count": usable[0].metrics.cpu_count,
        "memory_rss_peak_bytes": _max(it.metrics.memory_rss_peak_bytes for it in usable),
    }


def _run_size_dict(run_size, iterations: list, not_run: bool, preflight_result: perf_mod.PreflightResult,
                    performance, unrecreatable_error) -> dict:
    """`not_run=True` means the REMAINING iterations for this run size were never
    attempted (PERF_SPEC.md §10) -- it does NOT mean `iterations` is empty. A run
    size that was mid-flight when an `UnrecreatableEnvironmentError` fired
    (`plugin.py`'s `not_run` condition is `len(iterations) < measured_runs`, which
    is true for the in-flight run size too, not just ones that never started) still
    carries whatever `IterationResult`s it completed before the error -- those are
    reported exactly like a normal run size's, with `not_run`/`not_run_reason`
    layered on top, so real measurements are never silently dropped."""
    expected_input_events = preflight_result.events_in_file * run_size.value
    run_size_head = {"authored": run_size.authored, "value": run_size.value}

    successes = [it for it in iterations if it.status == "success"]
    agg = perf_mod.aggregate_run_size(
        [it.duration_seconds for it in successes], [it.input_throughput for it in successes])
    first_success = successes[0] if successes else None
    metrics_summary = _run_metrics_summary(successes)

    not_run_reason = None
    if not_run and unrecreatable_error is not None:
        not_run_reason = {"failure_class": unrecreatable_error.failure_class, "error": str(unrecreatable_error)}

    return {
        "run_size": run_size_head,
        "not_run": not_run,
        "not_run_reason": not_run_reason,
        "expected_input_events": expected_input_events,
        "input_events": first_success.measured_input_events if first_success else None,
        "output_events": first_success.measured_output_events if first_success else None,
        "iterations": [_iteration_dict(i + 1, it) for i, it in enumerate(iterations)],
        "aggregates": {
            "duration_seconds": _aggregate_dict(agg.duration_seconds),
            "input_throughput": _aggregate_dict(agg.input_throughput),
        },
        "cpu": {
            "avg_percent": metrics_summary["avg_percent"],
            "peak_percent": metrics_summary["peak_percent"],
            "cpu_count": metrics_summary["cpu_count"],
        },
        "memory_rss_peak_bytes": metrics_summary["memory_rss_peak_bytes"],
        "correctness": _run_correctness_summary(iterations, performance.correctness.mode),
    }


def _redact_properties(properties: dict, password_properties: list) -> dict:
    return {k: ("***REDACTED***" if k in password_properties else v) for k, v in properties.items()}


def _overall_status(run_size_reports: list, unrecreatable_error) -> str:
    if unrecreatable_error is not None:
        return "FAIL"
    for entry in run_size_reports:
        if entry["not_run"]:
            return "FAIL"
        for it in entry["iterations"]:
            if it.status != "success":
                return "FAIL"
    return "PASS"


def build_report(*, perf_manifest, test_manifest, preflight_result: perf_mod.PreflightResult,
                  run_size_reports: list, unrecreatable_error, metadata: dict,
                  variant: str | None = None, permutation: dict | None = None) -> dict:
    """Assembles the full §11/§12 JSON report as a plain, `json.dumps`-safe dict --
    no dataclass, `Path`, or `datetime` leaks into it. `run_size_reports` is exactly
    what `PerfYamlItem.runtest()` already builds (a list of
    `{"run_size": RunSize, "iterations": [IterationResult, ...], "not_run": bool}`);
    nothing here re-runs anything."""
    performance = perf_manifest.performance

    run_size_blocks = [
        _run_size_dict(entry["run_size"], entry["iterations"], entry["not_run"], preflight_result,
                        performance, unrecreatable_error)
        for entry in run_size_reports
    ]
    status = _overall_status(run_size_reports, unrecreatable_error)

    unrecreatable = None
    if unrecreatable_error is not None:
        unrecreatable = {"failure_class": unrecreatable_error.failure_class, "message": str(unrecreatable_error)}

    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "test": {
            "name": test_manifest.name,
            # WHICH RUN produced this number: the `variants:` entry (the engine) and the
            # `matrix:` permutation (the knobs), each null/empty for a case not using that
            # axis. One test.yaml fans out to one report per run, and a throughput that does
            # not say which engine and which knobs produced it cannot be compared to
            # anything -- which is the whole use of the two axes here (§69.3).
            "variant": variant,
            "permutation": dict(permutation or {}),
            "manifest_path": str(perf_manifest.path),
            "perf_dir": str(perf_manifest.dir),
        },
        "input": {
            "path": performance.input,
            # A reader has no fixture: str(None) would put the literal "None" in the report.
            "resolved_path": str(performance.input_path) if performance.input_path else None,
            # For a `source:` (reader) case this counts TICKS per replay, not fixture records --
            # `replay_unit` says which, so a consumer never has to infer it from a null path.
            "events_in_file": preflight_result.events_in_file,
            "replay_unit": "tick" if test_manifest.source is not None else "event",
            "expected_path": str(performance.expected_path) if performance.expected_path else None,
            "expected_events": (
                len(preflight_result.expected_events) if preflight_result.expected_events is not None else None),
        },
        "configuration": {
            "run_sizes": [{"authored": rs.authored, "value": rs.value} for rs in performance.run_sizes],
            "warmup_runs": performance.warmup_runs,
            "measured_runs": performance.measured_runs,
            "timeout": performance.timeout,
            "correctness_mode": performance.correctness.mode,
            "capture_cpu": performance.metrics.capture_cpu,
            "capture_memory": performance.metrics.capture_memory,
            # The operator config this test actually measured (a perf test.yaml
            # is fully self-contained, PERF_SPEC.md §3) -- §12's "only comparable
            # under an identical effective configuration" promise needs this
            # self-describing, not just a manifest path, since two perf
            # test.yamls with the same "name" could still describe different
            # operator configs. Password-valued properties are redacted -- this
            # file is a persisted, potentially-shared artifact.
            "operator": {
                # Key name kept as "op_jar_ref" even for a UDF case -- it names the
                # driven module either way; this is a
                # persisted-artifact schema, and renaming it is a separate concern.
                "op_jar_ref": test_manifest.module_ref,
                "properties": _redact_properties(test_manifest.properties, test_manifest.password_properties),
                "requires": list(test_manifest.requires),
                "password_properties": list(test_manifest.password_properties),
            },
            "jvm": {
                "min_heap": performance.jvm.min_heap,
                "max_heap": performance.jvm.max_heap,
                "args": list(performance.jvm.args),
                "effective_args": perf_mod.jvm_args(performance.jvm),
            },
        },
        "run_sizes": run_size_blocks,
        "unrecreatable_environment": unrecreatable,
        "metadata": metadata,
    }


# ============================================================================
# Console rendering (pure -- no I/O)
# ============================================================================

# Column the value starts at on a run-size detail line, after the 2-space indent
# (PERF_SPEC.md §11's literal example lines up 5 of its 7 labels at this width;
# "Input Events:"/"Output Events:" are one space short there, evidently a
# hand-typing slip in the doc -- every label is aligned to this width here).
_LABEL_WIDTH = 19


def _line(label: str, value) -> str:
    return f"  {label:<{_LABEL_WIDTH}}{value}"


def _na_or(value, fmt):
    return "n/a" if value is None else fmt(value)


def _int(n) -> str:
    return _na_or(n, lambda v: f"{v:,}")


def _int_round(x) -> str:
    return _na_or(x, lambda v: f"{round(v):,}")


def _secs(x) -> str:
    return _na_or(x, lambda v: f"{v:.3f}")


def _pct(x) -> str:
    return _na_or(x, lambda v: f"{v:.0f}")


def _mb(b) -> str:
    return _na_or(b, lambda v: f"{v / 1024 / 1024:.0f}")


def _plural(n, word) -> str:
    return word if n == 1 else f"{word}s"


def _run_suffix(test: dict) -> str:
    """` [postgres, UseUpsert=false]` — which of the case's runs this report is.

    Mirrors `plugin._run_label`'s shape so a console block, a JSON report and a pytest node
    id all name the same run the same way. Empty for a case using neither axis.
    """
    parts = []
    if test.get("variant"):
        parts.append(test["variant"])
    parts.extend(f"{k}={v}" for k, v in (test.get("permutation") or {}).items())
    return "" if not parts else " [" + ", ".join(parts) + "]"


def format_console(report: dict, report_path=None) -> str:
    """Renders `report` (a `build_report` dict) as the §11 console block. The
    all-success path reproduces §11's layout; anomalies (a failed iteration, a
    not-run run size) only ever ADD lines, never reorder or drop the standard ones.
    `report_path`, when given, adds a trailing `Report: <path>` line under Summary --
    not in §11's literal example, but a report the user can't find isn't much of one."""
    test = report["test"]
    inp = report["input"]
    cfg = report["configuration"]

    lines = [
        f"Performance Test: {test['name']}{_run_suffix(test)}",
        (f"Source: {_int(inp['events_in_file'])} ticks per replay"
         if inp.get("replay_unit") == "tick"
         else f"Input: {inp['path']} ({_int(inp['events_in_file'])} events)"),
        f"JVM: {' '.join(cfg['jvm']['effective_args']) or '(defaults)'}",
        f"Warmup: {cfg['warmup_runs']} {_plural(cfg['warmup_runs'], 'replay')} | "
        f"Measured runs: {cfg['measured_runs']}",
    ]

    for block in report["run_sizes"]:
        lines.append("")
        rs = block["run_size"]
        lines.append(f"Run Size: {_int(rs['value'])} {_plural(rs['value'], 'replay')}")

        if block["not_run"] and not block["iterations"]:
            # This run size never started -- no completed iterations to report.
            reason = block["not_run_reason"] or {}
            lines.append(
                f"  NOT RUN: {reason.get('failure_class', 'unknown')} "
                f"({reason.get('error', 'unknown error')})")
            continue

        lines.append(_line(
            "Ticks:" if inp.get("replay_unit") == "tick" else "Input Events:",
            f"{_int(block['input_events'])} (expected {_int(block['expected_input_events'])})"))
        lines.append(_line("Output Events:", _int(block['output_events'])))
        median_tp = block["aggregates"]["input_throughput"]["median"]
        if inp.get("replay_unit") == "tick":
            # §8 aggregates INPUT throughput, which for a reader is TICKS per second. Labelling it
            # "events/sec" is not a wording nit: a reader emitting 1,000 events per tick would read
            # as "22 events/sec" when it is doing ~22,000. The emitted rate is what a reader
            # benchmark is for, so it is shown alongside rather than left to be inferred.
            lines.append(_line("Median Throughput:", f"{_int_round(median_tp)} ticks/sec"))
            duration_median = block["aggregates"]["duration_seconds"]["median"]
            if duration_median:
                emitted_rate = block["output_events"] / duration_median
                lines.append(_line("Emitted:", f"{_int_round(emitted_rate)} events/sec"))
        else:
            lines.append(_line("Median Throughput:", f"{_int_round(median_tp)} events/sec"))
        dur = block["aggregates"]["duration_seconds"]
        lines.append(_line(
            "Duration:",
            f"{_secs(dur['median'])} sec median "
            f"({_secs(dur['min'])} / {_secs(dur['median'])} / {_secs(dur['max'])} min/med/max)"))
        cpu = block["cpu"]
        cores = cpu["cpu_count"] if cpu["cpu_count"] is not None else "n/a"
        lines.append(_line(
            "CPU:", f"{_pct(cpu['avg_percent'])}% avg | {_pct(cpu['peak_percent'])}% peak "
                    f"(of 1 core; {cores} cores present)"))
        lines.append(_line("Memory:", f"{_mb(block['memory_rss_peak_bytes'])} MB peak RSS"))
        c = block["correctness"]
        c_status = "n/a" if c["passed"] is None else ("PASS" if c["passed"] else "FAIL")
        lines.append(_line(
            "Correctness:", f"{c_status} ({c['mode']}, {_int(c['records_compared'])} of "
                             f"{_int(c['of_records'])} records)"))

        iterations = block["iterations"]
        succeeded = sum(1 for it in iterations if it["status"] == "success")
        if iterations and succeeded < len(iterations):
            lines.append(_line("Iterations:", f"{succeeded} of {len(iterations)} succeeded"))
            for it in iterations:
                if it["status"] != "success":
                    lines.append(f"    iteration {it['index']} FAILED: {it['failure_reason']}")

        if block["not_run"]:
            reason = block["not_run_reason"] or {}
            lines.append(
                f"  NOT RUN (remaining iterations): {reason.get('failure_class', 'unknown')} "
                f"({reason.get('error', 'unknown error')})")

    lines.append("")
    lines.append("Summary:")
    lines.append(f"  Status: {report['status']}")
    if report_path is not None:
        lines.append(f"  Report: {report_path}")
    return "\n".join(lines)


# ============================================================================
# Report path resolution + writing
# ============================================================================

_UNSAFE_PATH_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(name: str) -> str:
    slug = _UNSAFE_PATH_CHARS_RE.sub("-", name).strip("-")
    return slug or "test"


def _timestamp_stamp(timestamp: datetime) -> str:
    # Basic (not extended) ISO 8601, no colons -- a literal extended-form timestamp
    # is legal in a filename on macOS/Linux but hostile to Finder/Windows/shell globs.
    return timestamp.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def report_key_for(perf_dir: Path, perf_root: Path, run_id: str = "") -> str:
    """The string that keys a report's filename -- `perf_dir`'s path RELATIVE TO
    `perf_root` (e.g. `scripts/integration/perf/`), flattened with `-`, plus
    `run_id` when the case fans out. Unlike a test's own `name:` (free text an
    author could give to two different cases, e.g. copy-pasting one perf
    `test.yaml` to author a second) or `perf_dir.name` alone (two different
    operators can each have a case directory with the same LAST segment, e.g.
    `perf/op/a/baseline/` and `perf/op/b/baseline/` -- only the full relative
    path tells them apart), this keys a report by the run that produced it rather than by a
    name an author chose.

    ⚠ NOT literally collision-free, and the earlier wording here claimed it was: the filename
    applies `_slug`, which collapses every run of non-`[A-Za-z0-9._-]` characters to one `-`,
    so `perf/op/x/case` under variant `postgres` and a sibling directory `perf/op/x/case-postgres`
    flatten to the same name. The path-flattening half of that predates the run id (`a/b` and
    `a-b` already collided); the run id neither introduces nor fixes it.

    ⚠ `run_id` IS PART OF THAT GUARANTEE, not decoration. Since `variants:`/`matrix:`
    arrived, ONE `perf_dir` produces one report PER RUN, so the path alone stopped
    identifying a report. The only thing separating two of them in a filename would then
    be `_timestamp_stamp`, which is second-granularity -- two fast runs of the same case
    finishing inside one second would silently overwrite each other, and the loss would
    look like a report that was never written."""
    return "-".join(perf_dir.relative_to(perf_root).parts) + run_id


def default_report_path(results_dir: Path, test_name: str, timestamp: datetime) -> Path:
    return Path(results_dir) / f"{_timestamp_stamp(timestamp)}-{_slug(test_name)}.json"


def resolve_report_path(perf_json_option: str | None, results_dir: Path, test_name: str,
                         timestamp: datetime, multiple_tests: bool = False) -> Path:
    """`--perf-json PATH` is singular but `pytest --perf` can select more than one
    test. With exactly one test selected, `PATH` IS the report file. With more than
    one, `PATH` is treated as a directory and each test's default filename is written
    inside it (each test still gets its own report; there is no combined shape)."""
    if perf_json_option is None:
        return default_report_path(results_dir, test_name, timestamp)
    path = Path(perf_json_option)
    if multiple_tests:
        return path / f"{_timestamp_stamp(timestamp)}-{_slug(test_name)}.json"
    return path


def write_report(report: dict, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n")
    return path


# ============================================================================
# Reproducibility metadata collectors (PERF_SPEC.md §12)
# ============================================================================


class _GitUnavailable(Exception):
    """Internal: any git command failing/missing -- collect_git_info turns this into
    the documented null-plus-reason shape, never raises out of this module."""


def collect_git_info(test_dir, run=None) -> dict:
    """Git identity resolved from `test_dir` (never the process cwd) -- PERF_SPEC.md
    §12: repository root, commit, branch, and whether the work tree is dirty. Every
    field is `null` with a stated reason when git is unavailable or `test_dir` is not
    inside a work tree; never raises."""
    run = run or (lambda argv: subprocess.run(
        argv, capture_output=True, text=True, timeout=_METADATA_SUBPROCESS_TIMEOUT_SECONDS))
    test_dir = str(test_dir)

    def _git(*args):
        try:
            r = run(["git", "-C", test_dir, *args])
        except (OSError, subprocess.SubprocessError) as e:
            raise _GitUnavailable(f"git executable not available: {e}") from e
        out = (getattr(r, "stdout", "") or "")
        err = (getattr(r, "stderr", "") or "").strip()
        code = getattr(r, "returncode", 1)
        if code != 0:
            raise _GitUnavailable(f"'git {' '.join(args)}' exited {code}: {err or out.strip()}")
        return out

    try:
        repository_root = _git("rev-parse", "--show-toplevel").strip()
        commit = _git("rev-parse", "HEAD").strip()
        branch = _git("rev-parse", "--abbrev-ref", "HEAD").strip()
        status = _git("status", "--porcelain")
    except _GitUnavailable as e:
        return {"repository_root": None, "commit": None, "branch": None, "dirty": None,
                "unavailable_reason": str(e)}

    return {
        "repository_root": repository_root, "commit": commit, "branch": branch,
        "dirty": bool(status.strip()), "unavailable_reason": None,
    }


def collect_build_info(*, artifact, release: dict, rebuild_reason: str | None, striim_home: str | None) -> dict:
    """Build identity (PERF_SPEC.md §12): resolved release, the built op jar's
    filename/size/mtime, its `Striim-Build-*` manifest fingerprint when present, and
    whether this run rebuilt it or reused an existing one."""
    try:
        st = artifact.path.stat()
        size_bytes = st.st_size
        modified_utc = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        size_bytes = None
        modified_utc = None

    fingerprint = None
    fingerprint_reason = None
    try:
        fingerprint = opartifacts_mod._read_manifest_fingerprint(artifact.path)  # noqa: SLF001
        if fingerprint is None:
            fingerprint_reason = "jar has no complete Striim-Build-* manifest stamp"
    except Exception as e:  # noqa: BLE001 - undocumented private API; must never fail the report
        fingerprint_reason = f"failed to read manifest fingerprint: {e!r}"

    return {
        "striim_home": striim_home,
        "release": {
            "STRIIM_VERSION": release.get("STRIIM_VERSION"),
            "STRIIM_SERIES": release.get("STRIIM_SERIES"),
            "JAVA_RELEASE": release.get("JAVA_RELEASE"),
        },
        "op_jar": {
            "name": artifact.name,
            "path": str(artifact.path),
            "size_bytes": size_bytes,
            "modified_utc": modified_utc,
        },
        "manifest_fingerprint": fingerprint,
        "manifest_fingerprint_unavailable_reason": fingerprint_reason,
        "rebuilt": rebuild_reason is not None,
        "rebuild_reason": rebuild_reason,
    }


def collect_environment(*, java_bin: str | None, timestamp: datetime, run=None) -> dict:
    """Host/toolchain identity (PERF_SPEC.md §12). `psutil` is a required dependency
    of performance mode (guaranteed present by `perf.preflight`, which runs long
    before this is ever called), so the IMPORT is unconditional -- but the actual
    `psutil`/`socket`/`platform` CALLS are still guarded, same as the git/build
    collectors: a restricted sandbox (e.g. `/proc` not mounted the way `psutil`
    expects) can make the calls themselves raise even though the import succeeded,
    and this collector's failures must not cost `collect_git_info`/
    `collect_build_info` (which run independently) or abort report-writing
    entirely."""
    import psutil

    run = run or (lambda argv: subprocess.run(
        argv, capture_output=True, text=True, timeout=_METADATA_SUBPROCESS_TIMEOUT_SECONDS))
    java_version = None
    java_version_reason = None
    if not java_bin:
        java_version_reason = "no java binary resolved"
    else:
        try:
            r = run([java_bin, "-version"])
        except (OSError, subprocess.SubprocessError) as e:
            java_version_reason = f"failed to run '{java_bin} -version': {e}"
        else:
            # java -version writes to stderr; fall back to stdout defensively.
            text = ((getattr(r, "stderr", "") or "") + (getattr(r, "stdout", "") or "")).strip()
            if text:
                java_version = " ".join(text.split())
            else:
                java_version_reason = "java -version produced no output"

    host_info_reason = None
    try:
        hostname = socket.gethostname()
        os_name = platform.system()
        os_release = platform.release()
        platform_str = platform.platform()
        cpu_count = psutil.cpu_count()
        memory_total_bytes = psutil.virtual_memory().total
    except Exception as e:  # noqa: BLE001 - must never abort report-writing
        hostname = os_name = os_release = platform_str = cpu_count = memory_total_bytes = None
        host_info_reason = f"failed to read host info: {e!r}"

    return {
        "timestamp_utc": timestamp.astimezone(timezone.utc).isoformat(),
        "hostname": hostname,
        "os": os_name,
        "os_release": os_release,
        "platform": platform_str,
        "cpu_count": cpu_count,
        "memory_total_bytes": memory_total_bytes,
        "host_info_unavailable_reason": host_info_reason,
        "java_version": java_version,
        "java_version_unavailable_reason": java_version_reason,
        "java_path": java_bin,
        "python_version": sys.version,
        "framework_version": _FRAMEWORK_VERSION,
    }


def collect_metadata(*, test_dir, artifact, release: dict, rebuild_reason: str | None,
                      striim_home: str | None, java_bin: str | None, timestamp: datetime) -> dict:
    """Bundles all three §12 metadata groups. Each collector already guards its own
    failures internally, so one group being unavailable never costs the other two."""
    return {
        "git": collect_git_info(test_dir),
        "build": collect_build_info(
            artifact=artifact, release=release, rebuild_reason=rebuild_reason, striim_home=striim_home),
        "environment": collect_environment(java_bin=java_bin, timestamp=timestamp),
    }
