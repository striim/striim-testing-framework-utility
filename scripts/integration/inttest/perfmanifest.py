"""Scripts/integration/perf/**/test.yaml parsing/normalization (PERF_SPEC.md §3).

A performance test lives in its own `test.yaml`, in its own tree
(`scripts/integration/perf/{op,udf}/<name>/<case>/`), parallel to and never merged
into the regression tree's `scripts/integration/regression/{op,udf}/<name>/<case>/
test.yaml`. A perf `test.yaml` is STRUCTURALLY IDENTICAL to a regression `test.yaml`
(`inttest.manifest.load_manifest`) except `assert:` is replaced by `performance:`
(PERF_SPEC.md §3) -- every other key (`name:`/`op:`/`properties:`/`purpose:`/
`requires:`/`ddl:`/`seed:`/`timeout:`/`disabled:`/`types:`/`password_properties:`)
means exactly the same thing and is validated by the exact same normalizers, so
authoring a perf test.yaml is: copy an existing regression `test.yaml`, delete
`assert:`, add `performance:`. There is no config reuse/pointer to a regression
case; each performance case declares its complete configuration.

This loader is pure Python, mirroring `inttest.manifest`'s style exactly: typed
frozen dataclasses for the `performance:` block, and `inttest.manifest`'s OWN
private normalizers reused directly for every other key -- one implementation of
each rule, not two, and a perf `test.yaml`'s `properties:`/`op:`/etc. validate
byte-identically to a regression one's. Errors raised naming the file and key, no
filesystem access beyond reading the one file handed to it. `assert:` is REJECTED
as an unknown key (it's `performance:`'s job now); the resulting
`manifest.TestManifest` gets a permissive placeholder (`AssertSpec(data=[],
smoke=True)`) satisfying its shape, since no perf-mode consumer ever reads
`.assert_`. `timeout:` is accepted, for the same schema-parity reason as every
other key, even though nothing in perf mode reads `TestManifest.timeout` --
`performance.timeout` is the field that actually governs a measured iteration's
budget; the two are unrelated and both can coexist harmlessly.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import manifest as manifest_mod

# Every key inttest.manifest.load_manifest accepts EXCEPT `assert:` (replaced by
# `performance:`, added separately below).
_TEST_MANIFEST_KEYS = {
    "name", "op", "properties", "purpose", "requires", "ddl", "seed", "timeout",
    "disabled", "types", "password_properties", "udf", "source", "target",
    # §85.1's first axis: the perf tier ran on PostgreSQL alone, so it could not rank a
    # change whose cost is engine-specific -- MERGE against ON CONFLICT, range locks
    # against row locks, bulk-copy eligibility. Same key, same normalizer and same refusals
    # as the regression tier, so a `variants:` block means one thing in both.
    "variants",
    # The PROPERTY axis, and `_normalize_matrix`'s own docstring is what sends it here:
    # "What may legitimately differ between permutations is PERFORMANCE, which this tier
    # does not measure and must not start to ... That is T3's."
    #
    # ⚠ IT MEANS THE OPPOSITE OF WHAT IT MEANS IN REGRESSION, and that is the whole reason
    # it belongs here. There, N permutations share ONE `assert:` -- the claim is that the
    # knobs are NOT observable. Here each permutation is its own measurement, and the claim
    # is that they ARE observable: the difference between two numbers one property apart is
    # the deliverable (§69.3). Composed with `variants:` by TestManifest.runs().
    "matrix",
}

_CORRECTNESS_MODES = {"full", "sampled", "disabled"}
# -Xms/-Xmx always carry their value concatenated (e.g. -Xmx4g), never as a separate
# following arg, so these are prefix-denied; -cp/-classpath always take a separate
# following arg, so the flag itself is always exactly this string.
_JVM_ARG_DENYLIST_PREFIXES = ("-Xms", "-Xmx")
_JVM_ARG_DENYLIST_EXACT = {"-cp", "-classpath"}
_RUN_SIZE_SUFFIXES = {"k": 1_000, "m": 1_000_000, "g": 1_000_000_000}
# JVM -Xms/-Xmx suffixes are BINARY (k=1024), unlike run-size suffixes above, which
# are decimal -- using 1000-based multipliers here would make e.g. min_heap: 1024m /
# max_heap: 1g (equal to the JVM) compare as min > max and be wrongly rejected.
_HEAP_SUFFIXES = {"k": 1024, "K": 1024, "m": 1024**2, "M": 1024**2, "g": 1024**3, "G": 1024**3}
_HEAP_RE = re.compile(r"^\d+[kKmMgG]?$")

DEFAULT_WARMUP_RUNS = 1
DEFAULT_MEASURED_RUNS = 3
DEFAULT_TIMEOUT = 900
DEFAULT_CORRECTNESS_MODE = "sampled"


class PerfManifestError(Exception):
    """Raised when a perf-tree test.yaml fails to parse or normalize per PERF_SPEC.md §3."""


def _reject_unknown_keys(raw: dict, allowed: set, context: str, path) -> None:
    """PERF_SPEC.md §3: "a typo in a disabled block is caught rather than silently
    ignored" -- applies to every key in this loader's shape, not just `disabled`.
    Without this, e.g. `warmup_run: 3` (missing the `s`) would silently fall back to
    the `warmup_runs` default, turning an intended steady-state benchmark into a
    near-cold one with no warning at all."""
    unknown = sorted(set(raw.keys()) - allowed)
    if unknown:
        raise PerfManifestError(
            f"{path}: {context} has unknown key(s) {unknown} (allowed: {sorted(allowed)})")


@dataclass(frozen=True)
class RunSize:
    """One `run_sizes:` entry: the literal authored form and its expanded integer
    value (PERF_SPEC.md §3's run-size notation) -- both are reported (Section 11)."""
    authored: str
    value: int


@dataclass(frozen=True)
class PerfJvmSpec:
    """Normalized `performance.jvm:` block. `min_heap`/`max_heap` are `None` when
    absent (JVM defaults apply); `args` defaults to an empty list."""
    min_heap: str | None = None
    max_heap: str | None = None
    args: list = field(default_factory=list)


@dataclass(frozen=True)
class PerfCorrectnessSpec:
    """Normalized `performance.correctness:` block. `mode` defaults to `"sampled"`."""
    mode: str = DEFAULT_CORRECTNESS_MODE


@dataclass(frozen=True)
class PerfMetricsSpec:
    """Normalized `performance.metrics:` block. Both toggles default to `True`."""
    capture_cpu: bool = True
    capture_memory: bool = True


@dataclass(frozen=True)
class PerformanceSpec:
    """Normalized `performance:` block (PERF_SPEC.md §3). No `enabled` field --
    an earlier revision had one, gating `--perf` SELECTION (a perf test.yaml with
    `enabled: false` was silently deselected, with no reason shown); removed once
    it became clear the shared `disabled:` key (same as a regression test.yaml)
    already covers "not ready to run yet" strictly better -- `disabled:` still
    SELECTS the test and skips it with a stated reason at `runtest()` time,
    visible in the run's output, instead of vanishing into the deselected count."""
    input: str | None
    input_path: Path | None
    run_sizes: list  # list[RunSize], ascending by expanded value
    expected: str | None = None
    expected_path: Path | None = None
    warmup_runs: int = DEFAULT_WARMUP_RUNS
    measured_runs: int = DEFAULT_MEASURED_RUNS
    timeout: int = DEFAULT_TIMEOUT
    jvm: PerfJvmSpec = field(default_factory=PerfJvmSpec)
    correctness: PerfCorrectnessSpec = field(default_factory=PerfCorrectnessSpec)
    metrics: PerfMetricsSpec = field(default_factory=PerfMetricsSpec)
    # Objects under this prefix (rendered, e.g. '${TID}gcsbw-perf-multi') are deleted from the
    # test bucket at every iteration's reset (§9). None: nothing pruned.
    gcs_prune_prefix: str | None = None


@dataclass(frozen=True)
class PerfManifest:
    """A fully parsed and normalized perf-tree `test.yaml` (PERF_SPEC.md §3):
    `performance` plus the fully-resolved `test_manifest` (a real
    `manifest.TestManifest`, built directly from this same file's `name:`/`op:`/
    `properties:`/etc. -- there is no separate regression manifest to merge with)."""
    performance: PerformanceSpec
    test_manifest: manifest_mod.TestManifest
    dir: Path                     # the directory containing this test.yaml
    path: Path                    # this test.yaml's own path


def _normalize_name(raw, path) -> str:
    if not isinstance(raw, str) or not raw:
        raise PerfManifestError(f"{path}: 'name' is required and must be a non-empty string")
    return raw


def _normalize_run_sizes(raw, path) -> list:
    if not isinstance(raw, list) or not raw:
        raise PerfManifestError(f"{path}: 'performance.run_sizes' is required and must be a non-empty list")
    expanded_seen = {}
    out = []
    for i, entry in enumerate(raw):
        if isinstance(entry, bool) or not isinstance(entry, (int, str)):
            raise PerfManifestError(f"{path}: 'performance.run_sizes[{i}]' must be an integer or a string like '10k', got {entry!r}")
        authored = str(entry)
        text = authored.strip()
        suffix = text[-1] if text and text[-1].lower() in _RUN_SIZE_SUFFIXES else None
        digits = text[:-1] if suffix else text
        if not digits.isdigit():
            raise PerfManifestError(
                f"{path}: 'performance.run_sizes[{i}]' {authored!r} is not a positive integer, "
                f"optionally suffixed with k/m/g (case-insensitive)")
        value = int(digits) * (_RUN_SIZE_SUFFIXES[suffix.lower()] if suffix else 1)
        if value <= 0:
            raise PerfManifestError(f"{path}: 'performance.run_sizes[{i}]' {authored!r} must be positive")
        if value in expanded_seen:
            raise PerfManifestError(
                f"{path}: 'performance.run_sizes' has duplicate values after expansion: "
                f"{expanded_seen[value]!r} and {authored!r} both expand to {value}")
        expanded_seen[value] = authored
        out.append(RunSize(authored=authored, value=value))
    out.sort(key=lambda rs: rs.value)
    return out


def _heap_value(raw: str) -> int:
    text = raw.strip()
    suffix = text[-1] if text and text[-1] in _HEAP_SUFFIXES else None
    digits = text[:-1] if suffix else text
    return int(digits) * (_HEAP_SUFFIXES[suffix] if suffix else 1)


def _normalize_jvm(raw, path) -> PerfJvmSpec:
    if raw is None:
        return PerfJvmSpec()
    if not isinstance(raw, dict):
        raise PerfManifestError(f"{path}: 'performance.jvm' must be a mapping, got {raw!r}")
    _reject_unknown_keys(raw, {"min_heap", "max_heap", "args"}, "'performance.jvm'", path)

    min_heap = raw.get("min_heap")
    max_heap = raw.get("max_heap")
    for key, val in (("min_heap", min_heap), ("max_heap", max_heap)):
        if val is not None and (not isinstance(val, str) or not _HEAP_RE.match(val)):
            raise PerfManifestError(f"{path}: 'performance.jvm.{key}' must match ^\\d+[kKmMgG]?$, got {val!r}")
    if min_heap is not None and max_heap is not None and _heap_value(min_heap) > _heap_value(max_heap):
        raise PerfManifestError(f"{path}: 'performance.jvm.min_heap' ({min_heap}) must not exceed 'performance.jvm.max_heap' ({max_heap})")

    args_raw = raw.get("args", [])
    if not isinstance(args_raw, list):
        raise PerfManifestError(f"{path}: 'performance.jvm.args' must be a list of non-empty strings, got {args_raw!r}")
    args = []
    seen = set()
    for i, a in enumerate(args_raw):
        if not isinstance(a, str) or not a:
            raise PerfManifestError(f"{path}: 'performance.jvm.args[{i}]' must be a non-empty string, got {a!r}")
        if a.startswith(_JVM_ARG_DENYLIST_PREFIXES) or a in _JVM_ARG_DENYLIST_EXACT:
            raise PerfManifestError(f"{path}: 'performance.jvm.args' must not contain {a!r} (conflicts with min_heap/max_heap/the harness's own classpath)")
        if a in seen:
            raise PerfManifestError(f"{path}: 'performance.jvm.args' has a duplicate entry: {a!r}")
        seen.add(a)
        args.append(a)

    return PerfJvmSpec(min_heap=min_heap, max_heap=max_heap, args=args)


def _normalize_correctness(raw, path) -> PerfCorrectnessSpec:
    if raw is None:
        return PerfCorrectnessSpec()
    if not isinstance(raw, dict):
        raise PerfManifestError(f"{path}: 'performance.correctness' must be a mapping, got {raw!r}")
    _reject_unknown_keys(raw, {"mode"}, "'performance.correctness'", path)
    mode = raw.get("mode", DEFAULT_CORRECTNESS_MODE)
    if mode not in _CORRECTNESS_MODES:
        raise PerfManifestError(f"{path}: 'performance.correctness.mode' must be one of {sorted(_CORRECTNESS_MODES)}, got {mode!r}")
    return PerfCorrectnessSpec(mode=mode)


def _normalize_metrics(raw, path) -> PerfMetricsSpec:
    if raw is None:
        return PerfMetricsSpec()
    if not isinstance(raw, dict):
        raise PerfManifestError(f"{path}: 'performance.metrics' must be a mapping, got {raw!r}")
    _reject_unknown_keys(raw, {"capture_cpu", "capture_memory"}, "'performance.metrics'", path)
    capture_cpu = raw.get("capture_cpu", True)
    capture_memory = raw.get("capture_memory", True)
    if not isinstance(capture_cpu, bool):
        raise PerfManifestError(f"{path}: 'performance.metrics.capture_cpu' must be true/false, got {capture_cpu!r}")
    if not isinstance(capture_memory, bool):
        raise PerfManifestError(f"{path}: 'performance.metrics.capture_memory' must be true/false, got {capture_memory!r}")
    return PerfMetricsSpec(capture_cpu=capture_cpu, capture_memory=capture_memory)


def _normalize_positive_int(raw, field_name: str, default: int, path) -> int:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise PerfManifestError(f"{path}: '{field_name}' must be an integer, got {raw!r}")
    if raw < 0:
        raise PerfManifestError(f"{path}: '{field_name}' must not be negative, got {raw!r}")
    return raw


def _normalize_performance(raw, test_dir: Path, path, *, is_source: bool = False) -> PerformanceSpec:
    if not isinstance(raw, dict) or not raw:
        raise PerfManifestError(f"{path}: 'performance' is required and must be a non-empty mapping")
    _reject_unknown_keys(
        raw,
        {"input", "expected", "run_sizes", "warmup_runs", "measured_runs",
         "timeout", "jvm", "correctness", "metrics", "gcs_prune_prefix"},
        "'performance'", path)

    input_ = raw.get("input")
    if is_source:
        # A reader replay is `source.max_ticks` ticks, not a pass over a fixture. Rejected rather
        # than ignored, exactly as the regression loader rejects `assert.data[].input`.
        if input_ is not None:
            raise PerfManifestError(
                f"{path}: 'performance.input' has no meaning for a 'source:' case -- a reader's "
                f"replay is 'source.max_ticks' ticks, not a pass over an input fixture; remove it")
    elif not isinstance(input_, str) or not input_:
        raise PerfManifestError(f"{path}: 'performance.input' is required and must be a non-empty path")

    expected = raw.get("expected")
    if expected is not None and (not isinstance(expected, str) or not expected):
        raise PerfManifestError(f"{path}: 'performance.expected' must be a non-empty path when present, got {expected!r}")

    run_sizes = _normalize_run_sizes(raw.get("run_sizes"), path)
    warmup_runs = _normalize_positive_int(raw.get("warmup_runs"), "performance.warmup_runs", DEFAULT_WARMUP_RUNS, path)
    measured_runs = _normalize_positive_int(raw.get("measured_runs"), "performance.measured_runs", DEFAULT_MEASURED_RUNS, path)
    if measured_runs < 1:
        raise PerfManifestError(f"{path}: 'performance.measured_runs' must be at least 1, got {measured_runs!r}")
    timeout = _normalize_positive_int(raw.get("timeout"), "performance.timeout", DEFAULT_TIMEOUT, path)
    if timeout <= 0:
        raise PerfManifestError(f"{path}: 'performance.timeout' must be positive, got {timeout!r}")

    gcs_prune_prefix = raw.get("gcs_prune_prefix")
    if gcs_prune_prefix is not None and (not isinstance(gcs_prune_prefix, str) or not gcs_prune_prefix.strip("/")):
        raise PerfManifestError(
            f"{path}: 'performance.gcs_prune_prefix' must be a non-empty object-key prefix, got "
            f"{gcs_prune_prefix!r} -- an empty one would empty the shared bucket")

    jvm = _normalize_jvm(raw.get("jvm"), path)
    correctness = _normalize_correctness(raw.get("correctness"), path)
    metrics = _normalize_metrics(raw.get("metrics"), path)

    if correctness.mode in ("full", "sampled") and not expected:
        raise PerfManifestError(
            f"{path}: 'performance.expected' is required when 'performance.correctness.mode' is "
            f"{correctness.mode!r} (absence never silently downgrades to 'disabled')")

    return PerformanceSpec(
        input=input_,
        input_path=None if input_ is None else (test_dir / input_).resolve(),
        expected=expected,
        expected_path=(test_dir / expected).resolve() if expected else None,
        run_sizes=run_sizes,
        warmup_runs=warmup_runs,
        measured_runs=measured_runs,
        timeout=timeout,
        jvm=jvm,
        gcs_prune_prefix=gcs_prune_prefix,
        correctness=correctness,
        metrics=metrics,
    )


def _perf_target_block(raw: dict, path) -> dict:
    """The `target:` block as the shared normalizer should see it in perf mode.

    Perf drives a writer through PerformanceProcessor, which honours only
    `target.distribution_id`: it always attaches a position and never restarts. So a key that
    asks for something else is refused here rather than silently measuring the recovery path
    with no restart. `target.input` is not read in perf mode (the driver is fed
    `performance.input`), so it is optional here; when absent the shared normalizer, which
    requires it, is handed `performance.input` instead."""
    target = raw.get("target")
    if not isinstance(target, dict):
        return target
    refused = []
    if "restart_after" in target:
        refused.append("'restart_after' (perf never restarts the writer)")
    if "mid_run" in target:
        refused.append("'mid_run' (perf runs no SQL between events)")
    if target.get("positions") is False:
        refused.append("'positions: false' (perf always attaches a position)")
    if refused:
        raise manifest_mod.ManifestError(
            f"{path}: perf mode does not support {', '.join(refused)} in 'target:'. Only "
            f"'distribution_id' changes what a perf target case measures; measure recovery in the "
            f"regression tier instead")
    if "input" not in target:
        performance = raw.get("performance")
        perf_input = performance.get("input") if isinstance(performance, dict) else None
        if perf_input is not None:
            target = {**target, "input": perf_input}
    return target


def load_perf_manifest(path) -> PerfManifest:
    """Load and normalize a `scripts/integration/perf/**/test.yaml` per
    PERF_SPEC.md §3. Raises `PerfManifestError` naming `path` and the offending key
    on any parse/validation failure. Does not check file existence for anything
    (`performance.input`/`expected`, `ddl:`/`seed:` files) beyond the one file
    handed to it -- matching `inttest.manifest.load_manifest`'s documented policy.

    Structurally identical to `inttest.manifest.load_manifest` except `assert:` is
    rejected (as an unknown key) and `performance:` is required instead --
    `name`/`op`/`properties`/`purpose`/`requires`/`ddl`/`seed`/`timeout`/
    `disabled`/`types`/`password_properties` are all normalized via
    `inttest.manifest`'s own private functions, so this file's `properties:`/
    `op:`/etc. validate byte-identically to a regression `test.yaml`'s. Those
    normalizers raise `manifest.ManifestError` (they have no reason to know about
    this module) -- re-raised as `PerfManifestError` so every failure out of this
    loader is the one type this module documents."""
    path = Path(path)
    try:
        text = path.read_text()
    except OSError as e:
        raise PerfManifestError(f"{path}: cannot read manifest: {e}") from e
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise PerfManifestError(f"{path}: invalid YAML: {e}") from e

    if raw is None:
        raise PerfManifestError(f"{path}: manifest is empty")
    if not isinstance(raw, dict):
        raise PerfManifestError(f"{path}: top level must be a mapping, got {type(raw).__name__}")
    _reject_unknown_keys(raw, {"performance"} | _TEST_MANIFEST_KEYS, "top level", path)

    perf_dir = path.parent
    try:
        source = manifest_mod._normalize_source(raw.get("source"), path)
    except manifest_mod.ManifestError as e:
        raise PerfManifestError(str(e)) from e
    performance = _normalize_performance(raw.get("performance"), perf_dir, path,
                                         is_source=source is not None)

    try:
        properties = manifest_mod._normalize_properties(raw.get("properties"), path)
        password_properties = manifest_mod._normalize_password_properties(raw.get("password_properties"), path)
        unknown_pw = [k for k in password_properties if k not in properties]
        if unknown_pw:
            raise PerfManifestError(
                f"{path}: 'password_properties' names key(s) not present in 'properties': {unknown_pw}")

        udf = manifest_mod._normalize_udf(raw.get("udf"), path)
        # `expect_events` stops ticking early once the count is reached, which is exactly what a
        # perf replay must NOT do: every replay has to be the same length or `measuredInputEvents`
        # and the emission-count stability check both become meaningless. The Java side ignores it
        # here, so reject it rather than let it sit dead in a fixture.
        if source is not None and source.expect_events is not None:
            raise PerfManifestError(
                f"{path}: 'source.expect_events' has no meaning in a perf case -- every replay "
                f"must be exactly 'source.max_ticks' ticks, so stopping early would make the "
                f"replays incomparable; remove it")
        # Same guard as the regression loader: a UDF has no tick to drive.
        if source is not None and udf is not None:
            raise PerfManifestError(
                f"{path}: 'source' and 'udf' cannot both be present -- a reader is ticked and a "
                f"UDF is a bare static function")
        # See inttest.manifest.load_manifest's identical check for why this is one
        # combined guard rather than two.
        if udf is not None and (properties or password_properties):
            raise PerfManifestError(
                f"{path}: 'properties'/'password_properties' have no meaning for a 'udf:' case "
                f"(a UDF is a bare static function, not a configured Processor); remove them")

        op = manifest_mod._normalize_op(raw.get("op"), path)
        manifest_mod._require_exactly_one_of_op_udf(op, udf, path)

        # §85.1 axis 1. The regression loader's refusals are repeated verbatim, not
        # relaxed: each one is a silent-wrong-answer trap (§98.1, §100.4), and a perf case
        # has no assertion to notice it -- `correctness.mode` is `disabled` for every
        # target case, so a variant reading or seeding the wrong engine reports a
        # throughput for a database nobody asked about and PASSES.
        variants = manifest_mod._normalize_variants(raw.get("variants"), perf_dir, path)
        if variants and udf is not None:
            raise PerfManifestError(
                f"{path}: 'variants:' has no meaning for a 'udf:' case -- a UDF has no "
                f"connection to vary.")
        if variants and manifest_mod._normalize_file_specs(raw.get("ddl"), "ddl", perf_dir, path):
            raise PerfManifestError(
                f"{path}: a case with 'variants:' puts its DDL on each VARIANT, not at case "
                f"level. Each engine needs its own SQL and its own route, and a shared 'ddl:' "
                f"block would run one engine's DDL through every variant's connection.")
        if variants and manifest_mod._normalize_file_specs(raw.get("seed"), "seed", perf_dir, path):
            raise PerfManifestError(
                f"{path}: a case with 'variants:' cannot carry a case-level 'seed:'. It would "
                f"be rendered with each variant's tokens but run through its own route, seeding "
                f"one engine with another's table names. Put the rows in that variant's 'ddl:' "
                f"instead.")

        # The property axis. `_normalize_matrix` validates shape only; composing it with
        # `variants:` is TestManifest.runs()'s job, and the plugin turns each (variant,
        # overrides) pair into its own item.
        matrix = manifest_mod._normalize_matrix(raw.get("matrix"), properties, path)
        # ⚠ A matrix value is a LITERAL here and is deliberately never token-rendered
        # (perf.py), matching the regression tier, which applies overrides after rendering.
        # So a `${...}` inside one would reach the operator as that exact text -- a
        # ConnectionURL or CheckPointTable that is quietly wrong, in a tier with NO assertion
        # to notice it (`correctness.mode` is `disabled` for every target case). Refused for
        # the same reason `_normalize_matrix` refuses its other silent-wrong-answer shapes.
        for name, values in matrix.items():
            for value in values:
                if "${" in str(value):
                    raise PerfManifestError(
                        f"{path}: 'matrix.{name}' value {value!r} contains a ${{...}} token. "
                        f"Matrix values are literals -- they are NOT rendered (the regression "
                        f"tier applies overrides after rendering, and this tier matches it), so "
                        f"this would reach the operator as that exact text. Put the token in "
                        f"'properties:' and matrix a different key.")
        if matrix and udf is not None:
            raise PerfManifestError(
                f"{path}: 'matrix:' has no meaning for a 'udf:' case. It overrides "
                f"'properties:', and a UDF is a bare static function with none -- every "
                f"permutation would be identical.")

        test_manifest = manifest_mod.TestManifest(
            name=_normalize_name(raw.get("name"), path),
            op=op,
            properties=properties,
            assert_=manifest_mod.AssertSpec(data=[], smoke=True),  # unused in perf mode -- see module docstring
            dir=perf_dir,
            purpose=manifest_mod._normalize_purpose(raw.get("purpose"), path),
            requires=manifest_mod._normalize_requires(raw.get("requires"), path),
            ddl=manifest_mod._normalize_file_specs(raw.get("ddl"), "ddl", perf_dir, path),
            seed=manifest_mod._normalize_file_specs(raw.get("seed"), "seed", perf_dir, path),
            timeout=manifest_mod._normalize_timeout(raw.get("timeout"), path),
            disabled=manifest_mod._normalize_disabled(raw.get("disabled"), path),
            types=manifest_mod._normalize_types(raw.get("types"), path),
            password_properties=password_properties,
            udf=udf,
            source=source,
            # ⚠ T3. The SAME normalizer the regression tier uses -- restart_after, positions,
            # mid_run and all -- so a `target:` block means exactly one thing across both tiers.
            # A perf case that used a different parser would drift from the one cases are written
            # against, and the drift would look like a writer difference.
            target=manifest_mod._normalize_target(_perf_target_block(raw, path), perf_dir, path)
            if raw.get("target") is not None else None,
            variants=variants,
            matrix=matrix,
        )
    except manifest_mod.ManifestError as e:
        raise PerfManifestError(str(e)) from e

    if performance.gcs_prune_prefix is not None and "gcs" not in test_manifest.requires:
        raise PerfManifestError(
            f"{path}: 'performance.gcs_prune_prefix' needs 'gcs' in 'requires:' -- there is no "
            f"bucket to prune otherwise")
    return PerfManifest(performance=performance, test_manifest=test_manifest, dir=perf_dir, path=path)
