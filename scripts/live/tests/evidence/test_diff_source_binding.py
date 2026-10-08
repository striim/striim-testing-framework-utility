"""A diff's ``expected.source`` is bound to the source table this run
witnessed and owned. The acceptance probe is the reviewer's, copied from rev42r4-scratch
``tests/evidence/test_review_r4_probes.py`` (imports adapted only)."""
from __future__ import annotations

import json

import pytest

from livetest import evidence
from livetest.evidence import EvidenceError

from .test_envelope import DIFF_SPEC, SRC_OWNED, doc_of, good, record  # noqa: F401 - good is a fixture
from .test_run_binding import _envelope, checked, produce

pytest_plugins = ["pytester"]   # run_case drives a fresh pytest process through pytester


def test_diff_foreign_source_must_be_refused(run_case):
    def prepare(root, name):
        path = root / 'cases/initial-load/test.yaml'
        text = path.read_text().replace('  data:\n', '  diff:\n')
        text = text.replace('      match: expected/tgt.csv\n', '      source: "${PG_SOURCE_SCHEMA}.${TID}src"\n      source_db: postgres-source\n')
        path.write_text(text)
    run, tier, identity = produce(run_case, prepare=prepare)
    assert run.ret == 0, run.text
    path = _envelope(tier)
    doc = json.loads(path.read_text())
    assert evidence.read(path).qualifies
    original = doc['data']['comparisons'][0]['expected']['source']
    doc['data']['comparisons'][0]['expected']['source'] = 'postgres-source:private.unowned'
    assert not any(r.get('name') == 'private.unowned' for r in doc['resources']['owned'])
    path.write_text(json.dumps(doc))
    rec = checked(tier, identity)
    assert not rec['valid'] and not rec['qualified'], (original, doc['assertions'], rec['valid'], rec['qualified'])


def _diff(good, source="postgres-source:qasource.t1_src", spec=None):
    comp = good._slt_data[0]
    good._slt_data = [{**comp, "type": "diff", "expected": {**comp["expected"], "source": source, "templateSha256": None}}]
    good._slt_records = [record(type="diff", spec=spec or DIFF_SPEC)]
    good._slt_resources["owned"] = [SRC_OWNED]


def _refused(doc):
    with pytest.raises(EvidenceError) as e:
        evidence.validate_case(doc)
    assert e.value.code == "evidence-invalid" and "is not the source table this run witnessed and owned" in str(e.value)


def test_diff_source_the_run_witnessed_and_owned_is_valid(good):
    _diff(good)
    doc = doc_of(good)
    evidence.validate_case(doc)
    assert doc["run"]["qualifies"] is True, doc["run"]["qualifiesReason"]


def test_diff_source_swapped_for_another_owned_table_is_refused(good):
    """Owned-table membership alone is not the binding (R4-2): another table this attempt owns is not the source the
    assertion record declared."""
    _diff(good)
    other = {"kind": "pg-table", "name": "qasource.t1_other", "db": "postgres-source", "state": "verified-absent"}
    good._slt_resources["owned"] = [SRC_OWNED, other]
    doc = doc_of(good)
    evidence.validate_case(doc)
    doc["data"]["comparisons"][0]["expected"]["source"] = "postgres-source:qasource.t1_other"
    _refused(doc)


@pytest.mark.parametrize("owned", [[], [{**SRC_OWNED, "state": "intended"}],
                                   [{**SRC_OWNED, "db": "postgres-target"}]], ids=["absent", "intent-only", "other-route"])
def test_diff_source_not_created_by_this_attempt_is_refused(good, owned):
    _diff(good)
    good._slt_resources["owned"] = owned
    _refused(doc_of(good))


# A token the envelope does not bind is NOT a wildcard. The recorder writes the
# diff source it rendered into its record (``exactdata``), so the record is the witness and the binding is exact.
CUSTOM_SPEC = {"source": "${PG_SOURCE_SCHEMA}.${TID}${SOURCE_NAME}", "source_db": "postgres-source",
               "target": "qatarget.t1_tgt", "target_db": "postgres-target"}
OTHER_OWNED = {"kind": "pg-table", "name": "qasource.t1_other", "db": "postgres-source", "state": "verified-absent"}
OTHER_SCHEMA = {"kind": "pg-table", "name": "qaother.t1_src", "db": "postgres-source", "state": "verified-absent"}


def test_review_p6_r1_unbound_tokens_do_not_admit_another_owned_table(good):
    """The reviewer's counterexample: a record whose source template has tokens the envelope does not bind, an attempt
    that owns both ``qasource.t1_src`` and ``qasource.t1_other``, and only ``expected.source`` swapped to the other."""
    tid = doc_of(good)["inputs"]["bindings"]["TID"]
    src, other = ({**SRC_OWNED, "name": f"qasource.{tid}{n}"} for n in ("src", "other"))
    _diff(good, source=f"postgres-source:qasource.{tid}src", spec=CUSTOM_SPEC)
    good._slt_resources["owned"] = [src, other]
    doc = doc_of(good)
    doc["data"]["comparisons"][0]["expected"]["source"] = f"postgres-source:qasource.{tid}other"
    _refused(doc)


def test_diff_record_with_an_unbound_source_token_fails_closed(good):
    """Nothing binds ``PG_SOURCE_SCHEMA``/``SOURCE_NAME``, so no table is provably the one the record declared --
    not even the right one. The binding fails closed rather than guessing."""
    tid = doc_of(good)["inputs"]["bindings"]["TID"]
    _diff(good, source=f"postgres-source:qasource.{tid}src", spec=CUSTOM_SPEC)
    good._slt_resources["owned"] = [{**SRC_OWNED, "name": f"qasource.{tid}src"}]
    _refused(doc_of(good))


@pytest.mark.parametrize("swap", ["postgres-source:qasource.t1_other", "postgres-source:qaother.t1_src"],
                         ids=["other-owned-table", "other-owned-schema"])
def test_diff_record_with_the_rendered_source_binds_exactly(good, swap):
    """The form the recorder writes: the fully qualified source it read. It stays valid and qualifying, and another
    table or schema this attempt owns on the same route is refused."""
    _diff(good, spec={**CUSTOM_SPEC, "source": "qasource.t1_src"})
    good._slt_resources["owned"] = [SRC_OWNED, OTHER_OWNED, OTHER_SCHEMA]
    doc = doc_of(good)
    evidence.validate_case(doc)
    assert doc["run"]["qualifies"] is True, doc["run"]["qualifiesReason"]
    doc["data"]["comparisons"][0]["expected"]["source"] = swap
    _refused(doc)


def test_diff_record_with_a_bound_identity_token_still_resolves_exactly(good):
    """A token the envelope does bind (the run identity's ``TID``) resolves to exactly its bound value."""
    tid = doc_of(good)["inputs"]["bindings"]["TID"]
    mine = {**SRC_OWNED, "name": f"qasource.{tid}src"}
    _diff(good, source=f"postgres-source:qasource.{tid}src", spec={**CUSTOM_SPEC, "source": "qasource.${TID}src"})
    good._slt_resources["owned"] = [mine, {**mine, "name": f"qasource.{tid}other"}]
    doc = doc_of(good)
    evidence.validate_case(doc)
    doc["data"]["comparisons"][0]["expected"]["source"] = f"postgres-source:qasource.{tid}other"
    _refused(doc)


def test_exact_diff_record_carries_the_source_it_rendered(run_case):
    """End to end: the exact diff's source template uses ``${PG_SOURCE_SCHEMA}``, which the envelope does not bind.
    The assertion record carries the rendered table, equal to the comparison's source, so the envelope validates."""
    def prepare(root, name):
        path = root / 'cases/initial-load/test.yaml'
        text = path.read_text().replace('  data:\n', '  diff:\n')
        text = text.replace('      match: expected/tgt.csv\n', '      source: "${PG_SOURCE_SCHEMA}.${TID}src"\n      source_db: postgres-source\n')
        path.write_text(text)
    run, tier, identity = produce(run_case, prepare=prepare)
    assert run.ret == 0, run.text
    doc = json.loads(_envelope(tier).read_text())
    [rec] = [r for r in doc["assertions"] if r["type"] == "diff"]
    source = doc["data"]["comparisons"][0]["expected"]["source"]
    assert "${" not in rec["spec"]["source"] and source == "postgres-source:" + rec["spec"]["source"], (rec["spec"], source)
    assert evidence.read(_envelope(tier)).qualifies
