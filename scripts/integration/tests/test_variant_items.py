"""INT_VARIANT_ITEMS (default on): a variants case is one pytest item per variant group.

Each item drives only its group's variants, carries only its services' markers and provisions only
those services, so `-m sqlserver` runs the SQL Server variants alone and `-m spanner` only the
Spanner ones.
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET

from inttest import plugin as P
from inttest.manifest import load_manifest

from .test_variant_concurrency import CONFTEST, case

PROVISION_LOG = r'''
_orig_provision = P._provision_requires
P._provision_requires = lambda name, requires, lock: log("provision", requires=sorted(requires))
'''

THREE = {"pg": "postgres-target", "ora": "oracle-target", "mss": "sqlserver-target"}


def run(pytester, monkeypatch, tmp_path, yaml_text, *args, env=None, scenario=None):
    d = pytester.mkdir("case")
    (d / "test.yaml").write_text(yaml_text)
    (d / "in.json").write_text(json.dumps([{"metadata": {"TableName": "T"},
                                            "data": {"values": [1], "present": [True]}}]))
    for line in yaml_text.splitlines():
        if line.strip().startswith("ddl: ["):
            (d / line.strip()[6:-1]).write_text("SELECT 1;")
    pytester.makeconftest(CONFTEST + PROVISION_LOG)
    log = tmp_path / "log.jsonl"
    log.write_text("")
    monkeypatch.setenv("XINT_SCENARIO", json.dumps({"sleep": 0, **(scenario or {})}))
    monkeypatch.setenv("XINT_LOG", str(log))
    (tmp_path / "state").mkdir()
    monkeypatch.setenv("SLT_STATE_DIR", str(tmp_path / "state"))
    for k in ("INT_VARIANT_WORKERS", "INT_VARIANT_ITEMS", "INT_VARIANT"):
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    junit = tmp_path / "junit.xml"
    result = pytester.runpytest_subprocess("-p", "inttest.plugin", "-p", "no:cacheprovider",
                                           f"--junitxml={junit}", *args, "case")
    events = [json.loads(line) for line in log.read_text().splitlines()]
    cases = {}
    if junit.exists():
        for el in ET.parse(junit).iter("testcase"):
            cases[el.get("name")] = "failed" if el.find("failure") is not None else (
                "skipped" if el.find("skipped") is not None else "passed")
    return result, cases, events


def test_each_engine_is_its_own_item(pytester, monkeypatch, tmp_path):
    _, cases, events = run(pytester, monkeypatch, tmp_path, case(THREE))
    assert cases == {"fan[pg]": "passed", "fan[ora]": "passed", "fan[mss]": "passed"}
    assert sorted(e["run"] for e in events if e["event"] == "start") == ["mss", "ora", "pg"]


def test_an_item_provisions_only_its_services(pytester, monkeypatch, tmp_path):
    two = {"ora": "oracle-target", "mss": "sqlserver-target"}
    text = case(two).replace("requires: [postgres]", "requires: [oracle, sqlserver]")
    _, cases, events = run(pytester, monkeypatch, tmp_path, text)
    assert set(cases) == {"fan[ora]", "fan[mss]"}
    assert sorted(tuple(e["requires"]) for e in events if e["event"] == "provision") == [
        ("oracle",), ("sqlserver",)]


def test_a_service_marker_selects_its_items(pytester, monkeypatch, tmp_path):
    text = case(THREE).replace("requires: [postgres]", "requires: [postgres, oracle, sqlserver]")
    _, cases, events = run(pytester, monkeypatch, tmp_path, text, "-m", "sqlserver")
    assert cases == {"fan[mss]": "passed"}
    assert [e["run"] for e in events if e["event"] == "start"] == ["mss"]


def test_variants_of_one_service_share_an_item(pytester, monkeypatch, tmp_path):
    shared = {"pg": "postgres-target", "pg2": "postgres-source", "ora": "oracle-target"}
    _, cases, events = run(pytester, monkeypatch, tmp_path, case(shared))
    assert cases == {"fan[pg+pg2]": "passed", "fan[ora]": "passed"}
    starts = [e["run"] for e in events if e["event"] == "start"]
    assert starts.index("pg") < starts.index("pg2")


def test_a_failure_fails_only_its_item(pytester, monkeypatch, tmp_path):
    _, cases, _ = run(pytester, monkeypatch, tmp_path, case(THREE), scenario={"fail": ["ora"]})
    assert cases == {"fan[pg]": "passed", "fan[ora]": "failed", "fan[mss]": "passed"}


def test_int_variant_narrows_to_one_item(pytester, monkeypatch, tmp_path):
    _, cases, _ = run(pytester, monkeypatch, tmp_path, case(THREE), env={"INT_VARIANT": "ora"})
    assert cases == {"fan[ora]": "passed"}


def test_items_off_is_one_item_for_the_case(pytester, monkeypatch, tmp_path):
    _, cases, events = run(pytester, monkeypatch, tmp_path, case(THREE),
                           env={"INT_VARIANT_ITEMS": "0"})
    assert cases == {"fan": "passed"}
    assert len([e for e in events if e["event"] == "start"]) == 3


def test_a_case_without_variants_keeps_its_id(pytester, monkeypatch, tmp_path):
    plain = "\n".join(["name: one", "purpose: p", "op: {jar: java/OpenProcessors/X}",
                       "requires: [postgres]", "properties: {Run: solo}",
                       "target: {input: in.json}", "assert:", "  acked: 1"]) + "\n"
    _, cases, _ = run(pytester, monkeypatch, tmp_path, plain)
    assert cases == {"one": "passed"}


def test_group_requires_keeps_a_service_no_variant_reaches(tmp_path):
    text = case({"ora": "oracle-target", "mss": "sqlserver-target"}).replace(
        "requires: [postgres]", "requires: [gcs, oracle, sqlserver]")
    (tmp_path / "test.yaml").write_text(text)
    (tmp_path / "in.json").write_text("[]")
    for name in ("ora", "mss"):
        (tmp_path / f"{name}.sql").write_text("SELECT 1;")
    m = load_manifest(tmp_path / "test.yaml")
    items = {suffix: requires for suffix, _, requires in P._group_items(m)}
    assert items == {"[ora]": ["gcs", "oracle"], "[mss]": ["gcs", "sqlserver"]}


def test_an_empty_marker_selection_is_ok_when_asked(pytester, monkeypatch, tmp_path):
    result, cases, _ = run(pytester, monkeypatch, tmp_path, case(THREE), "-m", "spanner",
                           env={"INT_EMPTY_SELECTION_OK": "1"})
    assert result.ret == 0 and cases == {}


def test_an_empty_marker_selection_fails_by_default(pytester, monkeypatch, tmp_path):
    result, _, _ = run(pytester, monkeypatch, tmp_path, case(THREE), "-m", "spanner")
    assert result.ret == 5
