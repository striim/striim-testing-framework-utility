"""`capture`, `alter_recompile`, drop_recreate_app's `stopped_seed:` / `tokens:` and `assert.monitor`'s
`component:` / matchers / captured tokens, executed through the real ``livetest.plugin`` with the
exec harness's fakes (tests/lifecycle/exec_harness.py), plus the client calls these actions make."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from . import exec_harness
from .exec_harness import _agree, run_case  # noqa: F401 - run_case is a fixture

pytest_plugins = ["pytester"]

FIXTURES = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "appactions"
POS = "{ContinuationToken[a1]-DocumentTimeStamp[1700000000]-InternalTs[2]-InternalTimeInc[3]}"

# The client calls the stopped-phase actions add, and the real TQL render (the shared harness
# stubs it, since no other case re-renders the app).
EXTRA = r'''
import time as _time
_log = log


def log(event, **kw):                      # every event carries its wall-clock time
    _log(event, t=_time.time(), **kw)


def _upload(ctx, files, run=None, keep_existing=False):
    log("uploaded_paths", paths=[str(f) for f in files])


P.opartifacts.upload_artifacts = _upload

def _stop_app(self, app):
    log("stop_app", app=app)
    STATE["running"] = False


def _describe(self, name):
    log("describe", name=name)
    return [{"name": name, "adapterName": STATE.get("adapter", "OldReader"),
             "Checkpoint": [{"Source Restart Position": {"CheckpointText": SC["position"]}}]}]


def _mon(self, name):
    bare = name.split(".", 1)[1] if "." in name else name
    if bare in SC["mon"]:
        log("mon", name=name)
        return SC["mon"][bare]
    ns = name.split(".", 1)[0]
    return {"entityType": "APPLICATION", "fullName": name, "components": [
        {"entityType": "SOURCE", "fullName": f"{ns}.{c}"} for c in SC["mon"]]}


def _deploy_tql(self, tql):
    log("deploy_tql", tql=tql)
    if "RECOMPILE" in tql and SC.get("alter_fail"):
        raise RuntimeError("'ALTER APPLICATION RECOMPILE' failed: no such adapter NewReader")
    if "Global.NewReader" in tql:
        STATE["adapter"] = "NewReader"
    STATE["running"] = True


def _force_drop(self, app):
    log("force_drop", app=app)
    STATE["running"] = False
    return True


FakeClient.stop_app = _stop_app
FakeClient.describe = _describe
FakeClient.mon = _mon
FakeClient.deploy_tql = _deploy_tql
FakeClient._force_drop = _force_drop
Api.undeploy_application = lambda self, app: log("undeploy", app=app)
P._rendered_tql = lambda m, tokens, renames: P.render((m.source_dir / m.tql).read_text(), tokens)
'''


@pytest.fixture
def run_actions(run_case, monkeypatch):
    monkeypatch.setattr(exec_harness, "CONFTEST", exec_harness.CONFTEST + EXTRA)

    def run(mon_body, **extra):
        return run_case("alter-capture", {"position": POS, "mon": {"Src": mon_body}, **extra},
                        fixtures=FIXTURES)
    return run


def _at(events, pred, start=0):
    return next(i for i, e in enumerate(events) if i >= start and pred(e))


GOOD = {"input": "7", "lastCheckpointedPosition": "^ " + POS, "lastEventPosition": "^ " + POS}


def test_capture_alter_recompile_and_recreate_run_in_order_with_captured_tokens(run_actions):
    run = run_actions(GOOD)
    env = _agree(run, "passed")
    # teardown after a successful alter_recompile: the namespace is gone, cleanup verified
    assert env["lifecycle"]["cleanup"]["status"] == "ok" and run.dump()["namespaces"] == []
    ev = run.events()
    tid = run.dump()["tid"]
    first_mon = _at(ev, lambda e: e["event"] == "mon")
    stop1 = _at(ev, lambda e: e["event"] == "stop_app")
    desc = _at(ev, lambda e: e["event"] == "describe", stop1)
    seed2 = _at(ev, lambda e: e["event"] == "insert" and e["ids"] == [2])
    alter = _at(ev, lambda e: e["event"] == "deploy_tql" and "RECOMPILE" in e["tql"])
    seed3 = _at(ev, lambda e: e["event"] == "insert" and e["ids"] == [3])
    stop2 = _at(ev, lambda e: e["event"] == "stop_app", seed3)
    seed4 = _at(ev, lambda e: e["event"] == "insert" and e["ids"] == [4])
    drop = _at(ev, lambda e: e["event"] == "force_drop")
    recreate = _at(ev, lambda e: e["event"] == "deploy_tql", drop)
    assert first_mon < stop1 < desc < seed2 < alter < seed3 < stop2 < seed4 < drop < recreate
    assert ev[seed2]["table"] == f"qasource.{tid}src"

    app = ev[stop1]["app"]
    ns = app.split(".", 1)[0]
    assert ev[desc]["name"] == f"{ns}.Src"
    assert ev[alter]["tql"] == (
        f"USE {ns};\nUNDEPLOY APPLICATION {app};\nALTER APPLICATION {app};\n"
        "-- the source re-created on another adapter, carrying the position read while stopped\n"
        f"CREATE OR REPLACE SOURCE Src USING Global.NewReader (Note: '{POS}') OUTPUT TO S;\n"
        f"ALTER APPLICATION {app} RECOMPILE;\nDEPLOY APPLICATION {app} ON ANY IN default;\n"
        f"START APPLICATION {app};\n")
    # the override renders the re-created block only; the first deploy rendered the default ""
    assert f"StartPosition: '^ {POS}'" in ev[recreate]["tql"]
    assert ev[recreate]["tql"].startswith(f"USE {ns};\nCREATE OR REPLACE APPLICATION {app} RECOVERY")
    # seed after: 1s -- no earlier than the recompiled app's RUNNING (which follows the import)
    assert ev[seed3]["t"] - ev[alter]["t"] >= 1.0


def test_a_load_jar_is_staged_in_a_private_dir_that_is_removed(run_actions):
    run = run_actions(GOOD)
    _agree(run, "passed")
    # An open_processor jar is staged under its content name (opartifacts.content_addressed_name).
    staged = [Path(p) for e in run.events() if e["event"] == "uploaded_paths" for p in e["paths"]
              if re.fullmatch(r"OldReader-[0-9a-f]{12}-5\.4\.jar", Path(p).name)]
    assert len(staged) == 1 and staged[0].parent.name.startswith("slt-load-jar-")
    assert not staged[0].parent.exists()


def test_a_monitor_value_other_than_the_captured_one_fails_the_case(run_actions):
    run = run_actions({"input": "7", "lastCheckpointedPosition": "^ {elsewhere}", "lastEventPosition": "^ " + POS})
    _agree(run, "failed")
    assert "lastCheckpointedPosition: shows '^ {elsewhere}', expected '^ " + POS in run.junit()[2]


def test_a_failed_alter_import_fails_the_case_and_still_tears_down(run_actions):
    run = run_actions(GOOD, alter_fail=True)
    env = _agree(run, "failed")
    assert "no such adapter NewReader" in run.junit()[2]
    names = run.names()
    assert "force_drop" not in names                        # nothing after the failed action ran
    assert not any(e["event"] == "insert" and e["ids"] == [3] for e in run.events())
    assert env["lifecycle"]["cleanup"]["status"] == "ok" and run.dump()["namespaces"] == []


def test_capture_expect_fails_the_step_when_the_adapter_did_not_change(run_actions, monkeypatch):
    monkeypatch.setattr(exec_harness, "CONFTEST", exec_harness.CONFTEST + "\n" + (   # run_actions added EXTRA
        "_real = FakeClient.deploy_tql\n"
        "def _no_swap(self, tql):\n"
        "    _real(self, tql.replace('Global.NewReader', 'Global.OldReader'))\n"
        "FakeClient.deploy_tql = _no_swap\n"))
    run = run_actions(GOOD)
    _agree(run, "failed")
    assert "'adapterName' shows 'OldReader', expected 'NewReader'" in run.junit()[2]


def test_the_alter_fragment_gets_the_op_upload_rename(run_case, monkeypatch):
    monkeypatch.setattr(exec_harness, "CONFTEST", exec_harness.CONFTEST + EXTRA)

    def edit(y):
        return y.replace("requires: [postgres]\n", "requires: [postgres]\nop:\n  jar: java/OpenProcessors/StubOp\n"
                         "  upload: [{from: cfg.json, to: \"${NS}_cfg.json\"}]\n")

    def prepare(root, name):
        case = root / "cases" / "alter-capture"
        (case / "cfg.json").write_text("{}")
        frag = case / "swap.tql"
        frag.write_text(frag.read_text().replace("(Note:", "(ConfigFile: 'UploadedFiles/cfg.json', Note:"))
    run = run_case("alter-capture", {"position": POS, "mon": {"Src": GOOD}, "op_stub": True},
                   fixtures=FIXTURES, edit=edit, prepare=prepare, env={"SLT_OPS_PRELOADED": "1"})
    _agree(run, "passed")
    alter = next(e for e in run.events() if e["event"] == "deploy_tql" and "RECOMPILE" in e["tql"])
    ns = alter["tql"].split(";", 1)[0][len("USE "):]
    assert f"ConfigFile: 'UploadedFiles/{ns}_cfg.json'" in alter["tql"]
    assert "UploadedFiles/cfg.json" not in alter["tql"]
