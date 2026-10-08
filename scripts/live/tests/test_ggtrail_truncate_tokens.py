from __future__ import annotations
import datetime
import os
import pytest
from livetest.ggtrail.record import write_truncate, write_transaction, write_insert
from livetest.ggtrail.header import write_header
from livetest.ggtrail.defwriter import ColumnSpec, TableSchema, write_def_file, ASCII_V

pytestmark = pytest.mark.skipif(not os.environ.get("STRIIM_HOME"), reason="requires STRIIM_HOME for the Java oracle")
# The Java oracle is tools/ggtrail-harness in your test repo: without it, skip, even with STRIIM_HOME set.
from livetest.ggtrail import harness as _ggtrail_harness  # noqa: E402
_NO_HARNESS = _ggtrail_harness.missing_reason()
pytestmark = [pytestmark, pytest.mark.skipif(_NO_HARNESS is not None, reason=_NO_HARNESS or "")]

SCHEMA = TableSchema("SCOTT.WIDGETS", [ColumnSpec("ID", ASCII_V, 10), ColumnSpec("NAME", ASCII_V, 50)])


def _decode(tmp_path, body):
    from livetest.ggtrail import harness
    trail_dir = tmp_path / "trail"
    trail_dir.mkdir()
    (trail_dir / "rt0000000").write_bytes(body)
    def_file = tmp_path / "widgets.def"
    write_def_file(def_file, [SCHEMA])
    return harness.decode_dir(trail_dir, def_file)


def test_truncate_decodes(tmp_path):
    ts = datetime.datetime(2026, 7, 14, 12, 0, 0)
    body = write_header(uri="uri:test", filename="rt0000000", seqno=0, creation_time=ts)
    body += write_truncate(SCHEMA, txn_id=1004, timestamp=ts)
    records = _decode(tmp_path, body)
    assert records[0]["op_type"] == "TRUNCATE"


def test_multi_part_transaction_shares_txn_id(tmp_path):
    ts = datetime.datetime(2026, 7, 14, 12, 0, 0)
    body = write_header(uri="uri:test", filename="rt0000000", seqno=0, creation_time=ts)
    body += write_transaction(SCHEMA, ops=[
        ("insert", {"ID": "1", "NAME": "A"}),
        ("insert", {"ID": "2", "NAME": "B"}),
    ], txn_id=1005, timestamp=ts)
    records = _decode(tmp_path, body)
    assert len(records) == 2
    assert records[0]["txn_id"] == records[1]["txn_id"]
