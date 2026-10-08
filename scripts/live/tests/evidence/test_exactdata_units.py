"""livetest.exactdata helpers that need no plugin hooks or manifest ``exact:`` block.

The legacy repo exercised these only through exact manifests (tests/evidence/test_exact_assert.py), which
need the manifest and plugin hooks re-applied in a later change. These tests pin the helpers directly.
"""
from __future__ import annotations

import subprocess
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from livetest import canon, exactdata
from livetest.assertions import AssertionFailed

CONTRACTS = Path(__file__).resolve().parents[1] / "fixtures" / "evidence" / "contracts"


# ---------------------------------------------------------------- load-time block

def test_parse_manifest_block_accepts_a_valid_exact_case(tmp_path):
    raw = yaml.safe_load((CONTRACTS / "exact.valid-data-any.yaml").read_text())
    block = exactdata.parse_manifest_block(raw, tmp_path / "test.yaml")
    assert block is not None and block["specs"]


def test_parse_manifest_block_names_the_manifest_on_refusal(tmp_path):
    raw = yaml.safe_load((CONTRACTS / "exact.invalid-no-top-level.yaml").read_text())
    with pytest.raises(exactdata.ExactSpecError, match=r"test\.yaml: exact: .*exact-block-missing"):
        exactdata.parse_manifest_block(raw, tmp_path / "test.yaml")


def test_parse_manifest_block_is_none_without_exact(tmp_path):
    assert exactdata.parse_manifest_block({"name": "c", "assert": {"smoke": True}}, tmp_path / "test.yaml") is None


def test_partition_splits_exact_specs_from_legacy_ones():
    specs = [{"target": "a"}, {"target": "b"}, {"target": "c"}]
    m = SimpleNamespace(exact={"specs": [("data", 1, "decl-b"), ("file", 0, "decl-f")]})
    legacy, exact = exactdata.partition(specs, m, "data")
    assert legacy == [specs[0], specs[2]]
    assert exact == [(1, specs[1], "decl-b")]
    assert exactdata.partition(specs, SimpleNamespace(), "data") == (specs, [])


# ---------------------------------------------------------------- readers

def test_read_pg_refuses_a_non_identifier_order_by_before_reading():
    with pytest.raises(canon.CanonError, match="invalid-declaration"):
        exactdata.read_pg(admin=None, table_sql='"s"."t"', order_by=["id; drop table t"],
                          max_rows=10, max_bytes=100, remaining=1.0)


def test_head_file_reads_one_byte_past_the_cap():
    seen = {}

    def run(argv, timeout):
        seen["argv"], seen["timeout"] = argv, timeout
        return SimpleNamespace(returncode=0, stdout=b"abc", stderr=b"")
    assert exactdata.head_file("node1", "/out/f.json", 10, 2.0, run=run) == b"abc"
    assert seen["argv"] == ["docker", "exec", "node1", "head", "-c", "11", "--", "/out/f.json"]
    assert seen["timeout"] == 2.0


def test_head_file_error_and_timeout_are_read_errors():
    def failing(argv, timeout):
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"No such file")

    def hanging(argv, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)
    with pytest.raises(exactdata.ExactReadError, match="No such file") as e:
        exactdata.head_file("n", "/p", 10, 1.0, run=failing)
    assert e.value.code == "exact-read-error"
    with pytest.raises(exactdata.ExactReadError) as e:
        exactdata.head_file("n", "/p", 10, 1.0, run=hanging)
    assert e.value.code == "exact-read-timeout"


def test_parse_events_keeps_numbers_decimal():
    events = exactdata.parse_events('[{"id": 1, "amount": 1.10},\n{"id": 2, "amount": 2.5}]', "out.json")
    assert events == [{"id": 1, "amount": Decimal("1.10")}, {"id": 2, "amount": Decimal("2.5")}]
    assert not any(isinstance(v, float) for e in events for v in e.values())


def test_parse_events_refuses_a_corrupt_stream_but_tolerates_a_torn_tail():
    with pytest.raises(canon.CanonError, match="invalid-value:actual:out.json"):
        exactdata.parse_events('{"id": 1} {"id": oops} {"id": 3}', "out.json")
    assert exactdata.parse_events('{"id": 1}\n{"id": 2, "na', "out.json") == [{"id": 1}]


# ---------------------------------------------------------------- records

def test_legacy_diff_records_exact_true_specs_as_legacy_text():
    specs = [{"source": "s.a", "target": "t.a", "exact": True, "target_db": "postgres-target"},
             {"source": "s.b", "target": "t.b"}]
    collector = []

    def assert_diff(admins, specs_, **kw):
        return [{"target": "t.a", "status": "passed"}, {"target": "t.b", "status": "passed"}]
    records = exactdata.legacy_diff(assert_diff, {}, specs, collector=collector)
    assert len(records) == 2
    assert collector == [{"index": None, "type": "diff", "target": "t.a", "route": "postgres-target",
                          "profile": canon.LEGACY_PROFILE, "equal": True, "actual": {"owned": False}}]


def test_legacy_diff_records_then_reraises_a_failure():
    collector = []

    def assert_diff(admins, specs_, **kw):
        raise AssertionFailed("differs", [{"target": "t.a", "status": "failed"}])
    with pytest.raises(AssertionFailed):
        exactdata.legacy_diff(assert_diff, {}, [{"target": "t.a", "exact": True}], collector=collector)
    assert collector[0]["equal"] is False and collector[0]["route"] == "postgres-source"


def test_data_section_without_comparisons_is_the_reason():
    assert exactdata.data_section([]) == {"reason": exactdata.NO_DATA_REASON}


def test_data_section_renumbers_and_aggregates_hashed_comparisons():
    a = {"sha256": "sha256:" + "a" * 64, "rowCount": 2}
    e = {"sha256": "sha256:" + "e" * 64}
    comps = [{"index": 7, "profile": canon.PROFILE, "declarationSha256": "sha256:" + "d" * 64,
              "actual": a, "expected": e},
             {"index": 9, "profile": canon.LEGACY_PROFILE, "actual": {"owned": False}}]
    data = exactdata.data_section(comps)
    assert [c["index"] for c in data["comparisons"]] == [0, 1]
    assert data["profile"] == canon.PROFILE and data["rowCount"] == 2
    assert data["canonicalSha256"] == canon.aggregate([a["sha256"]])
    assert data["expectedSha256"] == canon.aggregate([e["sha256"]])
    assert data["normalization"] == [{"index": 0, "declarationSha256": "sha256:" + "d" * 64}]
    assert data["sampleTruncated"] is False
