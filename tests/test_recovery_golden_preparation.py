"""Every recovery case ships the one reviewed golden, byte for byte."""
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = runpy.run_path(str(ROOT / "tools/prepare-recovery-goldens.py"))


def test_committed_copies_match_the_source():
    assert len(TOOL["targets"](ROOT)) == 6
    assert TOOL["check"](ROOT) == []


def test_check_reports_and_write_repairs(tmp_path):
    source = tmp_path / TOOL["SOURCE"]
    source.parent.mkdir(parents=True)
    source.write_bytes((ROOT / TOOL["SOURCE"]).read_bytes())
    assert len(TOOL["check"](tmp_path)) == 6
    TOOL["write"](tmp_path)
    assert TOOL["check"](tmp_path) == []
    changed = TOOL["targets"](tmp_path)[0]
    changed.write_text("id,msg\n1,hand-edited\n")
    assert TOOL["check"](tmp_path) == [changed]


def test_the_committed_source_is_the_generate_series_formula():
    lines = (ROOT / TOOL["SOURCE"]).read_text().splitlines()
    assert lines == ["id,msg"] + [f"{n},row-{n}" for n in range(1, 5001)]


def test_check_reports_a_source_that_is_not_the_formula(tmp_path):
    """A recovery defect's output (a duplicated row and a lost one) copied over the source and
    then over every copy with --write: the copies agree with it, the formula does not."""
    source = tmp_path / TOOL["SOURCE"]
    source.parent.mkdir(parents=True)
    rows = [f"{n},row-{n}" for n in range(1, 5001)]
    rows[41] = rows[40]
    source.write_text("id,msg\n" + "\n".join(rows) + "\n")
    TOOL["write"](tmp_path)
    assert TOOL["check"](tmp_path) == [source]


def test_main_names_the_source_when_it_is_not_the_formula(tmp_path, monkeypatch, capsys):
    source = tmp_path / TOOL["SOURCE"]
    source.parent.mkdir(parents=True)
    source.write_text("id,msg\n1,row-1\n")
    TOOL["write"](tmp_path)
    monkeypatch.setitem(TOOL["main"].__globals__, "Path", lambda *_: tmp_path / "tools" / "x.py")
    assert TOOL["main"]([]) == 1
    assert f"{TOOL['SOURCE']} is not (n, row-n) for n=1..5000" in capsys.readouterr().err
