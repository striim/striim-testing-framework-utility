"""expect_halt must read its OWN slice of the shared node log, not a fixed tail of it.

`_NODE_LOG` is one log for the whole cluster. Under SLT_PARALLEL three tests write to it at
once, and the halt assertion used to read a fixed last-20 KB window -- so a test's halt reason
could be pushed out of view by its siblings before the assertion ran. Observed on
spanner-json-parent-child-orphan-no-upsert (3 failures in 5 runs) and
spanner-json-missing-intermediate: the app reached HALT exactly as expected every time, but
expect_halt_contains intermittently reported one or both required substrings missing, and WHICH
one varied with where the 20 KB boundary fell. That is the signature of a truncated window, not
of a wrong expectation or a product bug.
"""
import pytest

from livetest import plugin, stack


class _Ctx:
    def __init__(self, mode="docker"):
        self.mode = mode


def test_marks_record_each_app_nodes_log_size():
    sizes = {n: 4096 + i for i, n in enumerate(stack.app_nodes())}

    def _run(argv):
        node = argv[2]
        return type("R", (), {"stdout": f"{sizes[node]}\n", "returncode": 0})()

    assert plugin._node_log_marks(_Ctx(), run=_run) == sizes


def test_marks_are_empty_off_the_docker_cluster():
    # No containers to inspect; _node_log_since then falls back to the fixed tail.
    assert plugin._node_log_marks(_Ctx(mode="native"), run=lambda a: None) == {}


def test_reads_forward_from_the_mark_not_a_fixed_tail():
    cmds = []

    def _run(argv):
        cmds.append(argv[-1])
        return type("R", (), {"stdout": "log slice", "returncode": 0})()

    marks = {n: 1000 for n in stack.app_nodes()}
    out = plugin._node_log_since(_Ctx(), marks, run=_run)
    assert "log slice" in out
    for cmd in cmds:
        # 1-based offset: mark+1 is the first byte written after the mark.
        assert "tail -c +1001" in cmd, cmd
        # ...and it must still degrade to a bounded tail if the log rotated under us.
        assert "tail -c 20000" in cmd, cmd


def test_a_node_without_a_mark_is_not_read():
    # Its fixed tail would hold an earlier case's halt reason and pass expect_halt_contains.
    cmds = []

    def _run(argv):
        cmds.append(argv)
        return type("R", (), {"stdout": "Duplicate key from an earlier case", "returncode": 0})()

    assert plugin._node_log_since(_Ctx(), {}, run=_run) == ""
    assert cmds == []


def test_a_mark_that_cannot_be_taken_is_left_out():
    def _run(argv):
        raise RuntimeError("docker exec failed")

    assert plugin._node_log_marks(_Ctx(), run=_run) == {}


def test_a_failed_docker_exec_is_not_a_mark_of_zero():
    # subprocess.run does not raise: a restarting container is rc 1 with empty stdout.
    from types import SimpleNamespace
    failed = lambda argv: SimpleNamespace(returncode=1, stdout="", stderr="container is restarting")  # noqa: E731
    assert plugin._node_log_marks(_Ctx(), run=failed) == {}
    junk = lambda argv: SimpleNamespace(returncode=0, stdout="wc: unexpected\n", stderr="")  # noqa: E731
    assert plugin._node_log_marks(_Ctx(), run=junk) == {}
    digits_but_failed = lambda argv: SimpleNamespace(returncode=1, stdout="0\n", stderr="")  # noqa: E731
    assert plugin._node_log_marks(_Ctx(), run=digits_but_failed) == {}


def test_an_empty_log_at_the_mark_is_read_from_its_start():
    cmds = []

    def _run(argv):
        cmds.append(argv[-1])
        return type("R", (), {"stdout": "x", "returncode": 0})()

    plugin._node_log_since(_Ctx(), {n: 0 for n in stack.app_nodes()}, run=_run)
    assert cmds and all("tail -c +1 " in c for c in cmds)


def test_covers_every_app_node():
    seen = []

    def _run(argv):
        seen.append(argv[2])
        return type("R", (), {"stdout": "x", "returncode": 0})()

    plugin._node_log_since(_Ctx(), {n: 5 for n in stack.app_nodes()}, run=_run)
    assert sorted(seen) == sorted(stack.app_nodes())


def test_a_node_that_cannot_be_read_does_not_sink_the_assertion():
    def _run(argv):
        raise RuntimeError("docker exec failed")

    assert plugin._node_log_since(_Ctx(), {n: 5 for n in stack.app_nodes()}, run=_run) == ""


# --- the halt check's reader: a log that could not be read is an error, not an empty slice ----
# `_node_log_since` returns "" for a node it could not read, which suits the OP-poison scan; the
# expect_halt_contains check reads through `_halt_log_tail`, which raises when no node's slice
# could be read, so the docker-mode halt assertion fails with the reason instead of passing.

import pytest  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from livetest.assertions import AssertionFailed  # noqa: E402
from livetest.assertions.halt import assert_halt  # noqa: E402


def test_halt_reader_refuses_a_cluster_whose_marks_failed():
    failing = lambda argv: SimpleNamespace(returncode=1, stdout="", stderr="no such container")  # noqa: E731
    marks = plugin._node_log_marks(_Ctx(), run=failing)
    assert marks == {}
    with pytest.raises(RuntimeError, match="no start mark") as exc:
        plugin._halt_log_tail(_Ctx(), marks, run=failing)
    for node in stack.app_nodes():
        assert node in str(exc.value)


def test_halt_reader_refuses_when_every_read_raises():
    def _run(argv):
        raise RuntimeError("docker exec failed")
    with pytest.raises(RuntimeError, match="docker exec failed"):
        plugin._halt_log_tail(_Ctx(), {n: 5 for n in stack.app_nodes()}, run=_run)


def test_halt_reader_refuses_when_every_read_exits_non_zero():
    failed = lambda argv: SimpleNamespace(returncode=1, stdout="", stderr="container is restarting")  # noqa: E731
    with pytest.raises(RuntimeError, match="container is restarting"):
        plugin._halt_log_tail(_Ctx(), {n: 5 for n in stack.app_nodes()}, run=failed)


def test_halt_reader_uses_the_nodes_it_could_read():
    # A node stopped by the case (a failover scenario) must not hide the reason on the other.
    first, *rest = stack.app_nodes()

    def _run(argv):
        if argv[2] == first:
            return SimpleNamespace(returncode=0, stdout="ORA-00001 unique constraint", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="container is not running")
    marks = {n: 5 for n in stack.app_nodes()}
    assert "ORA-00001" in plugin._halt_log_tail(_Ctx(), marks, run=_run)


def test_halt_reader_returns_nothing_off_the_docker_cluster():
    assert plugin._halt_log_tail(_Ctx(mode="native"), {}, run=lambda a: None) == ""


def test_halt_reader_runs_through_the_bounded_docker_runner(monkeypatch):
    # The halt read is a node-log read like the others: `dockercmd.run` under the query bound, so
    # a read that outlives it has its process group ended, not only its `docker` client.
    from livetest import dockercmd
    calls = []

    def bounded(argv, *, timeout=dockercmd.QUERY_TIMEOUT_S, **kw):
        calls.append((argv[:3], timeout))
        return SimpleNamespace(returncode=0, stdout="ORA-00001 unique constraint", stderr="")
    monkeypatch.setattr(dockercmd, "run", bounded)
    marks = {n: 5 for n in stack.app_nodes()}
    assert "ORA-00001" in plugin._halt_log_tail(_Ctx(), marks)
    assert calls == [(["docker", "exec", n], dockercmd.QUERY_TIMEOUT_S) for n in stack.app_nodes()]


def test_the_poison_scan_still_reads_an_unreadable_node_as_empty():
    def _run(argv):
        raise RuntimeError("docker exec failed")
    assert plugin._node_log_since(_Ctx(), {n: 5 for n in stack.app_nodes()}, run=_run) == ""
    assert plugin._node_log_since(_Ctx(), {}, run=_run) == ""


def test_failed_marks_fail_a_docker_halt_check_end_to_end():
    failing = lambda argv: SimpleNamespace(returncode=1, stdout="", stderr="no such container")  # noqa: E731
    marks = plugin._node_log_marks(_Ctx(), run=failing)

    class Halted:
        def current_status(self, app):
            return "CRASH"
    with pytest.raises(AssertionFailed, match="node log could not be read.*no start mark") as exc:
        assert_halt(Halted(), "NS.App", timeout=1, contains=("ORA-00001",), mode="docker",
                    log_tail=lambda: plugin._halt_log_tail(_Ctx(), marks, run=failing))
    assert exc.value.records == []


# ------------------------------------------------- node boundaries in the halt-reason chain
#
# Each node's slice starts at a byte mark, which can fall inside another app's multi-line
# record, so a slice may begin with a continuation line. Joined into one text, that line used
# to continue the previous node's last record and inherit its namespace.

from livetest.assertions import AssertionFailed  # noqa: E402
from livetest.assertions.halt import assert_halt, log_records  # noqa: E402

_MINE = "2026-11-14 12:00:00 ERROR NS_mine.App NullPointerException\n"
_FOREIGN_CONT = "    caused by: required refusal\n2026-11-14 12:00:01 ERROR NS_other.App boom\n"


class _Halted:
    def current_status(self, app):
        return "CRASH"


def _slices(*texts):
    """A docker-exec double: the i-th app node's slice is texts[i]."""
    by_node = dict(zip(stack.app_nodes(), texts))

    def _run(argv):
        return type("R", (), {"stdout": by_node.get(argv[2], ""), "returncode": 0})()
    return _run


def _halt_on(run):
    marks = {n: 0 for n in stack.app_nodes()}
    return assert_halt(_Halted(), "NS_mine.App", timeout=1, poll=0, contains=("required refusal",),
                       log_tail=lambda: plugin._node_log_since(_Ctx(), marks, run=run))


def test_a_continuation_at_the_start_of_another_nodes_slice_is_not_this_namespaces():
    assert len(stack.app_nodes()) >= 2
    with pytest.raises(AssertionFailed, match="do not name"):
        _halt_on(_slices(_MINE, _FOREIGN_CONT))


def test_a_continuation_in_the_same_nodes_slice_still_counts():
    # The control: the same line, inside node A's own NS_mine record.
    recs = _halt_on(_slices(_MINE + "    caused by: required refusal\n", _FOREIGN_CONT))
    assert recs[0]["status"] == "passed" and "verified" in recs[0]["detail"]


def test_a_slice_without_a_trailing_newline_does_not_run_into_the_next_node():
    with pytest.raises(AssertionFailed, match="do not name"):
        _halt_on(_slices(_MINE.rstrip("\n"), _FOREIGN_CONT))


def test_each_nodes_slice_starts_its_own_record():
    marks = {n: 0 for n in stack.app_nodes()}
    text = plugin._node_log_since(_Ctx(), marks, run=_slices(_MINE, _FOREIGN_CONT))
    recs = log_records(text)
    assert recs[0].strip() == _MINE.strip()
    assert any(r.strip() == "caused by: required refusal" for r in recs), recs


def test_empty_slices_still_read_as_no_log():
    # assert_halt's native-mode caveat (and any caller's emptiness check) tests text.strip().
    marks = {n: 0 for n in stack.app_nodes()}
    assert plugin._node_log_since(_Ctx(), marks, run=_slices("", "")).strip() == ""


def test_the_poison_scan_still_sees_every_nodes_text():
    from livetest import striim_provision as sp
    marks = {n: 0 for n in stack.app_nodes()}
    text = plugin._node_log_since(_Ctx(), marks, run=_slices(
        "ok\n", "java.util.zip.ZipException: invalid LOC header (bad signature)\n"))
    assert sp.op_loader_poisoned(text)
