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


@pytest.mark.parametrize('case', ['samples/live/09-raw-file',
    'scripts/live/regression/framework/framework-raw-file'])
def test_raw_sample_projects_only_declared_data_fields(case):
    tql = (REPO / case / 'app.tql').read_text()
    assert 'CREATE TYPE ${TID}row (id Integer, name String);' in tql
    assert 'SELECT TO_INT(e.data[0]), TO_STRING(e.data[1])' in tql
    assert "Members: 'id,name'" in tql
    assert ') INPUT FROM ${TID}rows;' in tql
    assert "Query: 'SELECT id, name FROM ${PG_SOURCE_SCHEMA}.${TID}src ORDER BY id'" in tql


@pytest.mark.parametrize('case', ['samples/live/09-raw-file',
    'scripts/live/regression/framework/framework-raw-file'])
def test_raw_sample_has_no_clock_driven_empty_roll(case):
    tql = (REPO / case / 'app.tql').read_text()
    policy = re.search(r"RolloverPolicy:\s*'([^']+)'", tql)
    assert policy, 'raw sample must override the default timed rollover'
    assert policy.group(1).lower() == 'eventcount:10000'


@pytest.mark.parametrize('case', ['samples/live/09-raw-file',
    'scripts/live/regression/framework/framework-raw-file'])
def test_raw_sample_refuses_even_an_empty_extra_roll(case):
    from livetest.assertions import AssertionFailed
    root = REPO / case
    spec = yaml.safe_load((root / 'test.yaml').read_text())['assert']['file'][0]
    base = re.search(r"FileName:\s*'([^']+)'", (root / 'app.tql').read_text()).group(1)
    tokens = {'OWNED_DIR': '/owned', 'TID': 'case_'}
    stem, extension = render(base, tokens).rsplit('.', 1)
    with pytest.raises(AssertionFailed, match='raw-file-inventory-mismatch'):
        assert_raw_files([spec], timeout=0, tokens=tokens, mode='docker', lifecycle=True,
            inventory=lambda directory: [('node', stem + '.' + n + '.' + extension) for n in ('00','01')],
            read=lambda node, path, *args: b'' if '.01.' in path else b'1,alpha\r\n2,beta\r\n')
