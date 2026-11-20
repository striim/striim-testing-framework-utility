"""A tier declared under `assert:` is dispatched by the plugin, recorded, and fails the case when
it fails.

`smoke: true` beside a tier keeps the "no supported assertion ran" guard quiet, so a dispatch
branch that stopped running would leave the case green with the declared assertion unasserted.
Each test runs the `legacy` fixture through the real `livetest.plugin` (the executed-plugin
harness, fakes at the infrastructure edges) with one tier added to its manifest. Only that
tier's engine function is replaced, by a recorder that logs the spec it was handed; the
manifest parse, the dispatch branch and the outcome are the plugin's own. The engine functions
have their own unit tests.
"""
import pytest

from .exec_harness import run_case  # noqa: F401

pytest_plugins = ["pytester"]

RECORDER = r'''

# ---- one tier's engine function replaced by a recorder ----
from livetest import node_fault as _node_fault
from livetest.assertions import AssertionFailed as _AF
from livetest.resultschema import build_assertion_result as _result


def _recorder(tier):
    def fake(*a, **k):
        log("tier_dispatched", tier=tier, args=repr((a, k)))
        status = "failed" if SC.get("tier_fail") else "passed"
        record = _result(type=tier, status=status, spec={}, target="t", db=None, detail="recorder " + status)
        if status == "failed":
            raise _AF("recorder: " + tier + " failed", [record])
        return [record]
    return fake


_OWNERS = {"P": P, "node_fault": _node_fault}
for _ref, _tier in SC.get("tier_patch", {}).items():
    _owner, _name = _ref.split(".")
    setattr(_OWNERS[_owner], _name, _recorder(_tier))
if SC.get("gcs_edges"):
    # the gcs service's own edges: no emulator, no key upload
    P._gcs_endpoint_ip = lambda ctx, run=None: "127.0.0.1"
    P._gcs_admin = lambda base: SimpleNamespace(delete_bucket=lambda b: None)
    P.ensure_fake_gcp_key = lambda *a, **k: None
if SC.get("bigtable_edges"):
    # the bigtable service's own edge: one admin under both routes, no emulator
    _bt_admin = SimpleNamespace(list_owned_tables=lambda: [])
    P._bigtable_admins = lambda base: [(_bt_admin, "bigtable-target"), (_bt_admin, "bigtable-source")]
'''

# tier -> (lines added under assert:, top-level lines, function replaced, a spec value it must receive)
TIERS = {
    "checkpoint_history": ("  checkpoint_history: nonempty\n", "",
                           "P.assert_checkpoint_history", "nonempty"),
    "json": ("  json:\n    - {target: qatarget.t, db: postgres-target, column: declareddoc, match: expected.json, key: id}\n",
             "", "P.assert_json", "declareddoc"),
    "jmx": ("  jmx:\n    - bean: {domain: com.example, type: FooOp, component: op1}\n      attributes: {DeclaredCount: 1}\n",
            "", "P.assert_jmx", "DeclaredCount"),
    "gcs": ("  gcs:\n    - {bucket: declaredbucket, object: o, size: 1}\n", "", "P.assert_gcs", "declaredbucket"),
    "bigtable": ("  bigtable:\n    - {table: declaredtable, db: bigtable-target, expected: []}\n", "",
                 "P.assert_bigtable", "declaredtable"),
    "placement": ("  placement:\n    - {flow: '${NS}.declaredflow', node: primary}\n", "",
                  "node_fault.assert_placement", "declaredflow"),
    # the top-level key, polled for a terminal HALT and recorded as tier `halt`
    "halt": ("", "expect_halt: true\n", "P.assert_halt", None),
}
ALL = sorted(TIERS)


def _edit(tier):
    assert_lines, top_lines, _fn, _marker = TIERS[tier]

    def edit(text):
        assert text.rstrip().endswith("smoke: true"), text
        if tier == "gcs":
            text = text.replace("requires: [postgres]", "requires: [postgres, gcs]")
        if tier == "bigtable":
            text = text.replace("requires: [postgres]", "requires: [postgres, bigtable]")
        return text.rstrip() + "\n" + assert_lines + top_lines
    return edit


def _prepare(root, name):
    conftest = root / "conftest.py"
    conftest.write_text(conftest.read_text() + RECORDER)


def _scenario(tier, **extra):
    return {"tier_patch": {TIERS[tier][2]: tier}, "gcs_edges": tier == "gcs",
            "bigtable_edges": tier == "bigtable", **extra}


@pytest.mark.parametrize("tier", ALL)
def test_a_declared_tier_is_dispatched_and_recorded(run_case, tier):
    run = run_case("legacy", _scenario(tier), edit=_edit(tier), prepare=_prepare)
    assert run.ret == 0, run.text[-3000:]
    dispatched = [e for e in run.events() if e["event"] == "tier_dispatched"]
    assert [e["tier"] for e in dispatched] == [tier], run.names()
    marker = TIERS[tier][3]
    if marker is not None:
        assert marker in dispatched[0]["args"], dispatched[0]["args"]
    assert [a["type"] for a in run.sidecar()["assertions"]] == ["smoke", tier]


@pytest.mark.parametrize("tier", ALL)
def test_a_declared_tier_that_fails_fails_the_case(run_case, tier):
    run = run_case("legacy", _scenario(tier, tier_fail=True), edit=_edit(tier), prepare=_prepare)
    assert run.ret == 1, run.text[-3000:]
    status, _props, xml = run.junit()
    assert status == "failed", xml
    failed = [a for a in run.sidecar()["assertions"] if a["status"] == "failed"]
    assert [a["type"] for a in failed] == [tier]
