import pytest
from livetest.assertions import AssertionFailed
from livetest.assertions.monitor import (
    MonitorSpecError, assert_monitor, find_targets, parse_monitor_specs, resolve_target, _same,
)
from livetest.resultschema import validate, SCHEMA_VERSION


class FakeStriim:
    """`mon(name)`: the app's tree for the app name, a figures body for a target's fullName.
    `bodies` is a list consumed one per poll so a test can script MON catching up."""

    def __init__(self, targets, bodies):
        self.targets = targets
        self.bodies = list(bodies)
        self.polls = 0

    def mon(self, name):
        if name == "NS.App":
            return {"entityType": "APPLICATION", "fullName": name, "components": [
                {"entityType": "FLOW", "fullName": "NS.F", "components": [
                    {"entityType": "TARGET", "fullName": t} for t in self.targets]}]}
        self.polls += 1
        return self.bodies.pop(0) if len(self.bodies) > 1 else self.bodies[0]


# ---- spec parsing --------------------------------------------------------------------

def test_parse_ok():
    parse_monitor_specs([{"metrics": {"processed": 6}}])
    parse_monitor_specs([{"target": "PgTarget",
                          "metrics": {"processed": 6, "individualOperationCount": {"Insert": 6}}}])


def test_parse_refuses_clocks_and_rates_by_name():
    for clock in ("rate", "acceptedRate", "lastCommitTime", "externalI/oLatency", "commitLag"):
        with pytest.raises(MonitorSpecError, match="CLOCK or a RATE"):
            parse_monitor_specs([{"metrics": {clock: 1}}])


def test_parse_refuses_bad_shapes():
    with pytest.raises(MonitorSpecError):
        parse_monitor_specs([])                                   # nothing to assert
    with pytest.raises(MonitorSpecError):
        parse_monitor_specs([{"processed": 6}])                   # figures outside metrics:
    with pytest.raises(MonitorSpecError):
        parse_monitor_specs([{"metrics": {}}])                    # empty
    with pytest.raises(MonitorSpecError):
        parse_monitor_specs([{"metrics": {"processed": [6]}}])    # a list is not a value
    with pytest.raises(MonitorSpecError):
        parse_monitor_specs([{"target": "", "metrics": {"processed": 6}}])


def test_parse_accepts_bounds_at_any_depth():
    parse_monitor_specs([{"metrics": {"processed": {"min": 1}, "input": {"max": 9},
                                      "targetAcked": {"min": 2, "max": 2.5},
                                      "individualOperationCount": {"Insert": {"min": 1}}}}])


def test_parse_refuses_bad_bounds():
    for bad in ({"min": "1"}, {"max": True}, {"min": None}, {"min": 1, "exact": 2},
                {"min": 3, "max": 2}):
        with pytest.raises(MonitorSpecError, match="bound is|min > max"):
            parse_monitor_specs([{"metrics": {"processed": bad}}])
        with pytest.raises(MonitorSpecError, match="bound is|min > max"):
            parse_monitor_specs([{"metrics": {"individualOperationCount": {"Insert": bad}}}])


# ---- matching --------------------------------------------------------------------------

def test_numbers_match_numerically_and_strings_exactly():
    assert _same(6, "6")
    assert _same(6, 6.0)
    assert _same(300000, "300,000")           # MON renders large counts with separators
    assert not _same(6, "7")
    assert not _same(6, None)
    assert _same("SRC.T", "SRC.T")
    assert not _same("SRC.T", "src.t")


def test_mapping_is_a_subset_match_over_json_text_or_a_map():
    assert _same({"Insert": 5}, '{"Insert": 5, "Update": 2}')
    assert _same({"Insert": 5}, {"Insert": "5", "Update": 2})
    assert not _same({"Insert": 5}, '{"Update": 2}')
    assert not _same({"Insert": 5}, "not json")
    assert not _same({"Insert": 5}, None)


def test_bounds_are_inclusive_and_either_side_optional():
    assert _same({"min": 5}, "5") and _same({"min": 5}, "300,000")
    assert not _same({"min": 5}, "4")
    assert _same({"max": 5}, 5) and not _same({"max": 5}, "5.5")
    assert _same({"min": 1, "max": 3}, "2") and not _same({"min": 1, "max": 3}, 0)
    assert not _same({"min": 1}, None) and not _same({"min": 1}, "n/a")
    assert _same({"Insert": {"min": 4}}, '{"Insert": 5}')
    assert not _same({"Insert": {"max": 4}}, '{"Insert": 5}')


def test_exact_values_stay_exact():
    assert not _same(6, "6.5") and not _same(6, "5")


# ---- target discovery ------------------------------------------------------------------

def test_targets_are_found_anywhere_in_the_tree_and_resolved_by_suffix():
    fake = FakeStriim(["NS.PgTarget", "NS.Other"], [{}])
    assert find_targets(fake.mon("NS.App")) == ["NS.Other", "NS.PgTarget"] or \
        set(find_targets(fake.mon("NS.App"))) == {"NS.PgTarget", "NS.Other"}
    assert resolve_target(["NS.PgTarget", "NS.Other"], "PgTarget", "NS.App") == "NS.PgTarget"
    assert resolve_target(["NS.PgTarget"], None, "NS.App") == "NS.PgTarget"
    with pytest.raises(AssertionFailed, match="name one with 'target:'"):
        resolve_target(["NS.PgTarget", "NS.Other"], None, "NS.App")
    with pytest.raises(AssertionFailed, match="matches 0"):
        resolve_target(["NS.PgTarget"], "Missing", "NS.App")


# ---- the assertion ---------------------------------------------------------------------

def test_polls_until_the_monitor_catches_up_and_returns_a_valid_record():
    # MON republishes per snapshot: the first two polls show the previous figure.
    fake = FakeStriim(["NS.PgTarget"], [{"processed": "0"}, {"processed": "0"},
                                        {"processed": "6", "targetAcked": 6}])
    records = assert_monitor(fake, "NS.App", [{"metrics": {"processed": 6, "targetAcked": 6}}],
                             timeout=10, poll=0)
    assert fake.polls == 3
    assert len(records) == 1 and records[0]["status"] == "passed"
    assert records[0]["type"] == "monitor" and records[0]["target"] == "NS.PgTarget"
    validate({"schema_version": SCHEMA_VERSION, "tests": [{
        "name": "t", "nodeid": "t", "status": "passed", "topology": "single", "services": [],
        "duration": 0.0, "skip_reason": None, "assertions": records}]})


def test_fails_at_the_deadline_naming_the_figure_shown():
    fake = FakeStriim(["NS.PgTarget"], [{"processed": "5"}])
    with pytest.raises(AssertionFailed) as e:
        assert_monitor(fake, "NS.App", [{"metrics": {"processed": 6}}], timeout=0, poll=0)
    assert "processed: shows '5', expected 6" in str(e.value)
    assert e.value.records[0]["status"] == "failed"
    assert e.value.records[0]["actual"]["rows"] == [{"metric": "processed", "value": "5"}]


def test_status_probe_runs_every_poll():
    calls = []
    fake = FakeStriim(["NS.PgTarget"], [{"processed": "6"}])
    assert_monitor(fake, "NS.App", [{"metrics": {"processed": 6}}], timeout=10, poll=0,
                   status_probe=lambda: calls.append(1))
    assert calls == [1]


@pytest.mark.parametrize("want,shown,message", [
    ({"min": 6}, "5", "processed: shows '5', below min 6"),
    ({"max": 4}, "5", "processed: shows '5', above max 4"),
    ({"min": 1, "max": 4}, "5", "processed: shows '5', above max 4"),
    ({"min": 1}, None, "processed: shows None, not a number, expected >= 1"),
])
def test_out_of_bounds_fails_naming_the_bound(want, shown, message):
    fake = FakeStriim(["NS.PgTarget"], [{"processed": shown}])
    with pytest.raises(AssertionFailed) as e:
        assert_monitor(fake, "NS.App", [{"metrics": {"processed": want}}], timeout=0, poll=0)
    assert message in str(e.value)


def test_in_bounds_passes_and_records_the_bound():
    fake = FakeStriim(["NS.PgTarget"], [{"processed": "3"}])
    records = assert_monitor(fake, "NS.App", [{"metrics": {"processed": {"min": 1, "max": 5}}}],
                             timeout=0, poll=0)
    assert records[0]["status"] == "passed"
    assert records[0]["expected"]["rows"] == [{"metric": "processed", "value": ">= 1 and <= 5"}]


def test_a_nan_figure_fails_a_bound():
    assert not _same({"min": 1, "max": 3}, "NaN")


@pytest.mark.parametrize("bound", [{"min": float("nan")}, {"max": float("inf")}])
def test_a_non_finite_bound_is_refused(bound):
    with pytest.raises(MonitorSpecError, match="numeric values"):
        parse_monitor_specs([{"metrics": {"processed": bound}}])


# ---- component: (any component) and string matchers ------------------------------------

POS = "{ContinuationToken[a1]-DocumentTimeStamp[1700000000]-InternalTs[2]-InternalTimeInc[3]}"


class FakeTree:
    """`mon(app)`: a tree with a SOURCE, a CQ and a TARGET; `mon(component)`: its body, recorded."""

    def __init__(self, bodies):
        self.bodies, self.read = bodies, []

    def mon(self, name):
        if name == "NS.App":
            return {"entityType": "APPLICATION", "fullName": name, "components": [
                {"entityType": "SOURCE", "fullName": "NS.Src"},
                {"entityType": "CQ", "fullName": "NS.Shape"},
                {"entityType": "TARGET", "fullName": "NS.Tgt"}]}
        self.read.append(name)
        return self.bodies.get(name, {})


def test_parse_component_and_matchers():
    parse_monitor_specs([{"component": "Src", "metrics": {
        "lastCheckpointedPosition": {"present": True}, "lastEventPosition": {"matches": r"^\^ \{"},
        "gone": {"absent": True}, "input": {"min": 1}, "nested": {"Insert": {"present": True}}}}])
    parse_monitor_specs([{"component": "Src", "metrics": {"pos": {"matches": "^\\^ ${RESTART}$"}}}])


@pytest.mark.parametrize("spec, match", [
    ({"target": "T", "component": "Src", "metrics": {"input": 1}}, "not both"),
    ({"component": "", "metrics": {"input": 1}}, "non-empty component"),
    ({"metrics": {"pos": {"present": False}}}, "'present' takes true"),
    ({"metrics": {"pos": {"absent": "yes"}}}, "'absent' takes true"),
    ({"metrics": {"pos": {"matches": ""}}}, "non-empty regex"),
    ({"metrics": {"pos": {"matches": "("}}}, "not a valid regex"),
    ({"metrics": {"pos": {"present": True, "matches": "x"}}}, "exactly one of"),
    ({"metrics": {"pos": {"present": True, "min": 1}}}, "exactly one of"),
])
def test_parse_refuses_bad_component_and_matchers(spec, match):
    with pytest.raises(MonitorSpecError, match=match):
        parse_monitor_specs([spec])


def test_matchers_present_absent_matches():
    assert _same({"present": True}, "^ x") and _same({"present": True}, 0)
    assert not _same({"present": True}, None) and not _same({"present": True}, "")
    assert _same({"absent": True}, None) and not _same({"absent": True}, "x")
    assert _same({"matches": r"^\^ \{Cont"}, "^ " + POS)
    assert not _same({"matches": r"^@ "}, "^ " + POS) and not _same({"matches": "x"}, None)


def test_component_reads_a_source_and_qualifies_names_the_tree_lacks():
    fake = FakeTree({"NS.Src": {"input": "50"}, "NS.Hidden": {"input": "1"}})
    records = assert_monitor(fake, "NS.App", [{"component": "Src", "metrics": {"input": 50}},
                                              {"component": "Hidden", "metrics": {"input": 1}}],
                             timeout=0, poll=0)
    assert [r["target"] for r in records] == ["NS.Src", "NS.Hidden"]
    assert fake.read[:2] == ["NS.Src", "NS.Hidden"]


def test_captured_tokens_render_into_values_and_are_escaped_in_patterns():
    body = {"lastCheckpointedPosition": "^ " + POS, "lastEventPosition": "^ " + POS}
    fake = FakeTree({"NS.Src": body})
    records = assert_monitor(fake, "NS.App", [{"component": "${SRC}", "metrics": {
        "lastCheckpointedPosition": "^ ${RESTART}",
        "lastEventPosition": {"matches": "^\\^ ${RESTART}$"},
        "lastErrorPosition": {"absent": True}}}],
        timeout=0, poll=0, tokens={"RESTART": POS, "SRC": "Src"})
    assert records[0]["status"] == "passed"
    assert {"metric": "lastEventPosition", "value": "matches /^\\^ " + __import__("re").escape(POS) + "$/"} \
        in records[0]["expected"]["rows"]


def test_a_matcher_miss_names_what_was_shown():
    fake = FakeTree({"NS.Src": {"lastEventPosition": "@ " + POS}})
    with pytest.raises(AssertionFailed) as e:
        assert_monitor(fake, "NS.App", [{"component": "Src", "metrics": {
            "lastEventPosition": {"matches": r"^\^ "}, "lastCheckpointedPosition": {"present": True}}}],
            timeout=0, poll=0)
    assert "lastEventPosition: shows '@ " in str(e.value) and r"does not match /^\^ /" in str(e.value)
    assert "lastCheckpointedPosition: shows None, absent, expected present" in str(e.value)


def test_a_missing_token_fails_loudly():
    from livetest.substitute import SubstitutionError
    with pytest.raises(SubstitutionError, match="RESTART"):
        assert_monitor(FakeTree({}), "NS.App", [{"component": "Src", "metrics": {"p": "^ ${RESTART}"}}],
                       timeout=0, poll=0, tokens={})


def test_two_components_with_one_name_are_refused():
    tree = {"entityType": "APPLICATION", "fullName": "NS.App", "components": [
        {"entityType": "SOURCE", "fullName": "NS.Src"}, {"entityType": "SOURCE", "fullName": "X.Src"}]}
    from livetest.assertions.monitor import find_components, resolve_component
    assert set(find_components(tree)) == {"NS.Src", "X.Src"}
    with pytest.raises(AssertionFailed, match="matches 2"):
        resolve_component(find_components(tree), "Src", "NS.App")
    assert resolve_component([], "Other.Src", "NS.App") == "Other.Src"


# ---- recapture: a moving value re-read on every poll -----------------------------------

class MovingSource:
    """DESCRIBE's restart position advances one step per read (idle heartbeats); MON shows the
    position `lag` reads behind it."""

    def __init__(self, lag=0):
        self.n, self.lag, self.reads = 0, lag, []

    def describe(self, name):
        self.n += 1
        self.reads.append(("describe", name))
        return [{"Checkpoint": [{"Source Restart Position": {"CheckpointText": f"pos-{self.n}"}}]}]

    def mon(self, name):
        if name == "NS.App":
            return {"entityType": "APPLICATION", "fullName": name,
                    "components": [{"entityType": "SOURCE", "fullName": "NS.Src"}]}
        self.reads.append(("mon", name))
        return {"lastCheckpointedPosition": f"^ pos-{self.n - self.lag}"}


RECAP = [{"token": "RESTART", "describe": "Src", "field": "Source Restart Position"}]


def test_recapture_compares_with_a_fresh_read_every_poll():
    fake = MovingSource()
    records = assert_monitor(fake, "NS.App", [{"component": "Src", "recapture": RECAP,
                                               "metrics": {"lastCheckpointedPosition": "^ ${RESTART}"}}],
                             timeout=5, poll=0, tokens={})
    assert records[0]["status"] == "passed" and records[0]["target"] == "NS.Src"
    assert fake.reads[0] == ("describe", "NS.Src")        # the recapture precedes the MON read


def test_without_recapture_a_once_captured_value_is_outrun():
    fake = MovingSource()
    fake.describe("NS.Src")                               # a capture taken once: pos-1
    with pytest.raises(AssertionFailed, match="shows '\\^ pos-2', expected '\\^ pos-1'"):
        fake.n += 1                                       # the checkpoint moved on
        assert_monitor(fake, "NS.App", [{"component": "Src",
                                         "metrics": {"lastCheckpointedPosition": "^ ${RESTART}"}}],
                       timeout=0, poll=0, tokens={"RESTART": "pos-1"})


def test_recapture_fails_when_mon_lags_past_the_timeout():
    fake = MovingSource(lag=1)
    with pytest.raises(AssertionFailed) as e:
        assert_monitor(fake, "NS.App", [{"component": "Src", "recapture": RECAP,
                                         "metrics": {"lastCheckpointedPosition": "^ ${RESTART}"}}],
                       timeout=0.2, poll=0.01, tokens={})
    assert "expected '^ pos-" in str(e.value) and fake.n > 2


def test_recapture_with_no_value_yet_is_a_miss_not_an_error():
    class Empty(MovingSource):
        def describe(self, name):
            return [{"Checkpoint": []}]
    with pytest.raises(AssertionFailed, match="recapture RESTART: DESCRIBE NS.Src shows no value"):
        assert_monitor(Empty(), "NS.App", [{"component": "Src", "recapture": RECAP,
                                            "metrics": {"lastCheckpointedPosition": "^ ${RESTART}"}}],
                       timeout=0, poll=0, tokens={})


@pytest.mark.parametrize("recap", [[], "x", [1]])
def test_parse_refuses_bad_recapture(recap):
    with pytest.raises(MonitorSpecError, match="recapture"):
        parse_monitor_specs([{"component": "Src", "recapture": recap, "metrics": {"input": 1}}])
