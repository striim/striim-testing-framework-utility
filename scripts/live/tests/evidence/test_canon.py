"""Canonical rows ``slt-canon/1`` (C8.2) and the ``legacy-text/1`` profile (C8.4).

Multiplicity, order, the NULL/empty/absent distinction, declared types, undeclared lossy conversions,
normalization digests, framing, limits, samples and logical digests, each through ``livetest.canon``;
pinned digests come from the hand-written reference cells, not from the code under test."""
from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from decimal import Decimal as D
from pathlib import Path

import pytest
import yaml

from livetest import canon
from livetest.assertions import diff as diff_assertion
from livetest.assertions.data import distinct_set
from livetest.canon import CanonError

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "evidence" / "canon"
CONTRACT_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "evidence" / "contracts"
UTC = dt.timezone.utc
SHA = re.compile(r"^sha256:[0-9a-f]{64}$")
ORDERS = {"id": "integer", "amount": "decimal:2", "created": "timestamptz", "payload": "json",
          "img": "binary:hex", "ok": "boolean"}


def decl(db_route=True, **exact):
    return canon.declaration(exact or True, db_route=db_route)


def refused(code, fn):
    with pytest.raises(CanonError) as ei:
        fn()
    assert ei.value.code == code, str(ei.value)
    return ei.value


def one(typ, expected=None, actual=None):
    """Canonical cell text for one golden text and/or one actual value under ``typ``."""
    d = canon.declaration({"columns": {"c": typ}}, db_route=True)
    out = []
    if expected is not None:
        out.append(canon.canon_row({"c": expected}, d, "expected")[0])
    if actual is not None:
        out.append(canon.canon_row({"c": actual}, d, "actual")[0])
    return out


def orders_actual():
    return [{"id": 1, "amount": D("10.50"), "created": dt.datetime(2026, 3, 1, 8, tzinfo=UTC),
             "payload": {"a": D("1.50"), "b": [1, 2]}, "img": memoryview(bytes.fromhex("deadbeef")), "ok": True, "note": ""},
            {"id": 2, "amount": None, "created": dt.datetime(2026, 3, 1, 9, 30, tzinfo=dt.timezone(dt.timedelta(hours=1, minutes=30))),
             "payload": {}, "img": b"\x00\xff", "ok": False, "note": None}]


# ---------------------------------------------------------------- multiplicity

def test_reread_same_fixture_same_digest():
    d = canon.declaration({"columns": ORDERS}, db_route=True)
    first = canon.compare(canon.parse_golden((FIX / "orders.csv").read_bytes(), db_route=True), orders_actual(), d)
    second = canon.compare(canon.parse_golden((FIX / "orders.csv").read_bytes(), db_route=True), orders_actual(), d)
    assert first["equal"] and second["equal"]
    assert first["expected"]["sha256"] == second["expected"]["sha256"] == first["actual"]["sha256"]
    assert SHA.match(first["expected"]["sha256"]) and first["expected"]["rowCount"] == 2


def test_duplicate_row_changes_digest():
    d = canon.declaration({"columns": ORDERS}, db_route=True)
    once = canon.parse_golden((FIX / "orders.csv").read_bytes(), db_route=True)
    twice = canon.parse_golden((FIX / "orders_dup.csv").read_bytes(), db_route=True)
    a, b = canon.compare(once, [], d), canon.compare(twice, [], d)
    assert a["expected"]["rowCount"] == 2 and b["expected"]["rowCount"] == 3
    assert a["expected"]["sha256"] != b["expected"]["sha256"]
    rows = [{"k": "1"}, {"k": "2"}]
    assert canon.compare(rows, [], decl())["expected"]["sha256"] != \
        canon.compare(rows + [{"k": "1"}], [], decl())["expected"]["sha256"]


def test_extra_duplicate_missing_row_extra_row_each_fail_with_samples():
    golden = [{"id": "1", "v": "a"}, {"id": "2", "v": "b"}]
    base = [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}]
    assert canon.compare(golden, base, decl())["equal"]
    dup = canon.compare(golden, base + [{"id": 1, "v": "a"}], decl())
    assert distinct_set(golden) == distinct_set(base + [{"id": 1, "v": "a"}])     # dedup-blind match would pass
    assert not dup["equal"] and dup["samples"]["missing"] == []
    assert dup["samples"]["extra"] == [{"row": [["id", "text", "1"], ["v", "text", "a"]], "count": 1}]
    missing = canon.compare(golden, base[:1], decl())
    assert not missing["equal"] and missing["samples"]["extra"] == []
    assert missing["samples"]["missing"] == [{"row": [["id", "text", "2"], ["v", "text", "b"]], "count": 1}]
    extra = canon.compare(golden, base + [{"id": 3, "v": "c"}], decl())
    assert not extra["equal"] and extra["samples"]["missing"] == []
    assert extra["samples"]["extra"] == [{"row": [["id", "text", "3"], ["v", "text", "c"]], "count": 1}]
    assert len({dup["actual"]["sha256"], missing["actual"]["sha256"], extra["actual"]["sha256"],
                dup["expected"]["sha256"]}) == 4


# ---------------------------------------------------------------- order

def test_permutation_equal_under_any_and_unequal_under_sequence():
    rows = [{"id": "1"}, {"id": "2"}]
    perm = [{"id": 2}, {"id": 1}]
    anyc = canon.compare(rows, perm, decl(db_route=False))
    assert anyc["equal"] and anyc["order"] == "any"
    seq = canon.compare(rows, perm, decl(db_route=False, order="sequence"))
    assert not seq["equal"] and seq["order"] == "sequence"
    assert seq["samples"]["missing"] == [] and seq["samples"]["extra"] == []
    assert seq["samples"]["firstMismatch"] == {"index": 0, "expected": [["id", "text", "1"]], "actual": [["id", "text", "2"]]}
    db = canon.compare(rows, perm, decl(order="sequence", order_by=["id"]))
    assert not db["equal"]
    assert anyc["declarationSha256"] != seq["declarationSha256"]


# ---------------------------------------------------------------- markers

def test_null_empty_absent_are_three_distinct_cells():
    d = decl(db_route=False)
    null, empty, absent = ({"id": "1", "c": None}, {"id": "1", "c": ""}, {"id": "1"})
    rows = [canon.row_bytes(canon.canon_row(r, d, "actual")) for r in (null, empty, absent)]
    assert len(set(rows)) == 3
    assert rows == [b'[["c",null],["id","text","1"]]', b'[["c","text",""],["id","text","1"]]', b'[["id","text","1"]]']
    golden = canon.parse_golden(b"id,c\n1,<null>\n1,\n1,<absent>\n", db_route=False)
    assert golden == [null, empty, absent]
    digests = {canon.compare([g], [], d)["expected"]["sha256"] for g in golden}
    assert len(digests) == 3


def test_golden_null_and_absent_markers_parse():
    rows = canon.parse_golden((FIX / "events.csv").read_bytes(), db_route=False)
    assert rows == [{"id": "1", "name": "a\nb", "price": "1.5", "tag": "x"}, {"id": "2", "name": None}]
    db_rows = canon.parse_golden(b"id,c\r\n1,<null>\r\n", db_route=True)
    assert db_rows == [{"id": "1", "c": None}]
    # the actual side is never rewritten: a literal marker string in observed data stays text
    assert canon.canon_row({"c": "<null>"}, decl(), "actual") == [["c", "text", "<null>"]]
    assert canon.canon_row({"c": "<absent>"}, decl(db_route=False), "actual") == [["c", "text", "<absent>"]]
    refused("golden-bom", lambda: canon.parse_golden(b"\xef\xbb\xbfid\n1\n", db_route=True))
    refused("golden-undecodable", lambda: canon.parse_golden(b"id\n\xff\n", db_route=True))
    refused("golden-ragged", lambda: canon.parse_golden(b"id,c\n1\n", db_route=True))


def test_absent_marker_refused_for_db_rows():
    e = refused("absent-marker-db-route:c", lambda: canon.parse_golden(b"id,c\n1,<absent>\n", db_route=True))
    assert "<null>" in str(e)


# ---------------------------------------------------------------- declared types

def test_decimal_scale_equal_values_equal():
    texts = {cell[2] for g in ("10.5", "10.50", "1.05e1", "+10.500") for cell in one("decimal:2", expected=g)}
    texts |= {cell[2] for a in (D("10.5"), D("10.500"), D("1.05E+1")) for cell in one("decimal:2", actual=a)}
    assert texts == {"10.50"}
    assert {c[2] for c in one("decimal:2", "-0", D("-0.000"))} == {"0.00"}
    assert one("decimal:0", actual=7) == [["c", "decimal:0", "7"]]
    assert one("decimal:2", "10.51")[0] != one("decimal:2", "10.50")[0]


def test_decimal_lossy_is_refused_not_rounded():
    e = refused("lossy-decimal:expected:c", lambda: one("decimal:2", expected="10.555"))
    assert "10.555" in str(e) and "10.56" not in str(e)
    refused("lossy-decimal:actual:c", lambda: one("decimal:2", actual=D("10.555")))
    refused("lossy-decimal:actual:c", lambda: one("decimal:0", actual=D("0.5")))
    refused("invalid-value:expected:c", lambda: one("decimal:2", expected="ten"))
    refused("invalid-value:actual:c", lambda: one("decimal:2", actual=D("NaN")))


def test_float_undeclared_refused_and_declared_decimal_exactness():
    refused("undeclared-conversion:c:float", lambda: canon.canon_row({"c": 0.1}, decl(), "actual"))
    assert one("decimal:1", "0.1", 0.1) == [["c", "decimal:1", "0.1"]] * 2
    refused("lossy-decimal:actual:c", lambda: one("decimal:2", actual=0.1 + 0.2))
    refused("invalid-value:actual:c", lambda: one("decimal:2", actual=float("inf")))


def test_timestamptz_equivalent_offsets_equal():
    want = [["c", "timestamptz", "2026-03-01T08:00:00.000000Z"]]
    for text in ("2026-03-01T10:00:00+02:00", "2026-03-01 08:00:00Z", "2026-03-01T03:00:00.000-05:00"):
        assert one("timestamptz", expected=text) == want
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    assert one("timestamptz", actual=dt.datetime(2026, 3, 1, 13, 30, tzinfo=ist)) == want
    assert one("timestamptz", actual=dt.datetime(2026, 3, 1, 8, 0, 0, 1, tzinfo=UTC)) != want


@pytest.mark.parametrize("case", ["naive-aware", "aware-naive"])
def test_timestamp_zone_mismatch_refused(case):
    if case == "naive-aware":           # a timestamptz column given a naive value on either side
        refused("timestamp-without-zone:actual:c", lambda: one("timestamptz", actual=dt.datetime(2026, 3, 1, 8)))
        refused("timestamp-without-zone:expected:c", lambda: one("timestamptz", expected="2026-03-01T08:00:00"))
    else:                               # a timestamp column given an aware value on either side
        refused("timestamp-with-zone:actual:c", lambda: one("timestamp", actual=dt.datetime(2026, 3, 1, 8, tzinfo=UTC)))
        refused("timestamp-with-zone:expected:c", lambda: one("timestamp", expected="2026-03-01T08:00:00Z"))
        assert one("timestamp", "2026-03-01T08:00:00", dt.datetime(2026, 3, 1, 8)) == \
            [["c", "timestamp", "2026-03-01T08:00:00.000000"]] * 2


def test_binary_hex_base64_bytes_memoryview_equal():
    raw = bytes.fromhex("deadbeef")
    hexed = {tuple(c) for v in (raw, bytearray(raw), memoryview(raw)) for c in one("binary:hex", actual=v)}
    hexed |= {tuple(c) for c in one("binary:hex", expected="DEADBEEF")}
    assert hexed == {("c", "binary:hex", "deadbeef")}
    assert one("binary:base64", "3q2+7w==", raw) == [["c", "binary:base64", "deadbeef"]] * 2
    assert one("binary:base64", actual="3q2+7w==") == [["c", "binary:base64", "deadbeef"]]
    refused("invalid-value:expected:c", lambda: one("binary:hex", expected="xyz"))
    refused("invalid-value:expected:c", lambda: one("binary:base64", expected="***"))
    refused("invalid-value:actual:c", lambda: one("binary:hex", actual=12))


def test_json_key_order_insensitive_number_text_exact():
    a = one("json", '{"b": 1, "a": [1, 2]}', {"a": [1, 2], "b": 1})
    assert a == [["c", "json", '{"a":[1,2],"b":1}']] * 2
    assert one("json", expected='{"a":1.0}') != one("json", expected='{"a":1.00}')
    assert one("json", '{"a":1.50}', {"a": D("1.50")})[0] == one("json", '{"a":1.50}', {"a": D("1.50")})[1]
    assert one("json", expected='{"k":"ü"}') == [["c", "json", '{"k":"ü"}']]
    refused("invalid-value:expected:c", lambda: one("json", expected='{"a":1,"a":2}'))
    refused("invalid-value:expected:c", lambda: one("json", expected='{"a":NaN}'))
    refused("invalid-value:expected:c", lambda: one("json", expected="{not json"))
    refused("invalid-value:actual:c", lambda: one("json", actual=5))


def test_boolean_only_true_false():
    assert one("boolean", "true", True) == [["c", "boolean", "true"]] * 2
    assert one("boolean", "false", False) == [["c", "boolean", "false"]] * 2
    for text in ("True", "1", "t", "yes", ""):
        refused("invalid-value:expected:c", lambda text=text: one("boolean", expected=text))
    for value in (1, 0, "true"):
        refused("invalid-value:actual:c", lambda value=value: one("boolean", actual=value))
    assert one("integer", actual=D("12")) == [["c", "integer", "12"]]
    refused("invalid-value:actual:c", lambda: one("integer", actual=True))


@pytest.mark.parametrize("value", [D("1.5"), 1.5, dt.datetime(2026, 3, 1), dt.date(2026, 3, 1), dt.time(8, 0),
                                   b"\x01", memoryview(b"\x01"), {"a": 1}, [1], uuid.UUID(int=1)],
                         ids=["Decimal", "float", "datetime", "date", "time", "bytes", "memoryview", "dict", "list", "UUID"])
def test_undeclared_conversion_refused(value):
    e = refused(f"undeclared-conversion:c:{type(value).__name__}",
                lambda: canon.compare([], [{"id": 1, "c": value}], decl()))
    assert "exact.columns.c" in str(e)
    assert canon.canon_row({"c": 7}, decl(), "actual") == [["c", "text", "7"]]
    assert canon.canon_row({"c": True}, decl(), "actual") == [["c", "text", "true"]]


# ---------------------------------------------------------------- normalization

def test_ignore_and_keys_recorded_in_declaration_digest():
    plain = canon.declaration({}, db_route=True)
    ignoring = canon.declaration({"ignore": ["loaded_at"]}, db_route=True)
    keyed = canon.declaration({}, ["id", "loaded_at"], db_route=True)
    assert ignoring.to_dict()["ignore"] == ["loaded_at"] and keyed.to_dict()["keys"] == ["id", "loaded_at"]
    assert len({plain.sha256, ignoring.sha256, keyed.sha256}) == 3
    golden = [{"id": "1", "loaded_at": "x"}]
    actual = [{"id": 1, "loaded_at": "y", "extra": "z"}]
    assert not canon.compare(golden, actual, plain)["equal"]
    both = canon.declaration({"ignore": ["loaded_at"]}, ["id", "loaded_at"], db_route=True)
    res = canon.compare(golden, actual, both)
    assert res["equal"] and res["declaration"] == both.to_dict() and res["declarationSha256"] == both.sha256
    assert canon.compare([{"id": "1"}], [], plain)["expected"]["sha256"] != \
        canon.compare([{"id": "1"}], [], canon.declaration({"columns": {"id": "integer"}}, db_route=True))["expected"]["sha256"]


def test_ignored_column_must_be_present():
    d = canon.declaration({"ignore": ["gone"]}, db_route=True)
    refused("ignored-column-not-present", lambda: canon.compare([{"id": "1"}], [{"id": 1}], d))
    assert canon.compare([{"id": "1", "gone": "a"}], [{"id": 1}], d)["equal"]


# ---------------------------------------------------------------- framing and limits

def test_sequence_order_by_ties_are_order_not_total():
    d = decl(order="sequence", order_by=["id"])
    refused("order-not-total", lambda: canon.compare([], [{"id": 1, "v": "a"}, {"id": 1, "v": "b"}], d))
    refused("order-not-total", lambda: canon.compare([], [{"v": "a"}], d))
    assert canon.compare([{"id": "1"}, {"id": "2"}], [{"id": 1}, {"id": 2}], d)["equal"]
    refused("invalid-declaration", lambda: decl(order_by=["id"]))
    refused("invalid-declaration", lambda: decl(order="sequence"))
    refused("invalid-declaration", lambda: decl(db_route=False, order="sequence", order_by=["id"]))


def test_framing_newline_and_json_injection_unambiguous():
    d = decl(db_route=False)
    joined = canon.compare([{"a": "x\ny"}], [], d)["expected"]["sha256"]
    split = canon.compare([{"a": "x"}, {"a": "y"}], [], d)["expected"]["sha256"]
    assert joined != split
    assert b"\n" not in canon.row_bytes(canon.canon_row({"a": "x\ny"}, d, "actual"))
    injected = canon.compare([{"a": 'x"],["b","text","y'}], [], d)["expected"]["sha256"]
    two_cols = canon.compare([{"a": "x", "b": "y"}], [], d)["expected"]["sha256"]
    assert injected != two_cols
    assert canon.compare([{"a": "b,c"}], [], d)["expected"]["sha256"] != canon.compare([{"a,b": "c"}], [], d)["expected"]["sha256"]


def test_limits_exceeded_fails_and_never_truncates():
    rows = [{"id": str(i)} for i in range(4)]
    refused("canonical-limit-exceeded", lambda: canon.compare(rows[:3], rows, decl(), max_rows=3))
    refused("canonical-limit-exceeded", lambda: canon.compare(rows, rows[:3], decl(), max_rows=3))
    assert canon.compare(rows[:3], rows[:3], decl(), max_rows=3)["equal"]
    size = len(canon.row_bytes(canon.canon_row(rows[0], decl(), "actual"))) + 1
    refused("canonical-limit-exceeded", lambda: canon.compare([], rows, decl(), max_bytes=4 * size - 1))
    res = canon.compare([], rows, decl(), max_bytes=4 * size)
    assert res["actual"]["rowCount"] == 4 and res["limits"] == {"maxRows": canon.DEFAULT_MAX_ROWS, "maxBytes": 4 * size}


def test_samples_bounded_digest_covers_all_rows():
    rows = [{"id": str(i)} for i in range(100)]
    res = canon.compare(rows, [], decl())
    assert len(res["samples"]["missing"]) == canon.SAMPLE_ROWS and res["sampleTruncated"]
    assert res["expected"]["rowCount"] == 100
    changed = rows[:-1] + [{"id": "changed"}]
    later = canon.compare(changed, [], decl())
    assert later["expected"]["sha256"] != res["expected"]["sha256"]
    assert later["samples"]["missing"][:10] == res["samples"]["missing"][:10]     # outside the sample, still in the digest
    big = [{"id": str(i), "blob": "x" * 20000} for i in range(5)]
    res = canon.compare(big, [], decl())
    assert res["sampleTruncated"] and 1 <= len(res["samples"]["missing"]) <= 3
    assert len(json.dumps(res["samples"])) <= canon.SAMPLE_BYTES + 1024
    assert canon.compare(rows[:2], rows[:2], decl())["sampleTruncated"] is False


# ---------------------------------------------------------------- logical digest

def test_logical_digest_equal_across_namespaces_rendered_digest_differs():
    template = b"id,name,app\n1,${TID}widget,${APP}\n"
    runs = []
    for tid in ("a1b2c3d4", "e5f6a7b8"):
        tokens = {"TID": tid, "APP": f"ns_{tid}.orders"}
        golden = canon.parse_golden(template, db_route=True, tokens=tokens)
        actual = [{"id": 1, "name": f"{tid}widget", "app": f"ns_{tid}.orders"}]
        runs.append(canon.compare(golden, actual, decl(), bindings=tokens))
    a, b = runs
    assert a["equal"] and b["equal"]
    assert a["expected"]["sha256"] != b["expected"]["sha256"]
    assert a["logical"] == b["logical"]
    assert a["logical"]["expectedSha256"] == a["logical"]["actualSha256"]
    wrong = canon.compare(canon.parse_golden(template, db_route=True, tokens={"TID": "a1b2c3d4", "APP": "ns_a1b2c3d4.orders"}),
                          [{"id": 2, "name": "a1b2c3d4widget", "app": "ns_a1b2c3d4.orders"}], decl(),
                          bindings={"TID": "a1b2c3d4", "APP": "ns_a1b2c3d4.orders"})
    assert wrong["logical"]["actualSha256"] != a["logical"]["actualSha256"]


# ---------------------------------------------------------------- legacy profile

LEGACY_CORPUS = [
    [], [{"a": "1"}], [{"a": 1}, {"a": 1}], [{"a": None}, {"a": ""}], [{"a": True}, {"a": "true"}],
    [{"a": D("1.50")}, {"a": "1.5"}], [{"a": 1.0}, {"a": "1.0"}], [{"t": dt.datetime(2026, 3, 1, tzinfo=UTC)}],
    [{"b": b"\x01"}, {"b": memoryview(b"\x01")}], [{"a": "1", "b": "2"}, {"b": "2", "a": "1"}],
    [{"j": {"x": 1}}, {"j": [1, 2]}], [{1: "int-key"}, {"1": "int-key"}, {"k": uuid.UUID(int=3)}],
]


def test_legacy_text_multiset_equals_diff_multiset_on_corpus():
    assert len(LEGACY_CORPUS) == 12
    for rows in LEGACY_CORPUS:
        assert canon.legacy_text_multiset(rows) == diff_assertion._multiset(rows), rows
    assert canon.LEGACY_PROFILE == "legacy-text/1"


# ---------------------------------------------------------------- pinned fixtures

def test_pinned_fixture_digests():
    pinned = json.loads((FIX / "digests.json").read_text())
    cells = json.loads((FIX / "reference-cells.json").read_text())
    for name in ("orders.csv", "orders_dup.csv"):
        d = canon.declaration({"columns": ORDERS}, db_route=True)
        res = canon.compare(canon.parse_golden((FIX / name).read_bytes(), db_route=True), [], d)
        assert res["declaration"] == cells[name]["declaration"]
        assert res["declarationSha256"] == pinned[name]["declarationSha256"]
        assert res["expected"]["sha256"] == pinned[name]["sha256"], name
    assert canon.compare(canon.parse_golden((FIX / "orders.csv").read_bytes(), db_route=True), orders_actual(),
                         canon.declaration({"columns": ORDERS}, db_route=True))["actual"]["sha256"] == pinned["orders.csv"]["sha256"]
    d = canon.declaration({"columns": {"price": "decimal:2"}, "order": "sequence"}, db_route=False)
    events = json.loads((FIX / "events.json").read_text(), parse_float=D)
    res = canon.compare(canon.parse_golden((FIX / "events.csv").read_bytes(), db_route=False), events, d)
    assert res["declarationSha256"] == pinned["events"]["declarationSha256"]
    assert res["expected"]["sha256"] == res["actual"]["sha256"] == pinned["events"]["sha256"] and res["equal"]


# ---------------------------------------------------------------- contract fixtures (C8.1, C4 1.9.0)

EXACT_REFUSALS = {"exact.invalid-no-top-level.yaml": "exact-block-missing",
                  "exact.invalid-route.yaml": "exact-route-unsupported",
                  "exact.invalid-lossy-with-rows.yaml": "invalid-exact-spec",
                  "exact.invalid-sequence-no-order-by.yaml": "invalid-declaration"}
CASE_KEYS = {"evidenceVersion", "kind", "run", "inputs", "runtime", "lifecycle", "assertions", "data", "reports",
             "resources", "integrity", "review"}
RUN_KEYS = {"evidenceVersion", "kind", "runId", "invocation", "tier", "label", "createdAt", "identity", "reports",
            "selection", "cases", "checks", "valid", "qualified", "qualifiedReason", "installedWheel"}
RUN_CHECKS = ["junit-present", "junit-wellformed", "junit-invocation", "counts-agree", "one-envelope-per-executed-case",
              "no-foreign-envelopes", "envelope-valid", "case-identity", "cleanup-record", "identity",
              "goldens-unchanged", "no-evidence-errors"]
DATA_REASON = "no exact data assertion"


def _shas(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if (k == "sha256" or k.endswith("Sha256")) and isinstance(v, str):
                yield v
            yield from _shas(v)
    elif isinstance(node, list):
        for v in node:
            yield from _shas(v)


def _check_complete_case(doc):
    assert set(doc) == CASE_KEYS and doc["evidenceVersion"] == 2 and doc["kind"] == "case"
    assert {"invocation", "nodeid"} <= set(doc["run"])
    assert all("fresh" not in r for r in doc["reports"])
    assert all(SHA.match(s) for s in _shas(doc))
    comps = doc["data"]["comparisons"]
    assert comps and all(c["profile"] == canon.PROFILE for c in comps)
    for c in comps:
        assert c["declarationSha256"] == canon.declaration_sha256(c["declaration"])
        assert c["equal"] == (c["expected"]["sha256"] == c["actual"]["sha256"])
    assert doc["data"]["canonicalSha256"] == canon.aggregate([c["actual"]["sha256"] for c in comps])
    assert doc["data"]["expectedSha256"] == canon.aggregate([c["expected"]["sha256"] for c in comps])
    assert doc["run"]["qualifies"] == (all(c["equal"] and c["actual"]["owned"] for c in comps)
                                       and all(g["unchanged"] for g in doc["integrity"]["goldens"].values())
                                       and not doc["integrity"]["evidenceErrors"])


@pytest.mark.parametrize("name", sorted(p.name for pattern in ("exact.*.yaml", "evidence.case.*.json", "evidence.run.*.json")
                                        for p in CONTRACT_FIXTURES.glob(pattern)))
def test_contract_fixtures_parse_as_documented(name):
    path = CONTRACT_FIXTURES / name
    if name.startswith("exact."):
        raw = yaml.safe_load(path.read_text())
        if ".valid-" in name:
            resolved = canon.check_manifest(raw)
            assert resolved is not None and resolved["specs"]
            assert all(isinstance(d, canon.Declaration) for _, _, d in resolved["specs"])
        else:
            refused(EXACT_REFUSALS[name], lambda: canon.check_manifest(raw))
        return
    doc = json.loads(path.read_text())
    if name == "evidence.run.valid.json":
        assert set(doc) == RUN_KEYS and doc["kind"] == "run" and doc["evidenceVersion"] == 2
        assert [c["name"] for c in doc["checks"]] == RUN_CHECKS
        assert doc["valid"] == all(c["ok"] for c in doc["checks"])
        assert not doc["qualified"] or (doc["valid"] and not doc["selection"]["skipped"]
                                        and doc["selection"]["executed"] == doc["selection"]["selected"]
                                        and all(c["qualifies"] for c in doc["cases"]))
        assert all(SHA.match(s) for s in _shas(doc))
        assert doc["reports"]["junit"]["invocationProperty"] == doc["invocation"]
    elif name == "evidence.case.valid.json":
        _check_complete_case(doc)
    elif name == "evidence.case.invalid-partial.json":
        assert "partial" in doc                                   # C7.4 1.9.0: no new partial envelopes
        _check_complete_case({k: v for k, v in doc.items() if k != "partial"})
    elif name == "evidence.case.invalid-reason-in-data.json":
        reason = doc["data"].get("reason")
        assert set(doc["data"]) == {"reason"} and reason != DATA_REASON   # C4 1.9.0: inapplicable only, never pending
    else:
        pytest.fail(f"undocumented contract fixture {name}")
