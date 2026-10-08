"""striim-test's pytest guard: loaded FIRST in every tier process (``-p striim_test.pytest_guard``)
and, through ``-p`` propagation, in xdist workers. Inert unless striim-test launched the process.

* ``pytest_configure`` (tryfirst): activates the C1 project in the live tier, when there is one.
* ``pytest_collection_modifyitems`` (tryfirst): keeps only items under the selected suite root,
  of the tier's kind (perf items only in perf, never in integration), matching ``--case`` (item
  name, manifest ``name``, case dir or case ID), and deselects manifest ``disabled:`` cases
  (raw YAML read, honouring ``SLT_RUN_DISABLED`` as each engine does) with the reason recorded.
  A duplicate item name within the selection is a configuration error.
* ``pytest_collection_finish`` and ``pytest_sessionfinish``: provenance re-check — no other copy
  of an engine package on ``sys.path`` and every imported ``livetest*``/``inttest*``/
  ``striim_test*`` module from the selected origins. A consumer conftest that injects a legacy
  path is detected here and fails the run.
* Writes ``selection.json`` (collected / selected / deselected-with-reason, plugins loaded,
  collection errors, duplicates, provenance) and ``results.json`` (passed / failed / skipped with
  reasons / errors; each failure and error with a one-line message). Under xdist each worker
  writes ``selection-<worker>.json``.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
import yaml

from striim_test import _bootstrap

ENV = "STRIIM_TEST_GUARD"
_STATE: dict = {}


def _cfg() -> dict:
    if "cfg" not in _STATE:
        raw = os.environ.get(ENV)
        if not raw:
            raise pytest.UsageError(f"striim_test.pytest_guard is loaded only by striim-test "
                                    f"({ENV} is not set)")
        _STATE.update(cfg=json.loads(Path(raw).read_text()), collected=[], selected=[],
                      deselected=[], collection_errors=[], duplicates={}, violations=[],
                      collected_here=False,
                      results={"passed": [], "failed": [], "skipped": [], "xfailed": [],
                               "errors": []})
    return _STATE["cfg"]


def _worker_id(config):
    wi = getattr(config, "workerinput", None)
    return None if wi is None else wi.get("workerid", "worker")


def _write(key: str, config, payload) -> None:
    p = Path(_cfg()[key])
    wid = _worker_id(config)
    if wid is not None:
        p = p.with_name(f"{p.stem}-{wid}{p.suffix}")
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, p)


def _check_provenance() -> list:
    packages = {k: Path(v) for k, v in _cfg()["packages"].items()}
    for problem in _bootstrap._decoys(packages, sys.path) + _bootstrap._foreign_modules(packages):
        if problem not in _STATE["violations"]:
            _STATE["violations"].append(problem)
    return _STATE["violations"]


def _write_selection(config) -> None:
    pm = config.pluginmanager
    _write("selectionOut", config, {
        "tier": _cfg()["tier"],
        "suiteRoot": _cfg()["suiteRoot"],
        "cases": _cfg()["cases"],
        "collected": _STATE["collected"],
        "selected": _STATE["selected"],
        "deselected": _STATE["deselected"],
        "pluginsLoaded": [n for n in ("livetest.plugin", "inttest.plugin")
                          if pm.get_plugin(n) is not None],
        "collectionErrors": _STATE["collection_errors"],
        "duplicates": _STATE["duplicates"],
        "provenanceViolations": _STATE["violations"],
    })


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    cfg = _cfg()
    if cfg["tier"] == "live" and cfg["manifest"]:
        from livetest import project
        try:
            project.load_and_activate(cfg["manifest"])
        except Exception as e:  # noqa: BLE001 - any activation failure is a configuration error
            raise pytest.UsageError(f"striim-test: cannot activate {cfg['manifest']}: {e}")


def pytest_collectreport(report):
    if report.failed:
        _cfg()
        _STATE["collection_errors"].append(
            {"nodeid": report.nodeid, "detail": str(report.longrepr)[-2000:]})


def _raw_manifest(path: Path) -> dict:
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _disabled_reason(tier: str, raw: dict):
    value = raw.get("disabled")
    label = value if isinstance(value, str) else "disabled"
    if tier == "live":
        forced = bool(os.environ.get("SLT_RUN_DISABLED"))
        if value and not forced:
            return f"disabled: {label}"
        par = raw.get("disabled_parallel")
        if par and os.environ.get("SLT_PARALLEL") and not forced:
            return f"disabled_parallel: {par if isinstance(par, str) else 'disabled_parallel'}"
        return None
    if value and os.environ.get("SLT_RUN_DISABLED") != "1":
        return f"disabled: {label}"
    return None


def _under(p: Path, root: Path) -> bool:
    p = p.resolve()
    return p == root or root in p.parents


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(session, config, items):
    cfg = _cfg()
    tier = cfg["tier"]
    consumer = Path(cfg["consumerRoot"]).resolve()
    suites = [Path(r).resolve() for r in cfg.get("suiteRoots") or [cfg["suiteRoot"]]]
    # Ids name a case outside the project by its declared case root, never by the selection, so
    # an id is the same for a full listing and a narrowed --suite/PATH. One root keeps the
    # absolute id it always had.
    declared = [Path(r).resolve() for r in cfg.get("caseRoots") or []]
    named = declared if len(declared) > 1 else []
    cases = set(cfg["cases"])
    collected, selected, deselected = [], [], []
    for item in items:
        mpath = getattr(item, "manifest_path", None)
        src = Path(mpath) if mpath else Path(str(item.path))
        is_case = src.name == "test.yaml"
        case_dir = (src.parent if is_case else src).resolve()
        try:
            rel = case_dir.relative_to(consumer).as_posix()
        except ValueError:
            # A case root outside the project: named by its directory name, which livetest
            # keeps unique across a tier's roots, so the id is stable on every machine.
            outer = next((r for r in named if r in case_dir.parents or r == case_dir), None)
            rel = (f"{outer.name}/{case_dir.relative_to(outer).as_posix()}".rstrip("/")
                   if outer is not None else str(case_dir))
        raw = _raw_manifest(src) if is_case else {}
        entry = {"id": f"{tier}:{rel}::{item.name}", "nodeid": item.nodeid, "name": item.name,
                 "manifestName": raw.get("name"), "caseDir": rel, "kind": type(item).__name__,
                 "markers": sorted({m.name for m in item.iter_markers()})}
        collected.append(entry)
        if not any(_under(src, suite) for suite in suites):
            reason = "outside-suite"
        elif tier == "perf" and entry["kind"] != "PerfYamlItem":
            reason = "not-a-perf-case"
        elif tier == "integration" and entry["kind"] == "PerfYamlItem":
            reason = "perf-case"
        elif cases and not cases & {entry["id"], entry["name"], entry["manifestName"], rel}:
            reason = "not-selected"
        else:
            reason = _disabled_reason(tier, raw)
        if reason:
            deselected.append((item, dict(entry, reason=reason)))
        else:
            selected.append((item, entry))

    names: dict = {}
    for _, e in selected:
        names.setdefault(e["name"], []).append(e["id"])
    dups = {n: ids for n, ids in sorted(names.items()) if len(ids) > 1}

    items[:] = [i for i, _ in selected]
    if deselected:
        config.hook.pytest_deselected(items=[i for i, _ in deselected])
    _STATE.update(collected=collected, selected=[e for _, e in selected],
                  deselected=[e for _, e in deselected], duplicates=dups, collected_here=True)
    _check_provenance()
    _write_selection(config)
    if dups:
        raise pytest.UsageError("striim-test: duplicate case name(s) in the selection: "
                                + "; ".join(f"{n}: {ids}" for n, ids in dups.items()))


def pytest_collection_finish(session):
    violations = _check_provenance()
    if _STATE.get("collected_here"):
        final = {it.nodeid for it in session.items}
        _STATE["selected"] = [e for e in _STATE["selected"] if e["nodeid"] in final]
        _write_selection(session.config)
    if violations:
        raise pytest.UsageError("striim-test: mixed provenance: " + "; ".join(violations))


def _skip_reason(report) -> str:
    lr = report.longrepr
    if isinstance(lr, tuple) and len(lr) == 3:
        return str(lr[2])
    return str(lr)


def _failure_line(report) -> str:
    """The failure in one line for the console: the crash message's first line and, when it runs
    on, its last one too (a `docker compose up` failure names the container that died last)."""
    crash = getattr(report.longrepr, "reprcrash", None)
    text = crash.message if crash is not None else str(report.longrepr)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    return (lines[0] if len(lines) == 1 else f"{lines[0]} ... {lines[-1]}")[:400]


def pytest_runtest_logreport(report):
    _cfg()
    r = _STATE["results"]
    if hasattr(report, "wasxfail") and report.outcome == "skipped":
        r["xfailed"].append({"nodeid": report.nodeid})
    elif report.when == "call":
        extra = {}
        if report.outcome == "skipped":
            extra = {"reason": _skip_reason(report)}
        elif report.outcome == "failed":
            extra = {"message": _failure_line(report)}
        r[report.outcome].append({"nodeid": report.nodeid, **extra})
    elif report.outcome == "skipped":
        r["skipped"].append({"nodeid": report.nodeid, "reason": _skip_reason(report)})
    elif report.outcome == "failed":
        r["errors"].append({"nodeid": report.nodeid, "when": report.when,
                            "message": _failure_line(report)})


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    violations = _check_provenance()
    if _worker_id(session.config) is not None:
        if violations and _STATE.get("collected_here"):
            _write_selection(session.config)
        return
    _write("resultsOut", session.config,
           dict(_STATE["results"], provenanceViolations=violations, exitstatus=int(exitstatus),
                collectOnly=bool(session.config.option.collectonly)))
    if violations and _STATE.get("collected_here"):
        _write_selection(session.config)
