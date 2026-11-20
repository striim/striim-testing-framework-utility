"""An unknown top-level key in a regression test.yaml is refused at load.

The regression loader read the keys it knew and ignored the rest, while every nested block and
the perf loader refused unknown keys. A misspelt `matrix:` ran the case once and reported one
pass for permutations that never ran; `requries:` dropped the service markers, `tiemout:` kept
the default timeout and `disable:` left a broken case enabled.
"""
from __future__ import annotations

import inspect
import re
import textwrap
from pathlib import Path

import pytest

from inttest import manifest as M, perfmanifest as PM

BODY = """\
name: typo
op: {jar: java/OpenProcessors/X}
assert:
  data: [{input: in.json, match: out.json}]
"""


def _case(tmp_path, extra: str = "") -> Path:
    d = tmp_path / "case"
    d.mkdir(exist_ok=True)
    for f in ("in.json", "out.json"):
        (d / f).write_text("[]")
    (d / "test.yaml").write_text(BODY + textwrap.dedent(extra))
    return d / "test.yaml"


@pytest.mark.parametrize("extra,key", [
    ("matrx: {UseUpsert: ['true', 'false']}\n", "matrx"),
    ("varients: {a: {db: postgres-target}}\n", "varients"),
    ("requries: [postgres]\n", "requries"),
    ("tiemout: 5\n", "tiemout"),
    ("disable: 'broken, see ticket'\n", "disable"),
], ids=["matrx", "varients", "requries", "tiemout", "disable"])
def test_an_unknown_top_level_key_is_refused_naming_it(tmp_path, extra, key):
    f = _case(tmp_path, extra)
    with pytest.raises(M.ManifestError) as e:
        M.load_manifest(f)
    msg = str(e.value)
    assert str(f) in msg and "unknown top-level key" in msg and repr(key) in msg
    assert "'matrix'" in msg and "'assert'" in msg        # the allowed keys are listed


def test_every_unknown_key_is_named_at_once(tmp_path):
    with pytest.raises(M.ManifestError, match=r"\['requries', 'tiemout'\]"):
        M.load_manifest(_case(tmp_path, "tiemout: 5\nrequries: [postgres]\n"))


def test_the_allowed_set_is_what_load_manifest_reads():
    # Derived from the code, so a key added to the loader without the set (or the reverse) fails.
    src = inspect.getsource(M.load_manifest)
    read = set(re.findall(r'raw\.get\("([a-z_]+)"', src))
    assert read == M._TEST_MANIFEST_KEYS | {"assert"}


def test_the_perf_loader_shares_the_set_with_performance_for_assert(tmp_path):
    assert PM._TEST_MANIFEST_KEYS == M._TEST_MANIFEST_KEYS
    d = tmp_path / "p"
    d.mkdir()
    (d / "in.json").write_text("[]")
    (d / "test.yaml").write_text(
        "name: typo\nop: {jar: java/OpenProcessors/X}\nmatrx: {A: ['1', '2']}\n"
        "performance: {input: in.json, run_sizes: [1], correctness: {mode: disabled}}\n")
    with pytest.raises(PM.PerfManifestError, match="unknown key.*'matrx'"):
        PM.load_perf_manifest(d / "test.yaml")


def test_a_perf_manifest_reads_setup(tmp_path):
    # `setup:` is a regression key the perf loader shares and reads (#128): `setup: bigtable:`
    # re-creates its tables at every per-iteration reset.
    d = tmp_path / "p"
    d.mkdir()
    (d / "in.json").write_text("[]")
    (d / "test.yaml").write_text(
        "name: p\nop: {jar: java/OpenProcessors/X}\nrequires: [bigtable]\n"
        "setup: {bigtable: [{table: 't_${TID}', families: {cf: null}}]}\n"
        "performance: {input: in.json, run_sizes: [1], correctness: {mode: disabled}}\n")
    m = PM.load_perf_manifest(d / "test.yaml")
    assert [s.table for s in m.test_manifest.bigtable_setup] == ["t_${TID}"]


def test_a_perf_manifest_is_refused_by_the_regression_loader_naming_performance(tmp_path):
    d = tmp_path / "p"
    d.mkdir()
    (d / "in.json").write_text("[]")
    (d / "test.yaml").write_text(
        "name: p\nop: {jar: java/OpenProcessors/X}\n"
        "performance: {input: in.json, run_sizes: [1], correctness: {mode: disabled}}\n")
    with pytest.raises(M.ManifestError, match="'performance'"):
        M.load_manifest(d / "test.yaml")


def test_every_documented_key_still_loads(tmp_path):
    m = M.load_manifest(_case(tmp_path, """\
        purpose: one line
        properties: {BatchSize: '10'}
        requires: [postgres]
        timeout: 30
        disabled: 'reason'
        matrix: {UseUpsert: ['true', 'false']}
        """))
    assert m.requires == ["postgres"] and m.timeout == 30 and m.disabled == "reason"
    assert m.matrix == {"UseUpsert": ["true", "false"]} and len(m.runs()) == 2


def test_the_guide_states_the_rule():
    doc = (Path(__file__).resolve().parents[3] / "docs" / "INTEGRATION-TESTS.md").read_text()
    assert "unknown top-level key" in doc
