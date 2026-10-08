"""`assert.jmx:` -- load-time validation, the harness sidecar, and evaluation (plugin.jmx_mismatches).

Hermetic: no Docker, no JVM. The Java half is IntegrationProcessorJmxTest.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from inttest import harness, plugin
from inttest.manifest import JmxSpec, ManifestError, load_manifest

_BASE = """
name: jmx-case
op:
  jar: java/OpenProcessors/LookupOp
assert:
{assert_block}
"""

_DATA = """  data:
    - input: input/events.json
      match: expected/events.json
"""


def _load(tmp_path, assert_block: str, extra: str = ""):
    d = tmp_path / "case"
    d.mkdir(parents=True)
    p = d / "test.yaml"
    p.write_text(_BASE.format(assert_block=assert_block) + extra)
    return load_manifest(p)


def test_jmx_is_normalized(tmp_path):
    m = _load(tmp_path, _DATA + """  jmx:
    bean: LookupMBeanView
    attributes:
      Hits: 2
      Misses: {min: 1}
      HitRate: {min: 0, max: 100.0}
      GateRunning: false
""")
    assert m.assert_.jmx == JmxSpec(
        attributes={"Hits": 2, "Misses": {"min": 1}, "HitRate": {"min": 0, "max": 100.0},
                    "GateRunning": False},
        bean="LookupMBeanView")


def test_no_jmx_is_none(tmp_path):
    assert _load(tmp_path, _DATA).assert_.jmx is None


@pytest.mark.parametrize("jmx_block, needle", [
    ("  jmx:\n    attributes: {}\n", "non-empty mapping"),
    ("  jmx:\n    attributes: {Hits: 1}\n    atributes: {}\n", "unknown key"),
    ("  jmx: [Hits]\n", "must be a mapping"),
    ("  jmx:\n    attributes: {ProbeMaxOverrunMillis: 0}\n", "CLOCK"),
    ("  jmx:\n    attributes: {LastEventTime: 0}\n", "CLOCK"),
    ("  jmx:\n    attributes: {CommitLatency: 0}\n", "CLOCK"),
    ("  jmx:\n    attributes: {LastEventAge: 0}\n", "CLOCK"),      # same word list as the live tier
    ("  jmx:\n    attributes: {FlushDuration: 0}\n", "CLOCK"),
    ("  jmx:\n    attributes: {Hits: {min: 3, max: 1}}\n", "min > max"),
    ("  jmx:\n    attributes: {Hits: {exactly: 1}}\n", "'min' and/or 'max'"),
    ("  jmx:\n    attributes: {Hits: {min: true}}\n", "must be a number"),
    ("  jmx:\n    attributes: {Hits: [1]}\n", "must be a number, true/false"),
    ("  jmx:\n    attributes: {Hits: 1}\n    bean: ''\n", "class name"),
])
def test_bad_jmx_blocks_are_refused(tmp_path, jmx_block, needle):
    with pytest.raises(ManifestError, match=needle):
        _load(tmp_path, _DATA + jmx_block)


def test_a_count_named_timeouts_is_not_a_clock(tmp_path):
    m = _load(tmp_path, _DATA + "  jmx:\n    attributes: {GateTimeoutsPassThrough: 0}\n")
    assert m.assert_.jmx.attributes == {"GateTimeoutsPassThrough": 0}


def test_jmx_alone_is_refused(tmp_path):
    # It supplements `data`: a case without a data drive never drives the op, so the snapshot
    # would not exist.
    with pytest.raises(ManifestError, match="supplements 'assert.data'"):
        _load(tmp_path, "  jmx:\n    attributes: {Hits: 1}\n")
    with pytest.raises(ManifestError, match="supplements 'assert.data'"):
        _load(tmp_path / "s", "  smoke: true\n  jmx:\n    attributes: {Hits: 1}\n")


def test_jmx_with_expect_error_is_refused(tmp_path):
    with pytest.raises(ManifestError, match="'jmx'"):
        _load(tmp_path, _DATA + "  expect_error: 'refused the ConfigFile'\n"
                                "  jmx:\n    attributes: {Hits: 1}\n")


def test_jmx_is_refused_for_a_source_case(tmp_path):
    yaml_text = """
name: jmx-source
op:
  jar: java/OpenProcessors/ChangeReader
source:
  max_ticks: 1
assert:
  data:
    - match: expected/events.json
  jmx:
    attributes: {Hits: 1}
"""
    d = tmp_path / "case"
    d.mkdir()
    (d / "test.yaml").write_text(yaml_text)
    with pytest.raises(ManifestError, match="'op:' cases only"):
        load_manifest(d / "test.yaml")


# --- evaluation ---------------------------------------------------------------------------------

_SNAPSHOT = {
    "objectName": 'com.example:type=LookupOp,name="inttest.source"',
    "attributes": {"Hits": 2, "Misses": 1, "HitRate": 66.66, "GateRunning": False},
}


def test_matching_snapshot_has_no_mismatches():
    spec = JmxSpec(attributes={"Hits": 2, "Misses": {"min": 1, "max": 1},
                               "HitRate": {"min": 50}, "GateRunning": False})
    assert plugin.jmx_mismatches(spec, _SNAPSHOT) == []


def test_mismatches_name_expected_and_actual():
    spec = JmxSpec(attributes={"Hits": 3, "Misses": {"max": 0}, "GateRunning": True})
    out = plugin.jmx_mismatches(spec, _SNAPSHOT)
    assert "Hits: expected 3, got 2" in out
    assert "Misses: expected {'max': 0}, got 1" in out
    assert "GateRunning: expected True, got False" in out


def test_bool_and_int_do_not_compare_equal():
    # Python's True == 1 would otherwise let a boolean expectation pass on a count of one.
    snapshot = {"attributes": {"Hits": 1, "GateRunning": 0}}
    out = plugin.jmx_mismatches(JmxSpec(attributes={"Hits": True, "GateRunning": False}), snapshot)
    assert len(out) == 2


def test_a_missing_attribute_fails():
    out = plugin.jmx_mismatches(JmxSpec(attributes={"Hitz": 2}), _SNAPSHOT)
    assert out and out[0].startswith("Hitz: not an attribute")


def test_a_throwing_getter_fails():
    snapshot = {"attributes": {}, "attributeErrors": {"Hits": "java.lang.IllegalStateException"}}
    out = plugin.jmx_mismatches(JmxSpec(attributes={"Hits": 0}), snapshot)
    assert out == ["Hits: getter threw java.lang.IllegalStateException"]


@pytest.mark.parametrize("snapshot", [{"error": "no MBean class found"}, {}, None])
def test_an_unbuildable_bean_fails_never_passes_vacuously(snapshot):
    out = plugin.jmx_mismatches(JmxSpec(attributes={"Hits": {"min": 0}}), snapshot)
    assert out, "an absent or failed snapshot must fail the assertion"


# --- harness sidecar ----------------------------------------------------------------------------

def _fake_run(write_jmx: bool, seen: dict):
    def run(argv, **kwargs):
        req = json.loads(Path(argv[-1]).read_text())
        seen.update(req)
        Path(req["outputFile"]).write_text("[]")
        if write_jmx and "jmx" in req:
            Path(req["jmx"]["outputFile"]).write_text(json.dumps(_SNAPSHOT))
        res = mock.MagicMock()
        res.returncode = 0
        return res
    return run


def _drive(tmp_path, *, jmx, write_jmx=True):
    op_jar = tmp_path / "op.jar"
    op_jar.write_text("")
    seen: dict = {}
    sink: list = []
    with mock.patch("inttest.harness.ensure_harness_jar", return_value=tmp_path / "h.jar"), \
         mock.patch("inttest.harness._resolve_java", return_value="java"), \
         mock.patch("subprocess.run", side_effect=_fake_run(write_jmx, seen)):
        emitted = harness.drive(op_jar, {}, [], jmx=jmx, jmx_sink=sink)
    return emitted, seen, sink


def test_harness_sends_jmx_and_reads_the_sidecar(tmp_path):
    emitted, req, sink = _drive(tmp_path, jmx=JmxSpec(attributes={"Hits": 1}, bean="V"))
    assert emitted == []                       # the return value keeps its shape
    assert req["jmx"]["bean"] == "V" and req["jmx"]["outputFile"].endswith("jmx.json")
    assert sink == [_SNAPSHOT]


def test_harness_leaves_the_request_unchanged_without_jmx(tmp_path):
    _, req, sink = _drive(tmp_path, jmx=None)
    assert "jmx" not in req and sink == []


def test_harness_reports_a_missing_sidecar_as_an_error(tmp_path):
    _, _, sink = _drive(tmp_path, jmx=JmxSpec(attributes={"Hits": 1}), write_jmx=False)
    assert "error" in sink[0]


def test_jmx_is_wired_into_the_data_drive():
    src = Path(plugin.__file__).read_text()
    assert "jmx=m.assert_.jmx, jmx_sink=jmx_sink" in src
    assert "jmx_mismatches(m.assert_.jmx, snapshot)" in src
