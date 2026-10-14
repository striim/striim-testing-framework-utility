"""The sample inventory follows FileWriter's documented initial counter 00."""
from pathlib import Path
import posixpath
import re
import pytest
import yaml
from livetest.rawfile import assert_raw_files
from livetest.substitute import render

REPO = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("case", ["samples/live/09-raw-file",
    "scripts/live/regression/framework/framework-raw-file"])
def test_raw_sample_accepts_documented_first_rolled_file(case):
    root = REPO / case
    spec = yaml.safe_load((root / "test.yaml").read_text())["assert"]["file"][0]
    base = re.search(r"FileName:\s*'([^']+)'", (root / "app.tql").read_text()).group(1)
    tokens = {"OWNED_DIR": "/owned", "TID": "case_"}
    stem, extension = render(base, tokens).rsplit(".", 1)
    rolled = stem + ".00." + extension
    records = assert_raw_files([spec], timeout=1, tokens=tokens, mode="docker", lifecycle=True,
        owned=lambda *args: True, inventory=lambda directory: [("node", rolled)],
        read=lambda *args: b"1,alpha\r\n2,beta\r\n")
    assert records[0]["status"] == "passed"
    assert records[0]["actual"]["rows"][0]["name"] == posixpath.basename(rolled)
