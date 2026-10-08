from __future__ import annotations
import datetime
import os
import pytest
from livetest.ggtrail.ddl import write_ddl
from livetest.ggtrail.header import write_header

pytestmark = pytest.mark.skipif(not os.environ.get("STRIIM_HOME"), reason="requires STRIIM_HOME for the Java oracle")
# The Java oracle is tools/ggtrail-harness in your test repo: without it, skip, even with STRIIM_HOME set.
from livetest.ggtrail import harness as _ggtrail_harness  # noqa: E402
_NO_HARNESS = _ggtrail_harness.missing_reason()
pytestmark = [pytestmark, pytest.mark.skipif(_NO_HARNESS is not None, reason=_NO_HARNESS or "")]


def test_ddl_decodes_tags(tmp_path):
    from livetest.ggtrail import harness

    ts = datetime.datetime(2026, 7, 14, 12, 0, 0)
    body = write_header(uri="uri:test", filename="rt0000000", seqno=0, creation_time=ts)
    body += write_ddl(schema="SCOTT", object_name="WIDGETS",
                      ddl_text="ALTER TABLE SCOTT.WIDGETS ADD (COLOR VARCHAR2(10))",
                      txn_id=2001, timestamp=ts)
    trail_dir = tmp_path / "trail"
    trail_dir.mkdir()
    (trail_dir / "rt0000000").write_bytes(body)
    # DDL records need no .def table entry (they define schema, not consume it) --
    # pass an empty schema file; SourceDefinitions accepts zero table definitions.
    def_file = tmp_path / "empty.def"
    def_file.write_text("*+- Defgen version 2.0, Encoding UTF-8\n*\nDatabase type: ORACLE\n*\n")

    records = harness.decode_dir(trail_dir, def_file)
    assert records[0]["op_type"] == "DDLOP"
    assert records[0]["schema"] == "SCOTT"
    assert records[0]["object"] == "WIDGETS"
    assert "ADD (COLOR VARCHAR2(10))" in records[0]["ddl"]
