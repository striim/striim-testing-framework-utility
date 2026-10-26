"""`action.concurrent` SQL loops: what they ran decides the case. A loop that never succeeds, or
whose statements error, used to be a progress line only, so a case "under concurrent writes"
passed with none."""
import ast
import inspect
import threading
import time
from types import SimpleNamespace

import pytest


def _nested(name, namespace):
    from livetest import plugin
    node = next(n for n in ast.walk(ast.parse(inspect.getsource(plugin)))
                if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<plugin action>", "exec"), namespace)
    return namespace[name]


def _run(tmp_path, ops, fail=lambda admin: False, down_service=None, seconds=0.1):
    (tmp_path / "op.sql").write_text("INSERT INTO t VALUES (1)")
    calls = {"n": 0}

    def run_admin_sql(entry, sql, **kw):
        calls["n"] += 1
        if fail(entry["admin"]):
            raise RuntimeError(f"{entry['admin']} refused")
    ns = dict(m=SimpleNamespace(source_dir=tmp_path, name="c"), threading=threading,
              admins={"postgres-source": {"admin": "pg"}, "mysql-source": {"admin": "my"}},
              tokens={}, ident=SimpleNamespace(run_id="r"), _report=lambda *a: None,
              render=lambda text, tokens: text, _run_admin_sql=run_admin_sql)
    start, stop = _nested("_start_concurrent", ns), _nested("_stop_concurrent", ns)
    flag, threads = start([{"db": db, "file": "op.sql", "loop_interval": 0.01} for db in ops])
    try:
        time.sleep(seconds)
        return stop(flag, threads, down_service=down_service)
    finally:
        flag.set()                                         # never leave a loop running


def test_loops_that_succeed_pass(tmp_path):
    assert _run(tmp_path, ["postgres-source", "mysql-source"]) is None


def test_unknown_route_fails(tmp_path):
    problem = _run(tmp_path, ["postgres-target"])
    assert "op.sql on postgres-target: 0 run(s) succeeded, 1 failed (first: db postgres-target not found)" == problem


def test_statement_errors_fail(tmp_path):
    flaky = iter([False, True] * 100)
    problem = _run(tmp_path, ["postgres-source"], fail=lambda admin: next(flaky))
    assert problem and "op.sql on postgres-source" in problem and "pg refused" in problem


def test_outage_tolerates_errors_on_the_stopped_service_only(tmp_path):
    flaky = iter([False, True] * 100)
    assert _run(tmp_path, ["postgres-source"], fail=lambda admin: next(flaky), down_service="postgres") is None
    problem = _run(tmp_path, ["mysql-source"], fail=lambda admin: True, down_service="postgres")
    assert problem and "mysql-source: 0 run(s) succeeded" in problem


def test_outage_still_needs_one_success(tmp_path):
    problem = _run(tmp_path, ["postgres-source"], fail=lambda admin: True, down_service="postgres")
    assert problem and "0 run(s) succeeded" in problem


def test_no_loops_is_no_problem(tmp_path):
    assert _run(tmp_path, [], seconds=0) is None


@pytest.mark.parametrize("phrase", ["_conc_problem = _stop_concurrent(_action_stop_flag, _action_threads)",
                                    "_conc_problem = _stop_concurrent(_so_flag, _so_threads, down_service=_so_svc)"])
def test_both_actions_fail_the_case_on_a_problem(phrase):
    from livetest import plugin
    src = inspect.getsource(plugin)
    at = src.index(phrase)
    assert 'pytest.fail(f"{m.name}: action concurrent SQL: {_conc_problem}"' in src[at:at + 400]
