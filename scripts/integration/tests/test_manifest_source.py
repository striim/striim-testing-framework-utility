"""Unit tests for inttest.manifest's `source:` block normalization -- pure
parsing tests, no Docker/Striim/pytest-plugin collection involved.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from inttest import harness
from inttest.manifest import ManifestError, SourceSpec, load_manifest, source_to_wire


def _write(tmp_path, text: str):
    test_dir = tmp_path / "some-test"
    test_dir.mkdir()
    manifest_path = test_dir / "test.yaml"
    manifest_path.write_text(text)
    return manifest_path, test_dir


BASE = """
name: reader-case
op:
  jar: java/OpenProcessors/ChangeReader
"""

# A reader case's assertion: a `match` fixture and no `input` (the loader rejects one).
DATA = BASE + """
assert:
  data:
    - match: expected/events.json
"""


def test_absent_source_normalizes_to_none(tmp_path):
    # An ordinary in-stream case: it is fed an input fixture and declares no `source:`.
    path, _ = _write(tmp_path, BASE + """
assert:
  data:
    - input: input/events.json
      match: expected/events.json
""")
    assert load_manifest(path).source is None
    assert source_to_wire(None) is None


def test_minimal_source_normalizes_and_wires(tmp_path):
    path, _ = _write(tmp_path, DATA + """
source:
  max_ticks: 5
""")
    source = load_manifest(path).source
    assert source.max_ticks == 5
    assert source.expect_events is None
    assert source_to_wire(source) == {"maxTicks": 5, "expectEvents": None,
                                      "seedWhen": "pre_start"}


def test_expect_events_rides_along_to_the_wire(tmp_path):
    path, _ = _write(tmp_path, DATA + """
source:
  max_ticks: 20
  expect_events: 12
""")
    assert source_to_wire(load_manifest(path).source) == {"maxTicks": 20, "expectEvents": 12,
                                                          "seedWhen": "pre_start"}


def test_a_reader_case_declares_no_input_fixture(tmp_path):
    path, test_dir = _write(tmp_path, BASE + """
source:
  max_ticks: 3
assert:
  data:
    - match: expected/events.json
""")
    manifest = load_manifest(path)
    entry = manifest.assert_.data[0]
    assert entry.input is None
    assert entry.input_path is None
    assert entry.match_path == (test_dir / "expected/events.json").resolve()


def test_an_input_fixture_on_a_reader_case_is_rejected_not_ignored(tmp_path):
    # The whole point: an input file here would never be read, and the author would be
    # asserting against events they believe they supplied.
    path, _ = _write(tmp_path, BASE + """
source:
  max_ticks: 3
assert:
  data:
    - input: input/events.json
      match: expected/events.json
""")
    with pytest.raises(ManifestError, match="has no meaning for a 'source:' case"):
        load_manifest(path)


def test_an_in_stream_case_still_requires_its_input(tmp_path):
    path, _ = _write(tmp_path, BASE + """
assert:
  data:
    - match: expected/events.json
""")
    with pytest.raises(ManifestError, match="needs a non-empty 'input'"):
        load_manifest(path)


@pytest.mark.parametrize("value", ["0", "-1", "'3'", "true", "1.5"])
def test_max_ticks_must_be_a_positive_integer(tmp_path, value):
    path, _ = _write(tmp_path, DATA + f"""
source:
  max_ticks: {value}
""")
    with pytest.raises(ManifestError, match="'source.max_ticks' is required"):
        load_manifest(path)


def test_max_ticks_is_required(tmp_path):
    path, _ = _write(tmp_path, DATA + """
source:
  expect_events: 4
""")
    with pytest.raises(ManifestError, match="'source.max_ticks' is required"):
        load_manifest(path)


@pytest.mark.parametrize("value", ["-1", "'4'", "true"])
def test_expect_events_must_be_a_non_negative_integer(tmp_path, value):
    path, _ = _write(tmp_path, DATA + f"""
source:
  max_ticks: 5
  expect_events: {value}
""")
    with pytest.raises(ManifestError, match="'source.expect_events' must be an integer"):
        load_manifest(path)


def test_expect_events_zero_names_the_alternative(tmp_path):
    # It would be met before the first tick, so the reader would never run -- and the case
    # that wants zero events is exactly the one that must tick.
    path, _ = _write(tmp_path, DATA + """
source:
  max_ticks: 5
  expect_events: 0
""")
    with pytest.raises(ManifestError, match="would stop before the first tick"):
        load_manifest(path)


def test_a_timeout_key_is_rejected_with_the_reason(tmp_path):
    # The one key an author is most likely to reach for, and the one the design forbids.
    path, _ = _write(tmp_path, DATA + """
source:
  max_ticks: 5
  timeout: 30
""")
    with pytest.raises(ManifestError, match="deliberately no timeout/wait key"):
        load_manifest(path)


def test_source_and_udf_cannot_both_be_present(tmp_path):
    path, _ = _write(tmp_path, """
name: reader-case
udf:
  jar: java/UserDefinedFunctions/ReferenceUdf
  class: com.example.ReferenceUdf
  pipeline:
    - function: trim
source:
  max_ticks: 3
assert:
  data:
    - match: expected/events.json
""")
    with pytest.raises(ManifestError, match="cannot both be present"):
        load_manifest(path)


def test_source_must_be_a_mapping_not_a_list(tmp_path):
    path, _ = _write(tmp_path, DATA + """
source:
  - max_ticks: 3
""")
    with pytest.raises(ManifestError, match="must be a single mapping"):
        load_manifest(path)


def test_a_smoke_only_case_cannot_declare_a_source(tmp_path):
    # It would be ticked zero times: plugin.py drives the operator only for assert.data.
    path, _ = _write(tmp_path, BASE + """
source:
  max_ticks: 3
assert:
  smoke: true
""")
    with pytest.raises(ManifestError, match="'source' needs 'assert.data'"):
        load_manifest(path)


# --- the wire, from the Python side ------------------------------------------------
#
# The Java half of this contract is pinned by IntegrationProcessorSourceTest's
# `aRequestJsonCarriesTheSourceBlockThroughRun`, which deserializes exactly the key
# spellings asserted here. Together they are the only guard against the two halves
# drifting: nothing else on either side reads a request's `source` block.

def _spec(max_ticks: int, expect_events: int | None = None) -> SourceSpec:
    return SourceSpec(max_ticks=max_ticks, expect_events=expect_events)


def _capturing_drive(monkeypatch, tmp_path) -> dict:
    """Runs `harness.drive` without a subprocess, capturing the request it would have sent."""
    captured: dict = {}

    def fake_run(argv, **kwargs):
        request = json.loads(Path(argv[-1]).read_text())
        captured["request"] = request
        captured["input"] = json.loads(Path(request["inputFile"]).read_text())
        Path(request["outputFile"]).write_text("[]")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(harness, "ensure_harness_jar", lambda: tmp_path / "harness.jar")
    monkeypatch.setattr(harness.subprocess, "run", fake_run)
    return captured


def test_the_wire_keys_are_exactly_the_ones_java_deserializes():
    wire = source_to_wire(_spec(7, 3))
    assert wire == {"maxTicks": 7, "expectEvents": 3, "seedWhen": "pre_start"}
    assert set(wire) == {"maxTicks", "expectEvents", "seedWhen"}


def test_drive_puts_the_source_block_and_an_empty_input_on_the_wire(monkeypatch, tmp_path):
    captured = _capturing_drive(monkeypatch, tmp_path)
    op_jar = tmp_path / "op.jar"
    op_jar.write_bytes(b"")

    harness.drive(op_jar, {}, None, source=_spec(7, 3))

    assert captured["request"]["source"] == {"maxTicks": 7, "expectEvents": 3,
                                             "seedWhen": "pre_start"}
    # A reader is not fed: `input_events=None` must still produce a readable, EMPTY inputFile,
    # which is what the Java side asserts on before it will tick anything.
    assert captured["input"] == []


def test_drive_without_a_source_still_sends_null_and_the_input_fixture(monkeypatch, tmp_path):
    captured = _capturing_drive(monkeypatch, tmp_path)
    op_jar = tmp_path / "op.jar"
    op_jar.write_bytes(b"")

    harness.drive(op_jar, {}, [{"metadata": {"TableName": "S.T"}}])

    # The in-stream path is unchanged: `"source": null` is what every existing test.yaml now
    # sends, and Java treats it identically to the field's absence.
    assert captured["request"]["source"] is None
    assert len(captured["input"]) == 1
