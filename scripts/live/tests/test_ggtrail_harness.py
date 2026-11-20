from __future__ import annotations
import os
import pytest
from pathlib import Path
from livetest.ggtrail import harness

# Striim's own GGTrailParser TestData, which lives in the product repo, not this one. There is
# no default: set GGTRAIL_TESTDATA to its TestData/data directory to run these tests.
_TESTDATA = os.environ.get("GGTRAIL_TESTDATA", "").strip()
FIXTURES = Path(_TESTDATA) if _TESTDATA else None
# The Java oracle needs STRIIM_HOME and tools/ggtrail-harness from your test repo; the skip says how to run it.
from livetest.ggtrail import harness as _ggtrail_harness  # noqa: E402
_NO_ORACLE = _ggtrail_harness.oracle_skip_reason()
pytestmark = pytest.mark.skipif(_NO_ORACLE is not None, reason=_NO_ORACLE or "")


@pytest.mark.skipif(FIXTURES is None or not FIXTURES.exists(), reason=f"GGTrailParser TestData not at {FIXTURES} -- set GGTRAIL_TESTDATA to its TestData/data directory")
def test_decodes_known_posauthorizations_row():
    records = harness.decode_dir(FIXTURES, FIXTURES / "sample.def")
    insert_rows = [r for r in records if r.get("table", "").endswith("POSAUTHORIZATIONS") and r["op_type"] == "INSERT"]
    assert insert_rows, "expected at least one decoded INSERT against SCOTT.POSAUTHORIZATIONS"
    # BUSINESS_NAME's on-wire span is confirmed (Evidence appendix §D) to be
    # [00 00][00 09]["COMPANY 1"] -- colLen 13, NOT 9. convertBytes' ASCII_V path reads
    # span[4:13], so a correct decode returns the full string. This assertion proves the
    # harness works; the *encoder* must reproduce that 4-byte inner prefix (see
    # record._encode_column), which a passing decode alone does not establish.
    business_names = {r["columns"].get("BUSINESS_NAME") for r in insert_rows}
    assert "COMPANY 1" in business_names, (
        f"expected full BUSINESS_NAME value 'COMPANY 1', got {business_names} -- "
        "if this is 'ANY 1' the ASCII_V decode truncates and column values need a "
        "4-byte pad prefix on the wire, not just [colIndex][colLen][value]"
    )
