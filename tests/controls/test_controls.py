"""Hermetic contract tests for the negative and lifecycle controls (tests/controls/controls.py)."""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from livetest import canon, exactdata, lifecycle
from livetest.assertions import AssertionFailed
from livetest.lifecycle import LifecycleError, parse_spec


ROOT = Path(__file__).resolve().parents[2]
CONTROL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts" / "live" / "tests" / "lifecycle"))

from exec_harness import _agree, run_case  # noqa: E402, F401

# run_case is a pytester fixture; the root tests/ suite does not load pytester otherwise.
pytest_plugins = ["pytester"]


def _module():
    spec = importlib.util.spec_from_file_location("framework_controls", CONTROL_DIR / "controls.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cases(tmp_path: Path) -> Path:
    target = tmp_path / "cases"
    shutil.copytree(ROOT / "samples" / "live", target)
    assert (target / "04-lifecycle-check" / "changes.sql").is_file()
    return target


def _comparison(*, kind="data", order="any", error=None, sequence_only=False, duplicate=False):
    """Produce the evidence record through the real exact-data classification path."""
    declaration = canon.Declaration(order=order)
    # Golden CSV values reach canon as strings; keep the observed fixture on the
    # same inferred type so a reversal is a true same-multiset sequence failure.
    expected = [{"id": "1"}, {"id": "2"}]
    actual = list(reversed(expected)) if sequence_only else (
        [{"id": "1"}, {"id": "2"}, {"id": "2"}] if duplicate else [{"id": "1"}, {"id": "3"}]
    )

    def evaluate(_remaining):
        if error is not None:
            raise canon.CanonError(error, "control fixture")
        return canon.compare(expected, actual, declaration)

    prepared = exactdata._Prepared(
        kind, 0, {}, declaration, "fixture-target", None, True, evaluate,
        source="fixture.csv", template_sha="sha256:" + "0" * 64,
    )
    collector = []
    with pytest.raises(AssertionFailed):
        exactdata._drive(
            kind, [prepared], SimpleNamespace(timeout=1), lifecycle=True,
            collector=collector, status_probe=None, poll=0, clock=None,
        )
    assert len(collector) == 1
    return collector[0]


_LATE_ROW_TOKENS = {
    "PG_SOURCE_SCHEMA": "qasource", "PG_TARGET_SCHEMA": "qatarget", "TID": "tlate_",
}


class _LateRowClock:
    def __init__(self):
        self.t = 1000.0
        self.n = 0

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += max(float(seconds), 0.001)

    def wall(self):
        self.n += 1
        return f"2026-09-21T00:00:00.{self.n:03d}Z"


class _LateRowProbe:
    def __init__(self, values):
        self.values = iter(values)
        self.last = values[-1]

    def query(self, _sql, _params=None, remaining=10.0):
        del remaining
        self.last = next(self.values, self.last)
        return [(self.last,)]


def _real_late_row_completion(
    monkeypatch, *, source_values=(3, 4), target_values=(3, 3), acknowledge_before_start=False,
):
    """Run the actual source-count completion witness against a fake count edge."""
    clock = _LateRowClock()
    source = _LateRowProbe(source_values)
    target = _LateRowProbe(target_values)
    state = {"source": None, "injected": False, "ack_at": None}

    def source_count():
        value = source.query("", None)[0][0]
        state["source"] = value
        return value

    def target_count():
        value = target.query("", None)[0][0]
        if value == 3 and state["source"] == 3 and not state["injected"]:
            state["injected"] = True
            state["ack_at"] = clock.wall()
        return value

    class Probe:
        def __init__(self, fn):
            self.fn = fn

        def query(self, _sql, _params=None, remaining=10.0):
            del remaining
            return [(self.fn(),)]

    # The target callback needs to observe the source value from the same count pair.  The
    # lifecycle implementation always queries source before target, so this small edge is enough
    # to place the row at the first 3/3 observation.
    source_probe = Probe(source_count)
    target_probe = Probe(target_count)
    monkeypatch.setattr(lifecycle, "make_probe", lambda admin: admin.probe)
    monkeypatch.setattr(lifecycle, "status_bounded", lambda client, app, remaining: "RUNNING")
    admins = {
        "postgres-source": {"admin": SimpleNamespace(probe=source_probe)},
        "postgres-target": {"admin": SimpleNamespace(probe=target_probe)},
    }
    block = {
        "version": 1,
        "mode": "initial-load",
        "sink": "db",
        "readiness": {
            "kind": "baseline-landed",
            "source": {"db": "postgres-source", "table": "${PG_SOURCE_SCHEMA}.${TID}src"},
            "target": {"db": "postgres-target", "table": "${PG_TARGET_SCHEMA}.${TID}tgt"},
        },
        "completion": {
            "kind": "source-count",
            "source": {"db": "postgres-source", "table": "${PG_SOURCE_SCHEMA}.${TID}src"},
            "target": {"db": "postgres-target", "table": "${PG_TARGET_SCHEMA}.${TID}tgt"},
        },
        "stability": "2s",
        "deadlines": {"readiness": 5, "completion": 5},
    }
    spec = parse_spec(block, "late-row")
    lifecycle_state = lifecycle.State("initial-load")
    lifecycle_state.baseline = {"count": 3}
    if acknowledge_before_start:
        state["injected"] = True
        state["ack_at"] = clock.wall()
    try:
        lifecycle.complete(
            lifecycle_state, spec, client=SimpleNamespace(), apps=["NS.late-row"], admins=admins,
            tokens=_LATE_ROW_TOKENS, ident=None, read_files=lambda path, **kwargs: "",
            owned=lambda *args: True, clock=clock,
        )
    except LifecycleError as exc:
        failure = str(exc)
    else:
        raise AssertionError("the fake late-row edge must fail completion")
    assert state["injected"] and state["ack_at"] is not None
    completion = lifecycle_state.completion
    if acknowledge_before_start:
        assert state["ack_at"] < completion["startedAt"]
    return {
        "envelope": {
            "evidenceVersion": 2,
            "kind": "case",
            "run": {"status": "failed", "failure": failure, "qualifies": False},
            "data": {"comparisons": []},
            "lifecycle": {
                "baseline": {"count": 3}, "completion": completion,
                "stability": lifecycle_state.stability,
            },
        },
        "completion": completion,
        "ack_at": state["ack_at"],
    }


def _run_through_default(
    module,
    monkeypatch,
    spec,
    cases,
    output,
    control_id,
    envelope,
    junit_text,
    *,
    subject_exit=1,
    outcome="failed",
    events=(),
    ledger_count=0,
    recovery_exit=0,
    control_tokens=None,
    control_ack_at=None,
):
    calls = []

    def subprocess_edge(argv, **kwargs):
        calls.append(list(argv))
        if "livetest.ownership" in argv:
            return SimpleNamespace(returncode=recovery_exit, stdout=b"replay\n", stderr=b"")
        state = output / "state" / "run" / "live"
        state.mkdir(parents=True, exist_ok=True)
        counts = {
            "failed": 'failures="1" errors="0" skipped="0"',
            "passed": 'failures="0" errors="0" skipped="0"',
            "skipped": 'failures="0" errors="0" skipped="1"',
        }
        detail = f"<failure>{junit_text}</failure>" if outcome == "failed" else \
            f"<skipped>{junit_text}</skipped>" if outcome == "skipped" else ""
        (state / "junit.xml").write_text(
            f'<testsuite tests="1" {counts[outcome]}>'
            f'<testcase name="control">{detail}</testcase></testsuite>\n'
        )
        evidence = state / "evidence" / "case" / "run" / "evidence.json"
        evidence.parent.mkdir(parents=True)
        evidence.write_text(json.dumps(envelope) + "\n")
        if events:
            (output / "control-events.json").write_text(json.dumps(list(events)) + "\n")
        for index in range(ledger_count):
            ledger = output / "state" / "lifecycle" / "ledgers" / f"ledger-{index}.json"
            ledger.parent.mkdir(parents=True, exist_ok=True)
            ledger.write_text("{}\n")
        return SimpleNamespace(returncode=subject_exit, stdout=b"", stderr=b"")

    class FakeProcess:
        def __init__(self, argv, **kwargs):
            result = subprocess_edge(argv, **kwargs)
            self.returncode = result.returncode

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(module.subprocess, "run", subprocess_edge)
    monkeypatch.setattr(module.subprocess, "Popen", FakeProcess)
    def fake_control_phase(*args, **kwargs):
        process = args[1] if len(args) > 1 else kwargs.get("process")
        if control_id == "late-row" and events and process is not None:
            process._slt_late_row_tokens = control_tokens or dict(_LATE_ROW_TOKENS)
            process._slt_late_row_ack_at = control_ack_at or "2026-09-21T00:00:00.500Z"
        return (), None

    monkeypatch.setattr(module, "_control_phase", fake_control_phase)
    result = module.run_control(spec, control_id, cases, output)
    return result, calls


def test_control_mutations_do_not_change_positive_assets(tmp_path):
    module = _module()
    cases = _cases(tmp_path)
    before = module.tree_sha256(cases)
    spec = module.load_spec(CONTROL_DIR / "controls.json")

    for control in spec["controls"]:
        output = tmp_path / "out" / control["id"]
        copied = output / "work" / "cases" / "live"
        shutil.copytree(cases, copied)
        plan = module._plan(control, output / "work", copied)
        assert plan.is_file()
        assert (copied / "04-lifecycle-check" / "changes.sql").is_file()
        assert module.tree_sha256(cases) == before


def test_controls_require_named_failure_and_nonzero_exit(tmp_path, monkeypatch):
    module = _module()
    cases = _cases(tmp_path)
    spec = module.load_spec(CONTROL_DIR / "controls.json")
    mismatch = {
        "evidenceVersion": 2, "kind": "case", "run": {"qualifies": False},
        "data": {"comparisons": [_comparison()]}, "lifecycle": {},
    }

    green_subject = tmp_path / "green-subject"
    result, _ = _run_through_default(
        module, monkeypatch, spec, cases, green_subject, "wrong-golden", mismatch,
        "comparison unequal", subject_exit=0,
    )
    assert result != 0
    assert json.loads((green_subject / "control-result.json").read_text())["wrapperExit"] != 0

    wrong_reason = tmp_path / "wrong-reason"
    invalid = {**mismatch, "data": {"comparisons": [_comparison(error="exact-read-error")]}}
    result, _ = _run_through_default(
        module, monkeypatch, spec, cases, wrong_reason, "wrong-golden", invalid, "exact read failed",
    )
    assert result != 0
    record = json.loads((wrong_reason / "control-result.json").read_text())
    assert record["failure"] is None
    assert record["observedFailure"] == "unexpected-subject-failure" and record["wrapperExit"] != 0


def test_missing_control_or_unexpected_skip_is_failure(tmp_path, monkeypatch):
    module = _module()
    raw = json.loads((CONTROL_DIR / "controls.json").read_text())
    raw["controls"] = raw["controls"][:-1]
    incomplete = tmp_path / "incomplete.json"
    incomplete.write_text(json.dumps(raw))
    with pytest.raises(module.ControlError, match="missing required control"):
        module.load_spec(incomplete)

    cases = _cases(tmp_path)
    spec = module.load_spec(CONTROL_DIR / "controls.json")
    output = tmp_path / "unexpected-skip"
    mismatch = {
        "evidenceVersion": 2, "kind": "case", "run": {"qualifies": False},
        "data": {"comparisons": [_comparison()]}, "lifecycle": {},
    }
    result, _ = _run_through_default(
        module, monkeypatch, spec, cases, output, "wrong-golden", mismatch,
        "unexpected skip", outcome="skipped",
    )
    assert result != 0
    result = json.loads((output / "control-result.json").read_text())
    assert result["junit"]["outcome"] == "skipped" and result["wrapperExit"] != 0


def test_late_row_requires_observed_injection_before_stability_end(tmp_path, monkeypatch):
    module = _module()
    cases = _cases(tmp_path)
    spec = module.load_spec(CONTROL_DIR / "controls.json")
    real = _real_late_row_completion(monkeypatch)

    missing = tmp_path / "missing-event"
    result, _ = _run_through_default(
        module, monkeypatch, spec, cases, missing, "late-row", real["envelope"],
        "lifecycle completion source-count failed: stability-lost",
        control_tokens=_LATE_ROW_TOKENS, control_ack_at=real["ack_at"],
    )
    assert result != 0
    copied_plan = json.loads((missing / "work" / "control-plan.json").read_text())
    assert copied_plan["injectAfterEvent"] == "baseline-counts-reached"
    assert copied_plan["ackEvent"] == "late-row-injected-before-stability-end"
    assert "sleep" not in json.dumps(copied_plan).lower()

    observed = tmp_path / "observed-event"
    result, _ = _run_through_default(
        module, monkeypatch, spec, cases, observed, "late-row", real["envelope"],
        "lifecycle completion source-count failed: stability-lost",
        events=("late-row-injected-before-stability-end",),
        control_tokens=_LATE_ROW_TOKENS, control_ack_at=real["ack_at"],
    )
    assert result == 0


def test_late_row_deadline_constant_source_ahead_is_accepted(tmp_path, monkeypatch):
    module = _module()
    cases = _cases(tmp_path)
    spec = module.load_spec(CONTROL_DIR / "controls.json")
    real = _real_late_row_completion(
        monkeypatch, source_values=(4,), target_values=(3,), acknowledge_before_start=True,
    )
    output = tmp_path / "late-row-deadline-accepted"
    result, _ = _run_through_default(
        module, monkeypatch, spec, cases, output, "late-row", real["envelope"],
        "lifecycle completion source-count failed: deadline",
        events=("late-row-injected-before-stability-end",),
        control_tokens=_LATE_ROW_TOKENS, control_ack_at=real["ack_at"],
    )

    record = json.loads((output / "control-result.json").read_text())
    assert real["completion"]["reason"] == "deadline"
    assert result == 0 and record["failure"] == "late-row-source-ahead-of-target"


def test_late_row_deadline_extra_source_rows_are_rejected(tmp_path, monkeypatch):
    module = _module()
    cases = _cases(tmp_path)
    spec = module.load_spec(CONTROL_DIR / "controls.json")
    real = _real_late_row_completion(
        monkeypatch, source_values=(5,), target_values=(3,), acknowledge_before_start=True,
    )
    output = tmp_path / "late-row-deadline-extra-source"
    result, _ = _run_through_default(
        module, monkeypatch, spec, cases, output, "late-row", real["envelope"],
        "lifecycle completion source-count failed: deadline",
        events=("late-row-injected-before-stability-end",),
        control_tokens=_LATE_ROW_TOKENS, control_ack_at=real["ack_at"],
    )

    record = json.loads((output / "control-result.json").read_text())
    assert real["completion"]["reason"] == "deadline"
    assert result != 0 and record["failure"] is None
    assert record["observedFailure"] == "late-row-source-ahead-of-target-not-observed"


def _write_real_control_subject(path: Path):
    path.write_text(
        "import json, sys, time\n"
        "from datetime import datetime, timezone\n"
        "from pathlib import Path\n"
        "output = Path(sys.argv[1])\n"
        "wait_for_injection = sys.argv[2] == 'wait'\n"
        "marker = output / 'late-row-injected.flag'\n"
        "if wait_for_injection:\n"
        "    deadline = time.time() + 5\n"
        "    while not marker.exists() and time.time() < deadline:\n"
        "        time.sleep(0.01)\n"
        "injected = marker.exists()\n"
        "state = output / 'state' / 'run' / 'live'\n"
        "state.mkdir(parents=True, exist_ok=True)\n"
        "sha_a = 'sha256:' + 'a' * 64\n"
        "sha_b = 'sha256:' + 'b' * 64\n"
        "if injected:\n"
        "    ended_at = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')\n"
        "    lifecycle = {'completion': {'kind': 'source-count',\n"
        "        'condition': '\"qatarget\".\"tlate_tgt\" count == \"qasource\".\"tlate_src\" count, both > 0',\n"
        "        'reason': 'stability-lost',\n"
        "        'endedAt': ended_at, 'observations': [{'value': {'source': 4, 'target': 3}}]},\n"
        "        'baseline': {'count': 3},\n"
        "        'stability': {'seconds': 2, 'held': False, 'value': {'source': 4, 'target': 3}}}\n"
        "    run_failure = 'lifecycle completion source-count failed: stability-lost'\n"
        "    junit = '<testsuite tests=\"1\" failures=\"1\" errors=\"0\" skipped=\"0\"><testcase>'\n"
        "    junit += '<failure>lifecycle completion source-count failed: stability-lost</failure></testcase></testsuite>\\n'\n"
        "    exit_code = 1\n"
        "else:\n"
        "    lifecycle = {'completion': {'kind': 'source-count',\n"
        "        'condition': '\"qatarget\".\"tlate_tgt\" count == \"qasource\".\"tlate_src\" count, both > 0',\n"
        "        'reason': 'satisfied',\n"
        "        'observations': [{'value': {'source': 3, 'target': 3}}]}}\n"
        "    run_failure = None\n"
        "    junit = '<testsuite tests=\"1\" failures=\"0\" errors=\"0\" skipped=\"0\"><testcase>'\n"
        "    junit += '</testcase></testsuite>\\n'\n"
        "    exit_code = 0\n"
        "run = {'status': 'failed' if injected else 'passed', 'qualifies': False}\n"
        "if run_failure:\n"
        "    run['failure'] = run_failure\n"
        "(state / 'junit.xml').write_text(junit)\n"
        "(state / 'evidence.json').write_text(json.dumps({'evidenceVersion': 2, 'kind': 'case',\n"
        "    'run': run, 'data': {'comparisons': []}, 'lifecycle': lifecycle}))\n"
        "raise SystemExit(exit_code)\n"
    )


class _FakeLateRowEdge:
    def __init__(self, enabled):
        self.enabled = enabled
        self.called = False
        self.tokens = dict(_LATE_ROW_TOKENS)

    def inject_late_row(self, process):
        self.called = True
        if not self.enabled:
            return False
        assert process.poll() is None
        invocation_output = Path(process._slt_control_output)
        (invocation_output / 'late-row-injected.flag').write_text('inserted\\n')
        return True


def test_late_row_real_entry_point_records_missing_injection_as_control_failure(tmp_path, monkeypatch):
    module = _module()
    cases = _cases(tmp_path)
    spec = module.load_spec(CONTROL_DIR / 'controls.json')
    subject = tmp_path / 'subject.py'
    _write_real_control_subject(subject)
    edge = _FakeLateRowEdge(enabled=False)
    monkeypatch.setattr(module, '_subject_command',
                        lambda invocation, project: [sys.executable, str(subject),
                                                     str(invocation.output), 'now'])
    monkeypatch.setattr(module, '_late_row_edge', lambda _invocation, _env=None: edge)
    output = tmp_path / 'late-row-no-injection'

    rc = module.main([
        'run', '--spec', str(CONTROL_DIR / 'controls.json'), '--id', 'late-row',
        '--cases', str(cases), '--output', str(output),
    ])

    result = json.loads((output / 'control-result.json').read_text())
    assert rc == 1 and result['wrapperExit'] == 1
    assert edge.called and result['subjectExit'] == 0 and result['events'] == []
    assert result['failure'] is None
    assert result['observedFailure'] == 'late-row-injection-acknowledgement-failed'
    assert 'injection event was not observed' in (output / 'control-errors.log').read_text()


def test_late_row_real_entry_point_injects_against_running_subject(tmp_path, monkeypatch):
    module = _module()
    cases = _cases(tmp_path)
    spec = module.load_spec(CONTROL_DIR / 'controls.json')
    subject = tmp_path / 'subject.py'
    _write_real_control_subject(subject)
    edge = _FakeLateRowEdge(enabled=True)
    monkeypatch.setattr(module, '_subject_command',
                        lambda invocation, project: [sys.executable, str(subject),
                                                     str(invocation.output), 'wait'])
    monkeypatch.setattr(module, '_late_row_edge', lambda _invocation, _env=None: edge)
    output = tmp_path / 'late-row-injected'

    rc = module.main([
        'run', '--spec', str(CONTROL_DIR / 'controls.json'), '--id', 'late-row',
        '--cases', str(cases), '--output', str(output),
    ])

    result = json.loads((output / 'control-result.json').read_text())
    assert rc == 0 and result['wrapperExit'] == 0
    assert edge.called and result['subjectExit'] == 1
    assert result['failure'] == 'late-row-source-ahead-of-target'
    assert result['events'] == ['late-row-injected-before-stability-end']


def test_late_row_subject_pass_is_a_named_control_failure(tmp_path, monkeypatch):
    module = _module()
    cases = _cases(tmp_path)
    spec = module.load_spec(CONTROL_DIR / 'controls.json')
    output = tmp_path / 'late-row-subject-passed'
    real = _real_late_row_completion(monkeypatch)
    envelope = real["envelope"]
    envelope["run"] = {"status": "passed", "qualifies": True}
    result, _ = _run_through_default(
        module, monkeypatch, spec, cases, output, 'late-row',
        envelope, 'subject passed',
        subject_exit=0, outcome='passed', events=('late-row-injected-before-stability-end',),
        control_tokens=_LATE_ROW_TOKENS, control_ack_at=real["ack_at"],
    )

    record = json.loads((output / 'control-result.json').read_text())
    assert result != 0 and record['failure'] is None
    assert record['observedFailure'] == 'late-row-subject-did-not-fail'


def test_late_row_early_divergence_is_a_named_control_failure(tmp_path, monkeypatch):
    module = _module()
    cases = _cases(tmp_path)
    spec = module.load_spec(CONTROL_DIR / 'controls.json')
    output = tmp_path / 'late-row-wrong-failure'
    real = _real_late_row_completion(monkeypatch, source_values=(3, 3, 4), target_values=(2, 3, 2))
    result, _ = _run_through_default(
        module, monkeypatch, spec, cases, output, 'late-row',
        real["envelope"], 'wrong final divergence',
        events=('late-row-injected-before-stability-end',),
        control_tokens=_LATE_ROW_TOKENS, control_ack_at=real["ack_at"],
    )

    record = json.loads((output / 'control-result.json').read_text())
    assert result != 0 and record['failure'] is None
    assert record['observedFailure'] == 'late-row-source-ahead-of-target-not-observed'


def test_late_row_wrong_table_is_a_named_control_failure(tmp_path, monkeypatch):
    module = _module()
    cases = _cases(tmp_path)
    spec = module.load_spec(CONTROL_DIR / 'controls.json')
    output = tmp_path / 'late-row-wrong-table'
    real = _real_late_row_completion(monkeypatch)
    envelope = real["envelope"]
    envelope["lifecycle"]["completion"]["condition"] = (
        '"qatarget"."wrong_tgt" count == "qasource"."tlate_src" count, both > 0'
    )
    result, _ = _run_through_default(
        module, monkeypatch, spec, cases, output, 'late-row', envelope, 'wrong table names',
        events=('late-row-injected-before-stability-end',),
        control_tokens=_LATE_ROW_TOKENS, control_ack_at=real["ack_at"],
    )

    record = json.loads((output / 'control-result.json').read_text())
    assert result != 0 and record['failure'] is None
    assert record['observedFailure'] == 'late-row-source-ahead-of-target-not-observed'


def test_late_row_ack_after_completion_end_is_a_named_control_failure(tmp_path, monkeypatch):
    module = _module()
    cases = _cases(tmp_path)
    spec = module.load_spec(CONTROL_DIR / 'controls.json')
    output = tmp_path / 'late-row-late-ack'
    real = _real_late_row_completion(monkeypatch)
    ended_at = real["completion"]["endedAt"]
    result, _ = _run_through_default(
        module, monkeypatch, spec, cases, output, 'late-row', real["envelope"], 'late ack',
        events=('late-row-injected-before-stability-end',),
        control_tokens=_LATE_ROW_TOKENS, control_ack_at=ended_at,
    )

    record = json.loads((output / 'control-result.json').read_text())
    assert result != 0 and record['failure'] is None
    assert record['observedFailure'] == 'late-row-source-ahead-of-target-not-observed'


def test_late_row_ack_after_subject_exit_is_rejected(tmp_path, monkeypatch):
    module = _module()
    cases = _cases(tmp_path)
    spec = module.load_spec(CONTROL_DIR / 'controls.json')
    control = next(row for row in spec['controls'] if row['id'] == 'late-row')
    copied = tmp_path / 'late-row' / 'work' / 'cases' / 'live'
    shutil.copytree(cases, copied)
    plan = module._plan(control, tmp_path / 'late-row' / 'work', copied)
    invocation = module.Invocation(control, cases, copied, tmp_path / 'late-row', plan)

    class Edge:
        def inject_late_row(self, _process):
            return True, None

    monkeypatch.setattr(module, '_late_row_edge', lambda _invocation, _env=None: Edge())
    events, reason = module._control_phase(invocation, SimpleNamespace(poll=lambda: 1), {})

    assert events == ()
    assert reason == 'late-row injection acknowledgement was observed after subject exit'


def test_postgres_late_row_edge_waits_for_baseline_and_renders_run_owned_sql(tmp_path, monkeypatch):
    module = _module()
    cases = _cases(tmp_path)
    spec = module.load_spec(CONTROL_DIR / 'controls.json')
    control = next(row for row in spec['controls'] if row['id'] == 'late-row')
    copied = tmp_path / 'edge' / 'work' / 'cases' / 'live'
    shutil.copytree(cases, copied)
    plan = module._plan(control, tmp_path / 'edge' / 'work', copied)
    invocation = module.Invocation(control, cases, copied, tmp_path / 'edge', plan)
    edge = module._PostgresControlEdge.__new__(module._PostgresControlEdge)
    edge.invocation = invocation
    edge.env = {'SLT_CONTROL_PHASE_TIMEOUT': '1'}
    edge.tokens = {
        'PG_SOURCE_SCHEMA': 'qasource', 'PG_TARGET_SCHEMA': 'qatarget', 'TID': 't9f8e7d6c_',
    }
    counts = iter((2, 2, 3, 3, 4))
    calls = []
    monkeypatch.setattr(edge, '_count', lambda role, schema, table: (calls.append((role, schema, table)), next(counts))[1])
    monkeypatch.setattr(edge, '_run_source_sql', lambda filename: calls.append(('sql', filename)))

    process = SimpleNamespace(poll=lambda: None)
    ok, reason = edge.inject_late_row(process)

    assert ok and reason is None
    assert calls == [
        ('source', 'qasource', 't9f8e7d6c_src'),
        ('target', 'qatarget', 't9f8e7d6c_tgt'),
        ('source', 'qasource', 't9f8e7d6c_src'),
        ('target', 'qatarget', 't9f8e7d6c_tgt'),
        ('sql', 'late-row.sql'),
        ('source', 'qasource', 't9f8e7d6c_src'),
    ]
    assert isinstance(process._slt_late_row_ack_at, str)
    assert module._wall_time(process._slt_late_row_ack_at) is not None


def test_postgres_late_row_edge_separates_cold_startup_from_injection_window(tmp_path, monkeypatch):
    module = _module()
    cases = _cases(tmp_path)
    spec = module.load_spec(CONTROL_DIR / 'controls.json')
    control = next(row for row in spec['controls'] if row['id'] == 'late-row')
    copied = tmp_path / 'edge' / 'work' / 'cases' / 'live'
    shutil.copytree(cases, copied)
    plan = module._plan(control, tmp_path / 'edge' / 'work', copied)
    invocation = module.Invocation(control, cases, copied, tmp_path / 'edge', plan)
    edge = module._PostgresControlEdge.__new__(module._PostgresControlEdge)
    edge.invocation = invocation
    edge.env = {'SLT_CONTROL_STARTUP_TIMEOUT': '120', 'SLT_CONTROL_PHASE_TIMEOUT': '1'}
    edge.tokens = {
        'PG_SOURCE_SCHEMA': 'qasource', 'PG_TARGET_SCHEMA': 'qatarget', 'TID': 't9f8e7d6c_',
    }
    now = [0.0]
    injected = []
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: now.__setitem__(0, now[0] + seconds))

    def count(role, _schema, _table):
        if now[0] < 116:
            return 0
        return 4 if injected else 3

    monkeypatch.setattr(edge, '_count', count)
    monkeypatch.setattr(edge, '_run_source_sql', lambda filename: injected.append(filename))

    ok, reason = edge.inject_late_row(SimpleNamespace(poll=lambda: None))

    assert ok and reason is None
    assert injected == ['late-row.sql']
    assert now[0] >= 116


@pytest.mark.parametrize(
    ('env', 'expected'),
    [
        (
            {
                'SLT_RUN_EPOCH': 'd2-edge',
                'SLT_PG_HOST_PORT': '25432',
                'SLT_SERVICES_HOST': 'host.docker.internal',
            },
            {'host': 'host.docker.internal', 'port': 25432, 'dbname': 'sltdb',
             'source_user': 'qasource', 'source_password': 'striim',
             'target_user': 'qatarget', 'target_password': 'striim'},
        ),
        (
            {
                'SLT_RUN_EPOCH': 'd2-edge',
                'SLT_PG_HOST': 'postgres.example', 'SLT_PG_PORT': '6432',
                'SLT_PG_HOST_PORT': '25432', 'SLT_PG_DB': 'live-db',
                'SLT_PG_SOURCE_USER': 'live-source', 'SLT_PG_SOURCE_PASSWORD': 'source-pass',
                'SLT_PG_TARGET_USER': 'live-target', 'SLT_PG_TARGET_PASSWORD': 'target-pass',
                'SLT_PG_SOURCE_SCHEMA': 'live-source-schema',
                'SLT_PG_TARGET_SCHEMA': 'live-target-schema',
            },
            {'host': 'postgres.example', 'port': 6432, 'dbname': 'live-db',
             'source_user': 'live-source', 'source_password': 'source-pass',
             'target_user': 'live-target', 'target_password': 'target-pass'},
        ),
    ],
)
def test_postgres_control_edge_uses_framework_connection_settings(tmp_path, monkeypatch, env, expected):
    module = _module()
    cases = _cases(tmp_path)
    spec = module.load_spec(CONTROL_DIR / 'controls.json')
    control = next(row for row in spec['controls'] if row['id'] == 'late-row')
    copied = tmp_path / 'edge' / 'work' / 'cases' / 'live'
    shutil.copytree(cases, copied)
    plan = module._plan(control, tmp_path / 'edge' / 'work', copied)
    invocation = module.Invocation(control, cases, copied, tmp_path / 'edge', plan)
    calls = []

    class Connection:
        autocommit = False

    def connect(**kwargs):
        calls.append(kwargs)
        return Connection()

    monkeypatch.setitem(sys.modules, 'psycopg2', SimpleNamespace(connect=connect))
    edge = module._PostgresControlEdge(invocation, env)
    edge._connect('source')
    edge._connect('target')

    base = {'host': expected['host'], 'port': expected['port'], 'dbname': expected['dbname'],
            'connect_timeout': 5}
    assert calls == [
        {**base, 'user': expected['source_user'], 'password': expected['source_password']},
        {**base, 'user': expected['target_user'], 'password': expected['target_password']},
    ]


def test_late_row_edge_receives_effective_child_environment(tmp_path, monkeypatch):
    module = _module()
    cases = _cases(tmp_path)
    spec = module.load_spec(CONTROL_DIR / 'controls.json')
    control = next(row for row in spec['controls'] if row['id'] == 'late-row')
    copied = tmp_path / 'edge' / 'work' / 'cases' / 'live'
    shutil.copytree(cases, copied)
    plan = module._plan(control, tmp_path / 'edge' / 'work', copied)
    invocation = module.Invocation(control, cases, copied, tmp_path / 'edge', plan)
    captured = []

    class Edge:
        def inject_late_row(self, _process):
            return False, 'not used'

    monkeypatch.setattr(module, '_late_row_edge',
                        lambda _invocation, env: captured.append(env) or Edge())
    events, reason = module._control_phase(
        invocation, SimpleNamespace(poll=lambda: None),
        {'SLT_PG_HOST_PORT': '25432', 'SLT_CONTROL_PHASE_TIMEOUT': '1'},
    )

    assert events == () and reason == 'not used'
    assert captured == [{'SLT_PG_HOST_PORT': '25432', 'SLT_CONTROL_PHASE_TIMEOUT': '1'}]


def test_postgres_block_target_edge_holds_the_real_transaction_until_cleanup(tmp_path):
    module = _module()
    statements = []

    class Cursor:
        def execute(self, sql):
            statements.append(sql)

        def close(self):
            statements.append('cursor-close')

    class Connection:
        autocommit = True
        rolled_back = False
        closed = False

        def cursor(self):
            return Cursor()

        def rollback(self):
            self.rolled_back = True

        def close(self):
            self.closed = True

    edge = module._PostgresControlEdge.__new__(module._PostgresControlEdge)
    plan = tmp_path / 'control-plan.json'
    plan.write_text('{}')
    edge.invocation = SimpleNamespace(plan=plan)
    edge.env = {'SLT_CONTROL_PHASE_TIMEOUT': '1'}
    edge.tokens = {'PG_TARGET_SCHEMA': 'qatarget', 'TID': 't9f8e7d6c_'}
    connection = Connection()
    edge._connect = lambda role: connection

    ok, reason, hold = edge.block_target_insert(SimpleNamespace(poll=lambda: None))

    assert ok and reason is None and hold is connection
    assert connection.autocommit is False
    assert statements == [
        "SET lock_timeout = '2s'",
        'LOCK TABLE "qatarget"."t9f8e7d6c_tgt" IN ACCESS EXCLUSIVE MODE',
        'cursor-close',
    ]
    assert not connection.closed
    hold.rollback()
    hold.close()
    assert connection.rolled_back and connection.closed


def test_striim_control_edge_waits_for_running_then_stops_the_application():
    module = _module()
    statuses = iter(('RUNNING', 'HALT'))

    class Client:
        stopped = None

        def current_status(self, app):
            return next(statuses)

        def stop_app(self, app):
            self.stopped = app

    edge = module._LiveControlEdge.__new__(module._LiveControlEdge)
    edge.env = {'SLT_CONTROL_PHASE_TIMEOUT': '1'}
    edge.tokens = {'APP': 'control-app'}
    client = Client()
    edge._client_for = lambda: client

    ok, reason = edge.suppress_capture(SimpleNamespace(poll=lambda: None))

    assert ok and reason is None and client.stopped == 'control-app'


def _write_real_lifecycle_subject(path: Path):
    path.write_text(
        "import json, sys, time\n"
        "from pathlib import Path\n"
        "output, control_id = Path(sys.argv[1]), sys.argv[2]\n"
        "marker = output / 'lifecycle-phase.flag'\n"
        "deadline = time.time() + 5\n"
        "while not marker.exists() and time.time() < deadline:\n"
        "    time.sleep(0.01)\n"
        "if not marker.exists():\n"
        "    raise SystemExit(2)\n"
        "if control_id == 'l1-capture-suppressed':\n"
        "    lifecycle = {'ready': {'kind': 'sentinel', 'reason': 'deadline'}, 'completion': None}\n"
        "elif control_id == 'l3-delete-empty-no-done':\n"
        "    lifecycle = {'ready': {'kind': 'sentinel', 'reason': 'satisfied'},\n"
        "                 'completion': {'kind': 'sentinel', 'reason': 'deadline'}}\n"
        "else:\n"
        "    lifecycle = {'ready': {'kind': 'baseline-landed', 'reason': 'deadline'}, 'completion': None}\n"
        "state = output / 'state' / 'run' / 'live'\n"
        "state.mkdir(parents=True, exist_ok=True)\n"
        "(state / 'junit.xml').write_text('<testsuite tests=\"1\" failures=\"1\" errors=\"0\" skipped=\"0\"><testcase><failure>deadline</failure></testcase></testsuite>\\n')\n"
        "(state / 'evidence.json').write_text(json.dumps({'evidenceVersion': 2, 'kind': 'case',\n"
        "    'run': {'status': 'failed', 'qualifies': False}, 'data': {'reason': 'no exact data assertion'},\n"
        "    'lifecycle': lifecycle}))\n"
        "raise SystemExit(1)\n"
    )


class _FakeLifecycleEdge:
    def __init__(self):
        self.calls = []
        self.hold = None

    def _mark(self, process, name):
        self.calls.append(name)
        Path(process._slt_control_output, 'lifecycle-phase.flag').write_text(name)
        return True, None

    def suppress_capture(self, process):
        return self._mark(process, 'capture')

    def suppress_done_sentinel(self, process):
        return self._mark(process, 'done')

    def block_target_insert(self, process):
        result = self._mark(process, 'target')

        class Hold:
            rolled_back = False
            closed = False

            def rollback(self):
                self.rolled_back = True

            def close(self):
                self.closed = True

        self.hold = Hold()
        return (*result, self.hold)


@pytest.mark.parametrize(
    ('control_id', 'method', 'call', 'event', 'failure'),
    [
        ('l1-capture-suppressed', 'suppress_capture', 'capture', 'capture-suppressed-after-running',
         'readiness-deadline'),
        ('l3-delete-empty-no-done', 'suppress_done_sentinel', 'done', 'done-sentinel-suppressed',
         'current-done-sentinel-not-observed'),
        ('l8-blocked-target-insert', 'block_target_insert', 'target', 'target-insert-blocked-after-running',
         'baseline-landed-deadline'),
    ],
)
def test_group_a_controls_drive_main_with_only_the_live_edge_faked(
    tmp_path, monkeypatch, control_id, method, call, event, failure,
):
    module = _module()
    cases = _cases(tmp_path)
    subject = tmp_path / f'{control_id}.py'
    _write_real_lifecycle_subject(subject)
    edge = _FakeLifecycleEdge()
    monkeypatch.setattr(module, '_subject_command',
                        lambda invocation, project: [sys.executable, str(subject),
                                                     str(invocation.output), control_id])
    monkeypatch.setattr(module, '_live_control_edge', lambda _invocation, _env=None: edge)
    output = tmp_path / control_id

    rc = module.main([
        'run', '--spec', str(CONTROL_DIR / 'controls.json'), '--id', control_id,
        '--cases', str(cases), '--output', str(output),
    ])

    result = json.loads((output / 'control-result.json').read_text())
    assert rc == 0 and result['wrapperExit'] == 0
    assert edge.calls == [call]
    assert result['subjectExit'] == 1 and result['failure'] == failure
    assert result['events'] == [event]
    if control_id == 'l8-blocked-target-insert':
        assert edge.hold.rolled_back and edge.hold.closed


def test_file_output_controls_reach_their_declared_comparisons(tmp_path, monkeypatch):
    module = _module()
    cases = _cases(tmp_path)
    spec = module.load_spec(CONTROL_DIR / 'controls.json')

    invalid = {
        'evidenceVersion': 2, 'kind': 'case',
        'run': {'status': 'failed', 'qualifies': False},
        'data': {'comparisons': []},
        'lifecycle': {},
    }
    invalid['data']['comparisons'] = [{
        'type': 'file', 'error': 'invalid-value:expected:id',
    }]
    result, _ = _run_through_default(
        module, monkeypatch, spec, cases, tmp_path / 'invalid-asset', 'invalid-asset', invalid,
        'invalid-value:expected:id',
    )
    assert result == 0
    assert json.loads((tmp_path / 'invalid-asset' / 'control-result.json').read_text())['failure'] == \
        'invalid-input-asset'

    sequence = {
        'evidenceVersion': 2, 'kind': 'case',
        'run': {'status': 'failed', 'qualifies': False},
        'data': {'comparisons': [_comparison(kind='file', order='sequence', sequence_only=True)]},
        'lifecycle': {},
    }
    result, _ = _run_through_default(
        module, monkeypatch, spec, cases, tmp_path / 'sequence-order', 'sequence-order', sequence,
        'sequence mismatch',
    )
    assert result == 0
    assert json.loads((tmp_path / 'sequence-order' / 'control-result.json').read_text())['failure'] == \
        'sequence-order-mismatch'


def test_file_output_subject_is_copied_without_claiming_server_ownership(tmp_path, monkeypatch):
    module = _module()
    cases = _cases(tmp_path)
    spec = module.load_spec(CONTROL_DIR / 'controls.json')
    envelope = {
        'evidenceVersion': 2, 'kind': 'case',
        'run': {'status': 'failed', 'qualifies': False},
        'data': {'comparisons': [{'type': 'file', 'error': 'invalid-value:expected:id'}]},
        'lifecycle': {},
    }
    result, calls = _run_through_default(
        module, monkeypatch, spec, cases, tmp_path / 'consumer-root', 'invalid-asset', envelope,
        'invalid input',
    )
    assert result == 0
    command = calls[0]
    project = Path(command[command.index('--targets') + 1])
    copied_case = Path(command[command.index('--case') + 1])
    assert project == tmp_path / 'consumer-root' / 'work' / 'consumer' / 'gold-targets.yaml'
    assert copied_case == Path('file-output')
    assert (project.parent / 'cases' / 'live' / '03-file-output').is_dir()


def test_file_output_framework_owns_the_server_side_witness_before_comparison(run_case):
    run = run_case('file-sink', {'mirror': 'live', 'slot_active': True})
    envelope = _agree(run, 'passed', qualifies=True)
    owned_dirs = [item['name'] for item in envelope['resources']['owned'] if item['kind'] == 'owned-dir']
    owned_dir = f"/opt/striim/slt-runs/{envelope['lifecycle']['identity']['namespace']}"
    assert owned_dirs == [owned_dir]
    assert envelope['lifecycle']['completion']['condition'].startswith(owned_dir + '/')
    assert envelope['data']['comparisons'][0]['target'].startswith(owned_dir + '/')


def test_unexpected_file_output_failure_is_recorded_not_accepted(tmp_path, monkeypatch):
    module = _module()
    cases = _cases(tmp_path)
    spec = module.load_spec(CONTROL_DIR / 'controls.json')
    envelope = {
        'evidenceVersion': 2, 'kind': 'case',
        'run': {'status': 'failed', 'qualifies': False,
                'failure': 'lifecycle readiness failed: witness-not-owned: completion.path'},
        'data': {'comparisons': []},
        'lifecycle': {'ready': {'kind': 'baseline-landed', 'reason': 'witness-not-owned'}},
    }
    for control_id in ('invalid-asset', 'sequence-order'):
        output = tmp_path / control_id
        result, _ = _run_through_default(
            module, monkeypatch, spec, cases, output, control_id, envelope, 'witness-not-owned',
        )
        assert result != 0
        record = json.loads((output / 'control-result.json').read_text())
        assert record['failure'] is None
        assert record['observedFailure'] == 'witness-not-owned'
        assert 'witness-not-owned' in (output / 'control-errors.log').read_text()


def test_extra_duplicate_uses_supported_transformed_fixture_and_exact_path(tmp_path, monkeypatch):
    module = _module()
    cases = _cases(tmp_path)
    spec = module.load_spec(CONTROL_DIR / 'controls.json')
    tql = (cases / '02-transform' / 'app.tql').read_text()
    assert 'CREATE STREAM TransformedCustomerRows OF Global.WAEvent;' in tql
    assert 'OF TYPE Global.WAEvent' not in tql
    assert re.search(r'MODIFY\s*\(\s*data\[2\]\s*=\s*TO_DOUBLE\(row_event\.data\[2\]\)', tql)

    envelope = {
        'evidenceVersion': 2, 'kind': 'case',
        'run': {'status': 'failed', 'qualifies': False},
        'data': {'comparisons': [_comparison(duplicate=True)]},
        'lifecycle': {},
    }
    result, _ = _run_through_default(
        module, monkeypatch, spec, cases, tmp_path / 'extra-duplicate', 'extra-duplicate', envelope,
        'duplicate row',
    )
    assert result == 0
    record = json.loads((tmp_path / 'extra-duplicate' / 'control-result.json').read_text())
    assert record['failure'] == 'exact-data-mismatch'


def test_overlap_controls_have_an_external_coordinator_contract(tmp_path, monkeypatch):
    module = _module()
    cases = _cases(tmp_path)
    spec = module.load_spec(CONTROL_DIR / 'controls.json')
    controls = {row['id']: row for row in spec['controls']}

    def fake_coordinator(invocation):
        required = tuple(invocation.control['expected']['requiredEvents'])
        return module.SubjectRun(
            0, tmp_path / 'junit.xml', (), required,
        )

    junit = tmp_path / 'junit.xml'
    junit.write_text('<testsuite tests="0" failures="0" errors="0" skipped="0"/>\n')
    monkeypatch.setattr(module, '_execute_overlap_coordinator', fake_coordinator, raising=False)
    for cid in ('l4-overlap-identities', 'l5-sibling-survival', 'l6-foreign-survival'):
        copied = tmp_path / cid / 'work' / 'cases' / 'live'
        shutil.copytree(cases, copied)
        plan = module._plan(controls[cid], tmp_path / cid / 'work', copied)
        invocation = module.Invocation(controls[cid], cases, copied, tmp_path / cid, plan)
        result = module.run_overlap_coordinator(invocation)
        assert result.exit_code == 0
        assert set(result.events) == set(controls[cid]['expected']['requiredEvents'])


def test_serialized_overlap_lanes_are_a_control_failure(tmp_path, monkeypatch):
    module = _module()
    cases = _cases(tmp_path)
    spec = module.load_spec(CONTROL_DIR / 'controls.json')
    subject = tmp_path / 'serialized-overlap-subject.py'
    subject.write_text(
        "import json, os, sys, time\n"
        "from datetime import datetime, timezone\n"
        "from pathlib import Path\n"
        "lane = os.environ['SLT_RUN_EPOCH'].rsplit('-', 1)[-1]\n"
        "marker = Path(os.environ['SLT_SERIAL_MARKER'])\n"
        "if lane == 'b':\n"
        "    deadline = time.time() + 5\n"
        "    while not marker.exists() and time.time() < deadline: time.sleep(0.01)\n"
        "if not marker.exists() and lane == 'b': raise SystemExit(2)\n"
        "def stamp(): return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')\n"
        "ready, completion = stamp(), None\n"
        "time.sleep(0.05)\n"
        "completion = stamp()\n"
        "state = Path(os.environ['SLT_STATE_DIR']); state.mkdir(parents=True, exist_ok=True)\n"
        "(state / 'junit.xml').write_text('<testsuite tests=\"1\" failures=\"0\" errors=\"0\" skipped=\"0\"><testcase name=\"lane\"/></testsuite>\\n')\n"
        "(state / 'evidence.json').write_text(json.dumps({'evidenceVersion': 2, 'kind': 'run',\n"
        "  'run': {'qualifies': True}, 'lifecycle': {'identity': {'runId': os.environ['SLT_RUN_EPOCH']},\n"
        "  'ready': {'endedAt': ready}, 'completion': {'endedAt': completion}}}))\n"
        "if lane == 'a': marker.write_text('a finished')\n"
    )
    marker = tmp_path / 'serialized.marker'
    monkeypatch.setenv('SLT_SERIAL_MARKER', str(marker))
    monkeypatch.setattr(module, '_overlap_subject_command',
                        lambda _invocation, _project: [sys.executable, str(subject)])
    monkeypatch.setattr(module, '_probe_overlap_endpoint', lambda _env: True)

    output = tmp_path / 'serialized-overlap'
    rc = module.main([
        'run', '--spec', str(CONTROL_DIR / 'controls.json'), '--id', 'l4-overlap-identities',
        '--cases', str(cases), '--output', str(output),
    ])

    result = json.loads((output / 'control-result.json').read_text())
    assert rc == 1 and result['wrapperExit'] == 1
    assert 'overlap-barrier-entered' not in result['events']
    assert result['overlap']['overlapped'] is False
    assert result['overlap']['lanes'][0]['completionAt'] <= result['overlap']['lanes'][1]['readyAt']


def test_l6_foreign_probe_never_counts_the_sibling_run_target(tmp_path, monkeypatch):
    module = _module()
    cases = _cases(tmp_path)
    spec = module.load_spec(CONTROL_DIR / 'controls.json')
    control = next(row for row in spec['controls'] if row['id'] == 'l6-foreign-survival')
    copied = tmp_path / 'l6' / 'work' / 'cases' / 'live'
    shutil.copytree(cases, copied)
    plan = module._plan(control, tmp_path / 'l6' / 'work', copied)
    invocation = module.Invocation(control, cases, copied, tmp_path / 'l6', plan)
    calls = []

    class Edge:
        tokens = {'PG_TARGET_SCHEMA': 'qatarget', 'TID': 'sibling_'}

        def __init__(self, *_args):
            pass

        def _count(self, role, schema, table):
            calls.append((role, schema, table))
            return 0

    monkeypatch.setattr(module, '_PostgresControlEdge', Edge)
    monkeypatch.setenv('SLT_CONTROL_MODE', 'docker')
    from livetest import striimfile
    monkeypatch.setattr(striimfile, 'read_server_files', lambda *_args, **_kwargs: 'sibling-file-witness')

    table_ok, file_ok, reason = module._overlap_lane_probes(
        invocation,
        {'SLT_CONTROL_FOREIGN_TABLE': 'qatarget.l6_foreign', 'SLT_CONTROL_MODE': 'docker'},
        tmp_path / 'sibling-state',
        True,
    )

    assert table_ok and not file_ok
    assert 'foreign table or sibling file' in (reason or '')
    assert 'server-file mode: docker' in (reason or '')
    assert calls == [('target', 'qatarget', 'l6_foreign')]


def _l6_fake_overlap(tmp_path, monkeypatch):
    """Run the real L6 coordinator against a file/DB edge with two real child lanes."""
    module = _module()
    cases = _cases(tmp_path)
    spec = module.load_spec(CONTROL_DIR / 'controls.json')
    control = next(row for row in spec['controls'] if row['id'] == 'l6-foreign-survival')
    output = tmp_path / 'l6'

    subject = tmp_path / 'l6-subject.py'
    subject.write_text(
        'import json, os, time\n'
        'from datetime import datetime, timezone\n'
        'from pathlib import Path\n'
        "lane = os.environ['SLT_RUN_EPOCH'].rsplit('-', 1)[-1]\n"
        "def stamp(): return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')\n"
        'ready = stamp()\n'
        "if lane == 'a':\n"
        '    time.sleep(0.12)\n'
        '    completion = stamp()\n'
        'else:\n'
        '    time.sleep(0.45)\n'
        '    completion = stamp()\n'
        "state = Path(os.environ['SLT_STATE_DIR']); state.mkdir(parents=True, exist_ok=True)\n"
        "(state / 'junit.xml').write_text('<testsuite tests=\"1\" failures=\"0\" errors=\"0\" skipped=\"0\"><testcase name=\"lane\"/></testsuite>\\n')\n"
        "(state / 'evidence.json').write_text(json.dumps({'evidenceVersion': 2, 'kind': 'run',\n"
        "  'run': {'qualifies': True},\n"
        "  'lifecycle': {'identity': {'runId': os.environ['SLT_RUN_EPOCH']},\n"
        "  'ready': {'endedAt': ready}, 'completion': {'endedAt': completion}}}))\n"
    )

    def fake_read(_ctx, path, run=None):
        if 'slt-runs' in str(path):
            return 'sibling-file-witness'
        return ''

    from livetest import striimfile
    monkeypatch.setattr(module, '_overlap_subject_command',
                        lambda _invocation, _project: [sys.executable, str(subject)])
    monkeypatch.setattr(module, '_probe_overlap_endpoint', lambda _env: True)
    monkeypatch.setattr(module, '_PostgresControlEdge',
                        type('Edge', (), {'__init__': lambda self, *_args, **_kwargs: None,
                                          '_count': lambda self, *_args: 0}))
    monkeypatch.setattr(striimfile, 'read_server_files', fake_read)
    monkeypatch.setenv('SLT_CONTROL_MODE', 'docker')
    monkeypatch.setenv('SLT_CONTROL_FOREIGN_TABLE', 'qatarget.l6_foreign')

    rc = module.main([
        'run', '--spec', str(CONTROL_DIR / 'controls.json'), '--id', 'l6-foreign-survival',
        '--cases', str(cases), '--output', str(output),
    ])
    return module, rc, output


def test_l6_foreign_and_sibling_probes_survive_lane_a_cleanup(tmp_path, monkeypatch):
    _module_, rc, output = _l6_fake_overlap(tmp_path, monkeypatch)
    result = json.loads((output / 'control-result.json').read_text())
    assert rc == 0 and result['wrapperExit'] == 0
    assert {'foreign-table-survived', 'sibling-file-survived'} <= set(result['events'])
    assert 'fixed-checkpoint-survived' not in result['events']


def test_l6_server_mode_uses_framework_container_probe(monkeypatch):
    module = _module()
    from livetest import services, stack
    monkeypatch.delenv('SLT_CONTROL_MODE', raising=False)
    monkeypatch.delenv('SLT_SERVICES_HOST', raising=False)
    monkeypatch.delenv('SLT_STACK_PREFIX', raising=False)
    monkeypatch.delenv('SLT_STRIIM_VIEW_HOST', raising=False)
    seen = []
    monkeypatch.setattr(stack, 'striim_container', lambda env=None: 'slt-striim')
    monkeypatch.setattr(services, 'container_running',
                        lambda name, run=None: seen.append(name) or True)

    assert module._overlap_server_mode({}) == 'docker'
    assert seen == ['slt-striim']


def test_l6_server_mode_probe_timeout_fails_closed(monkeypatch):
    module = _module()
    from livetest import stack
    monkeypatch.delenv('SLT_CONTROL_MODE', raising=False)
    monkeypatch.setattr(stack, 'striim_container', lambda env=None: 'slt-striim')
    calls = []

    def timed_out(argv, **kwargs):
        calls.append((argv, kwargs))
        raise subprocess.TimeoutExpired(argv, kwargs['timeout'])

    monkeypatch.setattr(module.subprocess, 'run', timed_out)

    assert module._overlap_server_mode({}) == 'native'
    assert calls == [(
        ['docker', 'container', 'inspect', '-f', '{{.State.Running}}', 'slt-striim'],
        {'capture_output': True, 'text': True, 'timeout': 15},
    )]


def test_group_a_live_phase_uses_an_observable_external_edge(tmp_path, monkeypatch):
    module = _module()
    cases = _cases(tmp_path)
    spec = module.load_spec(CONTROL_DIR / 'controls.json')
    controls = {row['id']: row for row in spec['controls']}
    calls = []

    class Edge:
        def suppress_capture(self, process):
            calls.append(('capture', process))
            return True, None

        def suppress_done_sentinel(self, process):
            calls.append(('done', process))
            return True, None

        def block_target_insert(self, process):
            calls.append(('target', process))
            return True, None

    process = SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(module, '_live_control_edge', lambda _invocation, _env=None: Edge(), raising=False)
    for cid, expected_call in (
        ('l1-capture-suppressed', 'capture'),
        ('l3-delete-empty-no-done', 'done'),
        ('l8-blocked-target-insert', 'target'),
    ):
        copied = tmp_path / cid / 'work' / 'cases' / 'live'
        shutil.copytree(cases, copied)
        plan = module._plan(controls[cid], tmp_path / cid / 'work', copied)
        invocation = module.Invocation(controls[cid], cases, copied, tmp_path / cid, plan)
        events, reason = module._control_phase(invocation, process)
        assert reason is None and events == (controls[cid]['expected']['requiredEvents'][0],)
        assert calls[-1][0] == expected_call


def test_required_skip_selects_case_then_skips_with_reason(tmp_path):
    module = _module()
    cases = _cases(tmp_path)
    spec = module.load_spec(CONTROL_DIR / 'controls.json')
    output = tmp_path / 'required-skip'
    copied = output / 'work' / 'cases' / 'live'
    shutil.copytree(cases, copied)
    before = (copied / '01-plain-replication' / 'test.yaml').read_text()
    plan_path = module._plan(next(row for row in spec['controls'] if row['id'] == 'required-skip'),
                             output / 'work', copied)
    plan = json.loads(plan_path.read_text())
    manifest = (copied / '01-plain-replication' / 'test.yaml').read_text()
    assert manifest == before and 'disabled:' not in manifest
    assert plan['operation'] == 'select-case-then-skip-verification'
    assert plan['environment']['SLT_SKIP_VERIFY'] == '1'
    assert plan['skipReason'] == 'SLT_SKIP_VERIFY: app started + seeded; output not verified'


def test_real_entry_point_refuses_disabled_lifecycle_setup(tmp_path, monkeypatch):
    module = _module()
    cases = _cases(tmp_path)
    output = tmp_path / 'disabled-setup'

    def subprocess_edge(argv, **kwargs):
        state = output / 'state' / 'run' / 'live'
        state.mkdir(parents=True, exist_ok=True)
        (state / 'junit.xml').write_text(
            '<testsuite tests="1" failures="0" errors="0" skipped="0">'
            '<testcase name="control"/></testsuite>\n'
        )
        (state / 'evidence.json').write_text(json.dumps({
            'evidenceVersion': 2, 'kind': 'case',
            'run': {'status': 'passed', 'qualifies': False},
            'data': {'comparisons': []},
            'lifecycle': {'ready': {'kind': 'sentinel', 'reason': 'satisfied'}},
        }))
        return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')

    class FakeProcess:
        def __init__(self, argv, **kwargs):
            self.returncode = subprocess_edge(argv, **kwargs).returncode

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(module.subprocess, 'run', subprocess_edge)
    monkeypatch.setattr(module.subprocess, 'Popen', FakeProcess)
    monkeypatch.setattr(module, '_control_phase',
                        lambda *_args, **_kwargs: ((), 'setup/injection deliberately disabled'))
    rc = module.main([
        'run', '--spec', str(CONTROL_DIR / 'controls.json'), '--id', 'l1-capture-suppressed',
        '--cases', str(cases), '--output', str(output),
    ])
    result = json.loads((output / 'control-result.json').read_text())
    assert rc == 1 and result['wrapperExit'] == 1
    assert result['failure'] is None and result['observedFailure'] == 'injection-event-not-observed'
    assert 'setup/injection deliberately disabled' in (output / 'control-errors.log').read_text()


def test_wrong_golden_rejects_invalid_value_as_exact_mismatch(tmp_path, monkeypatch):
    module = _module()
    cases = _cases(tmp_path)
    spec = module.load_spec(CONTROL_DIR / "controls.json")
    envelope = {
        "evidenceVersion": 2,
        "kind": "case",
        "run": {"qualifies": False},
        "data": {"comparisons": [_comparison(error="invalid-value:expected:created_at")]},
        "lifecycle": {},
    }
    result, _ = _run_through_default(
        module, monkeypatch, spec, cases, tmp_path / "invalid-golden", "wrong-golden", envelope,
        "exact data assertion failed: invalid-value:expected:created_at",
    )
    assert result != 0


def test_sequence_order_uses_structured_sequence_mismatch(tmp_path, monkeypatch):
    module = _module()
    cases = _cases(tmp_path)
    spec = module.load_spec(CONTROL_DIR / "controls.json")
    envelope = {
        "evidenceVersion": 2,
        "kind": "case",
        "run": {"qualifies": False},
        "data": {"comparisons": [_comparison(kind="file", order="sequence", sequence_only=True)]},
        "lifecycle": {},
    }
    result, _ = _run_through_default(
        module, monkeypatch, spec, cases, tmp_path / "sequence", "sequence-order", envelope,
        "exact file assertion failed: slt-canon/1 sequence: firstMismatch",
    )
    assert result == 0


def test_missing_required_service_uses_structured_setup_error(tmp_path, monkeypatch, run_case):
    module = _module()
    cases = _cases(tmp_path)
    spec = module.load_spec(CONTROL_DIR / "controls.json")
    executed = run_case(
        "initial-load",
        {"postgres_connect_fail": "connection to w3-required-postgres-unavailable.invalid failed"},
    )
    envelope = _agree(executed, "failed")
    assert envelope["run"]["status"] == "failed"
    assert envelope["data"] == {"reason": exactdata.NO_DATA_REASON}
    assert envelope["lifecycle"]["ready"] is None
    assert any(service["name"] == "postgres" for service in envelope["runtime"]["services"])
    output = tmp_path / "missing-service"
    result, _ = _run_through_default(
        module, monkeypatch, spec, cases, output, "missing-required-service", envelope,
        "setup failed without a semantic classifier",
    )
    assert result == 0
    plan = json.loads((output / "work" / "control-plan.json").read_text())
    assert plan["service"] == "postgres"
    assert plan["environment"]["SLT_PG_HOST"] == "w3-required-postgres-unavailable.invalid"
    copied = output / "work" / "consumer" / "cases" / "live" / "01-plain-replication" / "test.yaml"
    assert "requires: [postgres]" in copied.read_text()

    unrelated = {**envelope, "run": {**envelope["run"], "failure": "unrelated setup error"}}
    result, _ = _run_through_default(
        module, monkeypatch, spec, cases, tmp_path / "wrong-setup-error",
        "missing-required-service", unrelated, "unrelated setup error",
    )
    assert result != 0


@pytest.mark.parametrize(
    ("control_id", "lifecycle", "event"),
    [
        (
            "l1-capture-suppressed",
            {"ready": {"kind": "sentinel", "reason": "deadline"}, "completion": None},
            "capture-suppressed-after-running",
        ),
        (
            "l3-delete-empty-no-done",
            {
                "ready": {"kind": "sentinel", "reason": "satisfied"},
                "completion": {"kind": "sentinel", "reason": "deadline"},
            },
            "done-sentinel-suppressed",
        ),
        (
            "l8-blocked-target-insert",
            {"ready": {"kind": "baseline-landed", "reason": "deadline"}, "completion": None},
            "target-insert-blocked-after-running",
        ),
    ],
)
def test_lifecycle_failure_classification_matches_bound_case(
    tmp_path, monkeypatch, control_id, lifecycle, event,
):
    module = _module()
    cases = _cases(tmp_path)
    spec = module.load_spec(CONTROL_DIR / "controls.json")
    envelope = {
        "evidenceVersion": 2,
        "kind": "case",
        "run": {"status": "failed", "failure": "lifecycle deadline", "qualifies": False},
        "data": {"reason": exactdata.NO_DATA_REASON},
        "lifecycle": lifecycle,
    }
    result, _ = _run_through_default(
        module, monkeypatch, spec, cases, tmp_path / control_id, control_id, envelope,
        "lifecycle deadline", events=(event,),
    )
    assert result == 0


def test_default_execute_discovers_structured_mismatch_from_subprocess_edge(tmp_path, monkeypatch):
    module = _module()
    cases = _cases(tmp_path)
    spec = module.load_spec(CONTROL_DIR / "controls.json")
    envelope = {
        "evidenceVersion": 2,
        "kind": "case",
        "run": {"qualifies": False},
        "data": {"comparisons": [_comparison()]},
        "lifecycle": {},
    }
    result, calls = _run_through_default(
        module, monkeypatch, spec, cases, tmp_path / "mismatch", "wrong-golden", envelope,
        "comparison unequal without a stable semantic name",
    )
    assert result == 0
    assert calls and calls[0][:4] == [module.sys.executable, "-m", "striim_test", "run"]


def test_cleanup_replay_events_require_one_ledger_and_real_replay(tmp_path, monkeypatch):
    module = _module()
    cases = _cases(tmp_path)
    spec = module.load_spec(CONTROL_DIR / "controls.json")
    envelope = {
        "evidenceVersion": 2,
        "kind": "case",
        "run": {"qualifies": False},
        "data": {"comparisons": []},
        "lifecycle": {"cleanup": {"status": "failed"}, "faultInjected": {"kind": "pg-table"}},
    }
    no_ledger = tmp_path / "no-ledger"
    result, _ = _run_through_default(
        module, monkeypatch, spec, cases, no_ledger, "l7-cleanup-fault-replay", envelope,
        "cleanup failed", ledger_count=0,
    )
    assert result != 0
    events = json.loads((no_ledger / "control-result.json").read_text())["events"]
    assert events == ["cleanup-table-fault-injected"]

    one_ledger = tmp_path / "one-ledger"
    result, calls = _run_through_default(
        module, monkeypatch, spec, cases, one_ledger, "l7-cleanup-fault-replay", envelope,
        "cleanup failed", ledger_count=1,
    )
    assert result == 0
    events = json.loads((one_ledger / "control-result.json").read_text())["events"]
    assert events == [
        "cleanup-table-fault-injected", "ownership-replay-started", "ownership-replay-completed"
    ]
    assert any("livetest.ownership" in argv for argv in calls)


def test_refused_control_writes_nonzero_result_record(tmp_path, monkeypatch):
    module = _module()
    cases = _cases(tmp_path)
    output = tmp_path / "refused"
    monkeypatch.setattr(
        module,
        "_execute_overlap_coordinator",
        lambda _invocation: (_ for _ in ()).throw(
            module.ControlError("overlap coordinator refused: shared endpoint is unavailable")
        ),
    )
    rc = module.main([
        "run", "--spec", str(CONTROL_DIR / "controls.json"), "--id", "l4-overlap-identities",
        "--cases", str(cases), "--output", str(output),
    ])
    assert rc == 2
    result = json.loads((output / "control-result.json").read_text())
    assert result["wrapperExit"] == rc and result["subjectExit"] is None
    assert "shared endpoint is unavailable" in (output / "control-errors.log").read_text()


def test_refusal_does_not_overwrite_prior_result(tmp_path):
    module = _module()
    cases = _cases(tmp_path)
    output = tmp_path / "prior"
    output.mkdir()
    prior = {"schemaVersion": 1, "controlId": "wrong-golden", "wrapperExit": 0, "proof": "first-run"}
    result_path = output / "control-result.json"
    result_path.write_text(json.dumps(prior) + "\n")

    rc = module.main([
        "run", "--spec", str(CONTROL_DIR / "controls.json"), "--id", "wrong-golden",
        "--cases", str(cases), "--output", str(output),
    ])

    assert rc == 2
    assert json.loads(result_path.read_text()) == prior


# ---------------------------------------------------------------- each control fails its target check
#
# Every negative control is run against the framework check it targets, and the test asserts WHY that
# check failed (the framework's own reason) as well as the control's classification of it. The exact
# controls mutate a copy of the real sample with the control's own ``_plan`` and are compared through
# ``exactdata``/``canon`` (fakes at psycopg2 and ``docker exec`` only); the lifecycle and plugin controls
# run through the real ``livetest.plugin`` in the exec harness. The unmutated sample passes the same
# check and classifies as no failure, so no control is vacuous.

def _exact_harness():
    path = ROOT / "scripts" / "live" / "tests" / "evidence" / "test_exact_assert.py"
    spec = importlib.util.spec_from_file_location("_controls_exact_harness", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_EXACT_TOKENS = {"PG_SOURCE_SCHEMA": "qasource", "PG_TARGET_SCHEMA": "qatarget", "TID": "ab12cd34_",
                 "OWNED_DIR": "/opt/striim/slt-runs/ns_ab12cd34"}


def _control(module, control_id):
    spec = module.load_spec(CONTROL_DIR / "controls.json")
    return next(row for row in spec["controls"] if row["id"] == control_id)


def _mutated_sample(module, tmp_path, control_id):
    """A copy of samples/live with ``control_id``'s own mutation applied; returns (case dir, control)."""
    control = _control(module, control_id)
    copied = tmp_path / control_id / "work" / "cases" / "live"
    shutil.copytree(ROOT / "samples" / "live", copied)
    module._plan(control, tmp_path / control_id / "work", copied)
    return copied / control["case"], control


def _golden_rows(case_dir):
    """What the pipeline really delivers: the sample's reviewed golden, typed as Postgres returns it."""
    import csv
    import datetime as dt
    from decimal import Decimal
    with open(ROOT / "samples" / "live" / case_dir.name / "expected" / "rows.csv", newline="") as stream:
        rows = list(csv.DictReader(stream))
    typed = []
    for row in rows:
        out = dict(row)
        out["id"] = int(row["id"])
        if "amount" in row:
            out["amount"] = Decimal(row["amount"])
        if "created_at" in row:
            out["created_at"] = dt.datetime.fromisoformat(row["created_at"])
        typed.append(out)
    return typed


def _exact_db_record(monkeypatch, case_dir):
    harness = _exact_harness()
    world = harness.PgWorld()
    monkeypatch.setattr(lifecycle, "_pg_connect", world.connect)
    world.tables[f"qatarget.{_EXACT_TOKENS['TID']}tgt"] = _golden_rows(case_dir)
    from livetest import inputs
    from livetest.manifest import load_manifest
    m = load_manifest(case_dir / "test.yaml")
    specs = exactdata.partition(m.assert_["data"], m, "data")[1]
    collector = []
    call = lambda: exactdata.assert_exact_data(  # noqa: E731
        harness.admins(), specs, m, tokens=_EXACT_TOKENS, inputs=inputs.snapshot(m, case_dir / "test.yaml"),
        lifecycle=True, collector=collector,
        owned=harness.owns(("pg-table", "postgres-target", f"qatarget.{_EXACT_TOKENS['TID']}tgt")))
    try:
        records = call()
    except AssertionFailed as exc:
        return "failed", str(exc), collector[0]
    return records[0]["status"], None, collector[0]


def _exact_file_record(monkeypatch, case_dir):
    from livetest import inputs, ownership
    from livetest.manifest import load_manifest
    path = _EXACT_TOKENS["OWNED_DIR"] + "/rows.json"
    monkeypatch.setattr(ownership, "docker_ls", lambda pattern, nodes=None: [path])
    body = "".join(json.dumps({"data": row}) + "\n" for row in _golden_rows(case_dir)).encode()
    m = load_manifest(case_dir / "test.yaml")
    specs = exactdata.partition(m.assert_["file"], m, "file")[1]
    collector = []
    call = lambda: exactdata.assert_exact_file(  # noqa: E731
        specs, m, tokens=_EXACT_TOKENS, inputs=inputs.snapshot(m, case_dir / "test.yaml"), lifecycle=True,
        collector=collector, mode="docker", nodes=["slt-striim"],
        owned=lambda kind, db, name: (kind, db, name) == ("owned-file", None, path),
        run=lambda argv, timeout: SimpleNamespace(returncode=0, stdout=body, stderr=b""))
    try:
        records = call()
    except AssertionFailed as exc:
        return "failed", str(exc), collector[0] if collector else None
    return records[0]["status"], None, collector[0] if collector else None


def _envelope_of(comparison):
    return {"evidenceVersion": 2, "kind": "case", "run": {"status": "failed", "qualifies": False},
            "data": {"comparisons": [comparison]}, "lifecycle": {}}


@pytest.mark.parametrize("control_id, record_of", [
    ("wrong-golden", _exact_db_record),
    ("extra-duplicate", _exact_db_record),
    ("invalid-asset", _exact_file_record),
    ("sequence-order", _exact_file_record),
])
def test_unmutated_sample_passes_the_exact_check_a_control_targets(tmp_path, monkeypatch, control_id, record_of):
    module = _module()
    control = _control(module, control_id)
    status, detail, comparison = record_of(monkeypatch, ROOT / "samples" / "live" / control["case"])
    assert status == "passed", detail
    assert comparison["equal"] is True and not comparison.get("error")
    assert module._failure(control, [_envelope_of(comparison)], "passed") is None


def test_wrong_golden_fails_exact_data_on_the_changed_row(tmp_path, monkeypatch):
    module = _module()
    case_dir, control = _mutated_sample(module, tmp_path, "wrong-golden")
    status, detail, comparison = _exact_db_record(monkeypatch, case_dir)
    assert status == "failed" and "exact data assertion failed" in detail
    assert comparison["equal"] is False and not comparison.get("error")
    missing, extra = comparison["samples"]["missing"], comparison["samples"]["extra"]
    assert [dict((c[0], c[2]) for c in s["row"])["customer_name"] for s in missing] == ["Record03-wrong"]
    assert [dict((c[0], c[2]) for c in s["row"])["customer_name"] for s in extra] == ["Record03"]
    assert module._failure(control, [_envelope_of(comparison)], "failed") == "exact-data-mismatch"


def test_extra_duplicate_fails_exact_data_on_the_multiset_count(tmp_path, monkeypatch):
    module = _module()
    case_dir, control = _mutated_sample(module, tmp_path, "extra-duplicate")
    status, detail, comparison = _exact_db_record(monkeypatch, case_dir)
    assert status == "failed" and "exact data assertion failed" in detail
    assert comparison["equal"] is False and comparison["samples"]["extra"] == []
    [missing] = comparison["samples"]["missing"]
    assert dict((c[0], c[2]) for c in missing["row"])["id"] == "33" and missing["count"] == 1
    assert comparison["expected"]["rowCount"] == comparison["actual"]["rowCount"] + 1
    assert module._failure(control, [_envelope_of(comparison)], "failed") == "exact-data-mismatch"


def test_invalid_asset_fails_exact_file_on_the_golden_value(tmp_path, monkeypatch):
    module = _module()
    case_dir, control = _mutated_sample(module, tmp_path, "invalid-asset")
    status, detail, comparison = _exact_file_record(monkeypatch, case_dir)
    assert status == "failed"
    assert comparison["error"].startswith("invalid-value:expected:id"), comparison["error"]
    assert module._failure(control, [_envelope_of(comparison)], "failed") == "invalid-input-asset"
    # the same file, compared as an ordinary data mismatch, is not this control's reason
    assert module._failure(_control(module, "wrong-golden"), [_envelope_of(comparison)], "failed") is None


def test_sequence_order_fails_exact_file_on_order_alone(tmp_path, monkeypatch):
    module = _module()
    case_dir, control = _mutated_sample(module, tmp_path, "sequence-order")
    status, detail, comparison = _exact_file_record(monkeypatch, case_dir)
    assert status == "failed"
    assert comparison["order"] == "sequence" and comparison["equal"] is False and not comparison.get("error")
    assert comparison["samples"]["missing"] == [] and comparison["samples"]["extra"] == []
    assert comparison["samples"]["firstMismatch"]["index"] == 0
    assert module._failure(control, [_envelope_of(comparison)], "failed") == "sequence-order-mismatch"


def _classify(module, control_id, run, junit_status):
    return module._failure(_control(module, control_id), [run.envelope()], junit_status)


def test_required_skip_is_skipped_by_the_plugin_with_the_skip_verify_reason(run_case):
    module = _module()
    run = run_case("initial-load", {"initial_load": True}, env={"SLT_SKIP_VERIFY": "1"})
    status, _props, xml = run.junit()
    assert status == "skipped" and "SLT_SKIP_VERIFY" in xml
    assert module._junit(run.root / "live" / "junit.xml")[0] == "skipped"
    assert _classify(module, "required-skip", run, status) == "required-case-skipped"


def test_l1_capture_suppressed_fails_readiness_sentinel_on_its_deadline(run_case):
    module = _module()
    run = run_case("cdc-sentinel", {"mirror": "live", "mirror_ops": 0})   # nothing is captured
    env = _agree(run, "failed")
    ready = env["lifecycle"]["ready"]
    assert ready["kind"] == "sentinel" and ready["reason"] == "deadline"
    assert "readiness sentinel failed: deadline" in env["run"]["failure"]
    assert env["lifecycle"]["completion"] is None
    assert _classify(module, "l1-capture-suppressed", run, "failed") == "readiness-deadline"


def test_l2_prior_attempt_sentinel_in_the_target_is_ignored_by_readiness(run_case):
    """l2 is a proof expected to pass: capture works, and an earlier attempt's ready sentinel sits in
    the run's target for the whole readiness phase. Readiness must count only the current id: a
    count that ignores ``id = SENTINEL_ID`` (review mutation M5) sees the prior row, so the present
    step reads more than one row and the absent step never reaches zero."""
    module = _module()
    stale = 1999999999                                   # an earlier attempt's ready sentinel, delivered late
    run = run_case("cdc-sentinel", {"mirror": "live", "deliver_on_deploy": {"qatarget.<TID>tgt": [stale]}})
    env = _agree(run, "passed")                        # the fixture has no exact block, so it never qualifies
    ready = env["lifecycle"]["ready"]
    assert ready["kind"] == "sentinel" and ready["reason"] == "satisfied"
    [current] = [o["value"]["id"] for o in ready["observations"]
                 if isinstance(o["value"], dict) and o["value"].get("step") == "insert"]
    assert current != stale
    assert ready["witness"] == f"ready sentinel {current} observed present then absent"
    target = f"qatarget.{run.dump()['tid']}tgt"
    assert {"event": "delivered", "table": target, "ids": [stale]} in run.events()
    steps = [o["value"] for o in ready["observations"] if isinstance(o["value"], dict) and "count" in o["value"]]
    present = [v["count"] for v in steps if v["step"] == "present"]
    absent = [v["count"] for v in steps if v["step"] == "absent"]
    assert present and set(present) <= {0, 1} and present[-1] == 1   # the stale row never counted
    assert absent and absent[-1] == 0                                  # while the stale row stayed
    [done] = [o["value"]["id"] for o in env["lifecycle"]["completion"]["observations"]
              if isinstance(o["value"], dict) and o["value"].get("step") == "insert"]
    reads = [e for e in run.events() if e["event"] == "observe" and e["table"] == target]
    assert {e["id"] for e in reads} == {str(current), str(done)}      # never the stale id
    ready_reads = [e for e in reads if e["id"] == str(current)]
    assert ready_reads and all(stale in e["rows"] for e in ready_reads)   # beside the stale row, every time
    observed = {"priorSentinelId": stale, "currentSentinelIds": [current]}
    subject = module.SubjectRun(0, Path("junit.xml"), (), control_observed=observed)
    assert module._prior_sentinel_proof([env], subject) is None
    wrong = module.SubjectRun(0, Path("junit.xml"), (), control_observed={**observed, "currentSentinelIds": [7]})
    assert "is not an id observed beside the prior row" in module._prior_sentinel_proof([env], wrong)
    accepted = {**env, "lifecycle": {**env["lifecycle"], "ready": {
        **ready, "witness": f"ready sentinel {stale} observed present then absent"}}}
    assert "is the prior attempt's sentinel" in module._prior_sentinel_proof([accepted], subject)


def test_l3_withheld_done_sentinel_fails_completion_on_its_deadline(run_case):
    module = _module()
    run = run_case("cdc-sentinel", {"mirror": "live", "mirror_ops": 2})   # only the ready sentinel is captured
    env = _agree(run, "failed")
    lc = env["lifecycle"]
    assert lc["ready"]["reason"] == "satisfied"
    assert lc["completion"]["kind"] == "sentinel" and lc["completion"]["reason"] == "deadline"
    assert "completion sentinel failed: deadline" in env["run"]["failure"]
    assert _classify(module, "l3-delete-empty-no-done", run, "failed") == "current-done-sentinel-not-observed"
    # readiness held, so this is not the l1/l2 reason
    assert _classify(module, "l1-capture-suppressed", run, "failed") is None


def test_l7_injected_cleanup_fault_fails_the_case_after_passing_assertions(run_case):
    module = _module()
    run = run_case("initial-load", {"initial_load": True}, env={"SLT_LIFECYCLE_FAULT": "cleanup:table"})
    env = _agree(run, "failed")
    assert "cleanup failed: pg-table" in run.junit()[2] and "injected-fault" in env["run"]["failure"]
    assert env["lifecycle"]["cleanup"]["status"] == "failed"
    assert env["lifecycle"]["faultInjected"]["kind"] == "pg-table"
    assert _classify(module, "l7-cleanup-fault-replay", run, "failed") == "cleanup-fault"


def test_l8_blocked_target_fails_baseline_landed_readiness_on_its_deadline(run_case):
    module = _module()
    run = run_case("initial-load", {"initial_load": False})             # the target never receives the rows
    env = _agree(run, "failed")
    ready = env["lifecycle"]["ready"]
    assert ready["kind"] == "baseline-landed" and ready["reason"] == "deadline"
    assert "readiness baseline-landed failed: deadline" in env["run"]["failure"]
    assert _classify(module, "l8-blocked-target-insert", run, "failed") == "baseline-landed-deadline"


def test_passing_lifecycle_case_classifies_as_no_control_failure(run_case):
    module = _module()
    run = run_case("initial-load", {"initial_load": True})
    _agree(run, "passed", qualifies=True)
    for control_id in ("l1-capture-suppressed", "l3-delete-empty-no-done",
                       "l7-cleanup-fault-replay", "l8-blocked-target-insert", "required-skip"):
        assert _classify(module, control_id, run, "passed") is None, control_id


def test_late_row_fails_source_count_completion_with_source_ahead(monkeypatch):
    module = _module()
    real = _real_late_row_completion(monkeypatch)
    completion = real["completion"]
    assert completion["kind"] == "source-count" and completion["reason"] == "stability-lost"
    assert real["envelope"]["lifecycle"]["stability"]["value"] == {"source": 4, "target": 3}
    assert "lifecycle completion source-count failed: stability-lost" in real["envelope"]["run"]["failure"]
    run = module.SubjectRun(1, Path("junit.xml"), (), ("late-row-injected-before-stability-end",),
                            control_tokens=dict(_LATE_ROW_TOKENS), control_ack_at=real["ack_at"])
    control = _control(module, "late-row")
    assert module._failure(control, [real["envelope"]], "failed", run=run,
                           events=run.events) == "late-row-source-ahead-of-target"


# ---------------------------------------------------------------- review R1: identity, .env, the l2 edge

def test_live_phase_edge_and_subject_share_one_run_identity(tmp_path, monkeypatch):
    """F1: with no SLT_RUN_EPOCH in the shell, the control presets one, so the edge renders the
    subject's TID instead of failing with IdentityError."""
    from livetest import runident
    module = _module()
    cases = _cases(tmp_path)
    spec = module.load_spec(CONTROL_DIR / "controls.json")
    monkeypatch.delenv("SLT_RUN_EPOCH", raising=False)
    seen = {}

    class FakeProcess:
        def __init__(self, argv, **kwargs):
            seen["subject"] = kwargs["env"]
            self.returncode = 1

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

    def phase(invocation, process=None, env=None):
        seen["edge"] = env
        seen["tokens"] = module._control_tokens(invocation, env)
        return (), "stopped after the identity check"

    monkeypatch.setattr(module.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(module, "_control_phase", phase)
    monkeypatch.setattr(module, "_postgres_service_base", lambda env: {
        "source_schema": "qasource", "target_schema": "qatarget", "source_user": "s", "source_password": "p",
        "target_user": "t", "target_password": "p"})
    with pytest.raises(module.ControlError, match="missing or invalid JUnit"):
        module.run_control(spec, "late-row", cases, tmp_path / "out")
    epoch = seen["subject"]["SLT_RUN_EPOCH"]
    assert epoch.startswith("control-late-row-") and seen["edge"]["SLT_RUN_EPOCH"] == epoch
    subject_tid = runident.derive("plain-replication", seen["subject"]).tid
    assert seen["tokens"]["TID"] == subject_tid

    monkeypatch.setenv("SLT_RUN_EPOCH", "operator-epoch")          # an operator's epoch is kept
    with pytest.raises(module.ControlError, match="missing or invalid JUnit"):
        module.run_control(spec, "late-row", cases, tmp_path / "out2")
    assert seen["subject"]["SLT_RUN_EPOCH"] == seen["edge"]["SLT_RUN_EPOCH"] == "operator-epoch"


def test_control_edge_reads_service_settings_from_env_files_as_the_subject_does(tmp_path, monkeypatch):
    """F3: SLT_PG_* resolve shell > project .env > clone .env for the edge, as dispatch.service_env
    gives them to the subject."""
    from livetest import paths
    module = _module()
    cases = _cases(tmp_path)
    spec = module.load_spec(CONTROL_DIR / "controls.json")
    project = {"SLT_PG_HOST": "pg.project.example"}                   # the control's work/consumer .env
    clone = {"SLT_PG_HOST": "pg.clone.example", "SLT_PG_PORT": "6543"}
    clone["SLT_UNKNOWN_CONTROL_SETTING"] = "must not reach the subject"

    def read_dotenv(path, allowed=None):
        settings = project if str(path).endswith("/work/consumer/.env") else clone
        return {k: v for k, v in settings.items() if allowed is None or k in allowed}
    monkeypatch.setattr(paths, "read_dotenv", read_dotenv)
    monkeypatch.setattr(paths, "machine_values", lambda env=None, allowed=None: {})
    for key in ("SLT_PG_HOST", "SLT_PG_PORT"):
        monkeypatch.delenv(key, raising=False)
    seen = {}

    class FakeProcess:
        def __init__(self, argv, **kwargs):
            seen["subject"] = kwargs["env"]
            self.returncode = 1

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

    def phase(invocation, process=None, env=None):
        seen["base"] = module._postgres_service_base(env)
        return (), "stopped after the settings check"

    monkeypatch.setattr(module.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(module, "_control_phase", phase)
    with pytest.raises(module.ControlError, match="missing or invalid JUnit"):
        module.run_control(spec, "late-row", cases, tmp_path / "out")
    assert (seen["base"]["host"], str(seen["base"]["port"])) == ("pg.project.example", "6543")
    assert seen["subject"]["SLT_PG_HOST"] == "pg.project.example" and "SLT_PROJECT_ROOT" not in seen["subject"]
    assert "SLT_UNKNOWN_CONTROL_SETTING" not in seen["subject"]

    monkeypatch.setenv("SLT_PG_HOST", "pg.shell.example")         # the shell wins over both files
    with pytest.raises(module.ControlError, match="missing or invalid JUnit"):
        module.run_control(spec, "late-row", cases, tmp_path / "out2")
    assert seen["base"]["host"] == "pg.shell.example"


class _PriorSentinelWorld:
    """The l2 edge's database edge: target creation, the edge's writes and the target ids over time."""

    def __init__(self, prior, targets):
        self.prior, self.targets, self.created_after, self.sql, self.reads = prior, list(targets), 2, [], 0

    def query(self, role, sql, params=()):
        assert role == "target"
        if sql.startswith("SELECT to_regclass"):
            self.created_after -= 1
            return [(params[0] if self.created_after <= 0 else None,)]
        ids = self.targets[min(self.reads, len(self.targets) - 1)]
        self.reads += 1
        return [(i,) for i in ids]

    def execute(self, role, sql, params=()):
        assert role == "target"
        self.sql.append((sql.split()[0], sql.split(" (")[0].split(" WHERE")[0].split()[-1], params,
                         self.created_after <= 0))


def _prior_sentinel_edge(module, tmp_path, world, poll=lambda: None):
    cases = _cases(tmp_path)
    control = _control(module, "l2-prior-sentinel-ignored")
    copied = tmp_path / "l2" / "work" / "cases" / "live"
    shutil.copytree(cases, copied)
    plan = module._plan(control, tmp_path / "l2" / "work", copied)
    edge = module._PostgresControlEdge.__new__(module._PostgresControlEdge)
    edge.invocation = module.Invocation(control, cases, copied, tmp_path / "l2", plan)
    edge.env = {"SLT_CONTROL_PHASE_TIMEOUT": "5"}
    edge.tokens = {"PG_SOURCE_SCHEMA": "qasource", "PG_TARGET_SCHEMA": "qatarget", "TID": "t1a2b3c4d_"}
    edge._query = world.query
    edge._execute = world.execute
    return edge, json.loads(plan.read_text())["sentinelId"], SimpleNamespace(poll=poll)


def test_l2_edge_writes_the_created_target_and_removes_only_after_a_current_sentinel(tmp_path, monkeypatch):
    module = _module()
    monkeypatch.setattr(module.time, "sleep", lambda _s: None)
    world = _PriorSentinelWorld(None, [])
    edge, prior, process = _prior_sentinel_edge(module, tmp_path, world)
    world.targets = [[], [prior], [prior], [prior, 4242], [prior, 4242], []]
    events, reason, observed = edge.prove_prior_sentinel_ignored(process)
    assert reason is None
    assert events == ("prior-attempt-sentinel-in-target", "current-sentinel-observed-beside-prior",
                      "prior-attempt-sentinel-removed")
    table = '"qatarget"."t1a2b3c4d_tgt"'
    assert world.sql == [("INSERT", table, (prior,), True), ("DELETE", table, (prior,), True)]
    assert observed == {"priorSentinelId": prior, "currentSentinelIds": [4242]}


def test_l2_edge_fails_closed_when_the_prior_row_leaves_before_a_current_sentinel(tmp_path, monkeypatch):
    module = _module()
    monkeypatch.setattr(module.time, "sleep", lambda _s: None)
    world = _PriorSentinelWorld(None, [])
    edge, prior, process = _prior_sentinel_edge(module, tmp_path, world)
    world.targets = [[prior], []]
    clock = iter(range(0, 10_000))
    monkeypatch.setattr(module.time, "monotonic", lambda: float(next(clock)) * 0.01)
    events, reason, _observed = edge.prove_prior_sentinel_ignored(process)
    assert events == ("prior-attempt-sentinel-in-target",)
    assert "a current sentinel beside the prior row was not observed" in reason
    assert "the prior row left the target" in reason
    assert [row[0] for row in world.sql] == ["INSERT"]                     # never removed, never acked


def test_l2_proof_runs_on_one_subject_not_the_overlap_coordinator(tmp_path, monkeypatch):
    module = _module()
    cases = _cases(tmp_path)
    spec = module.load_spec(CONTROL_DIR / "controls.json")
    monkeypatch.setattr(module, "run_overlap_coordinator",
                        lambda _invocation: (_ for _ in ()).throw(AssertionError("overlap coordinator")))
    output = tmp_path / "l2-run"

    def execute(invocation):
        state = invocation.output / "state" / "run" / "live"
        state.mkdir(parents=True)
        (state / "junit.xml").write_text('<testsuite tests="1" failures="0" errors="0" skipped="0">'
                                         '<testcase name="c"/></testsuite>\n')
        (state / "evidence.json").write_text(json.dumps({
            "evidenceVersion": 2, "kind": "case", "run": {"status": "passed", "qualifies": True},
            "lifecycle": {"ready": {"kind": "sentinel", "reason": "satisfied",
                                    "witness": "ready sentinel 4242 observed present then absent"}}}))
        return module.SubjectRun(0, state / "junit.xml", (state / "evidence.json",),
                                 tuple(invocation.control["expected"]["requiredEvents"]),
                                 control_observed={"priorSentinelId": 17, "currentSentinelIds": [4242]})

    assert module.run_control(spec, "l2-prior-sentinel-ignored", cases, output, executor=execute) == 0
    record = json.loads((output / "control-result.json").read_text())
    assert record["failure"] is None and record["observed"]["currentSentinelIds"] == [4242]

    def accepted(invocation):
        run = execute(invocation)
        return run._replace(control_observed={"priorSentinelId": 4242, "currentSentinelIds": [4242]})

    assert module.run_control(spec, "l2-prior-sentinel-ignored", cases, tmp_path / "l2-bad",
                              executor=accepted) == 1
    record = json.loads((tmp_path / "l2-bad" / "control-result.json").read_text())
    assert record["observedFailure"] == "prior-sentinel-proof-not-shown"
    assert "is the prior attempt's sentinel 4242" in (tmp_path / "l2-bad" / "control-errors.log").read_text()
    assert module._default_execute.__module__ and "l2-prior-sentinel-ignored" not in module.OVERLAP
