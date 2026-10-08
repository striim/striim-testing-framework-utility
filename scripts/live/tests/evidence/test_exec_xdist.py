"""Distributed live runs through the real ``livetest.plugin`` under
real pytest-xdist (``-n 3 -p xdist.plugin``, pytester, the tests/lifecycle/exec_harness.py fakes at the infrastructure
edges only; no Docker, no live stack).

- An undeclared distributed live run is the C7.1 usage error (rc 4) with the ownership message, before any
  worker provisions anything -- not an INTERNALERROR (rc 3) from crashed workers.
- The ``.slt.json`` sidecar of a distributed run holds every case's record, the same set the JUnit reports,
  whichever worker ran it."""
from __future__ import annotations

import json
import re
import shutil
import xml.etree.ElementTree as ET

import pytest

from livetest import resultschema

pytest_plugins = ["pytester"]   # run_case drives a fresh pytest process through pytester

XDIST = ("-n", "3", "-p", "xdist.plugin")
# -p xdist.plugin assumes plugin autoload is off, as in striim-test's live child; with it on, xdist is
# registered twice and the child pytest cannot start.
XDIST_ENV = {"SLT_PARALLEL": "1"}   # run_case's child already has plugin autoload off


def test_exec_undeclared_distributed_live_run_is_usage_error_rc4_with_message(run_case):
    run = run_case("initial-load", {"initial_load": True}, declared=False, args=XDIST, env=XDIST_ENV)
    assert run.ret == 4, run.text
    assert "SLT_INFRA_OWNERSHIP" in run.text and "live runs must declare infrastructure ownership" in run.text
    assert "INTERNALERROR" not in run.text
    assert not {"resolve_striim", "resolve", "create", "deploy"} & set(run.names())


def _copies(n):
    def prepare(root, name):
        for i in range(2, n + 1):
            dest = root / "cases" / f"legacy-{i}"
            shutil.copytree(root / "cases" / "legacy", dest)
            text = (dest / "test.yaml").read_text()
            (dest / "test.yaml").write_text(re.sub(r"^name: .*$", f"name: lc-legacy-{i}", text, count=1, flags=re.M))
    return prepare


def test_exec_distributed_sidecar_has_every_case_record(run_case):
    run = run_case("legacy", {}, prepare=_copies(3), args=XDIST, env=XDIST_ENV)
    assert run.ret == 0, run.text
    cases = list(ET.parse(run.root / "live" / "junit.xml").getroot().iter("testcase"))
    doc = json.loads((run.root / "live" / "junit.slt.json").read_text())
    resultschema.validate(doc)
    assert len(cases) == 3
    assert sorted(t["name"] for t in doc["tests"]) == sorted(c.get("name") for c in cases)
    assert sorted(t["nodeid"] for t in doc["tests"]) == ["cases/legacy-2/test.yaml::legacy-2",
                                                        "cases/legacy-3/test.yaml::legacy-3",
                                                        "cases/legacy/test.yaml::legacy"]
    assert all(t["status"] == "passed" and [a["type"] for a in t["assertions"]] == ["smoke"] for t in doc["tests"])


@pytest.mark.parametrize("mode", ["serial", "distributed"])
def test_exec_disabled_only_selection_needs_no_ownership(run_case, mode):
    """Ownership is declared from the executable selection; a live selection whose only case
    is disabled executes nothing, so an undeclared run is no usage error (exit 5, nothing provisioned)."""
    run = run_case("initial-load", {"initial_load": True}, declared=False, guard=True,
                   edit=lambda y: y + "\ndisabled: deliberate review deselection\n", env=XDIST_ENV,
                   args=XDIST if mode == "distributed" else ())
    assert run.ret == 5, run.text
    assert not {"resolve_striim", "resolve", "create", "deploy"} & set(run.names())
