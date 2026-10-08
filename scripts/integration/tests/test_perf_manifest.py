"""Unit tests for inttest.perfmanifest -- scripts/integration/perf/**/test.yaml
parsing/normalization (PERF_SPEC.md §3).

A perf test.yaml is structurally identical to a regression test.yaml
(inttest.manifest.load_manifest) except `assert:` is replaced by `performance:` --
`name`/`op`/`properties`/`purpose`/`requires`/`ddl`/`seed`/`timeout`/`disabled`/
`types`/`password_properties` mean exactly the same thing and are validated by the
same inttest.manifest normalizers. Pure parsing tests: no Docker, no Striim, no
pytest-plugin collection. Each test writes a small inline perf test.yaml into
tmp_path and calls load_perf_manifest() directly.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from inttest import paths
from inttest.manifest import FileSpec
from inttest.perfmanifest import (
    PerfCorrectnessSpec,
    PerfJvmSpec,
    PerfManifestError,
    PerfMetricsSpec,
    RunSize,
    load_perf_manifest,
)


def _write(tmp_path, text: str):
    test_dir = tmp_path / "some-perf-case"
    test_dir.mkdir()
    manifest_path = test_dir / "test.yaml"
    manifest_path.write_text(text)
    return manifest_path, test_dir


# The minimum any perf test.yaml needs, mirroring what inttest.manifest.load_manifest
# itself requires unconditionally (name/op/properties) -- every test below builds on
# this instead of a test_dir: pointer.
_REQUIRED_PREFIX = """
name: fake-case
op:
  jar: java/OpenProcessors/FakeOp
properties:
  Foo: bar
"""

FULL_YAML = """
name: fake-case
purpose: exercises every optional key at once
op:
  jar: java/OpenProcessors/FakeOp
properties:
  Foo: bar
requires: [postgres]
ddl: ddl_lookup.sql
seed:
  - file: seed_lookup.sql
    db: postgres-source
timeout: 300
disabled: false
types:
  CUSTOMERS: [id, name]
password_properties: []

performance:
  input: performance/customers.json
  expected: performance/expected-customers.json

  run_sizes: [100, 1k, 10k]
  warmup_runs: 2
  measured_runs: 5
  timeout: 600

  jvm:
    min_heap: 512m
    max_heap: 2g
    args:
      - '-XX:+UseG1GC'

  correctness:
    mode: full

  metrics:
    capture_cpu: false
    capture_memory: false
"""

MINIMAL_YAML = _REQUIRED_PREFIX + """
performance:
  input: performance/customers.json
  run_sizes: [1000]
  correctness:
    mode: disabled
"""


def test_full_manifest_all_keys(tmp_path):
    manifest_path, test_dir = _write(tmp_path, FULL_YAML)
    m = load_perf_manifest(manifest_path)

    assert m.dir == test_dir
    assert m.path == manifest_path

    tm = m.test_manifest
    assert tm.name == "fake-case"
    assert tm.purpose == "exercises every optional key at once"
    assert tm.op.jar == "java/OpenProcessors/FakeOp"
    assert tm.properties == {"Foo": "bar"}
    assert tm.requires == ["postgres"]
    assert len(tm.ddl) == 1 and tm.ddl[0].file == "ddl_lookup.sql"
    assert len(tm.seed) == 1 and tm.seed[0].file == "seed_lookup.sql"
    assert tm.timeout == 300
    assert tm.disabled is False
    assert tm.types == {"CUSTOMERS": ["id", "name"]}
    assert tm.password_properties == []
    assert tm.dir == test_dir
    # assert: is never accepted (performance: replaces it) -- the placeholder
    # TestManifest.assert_ still satisfies the dataclass shape, unread by perf mode.
    assert tm.assert_.data == []
    assert tm.assert_.smoke is True

    p = m.performance
    assert p.input == "performance/customers.json"
    assert p.input_path == (test_dir / "performance/customers.json").resolve()
    assert p.expected == "performance/expected-customers.json"
    assert p.expected_path == (test_dir / "performance/expected-customers.json").resolve()
    assert p.run_sizes == [
        RunSize(authored="100", value=100),
        RunSize(authored="1k", value=1000),
        RunSize(authored="10k", value=10000),
    ]
    assert p.warmup_runs == 2
    assert p.measured_runs == 5
    assert p.timeout == 600
    assert p.jvm == PerfJvmSpec(min_heap="512m", max_heap="2g", args=["-XX:+UseG1GC"])
    assert p.correctness == PerfCorrectnessSpec(mode="full")
    assert p.metrics == PerfMetricsSpec(capture_cpu=False, capture_memory=False)


def test_minimal_manifest_defaults(tmp_path):
    manifest_path, test_dir = _write(tmp_path, MINIMAL_YAML)
    m = load_perf_manifest(manifest_path)

    tm = m.test_manifest
    assert tm.name == "fake-case"
    assert tm.purpose is None
    assert tm.requires == []
    assert tm.ddl == []
    assert tm.seed == []
    assert tm.timeout == 120  # inttest.manifest.DEFAULT_TIMEOUT
    assert tm.disabled is None
    assert tm.types == {}
    assert tm.password_properties == []

    p = m.performance
    assert p.expected is None
    assert p.expected_path is None
    assert p.run_sizes == [RunSize(authored="1000", value=1000)]
    assert p.warmup_runs == 1
    assert p.measured_runs == 3
    assert p.timeout == 900
    assert p.jvm == PerfJvmSpec(min_heap=None, max_heap=None, args=[])
    assert p.correctness == PerfCorrectnessSpec(mode="disabled")
    assert p.metrics == PerfMetricsSpec(capture_cpu=True, capture_memory=True)


# --------------------------------------------------------------------------------
# name:/op:/properties: -- required, same as inttest.manifest.load_manifest
# --------------------------------------------------------------------------------

def test_name_required(tmp_path):
    text = """
op:
  jar: java/OpenProcessors/FakeOp
properties:
  Foo: bar
performance:
  input: performance/in.json
  run_sizes: [100]
  correctness:
    mode: disabled
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(PerfManifestError, match="name"):
        load_perf_manifest(manifest_path)


def test_op_required(tmp_path):
    text = """
name: fake-case
properties:
  Foo: bar
performance:
  input: performance/in.json
  run_sizes: [100]
  correctness:
    mode: disabled
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(PerfManifestError, match="op"):
        load_perf_manifest(manifest_path)


def test_properties_absent_defaults_to_empty(tmp_path):
    # An operator with no configuration of its own is a real case -- properties:
    # is optional, not required (same as inttest.manifest.load_manifest).
    text = """
name: fake-case
op:
  jar: java/OpenProcessors/FakeOp
performance:
  input: performance/in.json
  run_sizes: [100]
  correctness:
    mode: disabled
"""
    manifest_path, _ = _write(tmp_path, text)
    m = load_perf_manifest(manifest_path)
    assert m.test_manifest.properties == {}


def test_assert_key_rejected(tmp_path):
    # performance: replaces assert: entirely -- a perf test.yaml with both is a
    # typo/copy-paste leftover, not a valid shape.
    text = _REQUIRED_PREFIX + """
assert:
  smoke: true
performance:
  input: performance/in.json
  run_sizes: [100]
  correctness:
    mode: disabled
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(PerfManifestError, match="assert"):
        load_perf_manifest(manifest_path)


# --------------------------------------------------------------------------------
# The other inttest.manifest-shared keys (requires/ddl/seed/disabled/types/
# password_properties) -- each reuses inttest.manifest's own normalizer, so these
# tests only need to confirm the wiring, not re-derive every rule that module's own
# test suite already covers.
# --------------------------------------------------------------------------------

def test_requires_string_form(tmp_path):
    text = _REQUIRED_PREFIX + """
requires: postgres
performance:
  input: performance/in.json
  run_sizes: [100]
  correctness:
    mode: disabled
"""
    manifest_path, _ = _write(tmp_path, text)
    m = load_perf_manifest(manifest_path)
    assert m.test_manifest.requires == ["postgres"]


def test_ddl_resolves_against_perf_dir(tmp_path):
    text = _REQUIRED_PREFIX + """
ddl: setup.sql
performance:
  input: performance/in.json
  run_sizes: [100]
  correctness:
    mode: disabled
"""
    manifest_path, perf_dir = _write(tmp_path, text)
    m = load_perf_manifest(manifest_path)
    assert m.test_manifest.ddl == [FileSpec(file="setup.sql", db="postgres-source",
                                             path=(perf_dir / "setup.sql").resolve())]


def test_disabled_string_reason(tmp_path):
    text = _REQUIRED_PREFIX + """
disabled: "flaky, see TICKET-123"
performance:
  input: performance/in.json
  run_sizes: [100]
  correctness:
    mode: disabled
"""
    manifest_path, _ = _write(tmp_path, text)
    m = load_perf_manifest(manifest_path)
    assert m.test_manifest.disabled == "flaky, see TICKET-123"


def test_types_block(tmp_path):
    text = _REQUIRED_PREFIX + """
types:
  CUSTOMERS: [id, name, email]
performance:
  input: performance/in.json
  run_sizes: [100]
  correctness:
    mode: disabled
"""
    manifest_path, _ = _write(tmp_path, text)
    m = load_perf_manifest(manifest_path)
    assert m.test_manifest.types == {"CUSTOMERS": ["id", "name", "email"]}


def test_password_properties_valid(tmp_path):
    text = """
name: fake-case
op:
  jar: java/OpenProcessors/FakeOp
properties:
  DbPassword: secret
password_properties: DbPassword
performance:
  input: performance/in.json
  run_sizes: [100]
  correctness:
    mode: disabled
"""
    manifest_path, _ = _write(tmp_path, text)
    m = load_perf_manifest(manifest_path)
    assert m.test_manifest.password_properties == ["DbPassword"]


def test_password_properties_unresolvable_raises(tmp_path):
    text = _REQUIRED_PREFIX + """
password_properties: NotAKey
performance:
  input: performance/in.json
  run_sizes: [100]
  correctness:
    mode: disabled
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(PerfManifestError, match="NotAKey"):
        load_perf_manifest(manifest_path)


def test_password_properties_with_no_properties_block_raises(tmp_path):
    # properties: is optional (both tiers) -- password_properties: naming a key
    # when properties: is absent ENTIRELY (not just present-and-empty) must still
    # be a hard error, no key can ever match against nothing.
    text = """
name: fake-case
op:
  jar: java/OpenProcessors/FakeOp
password_properties: DbPassword
performance:
  input: performance/in.json
  run_sizes: [100]
  correctness:
    mode: disabled
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(PerfManifestError, match="DbPassword"):
        load_perf_manifest(manifest_path)


def test_malformed_op_raises(tmp_path):
    text = """
name: fake-case
op: {}
properties:
  Foo: bar
performance:
  input: performance/in.json
  run_sizes: [100]
  correctness:
    mode: disabled
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(PerfManifestError):
        load_perf_manifest(manifest_path)


def test_malformed_ddl_raises(tmp_path):
    text = _REQUIRED_PREFIX + """
ddl: []
performance:
  input: performance/in.json
  run_sizes: [100]
  correctness:
    mode: disabled
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(PerfManifestError):
        load_perf_manifest(manifest_path)


def test_malformed_timeout_raises(tmp_path):
    text = _REQUIRED_PREFIX + """
timeout: -3
performance:
  input: performance/in.json
  run_sizes: [100]
  correctness:
    mode: disabled
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(PerfManifestError):
        load_perf_manifest(manifest_path)


def test_malformed_shared_key_raises_perf_manifest_error_not_manifest_error(tmp_path):
    # inttest.manifest's normalizers raise manifest.ManifestError -- must be
    # re-raised as PerfManifestError so PerfYamlFile.collect()'s `except
    # PerfManifestError` actually catches it (a raw ManifestError escaping would be
    # an uncaught collection-time traceback instead of a clean pytest.fail).
    text = _REQUIRED_PREFIX + """
types: {}
performance:
  input: performance/in.json
  run_sizes: [100]
  correctness:
    mode: disabled
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(PerfManifestError):
        load_perf_manifest(manifest_path)


# --------------------------------------------------------------------------------
# performance: block validation (independent of name:/op:/properties:/etc. --
# these rules only ever touch the performance: key)
# --------------------------------------------------------------------------------

@pytest.mark.parametrize("run_sizes_yaml,expected", [
    ("run_sizes: [1]", [("1", 1)]),
    ("run_sizes: ['1']", [("1", 1)]),
    ("run_sizes: [100, 1k, 10k]", [("100", 100), ("1k", 1000), ("10k", 10000)]),
    ("run_sizes: [1M, 2m]", [("1M", 1_000_000), ("2m", 2_000_000)]),
    ("run_sizes: [1g]", [("1g", 1_000_000_000)]),
    # authored out of order -> normalized ascending by expanded value
    ("run_sizes: [10k, 1k, 100]", [("100", 100), ("1k", 1000), ("10k", 10000)]),
])
def test_run_size_notation_forms(tmp_path, run_sizes_yaml, expected):
    text = _REQUIRED_PREFIX + f"""
performance:
  input: performance/in.json
  {run_sizes_yaml}
  correctness:
    mode: disabled
"""
    manifest_path, _ = _write(tmp_path, text)
    m = load_perf_manifest(manifest_path)
    got = [(rs.authored, rs.value) for rs in m.performance.run_sizes]
    assert got == expected


@pytest.mark.parametrize("run_sizes_yaml", [
    "run_sizes: []",
    "run_sizes: [0]",
    "run_sizes: [-1]",
    "run_sizes: [1.5k]",       # fractions not supported
    "run_sizes: ['abc']",
    "run_sizes: [1000, 1k]",   # duplicate after expansion
    "run_sizes: true",         # not a list
])
def test_malformed_run_sizes_raises(tmp_path, run_sizes_yaml):
    text = _REQUIRED_PREFIX + f"""
performance:
  input: performance/in.json
  {run_sizes_yaml}
  correctness:
    mode: disabled
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(PerfManifestError):
        load_perf_manifest(manifest_path)


def test_warmup_runs_zero_is_allowed(tmp_path):
    text = _REQUIRED_PREFIX + """
performance:
  input: performance/in.json
  run_sizes: [100]
  warmup_runs: 0
  correctness:
    mode: disabled
"""
    manifest_path, _ = _write(tmp_path, text)
    m = load_perf_manifest(manifest_path)
    assert m.performance.warmup_runs == 0


def test_measured_runs_zero_rejected(tmp_path):
    text = _REQUIRED_PREFIX + """
performance:
  input: performance/in.json
  run_sizes: [100]
  measured_runs: 0
  correctness:
    mode: disabled
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(PerfManifestError):
        load_perf_manifest(manifest_path)


@pytest.mark.parametrize("jvm_yaml", [
    "jvm:\n    min_heap: notaheap\n    max_heap: 2g\n",
    "jvm:\n    min_heap: 2g\n    max_heap: 512m\n",              # min > max
    "jvm:\n    args: ['-Xmx4g']\n",                              # denylisted flag
    "jvm:\n    args: ['-cp', '/foo']\n",                         # denylisted flag
    "jvm:\n    args: ['-Xfoo', '-Xfoo']\n",                      # duplicate arg
    "jvm:\n    args: ['']\n",                                    # empty string arg
])
def test_malformed_jvm_raises(tmp_path, jvm_yaml):
    text = _REQUIRED_PREFIX + f"""
performance:
  input: performance/in.json
  run_sizes: [100]
  {jvm_yaml}
  correctness:
    mode: disabled
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(PerfManifestError):
        load_perf_manifest(manifest_path)


def test_valid_jvm_block(tmp_path):
    text = _REQUIRED_PREFIX + """
performance:
  input: performance/in.json
  run_sizes: [100]
  jvm:
    min_heap: 512m
    max_heap: 512m
    args: ['-XX:+UseG1GC', '-XX:+PrintGCDetails']
  correctness:
    mode: disabled
"""
    manifest_path, _ = _write(tmp_path, text)
    m = load_perf_manifest(manifest_path)
    assert m.performance.jvm == PerfJvmSpec(min_heap="512m", max_heap="512m", args=["-XX:+UseG1GC", "-XX:+PrintGCDetails"])


def test_heap_suffixes_are_binary_not_decimal(tmp_path):
    # -Xms/-Xmx suffixes are binary (1024-based), like the JVM's own. 1024m == 1g
    # exactly -- must NOT be rejected as min > max.
    text = _REQUIRED_PREFIX + """
performance:
  input: performance/in.json
  run_sizes: [100]
  jvm:
    min_heap: 1024m
    max_heap: 1g
  correctness:
    mode: disabled
"""
    manifest_path, _ = _write(tmp_path, text)
    m = load_perf_manifest(manifest_path)
    assert m.performance.jvm.min_heap == "1024m"
    assert m.performance.jvm.max_heap == "1g"


@pytest.mark.parametrize("mode", ["full", "sampled", "disabled"])
def test_correctness_mode_valid_values(tmp_path, mode):
    expected_line = "" if mode == "disabled" else "  expected: performance/expected.json\n"
    text = _REQUIRED_PREFIX + f"""
performance:
  input: performance/in.json
{expected_line}  run_sizes: [100]
  correctness:
    mode: {mode}
"""
    manifest_path, _ = _write(tmp_path, text)
    m = load_perf_manifest(manifest_path)
    assert m.performance.correctness.mode == mode


def test_correctness_mode_invalid_raises(tmp_path):
    text = _REQUIRED_PREFIX + """
performance:
  input: performance/in.json
  run_sizes: [100]
  correctness:
    mode: bogus
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(PerfManifestError):
        load_perf_manifest(manifest_path)


def test_default_correctness_mode_is_sampled(tmp_path):
    text = _REQUIRED_PREFIX + """
performance:
  input: performance/in.json
  expected: performance/expected.json
  run_sizes: [100]
"""
    manifest_path, _ = _write(tmp_path, text)
    m = load_perf_manifest(manifest_path)
    assert m.performance.correctness.mode == "sampled"


@pytest.mark.parametrize("mode", ["full", "sampled"])
def test_expected_required_for_full_and_sampled(tmp_path, mode):
    text = _REQUIRED_PREFIX + f"""
performance:
  input: performance/in.json
  run_sizes: [100]
  correctness:
    mode: {mode}
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(PerfManifestError, match="expected"):
        load_perf_manifest(manifest_path)


def test_disabled_mode_never_requires_expected(tmp_path):
    text = _REQUIRED_PREFIX + """
performance:
  input: performance/in.json
  run_sizes: [100]
  correctness:
    mode: disabled
"""
    manifest_path, _ = _write(tmp_path, text)
    m = load_perf_manifest(manifest_path)
    assert m.performance.expected is None


@pytest.mark.parametrize("metrics_yaml", [
    "metrics:\n    capture_cpu: notabool\n",
    "metrics:\n    capture_memory: 1\n",
])
def test_malformed_metrics_raises(tmp_path, metrics_yaml):
    text = _REQUIRED_PREFIX + f"""
performance:
  input: performance/in.json
  run_sizes: [100]
  {metrics_yaml}
  correctness:
    mode: disabled
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(PerfManifestError):
        load_perf_manifest(manifest_path)


def test_performance_enabled_is_rejected_as_unknown_key(tmp_path):
    # performance.enabled was removed (PERF_SPEC.md §3): the shared `disabled:`
    # key already covers "not ready to run yet", strictly better (a stated reason,
    # visible in the run's output, instead of a silent deselect). Confirms nobody
    # can write `enabled:` expecting it to still do anything.
    text = _REQUIRED_PREFIX + """
performance:
  enabled: true
  input: performance/in.json
  run_sizes: [100]
  correctness:
    mode: disabled
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(PerfManifestError, match="enabled"):
        load_perf_manifest(manifest_path)


@pytest.mark.parametrize("missing_key,text", [
    ("performance", _REQUIRED_PREFIX),
    ("performance.input", _REQUIRED_PREFIX + """
performance:
  run_sizes: [100]
  correctness:
    mode: disabled
"""),
    ("performance.run_sizes", _REQUIRED_PREFIX + """
performance:
  input: performance/in.json
  correctness:
    mode: disabled
"""),
])
def test_missing_required_key_raises(tmp_path, missing_key, text):
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(PerfManifestError):
        load_perf_manifest(manifest_path)


def test_disabled_perf_test_still_shape_validated(tmp_path):
    """A disabled/typo'd perf test.yaml still fails loudly rather than being ignored."""
    text = _REQUIRED_PREFIX + """
disabled: "not ready yet"
performance:
  input: performance/in.json
  run_sizes: [not-a-valid-size]
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(PerfManifestError):
        load_perf_manifest(manifest_path)


def test_disabled_perf_test_loads_cleanly_when_well_formed(tmp_path):
    text = _REQUIRED_PREFIX + """
disabled: "not ready yet"
performance:
  input: performance/in.json
  run_sizes: [100]
  correctness:
    mode: disabled
"""
    manifest_path, _ = _write(tmp_path, text)
    m = load_perf_manifest(manifest_path)
    assert m.test_manifest.disabled == "not ready yet"


def test_invalid_yaml_raises(tmp_path):
    manifest_path, _ = _write(tmp_path, "name: [unclosed\n")
    with pytest.raises(PerfManifestError):
        load_perf_manifest(manifest_path)


def test_top_level_not_a_mapping_raises(tmp_path):
    manifest_path, _ = _write(tmp_path, "- just\n- a\n- list\n")
    with pytest.raises(PerfManifestError):
        load_perf_manifest(manifest_path)


def test_empty_file_raises(tmp_path):
    manifest_path, _ = _write(tmp_path, "")
    with pytest.raises(PerfManifestError):
        load_perf_manifest(manifest_path)


# --- unknown keys are rejected at every level (a typo must not silently fall back
# to a default -- PERF_SPEC.md §3) ---------------------------------------------

def test_unknown_top_level_key_raises(tmp_path):
    text = _REQUIRED_PREFIX + """
performance:
  input: performance/in.json
  run_sizes: [100]
  correctness:
    mode: disabled
extra_key: true
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(PerfManifestError, match="extra_key"):
        load_perf_manifest(manifest_path)


def test_unknown_performance_key_raises(tmp_path):
    # A typo'd warmup_runs (missing the trailing "s") must not silently fall back to
    # the default -- it changes what's actually measured.
    text = _REQUIRED_PREFIX + """
performance:
  input: performance/in.json
  run_sizes: [100]
  warmup_run: 5
  correctness:
    mode: disabled
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(PerfManifestError, match="warmup_run"):
        load_perf_manifest(manifest_path)


def test_unknown_jvm_key_raises(tmp_path):
    text = _REQUIRED_PREFIX + """
performance:
  input: performance/in.json
  run_sizes: [100]
  jvm:
    min_heap: 512m
    extra: true
  correctness:
    mode: disabled
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(PerfManifestError, match="extra"):
        load_perf_manifest(manifest_path)


def test_unknown_correctness_key_raises(tmp_path):
    text = _REQUIRED_PREFIX + """
performance:
  input: performance/in.json
  run_sizes: [100]
  correctness:
    mode: disabled
    extra: true
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(PerfManifestError, match="extra"):
        load_perf_manifest(manifest_path)


def test_unknown_metrics_key_raises(tmp_path):
    text = _REQUIRED_PREFIX + """
performance:
  input: performance/in.json
  run_sizes: [100]
  metrics:
    capture_cpu: true
    extra: true
  correctness:
    mode: disabled
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(PerfManifestError, match="extra"):
        load_perf_manifest(manifest_path)


# --------------------------------------------------------------------------------
# `udf:` block -- structurally identical to the regression
# tree's own `udf:` handling (inttest.manifest._normalize_udf), reused byte-for-byte;
# these tests just confirm perfmanifest.py's own wiring (the "udf" key is accepted by
# `_reject_unknown_keys`, and it reaches `TestManifest.udf`) rather than re-testing
# every udf normalization rule (see tests/test_manifest_udf.py for those).
# --------------------------------------------------------------------------------


def test_udf_block_is_accepted_and_normalized(tmp_path):
    text = """
name: udf-perf-case
udf:
  jar: java/UserDefinedFunctions/ReferenceUdf
  class: com.example.ReferenceUdf
  pipeline:
    - function: ReferenceUdfMarkProcessed
      args: ['$']

performance:
  input: performance/input.json
  run_sizes: [1000]
  correctness:
    mode: disabled
"""
    manifest_path, _ = _write(tmp_path, text)
    m = load_perf_manifest(manifest_path)
    assert m.test_manifest.op is None
    assert m.test_manifest.udf.jar == "java/UserDefinedFunctions/ReferenceUdf"
    assert m.test_manifest.udf.class_name == "com.example.ReferenceUdf"
    assert m.test_manifest.udf.pipeline[0].function == "ReferenceUdfMarkProcessed"
    assert m.test_manifest.module_ref == "java/UserDefinedFunctions/ReferenceUdf"


def test_udf_with_properties_raises_perf_manifest_error(tmp_path):
    text = """
name: udf-perf-case
properties:
  Foo: bar
udf:
  jar: java/UserDefinedFunctions/ReferenceUdf
  class: com.example.ReferenceUdf
  pipeline:
    - function: ReferenceUdfMarkProcessed
      args: ['$']

performance:
  input: performance/input.json
  run_sizes: [1000]
  correctness:
    mode: disabled
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(PerfManifestError, match="have no meaning"):
        load_perf_manifest(manifest_path)


def test_neither_op_nor_udf_raises_perf_manifest_error(tmp_path):
    text = """
name: neither-op-nor-udf-perf-case
performance:
  input: performance/input.json
  run_sizes: [1000]
  correctness:
    mode: disabled
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(PerfManifestError, match="exactly one of 'op'/'udf'"):
        load_perf_manifest(manifest_path)


def test_both_op_and_udf_raises_perf_manifest_error(tmp_path):
    text = """
name: both-op-and-udf-perf-case
op:
  jar: java/OpenProcessors/FakeOp
udf:
  jar: java/UserDefinedFunctions/ReferenceUdf
  class: com.example.ReferenceUdf
  pipeline:
    - function: ReferenceUdfMarkProcessed
      args: ['$']
performance:
  input: performance/input.json
  run_sizes: [1000]
  correctness:
    mode: disabled
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(PerfManifestError, match="exactly one of 'op'/'udf'"):
        load_perf_manifest(manifest_path)


# --------------------------------------------------------------------------------
# Real committed fixtures -- neither this loader nor perfmanifest.py checks file
# existence itself (by design: that's the runner's pre-flight job, PERF_SPEC.md
# §3), and pytest tests/ (the fast suite treated as primary verification)
# never actually loads scripts/integration/perf/**/test.yaml at all
# -- only inline YAML strings written to tmp_path. Without this, a renamed/
# deleted performance/customers.json, or a schema drift the inline-string tests
# don't happen to cover, surfaces only in a full `pytest` collection run or a
# real `pytest --perf` run against a JVM. This closes both gaps with one test.
# --------------------------------------------------------------------------------

# The project's perf library (SLT_PROJECT_ROOT; default this clone, whose library is empty).
_REAL_PERF_DIR = paths.project_root() / "scripts" / "integration" / "perf"
_REAL_PERF_TEST_YAMLS = sorted(_REAL_PERF_DIR.rglob("test.yaml")) if _REAL_PERF_DIR.is_dir() else []


@pytest.mark.skipif(not _REAL_PERF_TEST_YAMLS,
                    reason="no perf case library: scripts/integration/perf has no test.yaml")
@pytest.mark.parametrize(
    "manifest_path", _REAL_PERF_TEST_YAMLS,
    ids=[str(p.relative_to(_REAL_PERF_DIR)) for p in _REAL_PERF_TEST_YAMLS],
)
def test_real_committed_perf_fixture_loads_and_files_exist(manifest_path):
    m = load_perf_manifest(manifest_path)
    if m.test_manifest.source is None:
        assert m.performance.input_path.is_file(), f"missing performance.input: {m.performance.input_path}"
    else:
        # A reader replays `source.max_ticks` ticks, not a fixture, and the loader rejects a
        # `performance.input` on such a case -- so there is no file to look for here.
        assert m.performance.input_path is None, \
            f"a 'source:' perf case must declare no performance.input: {manifest_path}"
    if m.performance.expected_path is not None:
        assert m.performance.expected_path.is_file(), \
            f"missing performance.expected: {m.performance.expected_path}"


# --- `variants:` -- §85.1 axis 1 -------------------------------------------------------
#
# The perf tier ran on PostgreSQL alone, so it could not rank a change whose cost is
# engine-specific. The key is the regression tier's, normalized by the regression tier's
# own `_normalize_variants` and carrying its refusals verbatim -- each of those is a
# silent-wrong-answer trap (§98.1, §100.4), and a perf case has NO assertion to notice one:
# `correctness.mode` is `disabled` for every target case, so a variant that reads or seeds
# the wrong engine reports a throughput for a database nobody asked about and PASSES.

_VARIANTS_BLOCK = """
variants:
  postgres:
    db: postgres-target
    ddl: [ddl_pg.sql]
    tokens:
      V_URL: '${POSTGRES_URL}'
  oracle:
    db: oracle-target
    ddl: [ddl_ora.sql]
    tokens:
      V_URL: '${ORACLE_URL}'
"""

# ⚠ `correctness.mode` must be spelled out: the default is `sampled`, which REQUIRES
# `expected:`, and without it every test below fails inside _normalize_performance before it
# ever reaches the variants/matrix code it is about.
_UDF_PREFIX = """
name: fake-case
udf:
  jar: java/UserDefinedFunctions/ReferenceUdf
  class: com.example.ReferenceUdf
  pipeline:
    - function: ReferenceUdfMarkProcessed
"""

_PERF_BLOCK = """
performance:
  input: performance/events.json
  run_sizes: [10k]
  correctness:
    mode: disabled
"""


def test_variants_block_normalized(tmp_path):
    manifest_path, test_dir = _write(
        tmp_path, _REQUIRED_PREFIX + _VARIANTS_BLOCK + _PERF_BLOCK)
    variants = load_perf_manifest(manifest_path).test_manifest.variants
    assert [v.name for v in variants] == ["postgres", "oracle"]
    assert [v.db for v in variants] == ["postgres-target", "oracle-target"]
    assert variants[0].ddl == [FileSpec(db="postgres-target", file="ddl_pg.sql",
                                        path=test_dir / "ddl_pg.sql")]
    assert variants[1].tokens == {"V_URL": "${ORACLE_URL}"}


def test_variants_with_case_level_ddl_rejected(tmp_path):
    """One engine's DDL would run through every variant's connection."""
    manifest_path, _ = _write(
        tmp_path,
        _REQUIRED_PREFIX + "ddl: [{file: shared.sql, db: postgres-target}]\n"
        + _VARIANTS_BLOCK + _PERF_BLOCK)
    with pytest.raises(PerfManifestError) as e:
        load_perf_manifest(manifest_path)
    assert "puts its DDL on each VARIANT" in str(e.value)


def test_variants_with_case_level_seed_rejected(tmp_path):
    """It would be rendered with each variant's tokens and run through its OWN route --
    seeding one engine with another's table names, and failing loudly nowhere."""
    manifest_path, _ = _write(
        tmp_path,
        _REQUIRED_PREFIX + "seed: [{file: rows.sql, db: postgres-target}]\n"
        + _VARIANTS_BLOCK + _PERF_BLOCK)
    with pytest.raises(PerfManifestError) as e:
        load_perf_manifest(manifest_path)
    assert "cannot carry a case-level 'seed:'" in str(e.value)


def test_variants_with_udf_rejected(tmp_path):
    manifest_path, _ = _write(
        tmp_path,
        _UDF_PREFIX + _VARIANTS_BLOCK + _PERF_BLOCK)
    with pytest.raises(PerfManifestError) as e:
        load_perf_manifest(manifest_path)
    assert "no meaning for a 'udf:' case" in str(e.value)


def test_no_variants_leaves_the_list_empty(tmp_path):
    """A case without the key keeps working exactly as before -- one run, no fan-out."""
    manifest_path, _ = _write(tmp_path, MINIMAL_YAML)
    assert load_perf_manifest(manifest_path).test_manifest.variants == []


# --- `matrix:` -- the PROPERTY axis ----------------------------------------------------
#
# ⚠ It means the OPPOSITE of what it means in the regression tier, which is why it belongs
# here. There, N permutations share ONE `assert:`: the claim is that the knobs are NOT
# observable in the output. Here each permutation is its own measurement and the claim is
# that they ARE observable -- the difference between two numbers one property apart is the
# deliverable. `_normalize_matrix`'s own docstring says so: "What may legitimately differ
# between permutations is PERFORMANCE, which this tier does not measure and must not start
# to ... That is T3's."


def test_matrix_block_normalized(tmp_path):
    manifest_path, _ = _write(tmp_path, _REQUIRED_PREFIX + """
matrix:
  UseUpsert: ['true', 'false']
  CompactEvents: ['true', 'false']
""" + _PERF_BLOCK)
    assert load_perf_manifest(manifest_path).test_manifest.matrix == {
        "UseUpsert": ["true", "false"], "CompactEvents": ["true", "false"]}


def test_matrix_and_variants_compose_into_runs(tmp_path):
    """The two axes cross-product. Two engines x two knob values = four measurements from
    one authored case, which is the arithmetic that makes fan-out worth having."""
    manifest_path, _ = _write(
        tmp_path,
        _REQUIRED_PREFIX + _VARIANTS_BLOCK + "matrix:\n  UseUpsert: ['true', 'false']\n"
        + _PERF_BLOCK)
    runs = load_perf_manifest(manifest_path).test_manifest.runs()
    assert [(v.name, o) for v, o in runs] == [
        ("postgres", {"UseUpsert": "true"}),
        ("postgres", {"UseUpsert": "false"}),
        ("oracle", {"UseUpsert": "true"}),
        ("oracle", {"UseUpsert": "false"}),
    ]


def test_matrix_single_value_rejected(tmp_path):
    """A single value is not a matrix -- it belongs in `properties:`."""
    manifest_path, _ = _write(
        tmp_path, _REQUIRED_PREFIX + "matrix:\n  UseUpsert: 'true'\n" + _PERF_BLOCK)
    with pytest.raises(PerfManifestError) as e:
        load_perf_manifest(manifest_path)
    assert "must be a LIST of values" in str(e.value)


def test_matrix_with_udf_rejected(tmp_path):
    manifest_path, _ = _write(
        tmp_path,
        _UDF_PREFIX
        + "matrix:\n  UseUpsert: ['true', 'false']\n" + _PERF_BLOCK)
    with pytest.raises(PerfManifestError) as e:
        load_perf_manifest(manifest_path)
    assert "'matrix:' has no meaning for a 'udf:' case" in str(e.value)


def test_no_matrix_yields_one_run(tmp_path):
    """A case with neither axis runs exactly once, with exactly its own properties."""
    manifest_path, _ = _write(tmp_path, MINIMAL_YAML)
    assert load_perf_manifest(manifest_path).test_manifest.runs() == [(None, {})]


# ---- target: keys perf mode cannot honour (PERF_SPEC.md §4 "Writers") -----------------------
# PerformanceProcessor honours only target.distribution_id: it always attaches a position and
# never restarts. A key asking for anything else would silently measure the recovery path with
# no restart, so it is refused.

@pytest.mark.parametrize("target_extra, fragment", [
    ("  restart_after: 5\n", "'restart_after'"),
    ("  mid_run:\n    - {after: 1, db: postgres-target, sql: 'SELECT 1'}\n", "'mid_run'"),
    ("  positions: false\n", "'positions: false'"),
])
def test_perf_target_refuses_keys_perf_cannot_honour(tmp_path, target_extra, fragment):
    manifest_path, _ = _write(
        tmp_path,
        _REQUIRED_PREFIX + "target:\n  input: performance/events.json\n" + target_extra + _PERF_BLOCK)
    with pytest.raises(PerfManifestError) as e:
        load_perf_manifest(manifest_path)
    assert fragment in str(e.value)
    assert "perf mode does not support" in str(e.value)


def test_perf_target_input_is_optional_and_defaults_to_performance_input(tmp_path):
    manifest_path, _ = _write(tmp_path, _REQUIRED_PREFIX + "target:\n  distribution_id: d7\n" + _PERF_BLOCK)
    m = load_perf_manifest(manifest_path)
    assert m.test_manifest.target is not None
    assert str(m.test_manifest.target.input).endswith("performance/events.json")


def test_perf_target_positions_true_and_distribution_id_still_accepted(tmp_path):
    manifest_path, _ = _write(
        tmp_path,
        _REQUIRED_PREFIX + "target:\n  input: performance/events.json\n  positions: true\n"
        "  distribution_id: d3\n" + _PERF_BLOCK)
    assert load_perf_manifest(manifest_path).test_manifest.target is not None


# ---- performance.gcs_prune_prefix (PERF_SPEC.md §9) -----------------------------------------

def test_gcs_prune_prefix_is_carried_through(tmp_path):
    manifest_path, _ = _write(
        tmp_path,
        _REQUIRED_PREFIX + "requires: [gcs]\n" + _PERF_BLOCK + "  gcs_prune_prefix: '${TID}gcsbw-perf-multi'\n")
    assert load_perf_manifest(manifest_path).performance.gcs_prune_prefix == "${TID}gcsbw-perf-multi"


@pytest.mark.parametrize("value", ["''", "'/'", "5"])
def test_gcs_prune_prefix_must_be_a_non_empty_prefix(tmp_path, value):
    manifest_path, _ = _write(
        tmp_path, _REQUIRED_PREFIX + "requires: [gcs]\n" + _PERF_BLOCK + f"  gcs_prune_prefix: {value}\n")
    with pytest.raises(PerfManifestError, match="gcs_prune_prefix"):
        load_perf_manifest(manifest_path)


def test_gcs_prune_prefix_needs_gcs_in_requires(tmp_path):
    manifest_path, _ = _write(tmp_path, _REQUIRED_PREFIX + _PERF_BLOCK + "  gcs_prune_prefix: 'x/'\n")
    with pytest.raises(PerfManifestError, match="needs 'gcs'"):
        load_perf_manifest(manifest_path)
