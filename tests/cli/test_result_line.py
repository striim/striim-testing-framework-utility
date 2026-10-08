"""A finished tier says what happened on the console: a passing run said nothing at all, and a
failing one said ``tests-failed`` without naming the test or why, so the reader had to find
live/stdout.log before knowing whether to look further."""
import json
import sys
from types import SimpleNamespace

from _clikit import REPO

sys.path.insert(0, str(REPO / "scripts" / "cli"))

from striim_test import dispatch  # noqa: E402

KAFKA = ("[events-kafka] docker compose up failed for kafka: Container slt-zookeeper Recreate ... "
         "dependency failed to start: container slt-kafka exited (1)")
SELECTED = [{"id": f"live:tests/live/{n}::{n}", "nodeid": f"tests/live/{n}/test.yaml::{n}"}
            for n in ("customers-load-cdc", "events-kafka", "orders-cdc")]


def _tier(tmp_path, monkeypatch, results: dict, rc: int):
    """Run a stand-in tier that writes the guard's two files and exits with `rc`."""
    script = (f"import pathlib\n"
              f"pathlib.Path('selection.json').write_text({json.dumps(json.dumps({'selected': SELECTED}))})\n"
              f"pathlib.Path('results.json').write_text({json.dumps(json.dumps(results))})\n"
              f"raise SystemExit({rc})\n")
    monkeypatch.setattr(dispatch, "build_pytest_argv",
                        lambda *a, **k: ([sys.executable, "-c", script], {"SLT_RUN_EPOCH": "e",
                                                                           "SLT_INVOCATION_ID": "i",
                                                                           "SLT_RUN_IDENTITY": "r"}))
    return dispatch.run_tier(SimpleNamespace(run=tmp_path), "live", tmp_path, label="live",
                             collect_only=False)


def _node(name):
    return {"nodeid": f"tests/live/{name}/test.yaml::{name}"}


def _results(passed=(), failed=()):
    return {"passed": list(passed), "failed": list(failed), "skipped": [], "xfailed": [],
            "errors": []}


def test_a_passing_tier_says_it_passed(tmp_path, monkeypatch, capsys):
    out = _tier(tmp_path, monkeypatch, _results(passed=[_node("customers-load-cdc"),
                                                        _node("events-kafka"), _node("orders-cdc")]), 0)
    assert out.reason == "ok"
    err = capsys.readouterr().err
    assert "striim-test: live: 3 passed\n" in err
    assert f"striim-test: live: ok (exit 0) [logs: {tmp_path / 'live'}]" in err


def test_a_failing_tier_names_the_test_and_why(tmp_path, monkeypatch, capsys):
    out = _tier(tmp_path, monkeypatch,
                _results(passed=[_node("customers-load-cdc"), _node("orders-cdc")],
                         failed=[dict(_node("events-kafka"), message=KAFKA)]), 1)
    assert out.reason == "tests-failed"
    err = capsys.readouterr().err
    assert "striim-test: live: 2 passed, 1 failed\n" in err
    assert f"striim-test: live: FAILED live:tests/live/events-kafka::events-kafka: {KAFKA}\n" in err
    assert "live: tests-failed (exit 1)" in err
    assert f"the whole output is {tmp_path / 'live' / 'stdout.log'}" in err
    # The counts and the failures come first; the exit line stays the last word on the result.
    assert err.index("1 failed") < err.index("FAILED") < err.index("tests-failed (exit 1)")


def test_a_failure_recorded_without_a_message_points_at_the_log(tmp_path, monkeypatch, capsys):
    _tier(tmp_path, monkeypatch, _results(failed=[_node("orders-cdc")]), 1)
    assert "FAILED live:tests/live/orders-cdc::orders-cdc: see stdout.log" in capsys.readouterr().err


def test_failure_line_keeps_the_first_and_the_last_line():
    from striim_test import pytest_guard

    crash = SimpleNamespace(message="[events-kafka] docker compose up failed for kafka: A\n B\n\n"
                                    "dependency failed to start: container slt-kafka exited (1)\n")
    report = SimpleNamespace(longrepr=SimpleNamespace(reprcrash=crash))
    assert pytest_guard._failure_line(report) == (
        "[events-kafka] docker compose up failed for kafka: A ... "
        "dependency failed to start: container slt-kafka exited (1)")
    one = SimpleNamespace(longrepr="AssertionError: 2 rows, expected 3")
    assert pytest_guard._failure_line(one) == "AssertionError: 2 rows, expected 3"
    assert len(pytest_guard._failure_line(SimpleNamespace(longrepr="x" * 1000))) == 400



def test_the_generated_configuration_lists_every_depth_marker():
    # striim-test writes the run's pytest.ini; it listed the service markers and no depth markers,
    # so every case printed a PytestUnknownMarkWarning into live/stdout.log.
    sys.path.insert(0, str(REPO / "scripts" / "live"))
    from livetest.manifest import VALID_DEPTHS

    listed = {m.split(":", 1)[0] for m in dispatch.MARKERS["live"]}
    assert {f"depth_{d}" for d in VALID_DEPTHS} <= listed
