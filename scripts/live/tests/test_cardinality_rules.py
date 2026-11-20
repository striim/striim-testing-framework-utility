"""Load-time rules for data and file specs whose comparison cannot see a duplicate or a late row.

A plain `match:` compares distinct sets, so a writer that duplicates every row on retry passes it
unless the spec also pins the cardinality (`rows:`, `ordered: true` or `exact:` on data; `events:`,
`multiset: true` or `exact:` on file). Loading such a spec warns, and a `recover:` case, whose whole
point is what a retry left behind, refuses it.

A `rows: 0` spec passes at the first poll that reads an empty target, which is also what an app that
has not written yet looks like. It is refused unless something proves the app got there: a
positive spec on the same route (evaluated in the same poll, or, for a typed `exact:` spec, before
it), a `lifecycle:` block, or `expect_halt: true` (the data tiers run after the terminal halt).
"""
from __future__ import annotations

import warnings

import pytest
import yaml

from livetest.manifest import CardinalityWarning, ManifestError, load_manifest

SRC = {"db": "postgres-source", "table": "${PG_SOURCE_SCHEMA}.${TID}src"}
TGT = {"db": "postgres-target", "table": "${PG_TARGET_SCHEMA}.${TID}tgt"}
LIFECYCLE = {"version": 1, "mode": "initial-load", "sink": "db",
             "readiness": {"kind": "baseline-landed", "source": SRC, "target": TGT},
             "completion": {"kind": "source-count", "source": SRC, "target": TGT}}
PG_T = "postgres-target"


def _case(tmp_path, assert_, **raw):
    case = tmp_path / "card"
    case.mkdir()
    (case / "app.tql").write_text("CREATE APPLICATION ${APP};\nEND APPLICATION ${APP};\n")
    (case / "expected").mkdir()
    (case / "expected" / "rows.csv").write_text("id\n1\n")
    doc = {"name": "card", "purpose": "hermetic", "tql": "app.tql", "requires": ["postgres"],
           "assert": assert_, **raw}
    (case / "test.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    return case / "test.yaml"


def _load(path):
    """The manifest and the CardinalityWarnings its load raised."""
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        m = load_manifest(path)
    return m, [w for w in seen if issubclass(w.category, CardinalityWarning)]


def _match(**over):
    return {"target": "s.t", "target_db": PG_T, "match": "expected/rows.csv", **over}


# ---- FW-4: a set-only match warns; a recover case refuses it -----------------------------------

def test_a_data_match_without_a_cardinality_pin_warns_at_load(tmp_path):
    path = _case(tmp_path, {"data": [_match()]})
    _m, seen = _load(path)
    assert len(seen) == 1
    msg = str(seen[0].message)
    assert str(path) in msg and "assert.data[0]" in msg and "distinct" in msg
    assert "rows:" in msg and "exact:" in msg          # names the keys that would pin it


def test_a_file_match_without_a_cardinality_pin_warns_at_load(tmp_path):
    _m, seen = _load(_case(tmp_path, {"file": [{"path": "/o/out.json", "match": "expected/rows.csv"}]}))
    assert len(seen) == 1
    msg = str(seen[0].message)
    assert "assert.file[0]" in msg and "events:" in msg and "multiset: true" in msg


@pytest.mark.parametrize("tier,spec", [
    ("data", _match(rows=1)),
    ("data", _match(rows=1, min_rows=1)),
    ("file", {"path": "/o/out.json", "match": "expected/rows.csv", "events": 1}),
    ("file", {"path": "/o/out.json", "match": "expected/rows.csv", "multiset": True}),
])
def test_a_counted_match_does_not_warn(tmp_path, tier, spec):
    _m, seen = _load(_case(tmp_path, {tier: [spec]}))
    assert seen == []


def test_a_typed_exact_match_does_not_warn(tmp_path):
    _m, seen = _load(_case(tmp_path, {"data": [_match(exact=True)]}, exact={"version": 1}))
    assert seen == []


def test_multiset_false_is_not_a_pin(tmp_path):
    _m, seen = _load(_case(tmp_path, {"file": [{"path": "/o/x", "match": "expected/rows.csv",
                                                 "multiset": False}]}))
    assert len(seen) == 1


def test_specs_without_match_do_not_warn(tmp_path):
    _m, seen = _load(_case(tmp_path, {"data": [{"target": "s.t", "min_rows": 1}]}))
    assert seen == []


def test_recover_refuses_a_set_only_data_match(tmp_path):
    path = _case(tmp_path, {"data": [_match()]}, recover={"mode": "stop"})
    with pytest.raises(ManifestError) as ei:
        load_manifest(path)
    msg = str(ei.value)
    assert str(path) in msg and "assert.data[0]" in msg and "recover" in msg and "rows:" in msg


def test_recover_refuses_a_set_only_file_match(tmp_path):
    path = _case(tmp_path, {"file": [{"path": "/o/x", "match": "expected/rows.csv"}]},
                 recover={"mode": "kill"})
    with pytest.raises(ManifestError, match=r"assert\.file\[0\]"):
        load_manifest(path)


@pytest.mark.parametrize("assert_", [
    {"data": [_match(rows=1)]},
    {"file": [{"path": "/o/x", "match": "expected/rows.csv", "multiset": True}]},
    {"data": [{"target": "s.t", "min_rows": 1}]},
])
def test_recover_loads_a_counted_match_or_no_match(tmp_path, assert_):
    m, seen = _load(_case(tmp_path, assert_, recover={"mode": "stop"}))
    assert m.recover["mode"] == "stop" and seen == []


def test_recover_loads_a_match_declared_at_least_once_without_a_warning(tmp_path):
    # An audit branch written at least once is replayed after a restart by design: its match keeps
    # the distinct-set compare (every row present, nothing else) and the recover case loads it.
    m, seen = _load(_case(tmp_path, {"data": [_match(at_least_once=True)]}, recover={"mode": "kill"}))
    assert m.recover["mode"] == "kill" and seen == []


@pytest.mark.parametrize("spec,msg", [
    (_match(at_least_once=False), "must be true"),
    (_match(at_least_once="yes"), "must be true"),
    ({"target": "s.t", "target_db": PG_T, "min_rows": 1, "at_least_once": True}, "needs 'match:'"),
    (_match(at_least_once=True, rows=1), "cannot be combined with 'rows'"),
])
def test_at_least_once_is_refused_where_it_would_mean_nothing_or_contradict(tmp_path, spec, msg):
    with pytest.raises(ManifestError, match=msg):
        load_manifest(_case(tmp_path, {"data": [spec]}))


def test_a_typed_exact_spec_refuses_at_least_once_at_load(tmp_path):
    # The typed exact: path never runs parse_data_specs; canon refuses the combination itself.
    path = _case(tmp_path, {"data": [_match(at_least_once=True, exact={})]}, exact={"version": 1},
                 recover={"mode": "kill"})
    with pytest.raises(ManifestError, match="at_least_once"):
        load_manifest(path)


@pytest.mark.parametrize("extra,named", [
    ({"exact": True}, "'exact'"),
    ({"ordered": True, "project": "kafka_record", "keys": ["value.id"], "target_db": "kafka"}, "'ordered'"),
])
def test_the_parser_refuses_at_least_once_with_a_count(extra, named):
    from livetest.assertions.data import DataSpecError, parse_data_specs
    spec = {"target": "s.t", "match": "expected/rows.csv", "at_least_once": True, **extra}
    with pytest.raises(DataSpecError, match=f"cannot be combined with {named}"):
        parse_data_specs([spec])


# ---- FW-5: rows: 0 needs an anchor --------------------------------------------------------------

def _zero(**over):
    return {"target": "s.violations", "target_db": PG_T, "rows": 0, **over}


def test_rows_zero_alone_is_refused(tmp_path):
    path = _case(tmp_path, {"data": [_zero()]})
    with pytest.raises(ManifestError) as ei:
        load_manifest(path)
    msg = str(ei.value)
    assert str(path) in msg and "assert.data[0]" in msg and "rows: 0" in msg
    assert "lifecycle:" in msg and "expect_halt" in msg


def test_rows_zero_given_as_a_string_is_refused_too(tmp_path):
    with pytest.raises(ManifestError, match="rows: 0"):
        load_manifest(_case(tmp_path, {"data": [_zero(rows="0")]}))


def test_rows_zero_beside_smoke_only_is_refused(tmp_path):
    with pytest.raises(ManifestError, match="rows: 0"):
        load_manifest(_case(tmp_path, {"smoke": True, "data": [_zero()]}))


@pytest.mark.parametrize("positive", [
    {"target": "s.t", "target_db": PG_T, "min_rows": 1},
    {"target": "s.t", "target_db": PG_T, "rows": 3},
    _match(rows=1),
])
def test_rows_zero_beside_a_positive_spec_on_the_same_route_loads(tmp_path, positive):
    m = load_manifest(_case(tmp_path, {"data": [positive, _zero()]}))
    assert len(m.assert_["data"]) == 2


def test_rows_zero_beside_a_positive_spec_on_another_route_is_refused(tmp_path):
    # plugin.admin_groups runs one assert_data call per route, so the positive spec is
    # evaluated in a different poll loop and proves nothing about this one.
    other = {"target": "s.t", "db": "postgres-source", "min_rows": 1}
    with pytest.raises(ManifestError, match=r"assert\.data\[1\].*rows: 0"):
        load_manifest(_case(tmp_path, {"data": [other, _zero()]}))


def test_rows_zero_beside_another_rows_zero_is_refused(tmp_path):
    with pytest.raises(ManifestError, match="rows: 0"):
        load_manifest(_case(tmp_path, {"data": [_zero(), _zero(target="s.other")]}))


def test_rows_zero_beside_a_typed_exact_spec_on_the_same_route_loads(tmp_path):
    m = load_manifest(_case(tmp_path, {"data": [_match(exact=True), _zero()]}, exact={"version": 1}))
    assert m.exact is not None


def test_rows_zero_with_a_lifecycle_block_loads(tmp_path):
    m = load_manifest(_case(tmp_path, {"data": [_zero()]}, lifecycle=LIFECYCLE))
    assert m.lifecycle is not None


def test_rows_zero_with_expect_halt_loads(tmp_path):
    m = load_manifest(_case(tmp_path, {"data": [_zero()]}, expect_halt=True))
    assert m.expect_halt is True


# ---- stable_seconds is refused where nothing would honour it ------------------------------------

def test_stable_seconds_on_a_typed_exact_data_spec_is_refused(tmp_path):
    path = _case(tmp_path, {"data": [_match(exact=True, stable_seconds=5)]}, exact={"version": 1})
    with pytest.raises(ManifestError, match=r"assert\.data\[0\].*stable_seconds"):
        load_manifest(path)


def test_stable_seconds_on_a_typed_exact_diff_spec_is_refused(tmp_path):
    diff = {"source": "s.src", "target": "s.tgt", "source_db": "postgres-source", "target_db": PG_T,
            "exact": {"columns": {"id": "integer"}}, "stable_seconds": 5}
    with pytest.raises(ManifestError, match=r"assert\.diff\[0\].*stable_seconds"):
        load_manifest(_case(tmp_path, {"diff": [diff]}, exact={"version": 1}))


def test_stable_seconds_on_a_legacy_exact_diff_loads(tmp_path):
    # `exact: true` without the top-level block is the legacy multiset compare, which honours it.
    diff = {"source": "s.src", "target": "s.tgt", "exact": True, "stable_seconds": 5}
    m = load_manifest(_case(tmp_path, {"diff": [diff]}))
    assert m.assert_["diff"][0]["stable_seconds"] == 5


def test_stable_seconds_on_a_legacy_data_spec_loads(tmp_path):
    m = load_manifest(_case(tmp_path, {"data": [_match(rows=1, stable_seconds=5)]}))
    assert m.assert_["data"][0]["stable_seconds"] == 5
