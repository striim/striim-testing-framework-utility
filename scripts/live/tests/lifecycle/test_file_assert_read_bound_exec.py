"""The file assertion's polling read of a FileWriter output runs under the query bound (review N1).

`read_server_files` reads under the transfer bound (30 min) by default, for a copy of any size. The
file assertion polls that read every couple of seconds within its own timeout, so one `docker exec`
that hung after its listing blocked a single poll for 30 minutes. The assertion's reader now passes
the query bound. The case runs through the real `livetest.plugin` (the executed-plugin harness);
only the reader and `assert_file` are replaced, by recorders.
"""
from livetest import dockercmd

from .exec_harness import run_case  # noqa: F401

pytest_plugins = ["pytester"]

RECORDER = r'''

# ---- the server-file reader and the file assertion replaced by recorders ----
from livetest.resultschema import build_assertion_result as _result


def _read_server_files(ctx, path, **kw):
    log("server_file_read", path=path, kw=kw)
    return ""


def _assert_file(read_fn, specs, *a, **k):
    read_fn(specs[0]["path"])
    return [_result(type="file", status="passed", spec={}, target="t", db=None, detail="ok")]


P.read_server_files = _read_server_files
P.clear_server_files = lambda *a, **k: None
P.assert_file = _assert_file
'''


def _prepare(root, name):
    conftest = root / "conftest.py"
    conftest.write_text(conftest.read_text() + RECORDER)


def _edit(text):
    return text.rstrip() + "\n  file:\n    - {path: /tmp/slt-declared-out.json, min_events: 1}\n"


def test_the_file_assertions_read_runs_under_the_query_bound(run_case):
    run = run_case("legacy", {}, edit=_edit, prepare=_prepare)
    assert run.ret == 0, run.text[-3000:]
    reads = [e for e in run.events() if e["event"] == "server_file_read"]
    assert reads, run.names()
    assert all(e["kw"] == {"read_timeout": dockercmd.QUERY_TIMEOUT_S} for e in reads), reads
