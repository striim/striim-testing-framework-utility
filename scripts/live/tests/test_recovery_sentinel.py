from pathlib import Path
from dataclasses import replace
import pytest
import yaml
from livetest import lifecycle as lc
from livetest.manifest import _normalize_recover, load_manifest, ManifestError


def test_recover_sentinel_normalizes_true():
    assert _normalize_recover({"mode": "stop", "sentinel": True}, "case")["sentinel"] is True


@pytest.mark.parametrize("value", [False, "true", {}, 1])
def test_recover_sentinel_refuses_ambiguous_values(value):
    with pytest.raises(ManifestError, match="invalid-post-recovery-sentinel"):
        _normalize_recover({"mode": "stop", "sentinel": value}, "case")


def test_recover_sentinel_requires_declared_lifecycle_sentinel(tmp_path):
    p = tmp_path / "test.yaml"
    p.write_text('name: probe\ntql: app.tql\nrecover: {mode: stop, sentinel: true}\nassert:\n  data: [{target: tgt, min_rows: 1}]\n')
    with pytest.raises(ManifestError, match="invalid-post-recovery-sentinel"):
        load_manifest(p)


def test_after_restore_callback_runs_on_every_cycle_before_last_seeds():
    from livetest.plugin import _restore_then_post_recover
    events = []
    for last in (False, True):
        _restore_then_post_recover(lambda settle: events.append(("restore", settle)),
            lambda files: events.append("seed"), [("db", "changes.sql", 0)],
            settle=0, last=last, post_restore=lambda: events.append("transaction"),
            after_restore=lambda: events.append("sentinel"))
    assert events == [("restore", 0), "sentinel", ("restore", 0), "sentinel", "transaction", "seed"]


def fixture():
    p = Path(__file__).parent / "fixtures/lifecycle-contract/lifecycle.valid-cdc-sentinel.yaml"
    return lc.parse_manifest_block(yaml.safe_load(p.read_text()), p)


@pytest.mark.parametrize("sink", ["db", "file", "kafka"])
def test_post_restore_marker_has_fresh_cycle_identity_and_evidence(monkeypatch, sink):
    from livetest import recovery_sentinel
    state = lc.State("cdc"); state.sentinel_ids = {}; events = []; records = []; tokens = {}
    spec = replace(fixture(), sink=sink)
    monkeypatch.setattr(lc, "check_witness_refs", lambda *args: None)
    class Sentinel:
        def __init__(self, spec, admins, tokens, ident, phase, ids, source_sql, **kw):
            events.append((spec.sink, phase)); ids[phase] = len(events)
        def run(self, watch, terminal): watch.observe({"inserted": True}); return "satisfied"
    monkeypatch.setattr(lc, "Sentinel", Sentinel)
    for cycle in (1, 2):
        recovery_sentinel.run(state, spec, client=None, app="N.A", admins={}, tokens=tokens,
            ident=None, read_files=None, owned=lambda *args: True, source_dir=".",
            cycle=cycle, record=records.append)
    assert events == [(sink, "recovery_1"), (sink, "recovery_2")]
    assert tokens["SENTINEL_RECOVERY_1_ID"] != tokens["SENTINEL_RECOVERY_2_ID"]
    assert all(r["status"] == "passed" and r["type"] == "recovery" for r in records)


def test_missing_marker_is_recovery_error_not_expected_assertion(monkeypatch):
    from livetest import recovery_sentinel
    from livetest.recovery import RecoveryError
    from livetest.assertions import AssertionFailed
    records = []
    monkeypatch.setattr(lc, "check_witness_refs", lambda *args: None)
    class Sentinel:
        def __init__(self, *args, **kw): pass
        def run(self, watch, terminal): return "deadline"
    monkeypatch.setattr(lc, "Sentinel", Sentinel)
    with pytest.raises(RecoveryError, match="post-recovery-sentinel-missed") as caught:
        recovery_sentinel.run(lc.State("cdc"), fixture(), client=None, app="N.A", admins={},
            tokens={}, ident=None, read_files=None, owned=lambda *args: True,
            source_dir=".", cycle=1, record=records.append)
    assert not isinstance(caught.value, AssertionFailed)
    assert records[0]["status"] == "failed"


def test_unowned_marker_target_refused_before_write():
    from livetest import recovery_sentinel
    from livetest.recovery import RecoveryError
    with pytest.raises(RecoveryError, match="post-recovery-sentinel-missed"):
        recovery_sentinel.run(lc.State("cdc"), fixture(), client=None, app="N.A", admins={},
            tokens={}, ident=None, read_files=None, owned=lambda *args: False,
            source_dir=".", cycle=1, record=lambda r: None)


def test_recovery_marker_binding_cannot_be_shadowed(tmp_path):
    p = tmp_path / "test.yaml"
    p.write_text('name: probe\ntql: app.tql\ntokens: {SENTINEL_RECOVERY_1_ID: 10}\nassert: {smoke: true}\n')
    with pytest.raises(ManifestError, match="harness-provided"):
        load_manifest(p)


def test_recovery_sentinel_records_the_rendered_input_sql(monkeypatch):
    from livetest import recovery_sentinel
    state = lc.State('cdc'); inputs = []
    state.record_input = lambda *args, **kwargs: inputs.append(args)
    monkeypatch.setattr(lc,'check_witness_refs',lambda *args: None)
    class Marker:
        def __init__(self,*args,record=None,**kwargs): self.record = record
        def run(self,watch,terminal):
            if self.record:
                self.record('sentinel','insert.sql','INSERT marker')
                self.record('sentinel','delete.sql','DELETE marker')
            watch.observe({'step':'present','count':1}); watch.observe({'step':'absent','count':0})
            return 'satisfied'
    monkeypatch.setattr(lc,'Sentinel',Marker)
    recovery_sentinel.run(state,fixture(),client=None,app='N.app',admins={},tokens={},ident=None,
        read_files=None,owned=lambda *args:True,source_dir='.',cycle=1,record=lambda row:None)
    assert inputs == [('sentinel','insert.sql','INSERT marker'),('sentinel','delete.sql','DELETE marker')]
