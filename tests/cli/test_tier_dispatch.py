"""Verify: collect live/integration/perf synthetic inputs with exact IDs, no duplicates or
wrong-tier items; one engine plugin per process; skipped selected items are not a pass
."""
import json

from _clikit import framework_env, run_cli

LIVE = {"live:cases/live/alpha::alpha", "live:cases/live/twin::twin",
        "live:cases/live/hello-single::hello-single"}
INTEGRATION = {"integration:cases/integration/twin::twin",
               "integration:cases/integration/needs-absent::needs-absent"}
PERF = {"perf:cases/perf/p1::p1"}
OFF = "integration:cases/integration/off::off"


def _ids(r) -> set:
    ids = r.ids()
    assert len(ids) == len(set(ids)), ids
    return set(ids)



def test_list_exact_ids_per_tier(project, elsewhere):
    _list_exact_ids((("live", LIVE), ("integration", INTEGRATION)), project, elsewhere)


def test_list_exact_ids_perf_tier(project, elsewhere):
    _list_exact_ids((("perf", PERF),), project, elsewhere)


def _list_exact_ids(tiers, project, elsewhere):
    for tier, expected in tiers:
        r = run_cli(["list", "--tier", tier, "--targets", project], cwd=elsewhere)
        assert r.rc == 0, r.stderr
        assert _ids(r) == expected
        sel = r.part(tier)
        assert {e["id"] for e in sel["selected"]} == expected
        assert sorted(p.name for p in r.run_dir.iterdir() if p.is_dir()) == [tier]
        if tier == "integration":
            off = [d for d in sel["deselected"] if d["id"] == OFF]
            assert len(off) == 1 and off[0]["reason"].startswith("disabled: synthetic")
            assert f"# deselected {OFF} (disabled: synthetic" in r.stdout
            assert all(e["kind"] == "IntYamlItem" for e in sel["selected"])
        if tier == "perf":
            assert all(e["kind"] == "PerfYamlItem" for e in sel["selected"])


def test_no_tier_runs_each_tier_once(project, elsewhere):
    r = run_cli(["list", "--targets", project], cwd=elsewhere)
    assert r.rc == 0, r.stderr
    assert _ids(r) == LIVE | INTEGRATION
    assert sorted(p.name for p in r.run_dir.iterdir() if p.is_dir()) == ["integration", "live"]
    assert "tail -f" not in r.stderr                               # a listing is over in seconds


def test_case_in_two_tiers_is_ambiguous(project, elsewhere):
    r = run_cli(["run", "--case", "twin", "--dry-run", "--targets", project], cwd=elsewhere)
    assert r.rc == 2 and "ambiguous" in r.stderr
    dirs = sorted(p.name for p in r.run_dir.iterdir() if p.is_dir())
    assert dirs == ["integration-probe", "live-probe"]


def test_single_plugin_per_process(project, elsewhere):
    r = run_cli(["list", "--targets", project], cwd=elsewhere)
    assert r.rc == 0, r.stderr
    assert r.part("live")["pluginsLoaded"] == ["livetest.plugin"]
    assert r.part("integration")["pluginsLoaded"] == ["inttest.plugin"]


def test_consumer_addopts_not_applied(isolation_project, elsewhere):
    r = run_cli(["list", "--tier", "live", "--suite", "cases/addopts", "--targets",
                 isolation_project], cwd=elsewhere)
    assert r.rc == 0, r.stderr
    assert _ids(r) == {"live:cases/addopts/a1::a1"}
    logs = (r.run_dir / "live" / "stdout.log").read_text() + \
        (r.run_dir / "live" / "stderr.log").read_text()
    assert "already registered" not in logs
    assert r.part("live")["pluginsLoaded"] == ["livetest.plugin"]


def test_live_service_markers_retained(project, elsewhere):
    r = run_cli(["list", "--tier", "live", "--targets", project], cwd=elsewhere)
    assert r.rc == 0, r.stderr
    by_name = {e["name"]: e for e in r.part("live")["selected"]}
    assert {"live", "postgres"} <= set(by_name["alpha"]["markers"])
    assert "postgres" not in by_name["twin"]["markers"]


def test_perf_root_from_manifest(project, elsewhere):
    # The perf tree lives in the consumer, not the framework checkout: SLT_INT_CASES (its parent's
    # perf/ dir) makes the integration plugin collect it as perf items.
    r = run_cli(["run", "--tier", "perf", "--dry-run", "--targets", project], cwd=elsewhere)
    assert r.rc == 0, r.stderr
    assert [e["kind"] for e in r.part("perf")["selected"]] == ["PerfYamlItem"]


def test_run_disabled_env_includes_it(project, elsewhere):
    r = run_cli(["list", "--tier", "integration", "--targets", project], cwd=elsewhere,
                env=framework_env(SLT_RUN_DISABLED="1"))
    assert r.rc == 0, r.stderr
    assert _ids(r) == INTEGRATION | {OFF}


def test_no_selected_case_is_5(project, elsewhere):
    r = run_cli(["run", "--tier", "live", "--case", "does-not-exist", "--dry-run", "--targets",
                 project], cwd=elsewhere)
    assert r.rc == 5 and "no tests selected" in r.stderr


def test_skipped_selected_item_is_3(project, elsewhere, trap):
    r = run_cli(["run", "--tier", "integration", "--case", "needs-absent", "--targets", project],
                cwd=elsewhere, env=trap.env(framework_env()))
    assert r.rc == 3, (r.stdout, r.stderr)
    assert "integration:cases/integration/needs-absent::needs-absent" in r.stderr
    assert "unsupported service" in r.stderr
    # The console names the command that follows the tier's log while it runs.
    assert f"integration: follow it with: tail -f {r.run_dir / 'integration' / 'stdout.log'}" in r.stderr
    assert (r.run_dir / "integration" / "junit.xml").is_file()
    assert trap.calls() == []


def test_inherited_pytest_options_cannot_fake_execution(project, elsewhere, trap):
    # NC (final review 1): an exported PYTEST_ADDOPTS=--collect-only (and a PYTEST_PLUGINS naming a
    # missing module) must not reach the tier process: the case executes and its skip is reported.
    env = trap.env(framework_env(PYTEST_ADDOPTS="--collect-only", PYTEST_PLUGINS="xtr_no_such_plugin"))
    r = run_cli(["run", "--tier", "integration", "--case", "needs-absent", "--targets", project],
                cwd=elsewhere, env=env)
    assert r.rc == 3, (r.stdout, r.stderr)
    results = json.loads((r.run_dir / "integration" / "results.json").read_text())
    assert results["collectOnly"] is False
    assert [s["nodeid"] for s in results["skipped"]], results
    listed = run_cli(["list", "--tier", "integration", "--targets", project], cwd=elsewhere, env=env)
    assert listed.rc == 0 and _ids(listed) == INTEGRATION         # collection-only surfaces unchanged
    assert trap.calls() == []


def test_selected_but_not_executed_is_not_ok(project, elsewhere, trap):
    # NC (final review 1): a nonempty selection with no reported outcome is never success, whatever
    # switched execution off (here a consumer conftest turning collect-only on after collection).
    suite = project.parent / "cases" / "integration" / "collectonly"
    (suite / "c1").mkdir(parents=True)
    (suite / "c1" / "test.yaml").write_text(
        (project.parent / "cases" / "integration" / "needs-absent" / "test.yaml").read_text()
        .replace("name: needs-absent", "name: c1"))
    (suite / "conftest.py").write_text(
        "def pytest_collection_finish(session):\n    session.config.option.collectonly = True\n")
    argv = ["run", "--tier", "integration", "--suite", "cases/integration/collectonly", "--targets", project]
    r = run_cli(argv, cwd=elsewhere, env=trap.env(framework_env()))
    assert r.rc == 2 and "selected-not-executed" in r.stderr, (r.stdout, r.stderr)
    assert "integration:cases/integration/collectonly/c1::c1" in r.stderr
    dry = run_cli(argv + ["--dry-run"], cwd=elsewhere, env=trap.env(framework_env()))
    assert dry.rc == 0 and dry.ids() == ["integration:cases/integration/collectonly/c1::c1"], dry.stderr
    assert trap.calls() == []


def test_manifest_service_settings_resolved_before_child_dispatch(project, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from striim_test import dispatch, project_io
    from livetest import paths
    root = project.parent
    svc = root / 'private/widget'
    svc.mkdir(parents=True)
    (svc / 'service.yaml').write_text('name: widget\nisolation: none\npre_up_env: [{name: CUSTOM_FETCH, type: string}]\n')
    with project.open('a') as f:
        f.write('\nservicesRoots: [private]\n')
    (root / '.env').write_text('CUSTOM_FETCH=checkout-only\nUNRELATED_SECRET=private\n')
    monkeypatch.delenv('GOLD_TARGETS', raising=False)
    origins = SimpleNamespace(mode='clone', home=paths._default_project_root(), packages={})
    run = tmp_path / 'run'
    run.mkdir()
    tier = run / 'live'
    tier.mkdir()
    ctx = dispatch.Ctx(project_io.load(project), origins, run, run)
    _, env = dispatch.build_pytest_argv(ctx, 'live', root / 'cases/live', tier,
                                      cases=[], collect_only=True)
    assert env['CUSTOM_FETCH'] == 'checkout-only'
    assert 'UNRELATED_SECRET' not in env
