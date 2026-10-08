from __future__ import annotations
import os
import pytest
from pathlib import Path
from livetest import project
from livetest.ggtrail import harness

# Striim's own GGTrailParser TestData, which lives in the product repo, not this one. There is
# no default: set GGTRAIL_TESTDATA to its TestData/data directory to run these tests.
_TESTDATA = os.environ.get("GGTRAIL_TESTDATA", "").strip()
FIXTURES = Path(_TESTDATA) if _TESTDATA else None
# Second, in-repo ground truth (spec §9.5): a real GG-produced trail shipped with the
# repo. Its endianness and Defgen version (cust.def is 4.0, not the 2.0 grammar we emit)
# are unconfirmed, so its test is xfail-not-strict until someone runs it on the Mac.
# Under the project root (SLT_PROJECT_ROOT; default this clone), where a test repo ships it.
REPO_FIXTURES = project.example_root() / "tools" / "scripts" / "database" / "gg_trail_file"

pytestmark = pytest.mark.skipif(not os.environ.get("STRIIM_HOME"), reason="requires STRIIM_HOME for the Java oracle")
# The Java oracle is tools/ggtrail-harness in your test repo: without it, skip, even with STRIIM_HOME set.
from livetest.ggtrail import harness as _ggtrail_harness  # noqa: E402
_NO_HARNESS = _ggtrail_harness.missing_reason()
pytestmark = [pytestmark, pytest.mark.skipif(_NO_HARNESS is not None, reason=_NO_HARNESS or "")]


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


@pytest.mark.xfail(strict=False, reason="dm000003 endianness/def-version unconfirmed")
@pytest.mark.skipif(not REPO_FIXTURES.exists(), reason="in-repo gg_trail_file fixture missing")
def test_decodes_in_repo_dm000003_fixture():
    # Free corroboration against a second real GG trail (spec §9.5). cust.def is Defgen
    # 4.0 and the trail's byte order is unverified, so a failure here is informative,
    # not a regression -- hence xfail(strict=False).
    records = harness.decode_dir(REPO_FIXTURES, REPO_FIXTURES / "cust.def", wildcard="dm*")
    assert records, "expected at least one decoded record from tools/scripts/database/gg_trail_file/dm000003"
