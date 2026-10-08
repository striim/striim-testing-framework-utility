from __future__ import annotations
import datetime
import os
import pytest
from pathlib import Path
from livetest.ggtrail.header import write_header

pytestmark = pytest.mark.skipif(not os.environ.get("STRIIM_HOME"), reason="requires STRIIM_HOME for the Java oracle")
# The Java oracle is tools/ggtrail-harness in your test repo: without it, skip, even with STRIIM_HOME set.
from livetest.ggtrail import harness as _ggtrail_harness  # noqa: E402
_NO_HARNESS = _ggtrail_harness.missing_reason()
pytestmark = [pytestmark, pytest.mark.skipif(_NO_HARNESS is not None, reason=_NO_HARNESS or "")]


def test_header_only_file_is_readable_by_the_real_decoder(tmp_path):
    from livetest.ggtrail import harness
    from livetest.ggtrail.defwriter import ColumnSpec, TableSchema, write_def_file, ASCII_V

    trail_dir = tmp_path / "trail"
    trail_dir.mkdir()
    header_bytes = write_header(
        uri="uri:localhost:localdomain::media:WebAction:OGG",
        filename="rt0000000",
        seqno=0,
        creation_time=datetime.datetime(2026, 7, 14, 12, 0, 0),
    )
    (trail_dir / "rt0000000").write_bytes(header_bytes)
    def_file = tmp_path / "empty.def"
    write_def_file(def_file, [TableSchema("SCOTT.WIDGETS", [ColumnSpec("ID", ASCII_V, 10)])])

    # A header-only file with no records should decode without error (empty result list),
    # proving the file/header framing itself (signature, section TLVs, lengths) is valid --
    # exactly mirroring TestData/data/rt000000, which is header-only and 1067 bytes.
    records = harness.decode_dir(trail_dir, def_file)
    assert records == []
