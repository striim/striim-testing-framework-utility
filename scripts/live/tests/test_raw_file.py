import hashlib

import pytest

from livetest import rawfile
from livetest.assertions import AssertionFailed


def entry(data, records=2):
    return {"bytes": len(data), "sha256": "sha256:" + hashlib.sha256(data).hexdigest(), "records": records}


def spec(data=b"1,alpha\r\n2,bravo\r\n"):
    return {"path": "${OWNED_DIR}/rows.csv", "raw": {"format": "lines", "files": {"rows.csv": entry(data)}}}


@pytest.mark.parametrize("actual", [b"1,alpha\n2,bravo\n", b"2,bravo\r\n1,alpha\r\n"])
def test_raw_bytes_refuse_line_endings_and_reordering(actual):
    with pytest.raises(AssertionFailed):
        rawfile.assert_raw_files([spec()], timeout=0, tokens={"OWNED_DIR": "/owned"}, mode="docker",
            inventory=lambda path: [("node", "/owned/rows.csv")], read=lambda *a: actual)


def test_raw_match_records_hash_and_size():
    data = b"1,alpha\r\n2,bravo\r\n"
    records = rawfile.assert_raw_files([spec()], timeout=0, tokens={"OWNED_DIR": "/owned"}, mode="docker",
        inventory=lambda path: [("node", "/owned/rows.csv")], read=lambda *a: data)
    assert records[0]["status"] == "passed"
    assert records[0]["actual"]["rows"][0]["sha256"] == entry(data)["sha256"]


@pytest.mark.parametrize("names", [["rows.csv", "rows.csv.1"], [], ["rows.csv", "rows.csv"]])
def test_exact_inventory(names):
    with pytest.raises(AssertionFailed):
        rawfile.assert_raw_files([spec()], timeout=0, tokens={"OWNED_DIR": "/owned"}, mode="docker",
            inventory=lambda path: [(str(i), "/owned/" + name) for i, name in enumerate(names)],
            read=lambda *a: b"1,alpha\r\n2,bravo\r\n")


def test_record_count_is_per_file():
    s = spec()
    s["raw"]["files"]["rows.csv"]["records"] = 1
    with pytest.raises(AssertionFailed, match="raw-file-mismatch"):
        rawfile.assert_raw_files([s], timeout=0, tokens={"OWNED_DIR": "/owned"}, mode="docker",
            inventory=lambda path: [("node", "/owned/rows.csv")], read=lambda *a: b"1,alpha\r\n2,bravo\r\n")


@pytest.mark.parametrize("change", [lambda s: s["raw"].update(unknown=1),
    lambda s: s["raw"]["files"].update({"../escape": entry(b"a")}),
    lambda s: s["raw"]["files"]["rows.csv"].update(bytes=True),
    lambda s: s["raw"]["files"]["rows.csv"].update(sha256="bad"),
    lambda s: s["raw"].update(files={})])
def test_raw_schema_refusals(change):
    s = spec()
    change(s)
    with pytest.raises(ValueError, match="invalid-raw-file-spec"):
        rawfile.validate_specs([s])


def test_raw_native_is_refused_without_read():
    with pytest.raises(AssertionFailed, match="raw-file-route-unsupported"):
        rawfile.assert_raw_files([spec()], timeout=0, tokens={"OWNED_DIR": "/owned"}, mode="native",
                                  inventory=lambda *a: pytest.fail("must not read"))
