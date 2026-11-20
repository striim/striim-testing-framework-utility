"""DATETIME(192) columns and UPDATEs with before-image records (GoldenGate GETUPDATEBEFORES).

The byte-level tests run anywhere. The decode tests run the trail through Striim's own
GGTrailParser (tools/ggtrail-harness) and need STRIIM_HOME.
"""
from __future__ import annotations
import datetime
import os
import pytest
from livetest.ggtrail import record as rec
from livetest.ggtrail.record import write_update, write_insert
from livetest.ggtrail.header import write_header
from livetest.ggtrail.defwriter import ColumnSpec, TableSchema, write_def_file, ASCII_V, SINT64, DATETIME
from livetest.ggtrail.model import Column, wire_spec
from livetest.ggtrail.writer import TrailFileWriter
from livetest.ggtrail import harness as _ggtrail_harness

TS = datetime.datetime(2026, 7, 14, 12, 0, 0)
SCHEMA = TableSchema("SCOTT.KCBQ", [
    ColumnSpec("ID", SINT64, 19, is_key=True),
    ColumnSpec("NOTE", ASCII_V, 20),
    ColumnSpec("TIME_ROW_UPDATED", DATETIME, 19),
])

_NO_ORACLE = (None if os.environ.get("STRIIM_HOME") else "requires STRIIM_HOME for the Java oracle") \
    or _ggtrail_harness.missing_reason()
needs_oracle = pytest.mark.skipif(_NO_ORACLE is not None, reason=_NO_ORACLE or "")


# --- bytes ------------------------------------------------------------------------------------

@pytest.mark.parametrize("value,text", [
    (datetime.datetime(2026, 1, 1, 2, 0, 0), "2026-01-01:02:00:00"),
    (datetime.datetime(2026, 1, 1, 2, 0, 0, 120000), "2026-01-01:02:00:00.120000"),
    (datetime.date(2026, 7, 1), "2026-07-01"),
    ("2026-07-01 03:00:00", "2026-07-01:03:00:00"),
    ("2026-07-01T03:00:00", "2026-07-01:03:00:00"),
    ("2026-07-01:03:00:00", "2026-07-01:03:00:00"),
])
def test_datetime_is_goldengate_ascii_after_a_two_byte_null_indicator(value, text):
    col = ColumnSpec("D", DATETIME, 19)
    encoded = rec._encode_column(2, col, value)
    span = text.encode("ascii")
    assert encoded == b"\x00\x02" + len(b"\x00\x00" + span).to_bytes(2, "big") + b"\x00\x00" + span


@pytest.mark.parametrize("bad", ["2026/01/01", "2026-01-01 2:00", "yesterday"])
def test_a_malformed_datetime_string_is_refused(bad):
    with pytest.raises(ValueError):
        rec._encode_column(0, ColumnSpec("D", DATETIME, 19), bad)


def test_a_null_datetime_is_the_null_span():
    assert rec._encode_column(0, ColumnSpec("D", DATETIME, 19), None) == b"\x00\x00\x00\x02\xff\xff"


def test_logical_datetime_maps_to_goldengate_datetime():
    spec = wire_spec(Column(name="D", dtype="datetime"))
    assert (spec.gg_type, spec.ext_length) == (DATETIME, 19)


def test_date_and_timestamp_stay_ascii_strings():
    # D3 is unchanged: existing workloads keep their bytes.
    assert wire_spec(Column(name="D", dtype="date")).gg_type == ASCII_V
    assert wire_spec(Column(name="D", dtype="timestamp")).gg_type == ASCII_V


def test_before_image_update_is_a_before_record_then_an_after_record():
    before = {"ID": 2, "NOTE": "n2", "TIME_ROW_UPDATED": None}
    after = {"ID": 2, "NOTE": "u2", "TIME_ROW_UPDATED": "2026-03-03 05:00:00"}
    body = write_update(SCHEMA, before, after, txn_id=7, timestamp=TS, before_image=True)
    first_len = int.from_bytes(body[2:4], "big")
    first, second = body[:first_len], body[first_len:]
    assert int.from_bytes(second[2:4], "big") == len(second)
    for record_bytes, image in ((first, rec._BEFORE), (second, rec._AFTER)):
        header = record_bytes[rec._HEADER_DATA_BEGIN_OFFSET:]
        assert header[rec._BEFORE_AFTER_OFFSET] == image
        assert header[rec._OPERATION_TYPE_OFFSET] == rec._DBOP_UPDATE
    assert first == rec._build_record(SCHEMA, rec._DBOP_UPDATE, rec._BEFORE, before, 7, TS)


def test_without_before_image_the_update_bytes_are_unchanged():
    before = {"ID": 2, "NOTE": "n2"}
    after = {"ID": 2, "NOTE": "u2"}
    assert write_update(SCHEMA, before, after, txn_id=7, timestamp=TS) == \
        rec._build_record(SCHEMA, rec._DBOP_UPDATE, rec._AFTER, after, 7, TS)


def test_before_image_needs_a_before_image_and_a_kept_key():
    with pytest.raises(ValueError):
        write_update(SCHEMA, {}, {"ID": 2}, txn_id=1, timestamp=TS, before_image=True)
    with pytest.raises(NotImplementedError):
        write_update(SCHEMA, {"ID": 2}, {"ID": 3}, txn_id=1, timestamp=TS, before_image=True)


def test_writer_keeps_the_before_and_after_records_in_one_file(tmp_path):
    w = TrailFileWriter(tmp_path, [SCHEMA], max_records_per_file=1, clock=lambda: TS)
    w.insert("SCOTT.KCBQ", {"ID": 1, "NOTE": "n1", "TIME_ROW_UPDATED": None})
    w.update("SCOTT.KCBQ", {"ID": 1, "NOTE": "n1", "TIME_ROW_UPDATED": None},
             {"ID": 1, "NOTE": "u1", "TIME_ROW_UPDATED": "2026-02-02 04:00:00"}, before_image=True)
    w.close()
    files = sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("rt"))
    assert files == ["rt0000000", "rt0000001"]


# --- decoded by Striim's GGTrailParser --------------------------------------------------------

def _decode(tmp_path, ops):
    from livetest.ggtrail import harness
    trail = tmp_path / "trail"
    trail.mkdir()
    body = write_header(uri="uri:test", filename="rt0000000", seqno=0, creation_time=TS)
    for i, op in enumerate(ops):
        body += op(1000 + i)
    (trail / "rt0000000").write_bytes(body)
    write_def_file(tmp_path / "kcbq.def", [SCHEMA])
    return harness.decode_dir(trail, tmp_path / "kcbq.def")


@needs_oracle
def test_datetime_decodes_as_a_date_not_a_string(tmp_path):
    records = _decode(tmp_path, [
        lambda t: write_insert(SCHEMA, {"ID": 1, "NOTE": "n1", "TIME_ROW_UPDATED": "2026-01-01 02:00:00"},
                               txn_id=t, timestamp=TS),
    ])
    # Joda LocalDateTime.toString() -- the reader's default ReturnDateTimeAs. A String column
    # would print as written ("2026-01-01:02:00:00").
    assert records[0]["columns"]["TIME_ROW_UPDATED"] == "2026-01-01T02:00:00.000"


@needs_oracle
def test_before_image_update_decodes_with_before_values_including_a_present_null(tmp_path):
    records = _decode(tmp_path, [
        lambda t: write_update(SCHEMA, {"ID": 1, "NOTE": "n1", "TIME_ROW_UPDATED": None},
                               {"ID": 1, "NOTE": "u1", "TIME_ROW_UPDATED": "2026-02-02 04:00:00"},
                               txn_id=t, timestamp=TS, before_image=True),
        lambda t: write_update(SCHEMA, {"ID": 3, "NOTE": "n3", "TIME_ROW_UPDATED": "2026-07-01 03:00:00"},
                               {"ID": 3, "NOTE": "u3", "TIME_ROW_UPDATED": "2026-07-04 06:00:00"},
                               txn_id=t, timestamp=TS, before_image=True),
        lambda t: write_insert(SCHEMA, {"ID": 9, "NOTE": "after", "TIME_ROW_UPDATED": None},
                               txn_id=t, timestamp=TS),
    ])
    assert [r["op_type"] for r in records] == ["UPDATE", "UPDATE", "INSERT"]
    first, second, third = records
    assert first["columns"] == {"ID": "1", "NOTE": "u1", "TIME_ROW_UPDATED": "2026-02-02T04:00:00.000"}
    assert first["before"] == {"ID": "1", "NOTE": "n1", "TIME_ROW_UPDATED": "null"}
    assert second["before"]["TIME_ROW_UPDATED"] == "2026-07-01T03:00:00.000"
    assert second["columns"]["TIME_ROW_UPDATED"] == "2026-07-04T06:00:00.000"
    assert third["columns"]["NOTE"] == "after"
