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


def test_falls_back_to_a_fixed_tail_without_a_mark():
    cmds = []

    def _run(argv):
        cmds.append(argv)
        return type("R", (), {"stdout": "x", "returncode": 0})()

    plugin._node_log_since(_Ctx(), {}, run=_run)
    assert cmds and all("tail" in c and "-c" in c and "20000" in c for c in cmds)


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
