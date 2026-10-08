"""Service drivers (livetest.drivers) through the real plugin, with the exec harness's fakes."""
import json

import pytest

from .exec_harness import run_case  # noqa: F401 - run_case is a fixture

pytest_plugins = ["pytester"]

_LOG = '''import json, os
def log(event):
    with open(os.environ["XTR_LOG"], "a") as f:
        f.write(json.dumps({"event": event}) + "\\n")
'''
_PROVISION = '''def provision(admins, client, progress=None):
    log("driver_provision:" + ",".join(sorted(admins)))
'''
_ADMINS_AND_READER = '''class Admin:
    def run_sql(self, sql):
        log("driver_seed")
def admins(base, defn):
    log("driver_admins")
    return {"extdb-source": Admin()}
def reader_mode(tql):
    return "Direct"
def reader_mark():
    log("reader_mark")
    return {"mark": 1}
def wait_reader_ready(mode, mark, timeout, progress=None):
    log("reader_ready")
    return 0
'''
_USE_DRIVER = '''
_harness_load_service = P.load_service
def _with_driver(svc):
    defn = _harness_load_service(svc)
    if svc == "extdb":
        defn.driver = "extdb_exec_driver"
        defn.dir = Path(__file__).parent / "extdb-service"
    return defn
P.load_service = _with_driver
'''


def _fixtures(tmp_path, seed):
    case = tmp_path / "fixtures" / "driver-flow"
    case.mkdir(parents=True)
    (case / "test.yaml").write_text(
        "name: driver-flow\ntql: app.tql\nrequires: [extdb]\ntimeout: 1\nassert: {smoke: true}\n"
        + ("seed:\n  - {file: seed.sql, db: extdb-source, when: post_start}\n" if seed else ""))
    (case / "app.tql").write_text("CREATE APPLICATION driver_flow; END APPLICATION driver_flow;")
    (case / "seed.sql").write_text("SELECT 1;")
    return tmp_path / "fixtures"


def _driver(body):
    def prepare(root, name):
        svc = root / "extdb-service"
        svc.mkdir()
        (svc / "extdb_exec_driver.py").write_text(_LOG + body)
        conftest = root / "conftest.py"
        conftest.write_text(conftest.read_text() + _USE_DRIVER)
    return prepare


def _events(run):
    return [json.loads(line)["event"] for line in (run.root / "fake.log").read_text().splitlines()
            if line.strip()]


def test_a_provision_only_driver_provisions_with_no_admins(run_case, tmp_path):
    run = run_case("driver-flow", fixtures=_fixtures(tmp_path, seed=False), prepare=_driver(_PROVISION))
    assert run.ret == 0, run.text
    events = _events(run)
    assert "driver_provision:" in events, events
    assert events.index("driver_provision:") < events.index("deploy")


def test_the_driver_hooks_run_in_order_around_deploy(run_case, tmp_path):
    run = run_case("driver-flow", fixtures=_fixtures(tmp_path, seed=True),
                   prepare=_driver(_PROVISION + _ADMINS_AND_READER))
    assert run.ret == 0, run.text
    order = [e for e in _events(run) if e in ("driver_admins", "driver_provision:extdb-source", "reader_mark",
                                              "deploy", "reader_ready", "driver_seed")]
    assert order == ["driver_admins", "driver_provision:extdb-source", "reader_mark", "deploy",
                     "reader_ready", "driver_seed"], order


def test_a_consumer_service_marker_reaches_the_selection_without_an_ini_entry(run_case, tmp_path):
    # striim-test writes its own pytest configuration, which registers no consumer markers.
    run = run_case("driver-flow", fixtures=_fixtures(tmp_path, seed=False), guard=True, args=("--collect-only",))
    selection = json.loads((run.root / "live" / "selection.json").read_text())
    assert [c["name"] for c in selection["collected"]] == ["driver-flow"], selection
    assert "extdb" in selection["collected"][0]["markers"], selection
