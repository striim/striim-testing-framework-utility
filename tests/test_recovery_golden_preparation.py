"""Every recovery case ships the one reviewed golden, byte for byte."""
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = runpy.run_path(str(ROOT / "tools/prepare-recovery-goldens.py"))


def test_committed_copies_match_the_source():
    assert len(TOOL["targets"](ROOT)) == 7
    assert TOOL["check"](ROOT) == []


def test_check_reports_and_write_repairs(tmp_path):
    source = tmp_path / TOOL["SOURCE"]
    source.parent.mkdir(parents=True)
    source.write_bytes((ROOT / TOOL["SOURCE"]).read_bytes())
    assert len(TOOL["check"](tmp_path)) == 7
    TOOL["write"](tmp_path)
    assert TOOL["check"](tmp_path) == []
    changed = TOOL["targets"](tmp_path)[0]
    changed.write_text("id,msg\n1,hand-edited\n")
    assert TOOL["check"](tmp_path) == [changed]
