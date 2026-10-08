"""A case that requires the connection-only ``servicenow`` service, run through the real live plugin with
fakes at the infrastructure edges only (tests/lifecycle/exec_harness.py). The servicenow definition,
resolver and token builder are the real ones: nothing is brought up and no instance is contacted."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]   # run_case drives a fresh pytest process through pytester

SETTINGS = ("HOST", "SCHEME", "PORT", "USER", "PASSWORD", "CLIENT_ID", "CLIENT_SECRET", "VIEW_HOST")

TEST_YAML = """name: sn-case
purpose: hermetic servicenow fixture - the app reads the instance's credentials from its tokens
tql: app.tql
requires: [servicenow]
timeout: 30
assert:
  smoke: true
"""

APP_TQL = """CREATE APPLICATION SnCase;
CREATE SOURCE SnSource USING Global.ServiceNowReader (
  ConnectionURL: '${SERVICENOW_URL}',
  UserName: '${SERVICENOW_USER}',
  Password: '${SERVICENOW_PASSWORD}',
  ClientId: '${SERVICENOW_CLIENT_ID}',
  ClientSecret: '${SERVICENOW_CLIENT_SECRET}',
  Tables: 'u_slt_orders'
) OUTPUT TO SnStream;
END APPLICATION SnCase;
"""


@pytest.fixture
def sn_fixtures(tmp_path, monkeypatch):
    for s in SETTINGS:
        monkeypatch.delenv(f"SLT_SERVICENOW_{s}", raising=False)
    root = tmp_path / "fixtures"
    case = root / "sn-case"
    case.mkdir(parents=True)
    (case / "test.yaml").write_text(TEST_YAML)
    (case / "app.tql").write_text(APP_TQL)
    return root


def test_unset_host_skips_the_case_naming_the_setting_and_striim_test_exits_3(run_case, sn_fixtures):
    from striim_test import dispatch, errors
    run = run_case("sn-case", {"real_services": ["servicenow"], "real_tql": True}, fixtures=sn_fixtures,
                   guard=True)
    status, _props, xml = run.junit()
    assert status == "skipped", xml
    reason = run.envelope()["run"]["skipReason"]
    assert reason == ("service servicenow unavailable: no container ships for servicenow: set "
                      "SLT_SERVICENOW_HOST and its settings to your own instance, or add a servicenow "
                      "service that has a compose file through servicesRoots"), reason
    assert "deploy" not in run.names()                 # nothing ran against an instance
    sel = json.loads((run.root / "live" / "selection.json").read_text())
    res = json.loads((run.root / "live" / "results.json").read_text())
    assert dispatch.map_exit(run.ret, sel, res, False) == (errors.INFRA, "selected-item-skipped")


# ---------------------------------------------------------------- credentials never reach run output

PASSWORD = "Sn-Pass-7f3a9c1e-planted"
CLIENT_SECRET = "Sn-Secret-0b2d4e6f-planted"
INSTANCE = {"SLT_SERVICENOW_HOST": "sn.example.com", "SLT_SERVICENOW_USER": "slt_user",
            "SLT_SERVICENOW_PASSWORD": PASSWORD, "SLT_SERVICENOW_CLIENT_ID": "slt-client",
            "SLT_SERVICENOW_CLIENT_SECRET": CLIENT_SECRET}
# The harness's own bookkeeping (its fake log and state dump), not run output.
HARNESS_FILES = {"fake.log", "dump.json", "data.json", "guard.json"}


def _leaks(run) -> list:
    found = [f"output: {s}" for s in (PASSWORD, CLIENT_SECRET) if s in run.text]
    for p in sorted(run.root.rglob("*")):
        if p.is_file() and p.name not in HARNESS_FILES:
            data = p.read_bytes()
            found += [f"{p.relative_to(run.root)}: {s}" for s in (PASSWORD, CLIENT_SECRET) if s.encode() in data]
    return found


@pytest.mark.parametrize("scenario,status", [({}, "passed"), ({"deploy_fail_echo": True}, "failed")],
                         ids=["passing", "deploy-error-quotes-the-tql"])
def test_servicenow_credentials_appear_in_no_run_output(run_case, sn_fixtures, scenario, status):
    run = run_case("sn-case", {"real_services": ["servicenow"], "real_tql": True, **scenario},
                   fixtures=sn_fixtures, guard=True, env=INSTANCE)
    got, _props, xml = run.junit()
    assert got == status, xml
    if status == "failed":                             # the error did carry the rendered TQL: the URL is in it
        assert "https://sn.example.com:443" in xml, xml
    assert run.envelope()["run"]["status"] == status
    assert _leaks(run) == []


@pytest.mark.parametrize("replies,status", [(["Success"], "passed"), (["Failure"], "failed"),
                                            (["NPE", "Success"], "passed")],
                         ids=["success", "failure", "retried-npe"])
def test_the_real_striim_client_prints_no_servicenow_credentials(run_case, sn_fixtures, replies, status):
    # The real StriimClient and StriimApi, with only HTTP faked: Striim's answer quotes the submitted
    # statement, and the client prints every answer, the full answer of a failed import, and the
    # retry wrapper prints the error it retries. tee-sys puts all of it on the terminal.
    run = run_case("sn-case", {"real_services": ["servicenow"], "real_tql": True, "real_client": replies},
                   fixtures=sn_fixtures, guard=True, env=INSTANCE, args=("--capture=tee-sys",))
    got, _props, xml = run.junit()
    assert got == status, run.text
    assert "Tungsten status" in run.text                      # the client's own output was produced
    if replies[0] == "NPE":
        assert "retrying the import once" in run.text
    if status == "failed":
        assert "TQL import failed" in run.text
    assert _leaks(run) == []


# ---------------------------------------------------------------- service order

@pytest.mark.parametrize("order", [["postgres", "extapi", "extfront"], ["extfront", "postgres", "extapi"]],
                         ids=lambda o: "-".join(o))
def test_a_cases_services_come_up_in_the_order_requires_lists_them(run_case, tmp_path, order):
    # A service may depend on one listed before it (a front over a database it reads): each is
    # resolved, so brought up and healthy, before the next one starts, and all before the ddl runs.
    root = tmp_path / "order-fixtures"
    case = root / "order"
    case.mkdir(parents=True)
    (case / "test.yaml").write_text(
        f"name: order\npurpose: services come up in requires order\ntql: app.tql\n"
        f"requires: [{', '.join(order)}]\ntimeout: 30\nddl:\n  - file: ddl.sql\n    db: postgres-target\n"
        "assert:\n  smoke: true\n")
    (case / "app.tql").write_text("CREATE APPLICATION x;\nEND APPLICATION x;\n")
    (case / "ddl.sql").write_text("CREATE TABLE ${TID}tgt (id int);\n")
    run = run_case("order", {}, fixtures=root)
    events = run.events()
    assert [e["svc"] for e in events if e["event"] == "resolve"] == order, run.text
    first_ddl = next(i for i, e in enumerate(events) if e["event"] == "create")
    last_resolve = max(i for i, e in enumerate(events) if e["event"] == "resolve")
    assert last_resolve < first_ddl, events
