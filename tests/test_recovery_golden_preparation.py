"""Shared recovery expectations remain reviewed input, including local edits."""
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_preparation_preserves_modified_expectations(tmp_path):
    prepare = runpy.run_path(str(ROOT / "tools/prepare-recovery-goldens.py"))["prepare"]
    source = tmp_path / "samples/live/14-progress-recovery/expected/rows.csv"
    source.parent.mkdir(parents=True)
    source.write_bytes((ROOT / source.relative_to(tmp_path)).read_bytes())
    prepare(tmp_path)
    copies = list(tmp_path.rglob("rows.csv"))
    assert len(copies) == 8 and all(p.read_bytes() == source.read_bytes() for p in copies)
    changed = next(p for p in copies if p != source)
    changed.write_text("id,msg\n1,hand-edited\n")
    with pytest.raises(SystemExit, match="Refusing to overwrite a modified golden"):
        prepare(tmp_path)
    assert changed.read_text() == "id,msg\n1,hand-edited\n"
