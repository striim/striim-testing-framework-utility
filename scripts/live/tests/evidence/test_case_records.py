"""Per-case run records (livetest.caserecord) written by the real ``livetest.plugin`` (pytester, the
tests/lifecycle/exec_harness.py fakes at the infrastructure edges only): what a consumer's incremental
certification reuses, so every outcome, an xdist run and a dirty tree must be recorded as they were."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from livetest import caserecord

pytest_plugins = ["pytester"]   # run_case drives a fresh pytest process through pytester

CASES = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "evidence" / "cases"
XDIST_ENV = {"SLT_PARALLEL": "1"}


def _records(store: Path) -> dict[str, list[dict]]:
    """{process: lines} of the one session under `store`."""
    [session] = [p for p in store.iterdir() if p.is_dir()]
    return {f.stem: [json.loads(ln) for ln in f.read_text().splitlines()] for f in sorted(session.glob("*.jsonl"))}


def _run(run_case, tmp_path, case="initial-load", scenario=None, env=None, **kw):
    store = tmp_path / "records"
    run = run_case(case, {"initial_load": True} if scenario is None else scenario,
                   env={"SLT_CASE_RECORDS": str(store), **(env or {})}, **kw)
    return run, store


def test_a_passing_case_is_recorded_with_its_session_and_end(run_case, tmp_path, monkeypatch):
    monkeypatch.setenv("SLT_PROJECT_ROOT", str(tmp_path))
    run, store = _run(run_case, tmp_path, env={"STRIIM_VERSION": "5.4.0.6", "SLT_UI_FORM": "browser"})
    assert run.ret == 0, run.text
    [(process, lines)] = _records(store).items()
    assert process == "main"
    session, item, end = lines
    assert session["kind"] == "session" and session["schema"] == caserecord.SCHEMA and session["tier"] == "live"
    assert session["env"]["STRIIM_VERSION"] == "5.4.0.6" and session["env"]["SLT_UI_FORM"] == "browser"
    assert "unreadable" in session["source"]                      # tmp_path is no git worktree
    assert item["kind"] == "item" and item["status"] == "passed" and item["item"] == "initial-load"
    assert item["nodeid"].endswith("cases/initial-load/test.yaml::initial-load")
    assert item["variants"] == ["initial-load"] and item["skipReason"] is None and item["xfail"] is None
    assert end["kind"] == "end" and end["exitstatus"] == 0 and end["teardownFailures"] == [] \
        and end["evidenceErrors"] == []
    # The release the engine resolved, never the STRIIM_VERSION setting
    assert item["striim"]["release"] == run_release(run) and item["striim"]["tryouts"] is None
    assert end["source"] == session["source"] and "framework" in end


def run_release(run):
    return json.loads([p for p in (run.root / "live" / "evidence").glob("*/*/evidence.json")][0].read_text()
                      )["runtime"]["striim"]["expected"]["STRIIM_VERSION"]


def test_the_recorded_release_is_the_resolved_one_not_the_setting(run_case, tmp_path, monkeypatch):
    monkeypatch.setenv("SLT_PROJECT_ROOT", str(tmp_path))
    run, store = _run(run_case, tmp_path, env={"STRIIM_VERSION": "9.9.9"})
    assert run.ret == 0, run.text
    _, item, _ = _records(store)["main"]
    assert item["striim"]["release"] != "9.9.9" and item["striim"]["release"] == run_release(run)


def test_an_edit_during_the_run_shows_in_the_end_line(run_case, tmp_path, pytester, monkeypatch):
    root = pytester.path
    monkeypatch.setenv("SLT_PROJECT_ROOT", str(root))

    def prepare(root, name):
        git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run([*git, "init", "-q"], check=True)
        subprocess.run([*git, "add", "cases"], check=True)
        subprocess.run([*git, "commit", "-qm", "cases"], check=True)
        # Edited by the case itself, after the header was read: what another session's edit does.
        conftest = root / "conftest.py"
        conftest.write_text(conftest.read_text() + (
            "\nimport pytest as _p\n@_p.hookimpl(trylast=True)\ndef pytest_runtest_call(item):\n"
            f"    open({str(root / 'cases' / 'initial-load' / 'app.tql')!r}, 'a').write('-- edited mid-run\\n')\n"))
    run, store = _run(run_case, tmp_path, prepare=prepare)
    session, _, end = _records(store)["main"]
    assert session["source"]["head"] == end["source"]["head"]
    assert "cases/initial-load/app.tql" not in session["source"]["dirty"]
    assert "cases/initial-load/app.tql" in end["source"]["dirty"]


def test_a_rename_in_the_worktree_names_both_paths(tmp_path):
    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    (tmp_path / "cases").mkdir()
    (tmp_path / "cases" / "app.tql").write_text("x\n")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "c"], check=True)
    subprocess.run([*git, "mv", "cases/app.tql", "moved.tql"], check=True)
    subprocess.run([*git, "reset", "-q"], check=True)                    # unstaged: the worktree rename
    subprocess.run([*git, "add", "-N", "moved.tql"], check=True)
    dirty = caserecord.git_state(tmp_path)["dirty"]
    assert "cases/app.tql" in dirty and "moved.tql" in dirty


def test_a_failing_case_is_recorded_failed(run_case, tmp_path, monkeypatch):
    monkeypatch.setenv("SLT_PROJECT_ROOT", str(tmp_path))
    run, store = _run(run_case, tmp_path, case="exact-wrong-golden", fixtures=CASES)
    assert run.ret == 1, run.text
    _, item, end = _records(store)["main"]
    assert item["status"] == "failed" and end["exitstatus"] == 1


def test_a_skipped_case_is_recorded_skipped_with_its_reason(run_case, tmp_path, monkeypatch):
    monkeypatch.setenv("SLT_PROJECT_ROOT", str(tmp_path))
    run, store = _run(run_case, tmp_path, case="legacy", scenario={"no_cluster": True},
                      env={"SLT_ALLOW_NO_CLUSTER": "1"})
    assert run.ret == 0, run.text
    _, item, _ = _records(store)["main"]
    assert item["status"] == "skipped" and "no reachable Striim" in item["skipReason"]


def test_the_source_commit_and_dirty_paths_are_recorded(run_case, tmp_path, pytester, monkeypatch):
    root = pytester.path
    monkeypatch.setenv("SLT_PROJECT_ROOT", str(root))

    def prepare(root, name):
        git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run([*git, "init", "-q"], check=True)
        subprocess.run([*git, "add", "cases"], check=True)
        subprocess.run([*git, "commit", "-qm", "cases"], check=True)
        tql = root / "cases" / "initial-load" / "app.tql"
        tql.write_text("-- edited\n" + tql.read_text())
    run, store = _run(run_case, tmp_path, prepare=prepare)
    assert run.ret == 0, run.text
    session, item, _ = _records(store)["main"]
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert session["source"]["head"] == head
    assert "cases/initial-load/app.tql" in session["source"]["dirty"]
    assert item["case"] == "cases/initial-load"


def test_an_xdist_run_records_each_worker_and_the_controller_ends_it(run_case, tmp_path, monkeypatch):
    monkeypatch.setenv("SLT_PROJECT_ROOT", str(tmp_path))
    run, store = _run(run_case, tmp_path, case="legacy", scenario={}, args=("-n", "2", "-p", "xdist.plugin"),
                      env=XDIST_ENV)
    assert run.ret == 0, run.text
    records = _records(store)
    workers = {p: lines for p, lines in records.items() if p.startswith("gw")}
    items = [ln for lines in workers.values() for ln in lines if ln["kind"] == "item"]
    assert len(items) == 1 and items[0]["status"] == "passed", records
    assert all(lines[-1]["kind"] == "end" for lines in records.values())
    assert [ln["kind"] for ln in records["main"]] == ["session", "end"]      # the controller ran no case


def test_records_off_writes_nothing_and_the_default_store_is_the_project(run_case, tmp_path, pytester, monkeypatch):
    monkeypatch.setenv("SLT_PROJECT_ROOT", str(pytester.path))
    run = run_case("initial-load", {"initial_load": True}, env={"SLT_CASE_RECORDS": "off"})
    assert run.ret == 0, run.text
    assert not (pytester.path / ".local-evidence").exists()
    monkeypatch.delenv("SLT_CASE_RECORDS")
    store = caserecord.store_dir(pytester.path, env={})
    assert store == pytester.path.resolve() / ".local-evidence" / "case-records"


def test_a_run_that_records_no_case_leaves_no_file(run_case, tmp_path, monkeypatch):
    monkeypatch.setenv("SLT_PROJECT_ROOT", str(tmp_path))
    run, store = _run(run_case, tmp_path, args=("--collect-only",))
    assert run.ret == 0, run.text
    assert not store.exists() or not any(store.rglob("*.jsonl"))


def test_a_process_that_dies_in_its_first_case_leaves_a_file_without_an_end(run_case, tmp_path, monkeypatch):
    monkeypatch.setenv("SLT_PROJECT_ROOT", str(tmp_path))

    def prepare(root, name):
        conftest = root / "conftest.py"
        conftest.write_text(conftest.read_text() + (
            "\nimport os as _os, pytest as _p\n@_p.hookimpl(tryfirst=True)\ndef pytest_runtest_call(item):\n"
            "    _os._exit(3)\n"))
    run, store = _run(run_case, tmp_path, prepare=prepare)
    assert run.ret != 0
    lines = _records(store)["main"]
    assert [ln["kind"] for ln in lines] == ["session"]


def test_the_jars_a_case_loaded_and_the_set_overrides_are_recorded(run_case, tmp_path, monkeypatch):
    monkeypatch.setenv("SLT_PROJECT_ROOT", str(tmp_path))
    run, store = _run(run_case, tmp_path, scenario={"initial_load": True, "op_stub": True, "op_poison": True},
                      env={"SLT_OPS_PRELOADED": "1", "SLT_PREBUILT_JAR": "/somewhere/x.jar"},
                      edit=lambda y: y.replace("requires: [postgres]\n",
                                               "requires: [postgres]\nop: {jar: java/OpenProcessors/StubOp}\n"))
    session, item, _ = _records(store)["main"]
    assert "SLT_PREBUILT_JAR" in session["envNames"] and "SLT_OPS_PRELOADED" in session["envNames"]
    assert "/somewhere/x.jar" not in json.dumps(session), "names only, never values"
    [jar] = item["jars"]
    assert jar["module"] == "java/OpenProcessors/StubOp"
    assert jar["origin"] == "preloaded"                   # the console pre-flight loaded the bytes: never reusable
    assert jar["sha256"] is not None


def test_an_app_only_case_records_an_empty_jar_list(run_case, tmp_path, monkeypatch):
    monkeypatch.setenv("SLT_PROJECT_ROOT", str(tmp_path))
    run, store = _run(run_case, tmp_path)                 # initial-load names no op:/udf:
    assert run.ret == 0, run.text
    _, item, _ = _records(store)["main"]
    assert item["jars"] == []
