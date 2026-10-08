"""Token-rendered golden/expected-file loading (spec §A.5). Golden VALUES (not columns)
can embed object names -- e.g. a GCS object path derived from a table name -- so goldens
are rendered before comparison, same as tql/ddl/seed. Backwards compatible: a golden with
no ${...} tokens renders to itself; passing tokens=None skips rendering entirely."""
from __future__ import annotations
from pathlib import Path

from livetest.substitute import render


def load_expected_text(path: Path, tokens: dict | None) -> str:
    text = path.read_text()
    return render(text, tokens) if tokens else text
