"""The manifest ``exact`` block and exact specs are validated at load (C8.1), through
the real ``load_manifest`` (``ManifestError`` before any provisioning), and an older loader refuses the key."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from livetest import canon
from livetest.manifest import ManifestError, load_manifest

REPO = Path(__file__).resolve().parents[4]
TGT = "${PG_TARGET_SCHEMA}.${TID}tgt"


def _case(tmp_path, **raw) -> Path:
    doc = {"name": "ex", "purpose": "p", "tql": "app.tql", "requires": ["postgres"]}
    doc.update(raw)
    (tmp_path / "expected").mkdir(exist_ok=True)
    (tmp_path / "app.tql").write_text("CREATE APPLICATION ${APP};\nEND APPLICATION ${APP};\n")
    (tmp_path / "expected" / "tgt.csv").write_text("id\n1\n")
    (tmp_path / "test.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    return tmp_path / "test.yaml"


def data(**over):
    spec = {"target": TGT, "target_db": "postgres-target", "match": "expected/tgt.csv", "exact": True}
    spec.update(over)
    return spec


def refused(path, text):
    with pytest.raises(ManifestError, match=text) as ei:
        load_manifest(path)
    assert str(path) in str(ei.value)


@pytest.mark.parametrize("kind", ["data", "file", "diff-mapping"])
def test_spec_level_exact_without_top_level_block_refused(tmp_path, kind):
    spec = {"data": data(), "file": {"path": "${OWNED_DIR}/out.json", "match": "expected/tgt.csv", "exact": True},
            "diff-mapping": {"source": "${PG_SOURCE_SCHEMA}.${TID}src", "target": TGT,
                             "target_db": "postgres-target", "exact": {"columns": {"id": "integer"}}}}[kind]
    tier = kind.split("-")[0]
    refused(_case(tmp_path, **{"assert": {tier: [spec]}}), rf"exact-block-missing: assert\.{tier}\[0\]\.exact")


def test_top_level_version_2_refused(tmp_path):
    refused(_case(tmp_path, exact={"version": 2}, **{"assert": {"data": [data()]}}), "exact.version 2 is not supported")


def test_vacuous_top_level_block_refused(tmp_path):
    refused(_case(tmp_path, exact={"version": 1}, **{"assert": {"data": [{k: v for k, v in data().items() if k != "exact"}]}}),
            r"no exact spec \(vacuous\)")


@pytest.mark.parametrize("route", ["mssql-target", "kafka", "mysql-target"])
def test_non_postgres_route_refused(tmp_path, route):
    refused(_case(tmp_path, exact={"version": 1}, **{"assert": {"data": [data(target_db=route)]}}),
            f"exact-route-unsupported: assert.data\\[0\\]: route '{route}'")


def test_match_path_escaping_case_dir_refused(tmp_path):
    case = tmp_path / "case"
    case.mkdir()
    (tmp_path / "outside.csv").write_text("id\n1\n")
    refused(_case(case, exact={"version": 1}, **{"assert": {"data": [data(match="../outside.csv")]}}),
            "escapes the case dir")


@pytest.mark.parametrize("key,value", [("rows", 1), ("min_rows", 1), ("ordered", True), ("project", "kafka_record")],
                         ids=["rows", "min_rows", "ordered", "project"])
def test_exact_with_rows_min_rows_ordered_project_refused(tmp_path, key, value):
    refused(_case(tmp_path, exact={"version": 1}, **{"assert": {"data": [data(**{key: value})]}}),
            f"invalid-exact-spec: assert.data\\[0\\]: exact cannot be combined with \\['{key}'\\]")


@pytest.mark.parametrize("case", ["sequence-without", "any-with"])
def test_order_by_rules(tmp_path, case):
    exact = {"order": "sequence"} if case == "sequence-without" else {"order": "any", "order_by": ["id"]}
    text = "requires exact.order_by" if case == "sequence-without" else "order_by requires order: sequence"
    refused(_case(tmp_path, exact={"version": 1}, **{"assert": {"data": [data(exact=exact)]}}), text)
    ok = _case(tmp_path, exact={"version": 1},
               **{"assert": {"data": [data(exact={"order": "sequence", "order_by": ["id"]})]}})
    assert load_manifest(ok).exact["specs"][0][2].order_by == ["id"]


def test_unknown_type_spelling_refused(tmp_path):
    for typ in ("numeric", "decimal", "decimal:39", "Decimal:2", "timestamp with time zone"):
        refused(_case(tmp_path, exact={"version": 1}, **{"assert": {"data": [data(exact={"columns": {"id": typ}})]}}),
                "invalid-declaration")
    refused(_case(tmp_path, exact={"version": 1}, **{"assert": {"data": [data(exact={"colums": {}})]}}),
            "unknown exact keys")


def test_legacy_diff_exact_true_without_block_still_loads(tmp_path):
    diff = {"source": "${PG_SOURCE_SCHEMA}.src", "target": "${PG_TARGET_SCHEMA}.tgt", "exact": True}
    m = load_manifest(_case(tmp_path, **{"assert": {"diff": [diff]}}))
    assert m.exact is None and m.assert_["diff"][0]["exact"] is True
    gated = load_manifest(_case(tmp_path, exact={"version": 1}, **{"assert": {"diff": [diff]}}))
    assert [(k, i) for k, i, _ in gated.exact["specs"]] == [("diff", 0)]       # with the block, the diff is exact


# The manifest.py hunks that add the exact block, as (pre-1.9.0, current) pairs.
_EXACT_HUNKS = (
    ('''    "lifecycle",   # C7.2
''', '''    "lifecycle",   # C7.2
    "exact",   # C8.1
'''),
    ('''    lifecycle: object = None            # livetest.lifecycle.LifecycleSpec, or None (legacy)
''', '''    lifecycle: object = None            # livetest.lifecycle.LifecycleSpec, or None (legacy)
    exact: object = None                # livetest.canon.check_manifest resolution, or None
'''),
    ('''        raise ManifestError(str(e)) from None
    return TestManifest(
        name=name,
        lifecycle=_slt_lc_spec,
''', '''        raise ManifestError(str(e)) from None
    # C8.1: the exact block and every exact spec are validated at load, too.
    from livetest import exactdata as _slt_exact
    try:
        _slt_exact_spec = _slt_exact.parse_manifest_block(raw, path, lifecycle=_slt_lc_spec)
    except _slt_exact.ExactSpecError as e:
        raise ManifestError(str(e)) from None
    return TestManifest(
        name=name,
        lifecycle=_slt_lc_spec,
        exact=_slt_exact_spec,
'''),
)


def _manifest_1_8():
    """``livetest/manifest.py`` as the pre-1.9.0 framework ships it: the tree with the exact hunks reversed."""
    text = (REPO / "scripts/live/livetest/manifest.py").read_text()
    for old, new in reversed(_EXACT_HUNKS):
        assert text.count(new) == 1
        text = text.replace(new, old, 1)
    module = type(sys)("_slt_manifest_1_8")
    module.__file__ = str(REPO / "scripts/live/livetest/manifest.py")
    sys.modules[module.__name__] = module           # dataclasses resolve string annotations through sys.modules
    exec(compile(text, module.__file__, "exec"), module.__dict__)
    return module


def test_pre_1_9_loader_refuses_the_block(tmp_path):
    """Passes on HEAD by design: the older reader rejects ``exact`` as an unknown key, never runs it dedup-blind."""
    old = _manifest_1_8()
    assert "exact" not in old.VALID_MANIFEST_KEYS
    path = _case(tmp_path, exact={"version": 1}, **{"assert": {"data": [data()]}})
    with pytest.raises(old.ManifestError, match="exact"):
        old.load_manifest(path)

# ---- a tier that is not a list is a clean refusal (5.4 M2 FS5) ------------------------------

@pytest.mark.parametrize("kind,value", [("diff", True), ("data", True), ("file", {"match": "x"}),
                                        ("diff", "orders")])
def test_a_tier_that_is_not_a_list_is_a_canon_error_naming_the_key(kind, value):
    # `assert: {diff: true}` raised TypeError ('bool' object is not iterable), which the
    # console's detect swallowed along with the case's assert families.
    with pytest.raises(canon.CanonError, match=rf"invalid-assert-shape: assert\.{kind} must be a list"):
        canon.check_manifest({"assert": {kind: value}})


def test_an_assert_that_is_not_a_mapping_is_a_canon_error():
    with pytest.raises(canon.CanonError, match=r"invalid-assert-shape: assert must be a mapping"):
        canon.check_manifest({"assert": ["diff"]})
