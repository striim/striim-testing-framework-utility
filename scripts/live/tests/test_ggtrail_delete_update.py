from __future__ import annotations
import datetime
import os
import pytest
from livetest.ggtrail.record import write_delete, write_update
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


def test_delete_decodes_with_before_image(tmp_path):
    ts = datetime.datetime(2026, 7, 14, 12, 0, 0)
    body = write_header(uri="uri:test", filename="rt0000000", seqno=0, creation_time=ts)
    body += write_delete(SCHEMA, {"ID": "42", "NAME": "COMPANY 1"}, txn_id=1002, timestamp=ts)
    records = _decode(tmp_path, body)
    assert records[0]["op_type"] == "DELETE"
    assert records[0]["columns"]["ID"] == "42"


def test_update_decodes_after_image(tmp_path):
    ts = datetime.datetime(2026, 7, 14, 12, 0, 0)
    body = write_header(uri="uri:test", filename="rt0000000", seqno=0, creation_time=ts)
    body += write_update(SCHEMA, before={"ID": "42", "NAME": "OLD"}, after={"ID": "42", "NAME": "NEW"},
                         txn_id=1003, timestamp=ts)
    records = _decode(tmp_path, body)
    assert records[0]["op_type"] == "UPDATE"
    assert records[0]["columns"]["NAME"] == "NEW"
