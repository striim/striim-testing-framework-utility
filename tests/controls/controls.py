#!/usr/bin/env python3
"""Disposable negative/lifecycle controls: each proves an exact or lifecycle check fails when it should.

The source case tree (``samples/live`` by default) is read-only.  Each invocation copies it
below the caller's external output directory, mutates only that copy, runs one subject through
this clone's ``striim-test`` (``python -m striim_test`` with the clone's engines on
``PYTHONPATH``), validates the observed result, and atomically writes ``control-result.json``.
Control-only assets that a sample does not carry (``late-row.sql``) live in ``assets/``.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, NamedTuple

import yaml

REPO = Path(__file__).resolve().parents[2]
ENGINES = [REPO / "scripts" / name for name in ("live", "integration", "cli")]
ASSETS = Path(__file__).resolve().parent / "assets"
for _engine in reversed(ENGINES):
    if str(_engine) not in sys.path:
        sys.path.insert(0, str(_engine))

SCHEMA_VERSION = 1
CASE_SET = ("01-plain-replication", "02-transform", "04-lifecycle-check", "03-file-output")
TOP_KEYS = {"schemaVersion", "caseSet", "controls"}
CONTROL_KEYS = {"id", "case", "kind", "expected"}
EXPECTED_KEYS = {"subjectExit", "failure", "junit", "requiredEvents", "recoveryExit"}
ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SHA_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
MISSING_SERVICE = "postgres"
MISSING_SERVICE_HOST = "w3-required-postgres-unavailable.invalid"
L2 = "l2-prior-sentinel-ignored"
PRIOR_IN_TARGET = "prior-attempt-sentinel-in-target"
CURRENT_BESIDE_PRIOR = "current-sentinel-observed-beside-prior"
PRIOR_REMOVED = "prior-attempt-sentinel-removed"
# The lifecycle proofs that need two overlapping lanes; l2 is a proof on one ordinary subject.
OVERLAP = ("l4-overlap-identities", "l5-sibling-survival", "l6-foreign-survival")
# The controls whose external edge acts while the subject runs.
LIVE_PHASE = ("late-row", "l1-capture-suppressed", L2, "l3-delete-empty-no-done", "l8-blocked-target-insert")

ROWS = {
    "wrong-golden": ("01-plain-replication", "negative", "nonzero", "exact-data-mismatch", "failed", (), None),
    "extra-duplicate": ("02-transform", "negative", "nonzero", "exact-data-mismatch", "failed", (), None),
    "late-row": ("01-plain-replication", "negative", "nonzero", "late-row-source-ahead-of-target", "failed",
                 ("late-row-injected-before-stability-end",), None),
    "missing-required-service": ("01-plain-replication", "negative", "nonzero", "required-service-unavailable",
                                 "failed", (), None),
    "invalid-asset": ("03-file-output", "negative", "nonzero", "invalid-input-asset", "failed", (), None),
    "required-skip": ("01-plain-replication", "negative", "nonzero", "required-case-skipped", "skipped", (), None),
    "sequence-order": ("03-file-output", "negative", "nonzero", "sequence-order-mismatch", "failed", (), None),
    "l1-capture-suppressed": ("04-lifecycle-check", "negative", "nonzero", "readiness-deadline", "failed",
                              ("capture-suppressed-after-running",), None),
    "l2-prior-sentinel-ignored": ("04-lifecycle-check", "lifecycle-proof", 0, None, "passed",
                                  (PRIOR_IN_TARGET, CURRENT_BESIDE_PRIOR, PRIOR_REMOVED), None),
    "l3-delete-empty-no-done": ("04-lifecycle-check", "negative", "nonzero", "current-done-sentinel-not-observed",
                                "failed", ("done-sentinel-suppressed",), None),
    "l4-overlap-identities": (None, "lifecycle-proof", 0, None, "passed",
                              ("overlap-barrier-entered", "distinct-run-identities-observed"), None),
    "l5-sibling-survival": (None, "lifecycle-proof", 0, None, "passed",
                            ("first-run-finished", "sibling-still-active", "shared-services-survived"), None),
    "l6-foreign-survival": (None, "lifecycle-proof", 0, None, "passed",
                            ("foreign-table-survived", "sibling-file-survived"), None),
    "l7-cleanup-fault-replay": ("01-plain-replication", "negative", 1, "cleanup-fault", "failed",
                                ("cleanup-table-fault-injected", "ownership-replay-started",
                                 "ownership-replay-completed"), 0),
    "l8-blocked-target-insert": ("01-plain-replication", "negative", "nonzero", "baseline-landed-deadline", "failed",
                                 ("target-insert-blocked-after-running",), None),
}


class ControlError(ValueError):
    pass


class SubjectRun(NamedTuple):
    exit_code: int
    junit: Path
    envelopes: tuple[Path, ...]
    events: tuple[str, ...] = ()
    recovery_exit: int | None = None
    control_error: str | None = None
    overlap_evidence: dict | None = None
    control_tokens: dict | None = None
    control_ack_at: str | None = None
    control_observed: dict | None = None


class Invocation(NamedTuple):
    control: dict
    source_cases: Path
    copied_cases: Path
    output: Path
    plan: Path


def _expected(row: tuple) -> dict:
    case, kind, subject, failure, junit, events, recovery = row
    return {
        "case": case,
        "kind": kind,
        "expected": {
            "subjectExit": subject,
            "failure": failure,
            "junit": junit,
            "requiredEvents": list(events),
            "recoveryExit": recovery,
        },
    }


def load_spec(path: Path) -> dict:
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ControlError(f"controls spec is unreadable: {exc}") from None
    if not isinstance(raw, dict) or set(raw) != TOP_KEYS:
        raise ControlError(f"controls spec top-level fields must be exactly {sorted(TOP_KEYS)}")
    if raw["schemaVersion"] != SCHEMA_VERSION or isinstance(raw["schemaVersion"], bool):
        raise ControlError(f"unsupported controls schemaVersion {raw['schemaVersion']!r}")
    if not isinstance(raw["caseSet"], list) or set(raw["caseSet"]) != set(CASE_SET) \
            or len(raw["caseSet"]) != len(CASE_SET):
        raise ControlError(f"caseSet must be exactly {list(CASE_SET)!r}")
    if not isinstance(raw["controls"], list):
        raise ControlError("controls must be an array")
    seen = set()
    for control in raw["controls"]:
        if not isinstance(control, dict) or set(control) != CONTROL_KEYS:
            raise ControlError(f"control fields must be exactly {sorted(CONTROL_KEYS)}")
        cid = control["id"]
        if not isinstance(cid, str) or not ID_RE.fullmatch(cid):
            raise ControlError(f"invalid control id {cid!r}")
        if cid in seen:
            raise ControlError(f"duplicate control id {cid}")
        seen.add(cid)
        if cid not in ROWS:
            raise ControlError(f"unknown control id {cid}")
        if not isinstance(control["expected"], dict) or set(control["expected"]) != EXPECTED_KEYS:
            raise ControlError(f"{cid}: expected fields must be exactly {sorted(EXPECTED_KEYS)}")
        wanted = {"id": cid, **_expected(ROWS[cid])}
        if control != wanted:
            raise ControlError(f"{cid}: declaration differs from schema version 1")
    missing = sorted(set(ROWS) - seen)
    if missing:
        raise ControlError(f"missing required control(s): {missing}")
    return raw


def file_sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree_sha256(root: Path) -> str:
    root = Path(root).resolve()
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ControlError(f"case tree contains symlink: {path.relative_to(root)}")
        if path.is_file():
            rows.append([path.relative_to(root).as_posix(), file_sha256(path)])
    return "sha256:" + hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _contained(root: Path, path: Path) -> bool:
    root, path = Path(root).resolve(), Path(path).resolve()
    return path == root or root in path.parents


def _write_json_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _yaml(path: Path) -> dict:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ControlError(f"{path}: expected a YAML mapping")
    return raw


def _dump_yaml(path: Path, raw: dict) -> None:
    path.write_text(yaml.safe_dump(raw, sort_keys=False))


def _plan(control: dict, work: Path, copied_cases: Path) -> Path:
    cid, case = control["id"], control["case"]
    case_dir = copied_cases / case if case else copied_cases
    if not case_dir.is_dir():
        raise ControlError(f"control {cid} requires missing case directory {case!r}")
    plan = {
        "schemaVersion": 1,
        "controlId": cid,
        "case": case,
        "caseDirectory": case_dir.relative_to(work).as_posix(),
        "requiredEvents": control["expected"]["requiredEvents"],
    }
    manifest_path = case_dir / "test.yaml" if case else None
    if cid == "wrong-golden":
        raw = _yaml(manifest_path)
        golden = case_dir / raw["assert"]["data"][0]["match"]
        with open(golden, newline="") as stream:
            rows = list(csv.reader(stream))
        text_column = next((name for name in ("customer_name", "label", "name") if name in rows[0]), None)
        if text_column is None or len(rows) < 2:
            raise ControlError(f"{cid}: golden needs a text column and at least one row")
        rows[-1][rows[0].index(text_column)] += "-wrong"
        with open(golden, "w", newline="") as stream:
            csv.writer(stream, lineterminator="\n").writerows(rows)
        plan.update({"operation": "replace-type-valid-golden-text", "column": text_column})
    elif cid == "extra-duplicate":
        raw = _yaml(manifest_path)
        golden = case_dir / raw["assert"]["data"][0]["match"]
        lines = golden.read_text().splitlines()
        golden.write_text("\n".join([*lines, lines[-1]]) + "\n")
        plan.update({"operation": "duplicate-last-golden-row"})
    elif cid == "late-row":
        shutil.copy2(ASSETS / case / "late-row.sql", case_dir / "late-row.sql")
        plan.update({
            "injectAfterEvent": "baseline-counts-reached",
            "operation": "insert-late-source-row",
            "injectFile": "late-row.sql",
            "baselineRows": 3,
            "ackEvent": "late-row-injected-before-stability-end",
        })
    elif cid == "missing-required-service":
        raw = _yaml(manifest_path)
        if MISSING_SERVICE not in (raw.get("requires") or []):
            raise ControlError(f"{cid}: case does not require known service {MISSING_SERVICE!r}")
        plan.update({
            "operation": "route-known-required-service-to-unreachable-endpoint",
            "service": MISSING_SERVICE,
            "environment": {"SLT_PG_HOST": MISSING_SERVICE_HOST, "SLT_PG_PORT": "1"},
        })
    elif cid == "invalid-asset":
        raw = _yaml(manifest_path)
        golden = case_dir / raw["assert"]["file"][0]["match"]
        with open(golden, newline="") as stream:
            rows = list(csv.reader(stream))
        rows[-1][rows[0].index("id")] = "not-an-integer"
        with open(golden, "w", newline="") as stream:
            csv.writer(stream, lineterminator="\n").writerows(rows)
        plan.update({"operation": "make-golden-integer-invalid", "column": "id"})
    elif cid == "required-skip":
        plan.update({
            "operation": "select-case-then-skip-verification",
            "skipReason": "SLT_SKIP_VERIFY: app started + seeded; output not verified",
            "environment": {"SLT_SKIP_VERIFY": "1"},
        })
    elif cid == "sequence-order":
        raw = _yaml(manifest_path)
        raw["assert"]["file"][0]["exact"]["order"] = "sequence"
        _dump_yaml(manifest_path, raw)
        golden = case_dir / raw["assert"]["file"][0]["match"]
        lines = golden.read_text().splitlines()
        golden.write_text("\n".join([lines[0], *reversed(lines[1:])]) + "\n")
        plan.update({
            "operation": "reverse-golden-and-require-sequence-only-mismatch",
            "assumedActualOrder": "source insertion order",
        })
    elif cid == "l1-capture-suppressed":
        plan.update({"afterEvent": "application-running", "operation": "suppress-capture",
                     "ackEvent": "capture-suppressed-after-running"})
    elif cid == L2:
        prior_id = 1 + (int(hashlib.sha256(
            f"{os.environ.get('SLT_RUN_EPOCH', cid)}\x1f{cid}".encode()
        ).hexdigest()[:8], 16) % (2 ** 31 - 2))
        plan.update({"afterEvent": "target-table-created", "operation": "place-prior-attempt-sentinel-in-target",
                     "sentinelId": prior_id, "proof": "readiness ignores the prior attempt's sentinel"})
    elif cid == "l3-delete-empty-no-done":
        plan.update({"afterEvent": "delete-observed-empty", "operation": "suppress-done-sentinel",
                     "ackEvent": "done-sentinel-suppressed"})
    elif cid in OVERLAP:
        plan.update({
            "operation": "w3-09-overlap-coordinator",
            "coordinatorInterface": "qualification-runner-v1",
            "cases": [_yaml(copied_cases / name / "test.yaml")["name"] for name in CASE_SET],
            "sharedEndpoint": True,
        })
    elif cid == "l7-cleanup-fault-replay":
        plan.update({"environment": {"SLT_LIFECYCLE_FAULT": "cleanup:table"},
                     "recovery": "python -m livetest.ownership replay <ledger>"})
    elif cid == "l8-blocked-target-insert":
        plan.update({"afterEvent": "application-running", "operation": "block-target-insert",
                     "ackEvent": "target-insert-blocked-after-running"})
    path = work / "control-plan.json"
    _write_json_atomic(path, plan)
    return path


def run_overlap_coordinator(invocation: Invocation) -> SubjectRun:
    """Run the external two-lane coordinator used by the W3-09 lifecycle controls."""
    try:
        return _execute_overlap_coordinator(invocation)
    except ControlError:
        raise
    except Exception as exc:
        raise ControlError(
            f"{invocation.control['id']}: overlap coordinator failed: {type(exc).__name__}: {exc}"
        ) from None


def _subject_env(env: dict) -> dict:
    """This clone's engines first on PYTHONPATH: the subject is the clone's own striim-test."""
    paths = [str(path) for path in ENGINES]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    return {**env, "PYTHONPATH": os.pathsep.join(paths)}


def _overlap_subject_command(invocation: Invocation, project: Path) -> list[str]:
    return [
        sys.executable, "-m", "striim_test", "run", "--targets", str(project), "--tier", "live",
        "--case", *[_yaml(invocation.copied_cases / name / "test.yaml")["name"] for name in CASE_SET],
    ]


def _overlap_project(invocation: Invocation, lane: str) -> tuple[Path, Path]:
    consumer = invocation.output / "work" / "consumer"
    state = invocation.output / "state" / f"overlap-{lane}"
    state.mkdir(parents=True, exist_ok=True)
    project = consumer / f"overlap-{lane}.yaml"
    project.write_text(
        "schemaVersion: 1\ntargets: []\nsuites:\n  live: cases/live\n"
        f"stateDir: {str(state.resolve())!r}\n"
    )
    return project, state


def _service_env(env: dict, invocation: Invocation) -> dict:
    """The SLT_PG_* (and other service) settings the subject's striim-test takes from .env where the
    shell leaves them unset (shell > project .env > clone .env), so the control edge reaches the
    same database as the subject."""
    from striim_test import dispatch

    consumer = invocation.output / "work" / "consumer"
    return {**env, **dispatch.service_env({**env, "SLT_PROJECT_ROOT": str(consumer)})}


def _overlap_env(invocation: Invocation, lane: str, project: Path, state: Path, lock: Path) -> dict:
    env = _service_env(_subject_env(dict(os.environ)), invocation)
    base_epoch = env.get("SLT_RUN_EPOCH") or f"control-{invocation.control['id']}"
    env.update({
        "GOLD_TARGETS": str(project),
        "SLT_CONTROL_PLAN": str(invocation.plan),
        "SLT_RUN_EPOCH": f"{base_epoch}-{lane}",
        "SLT_STATE_DIR": str(state),
        "SLT_LOCK_DIR": str(lock),
        "SLT_INFRA_OWNERSHIP": "shared",
        "SLT_KEEP_SERVICES": "1",
    })
    lock.mkdir(parents=True, exist_ok=True)
    return env


def _probe_overlap_endpoint(env: dict) -> bool:
    import socket
    from urllib.parse import urlparse

    url = env.get("STRIIM_URL") or "http://localhost:9080"
    parsed = urlparse(url if "://" in url else f"http://{url}")
    try:
        with socket.create_connection((parsed.hostname or "localhost", parsed.port or 9080), timeout=5):
            return True
    except OSError:
        return False


def _overlap_window(state: Path, epoch: str) -> dict | None:
    starts, finishes = [], []
    ready_text, completion_text = None, None
    for path in state.rglob("evidence.json"):
        try:
            doc = json.loads(path.read_text())
            lifecycle = doc.get("lifecycle") or {}
            if ((lifecycle.get("identity") or {}).get("runId") != epoch):
                continue
            ready = (lifecycle.get("ready") or {}).get("endedAt")
            completion = (lifecycle.get("completion") or {}).get("endedAt")
            if ready and completion:
                starts.append(datetime.fromisoformat(str(ready).replace("Z", "+00:00")).timestamp())
                finishes.append(datetime.fromisoformat(str(completion).replace("Z", "+00:00")).timestamp())
                ready_text = str(ready)
                completion_text = str(completion)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    if not starts or not finishes:
        return None
    return {
        "epoch": epoch,
        "readyAt": ready_text,
        "completionAt": completion_text,
        "readyEpoch": min(starts),
        "completionEpoch": max(finishes),
    }


def _overlap_iso(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _overlap_identity_seen(state: Path, epoch: str) -> bool:
    for path in state.rglob("evidence.json"):
        try:
            doc = json.loads(path.read_text())
            if ((doc.get("lifecycle") or {}).get("identity") or {}).get("runId") == epoch:
                return True
        except (OSError, json.JSONDecodeError):
            pass
    return False


_CONTROL_MODE_ENV = "SLT_CONTROL_MODE"


def _overlap_server_mode(env: dict) -> str:
    explicit = (env.get(_CONTROL_MODE_ENV) or "").strip().lower()
    if explicit in {"docker", "native"}:
        return explicit
    try:
        from livetest import services, stack

        return "docker" if services.container_running(
            stack.striim_container(env),
            run=lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=15),
        ) else "native"
    except Exception:
        return "native"


def _overlap_lane_probes(invocation: Invocation, sibling_env: dict, sibling_state: Path,
                         first_finished: bool) -> tuple[bool, bool, str | None]:
    """Probe L6's foreign table and sibling-file witnesses."""
    table_ok = file_ok = False
    server_file_mode = _overlap_server_mode(sibling_env)
    foreign = sibling_env.get("SLT_CONTROL_FOREIGN_TABLE") or os.environ.get("SLT_CONTROL_FOREIGN_TABLE")
    if not foreign or foreign.count(".") != 1:
        return False, False, (
            "foreign table witness requires an explicit SLT_CONTROL_FOREIGN_TABLE schema.table "
            "outside both run identities"
        )
    foreign_schema, foreign_table = foreign.split(".", 1)
    try:
        edge = _PostgresControlEdge(invocation, sibling_env)
        table_ok = edge._count("target", _ident(foreign_schema, "schema"),
                               _ident(foreign_table, "table")) >= 0
    except Exception:
        table_ok = False
    try:
        from livetest.striimfile import read_server_files
        from types import SimpleNamespace
        from livetest import runident

        file_name = _yaml(invocation.copied_cases / "03-file-output" / "test.yaml")["name"]
        file_ident = runident.derive(file_name, sibling_env)
        file_ok = bool(read_server_files(SimpleNamespace(mode=server_file_mode),
                                         f"{file_ident.owned_dir}/rows.json"))
    except Exception:
        file_ok = False
    return table_ok, file_ok, None if table_ok and file_ok else \
        f"foreign table or sibling file did not survive first-run cleanup " \
        f"(server-file mode: {server_file_mode})"


def _execute_overlap_coordinator(invocation: Invocation) -> SubjectRun:
    """Run both full customer suites with distinct epochs and a shared services endpoint."""
    lanes = {}
    lock = invocation.output / "locks" / "overlap"
    for lane in ("a", "b"):
        project, state = _overlap_project(invocation, lane)
        env = _overlap_env(invocation, lane, project, state, lock)
        lanes[lane] = {
            "project": project, "env": env, "state": state, "process": None,
            "stdout": None, "stderr": None,
        }
    events = []
    cid = invocation.control["id"]
    try:
        for lane in ("a", "b"):
            stdout = open(invocation.output / f"overlap-{lane}.stdout.log", "wb")
            stderr = open(invocation.output / f"overlap-{lane}.stderr.log", "wb")
            process = subprocess.Popen(_overlap_subject_command(invocation, lanes[lane]["project"]),
                                       cwd=invocation.output, env=lanes[lane]["env"], stdin=subprocess.DEVNULL,
                                       stdout=stdout, stderr=stderr)
            lanes[lane].update(process=process, stdout=stdout, stderr=stderr)
        events.append("distinct-run-identities-observed" if lanes["a"]["env"]["SLT_RUN_EPOCH"] != lanes["b"]["env"]["SLT_RUN_EPOCH"] else "")
        events = [event for event in events if event]

        if cid == "l6-foreign-survival":
            lanes["a"]["process"].wait()
            first, sibling = "a", "b"
        else:
            while all(item["process"].poll() is None for item in lanes.values()):
                time.sleep(0.1)
            first = "a" if lanes["a"]["process"].poll() is not None else "b"
            sibling = "b" if first == "a" else "a"
            lanes[first]["process"].wait()
        events.append("first-run-finished")
        sibling_active = lanes[sibling]["process"].poll() is None
        if sibling_active:
            events.append("sibling-still-active")
        if sibling_active and _probe_overlap_endpoint(lanes[sibling]["env"]):
            events.append("shared-services-survived")
        l6_probe = None
        if cid == "l6-foreign-survival" and sibling_active:
            l6_probe = _overlap_lane_probes(
                invocation, lanes[sibling]["env"], lanes[sibling]["state"], True,
            )
        lanes[sibling]["process"].wait()

        windows = [_overlap_window(lanes[lane]["state"], lanes[lane]["env"]["SLT_RUN_EPOCH"])
                   for lane in ("a", "b")]
        overlap_start = max((window["readyEpoch"] for window in windows if window), default=None)
        overlap_end = min((window["completionEpoch"] for window in windows if window), default=None)
        overlap_ok = all(windows) and overlap_start < overlap_end
        if overlap_ok:
            events.append("overlap-barrier-entered")
        if not all(_overlap_identity_seen(lanes[lane]["state"], lanes[lane]["env"]["SLT_RUN_EPOCH"])
                   for lane in ("a", "b")):
            events = [event for event in events if event != "distinct-run-identities-observed"]

        control_error = None
        if cid == "l5-sibling-survival" and not {
                "first-run-finished", "sibling-still-active", "shared-services-survived"
        } <= set(events):
            control_error = "shared overlap did not prove first-run/sibling/service survival"
        elif cid == "l6-foreign-survival":
            table_ok, file_ok, probe_error = l6_probe or (
                False, False, "foreign-table-and-sibling-file-witnesses-not-probed: sibling was not active"
            )
            for event, ok in (("foreign-table-survived", table_ok), ("sibling-file-survived", file_ok)):
                if ok:
                    events.append(event)
            control_error = probe_error or (None if table_ok and file_ok else
                                                 "foreign table or sibling file did not survive first-run cleanup")
        elif cid == "l4-overlap-identities" and not {
                "overlap-barrier-entered", "distinct-run-identities-observed"
        } <= set(events):
            control_error = "run-side overlap or distinct identities were not observed"

        junit_paths = [next(item["state"].rglob("junit.xml"), None) for item in lanes.values()]
        junit = next((path for path in junit_paths if path is not None), invocation.output / "missing-junit.xml")
        envelopes = tuple(path for item in lanes.values() for path in item["state"].rglob("evidence.json"))
        exit_code = 0 if all(item["process"].returncode == 0 for item in lanes.values()) else 1
        overlap_evidence = {
            "overlapped": bool(overlap_ok),
            "overlapStart": _overlap_iso(overlap_start),
            "overlapEnd": _overlap_iso(overlap_end),
            "lanes": [
                {
                    "lane": lane,
                    "epoch": lanes[lane]["env"]["SLT_RUN_EPOCH"],
                    "readyAt": (windows[index] or {}).get("readyAt"),
                    "completionAt": (windows[index] or {}).get("completionAt"),
                }
                for index, lane in enumerate(("a", "b"))
            ],
        }
        return SubjectRun(exit_code, junit, envelopes, tuple(events), None, control_error, overlap_evidence)
    finally:
        for item in lanes.values():
            for stream in (item.get("stdout"), item.get("stderr")):
                if stream is not None:
                    stream.close()


def _subject_command(invocation: Invocation, project: Path) -> list[str]:
    case_dir = invocation.copied_cases / invocation.control["case"]
    case_id = _yaml(case_dir / "test.yaml")["name"]
    return [sys.executable, "-m", "striim_test", "run", "--targets", str(project), "--tier", "live",
            "--case", case_id]


def _postgres_service_base(env: dict) -> dict:
    """Resolve the host-side Postgres settings without bringing the service up."""
    from livetest import services

    definition = services.load_service("postgres")
    return services.resolve(
        "postgres", env, {definition.container}, compose_up=lambda _: None, post_up=lambda _: None,
    ).base


def _control_tokens(invocation: Invocation, env=None) -> dict[str, str]:
    """Render only the run identity and Postgres tokens needed by an external control edge."""
    from livetest import runident

    case = invocation.control["case"] or "01-plain-replication"
    manifest = _yaml(invocation.copied_cases / case / "test.yaml")
    env = os.environ if env is None else env
    postgres = _postgres_service_base(env)
    ident = runident.derive(manifest["name"], env)
    return {
        **runident.tokens(ident),
        "PG_SOURCE_SCHEMA": postgres["source_schema"],
        "PG_TARGET_SCHEMA": postgres["target_schema"],
        "PG_SOURCE_USER": postgres["source_user"],
        "PG_SOURCE_PASSWORD": postgres["source_password"],
        "PG_TARGET_USER": postgres["target_user"],
        "PG_TARGET_PASSWORD": postgres["target_password"],
        "SENTINEL_ID": str((_yaml(invocation.plan).get("sentinelId") or 0)),
    }


def _render_control_sql(text: str, tokens: dict[str, str]) -> str:
    def replace(match):
        name = match.group(1)
        if name not in tokens:
            raise ControlError(f"control SQL references unknown token {name!r}")
        return str(tokens[name])

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, text)


def _ident(value: str, what: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ControlError(f"unsafe Postgres {what}: {value!r}")
    return value


class _PostgresControlEdge:
    """The narrow external DB edge used by live controls; the subject remains a real process."""

    def __init__(self, invocation: Invocation, env=None):
        self.invocation = invocation
        self.env = os.environ if env is None else env
        self.tokens = _control_tokens(invocation, self.env)

    def _connect(self, role: str):
        try:
            import psycopg2
        except ImportError as exc:
            raise ControlError(f"Postgres control edge unavailable: {exc}") from None
        prefix = "source" if role == "source" else "target"
        postgres = _postgres_service_base(self.env)
        conn = psycopg2.connect(
            host=postgres["host"],
            port=int(postgres["port"]),
            dbname=postgres["dbname"],
            user=postgres[f"{prefix}_user"],
            password=postgres[f"{prefix}_password"],
            connect_timeout=5,
        )
        conn.autocommit = True
        return conn

    def _count(self, role: str, schema: str, table: str) -> int:
        schema, table = _ident(schema, "schema"), _ident(table, "table")
        conn = self._connect(role)
        try:
            cur = conn.cursor()
            try:
                cur.execute(f'SELECT count(*) FROM "{schema}"."{table}"')
                return int(cur.fetchone()[0])
            finally:
                cur.close()
        finally:
            conn.close()

    def _run_source_sql(self, filename: str) -> None:
        sql_path = self.invocation.copied_cases / self.invocation.control["case"] / filename
        sql = _render_control_sql(sql_path.read_text(), self.tokens)
        conn = self._connect("source")
        try:
            cur = conn.cursor()
            try:
                cur.execute(f'SET search_path TO "{_ident(self.tokens["PG_SOURCE_SCHEMA"], "schema")}"')
                cur.execute(sql)
            finally:
                cur.close()
        finally:
            conn.close()

    def inject_late_row(self, process) -> tuple[bool, str | None]:
        plan = _yaml(self.invocation.plan)
        source_schema = self.tokens["PG_SOURCE_SCHEMA"]
        source_table = self.tokens["TID"] + "src"
        target_schema = self.tokens["PG_TARGET_SCHEMA"]
        target_table = self.tokens["TID"] + "tgt"
        baseline = int(plan.get("baselineRows", 1))
        startup_deadline = time.monotonic() + float(
            self.env.get("SLT_CONTROL_STARTUP_TIMEOUT", "180")
        )
        baseline_observed = False
        last = "baseline was not observed"
        while process.poll() is None and time.monotonic() < startup_deadline:
            try:
                source_count = self._count("source", source_schema, source_table)
                target_count = self._count("target", target_schema, target_table)
                if source_count >= baseline and target_count >= baseline:
                    baseline_observed = True
                    break
                last = f"source={source_count}, target={target_count}, expected>={baseline}"
            except Exception as exc:  # the result is recorded; it is never treated as an injection
                last = f"{type(exc).__name__}: {exc}"
            time.sleep(0.1)
        if not baseline_observed:
            if process.poll() is not None:
                return False, f"late-row baseline was not observed before subject exit ({last})"
            return False, f"late-row baseline was not observed before startup deadline ({last})"

        injection_deadline = time.monotonic() + float(
            self.env.get("SLT_CONTROL_PHASE_TIMEOUT", "90")
        )
        injected = False
        last = "late-row source row was not verified"
        while process.poll() is None and time.monotonic() < injection_deadline:
            try:
                if not injected:
                    self._run_source_sql(plan["injectFile"])
                    injected = True
                source_count = self._count("source", source_schema, source_table)
                if source_count >= baseline + 1:
                    if not _acknowledge_late_row(process):
                        return False, "late-row injection acknowledgement was observed after subject exit"
                    return True, None
                last = f"source={source_count}, expected>={baseline + 1} after injection"
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
            time.sleep(0.1)
        if process.poll() is not None:
            return False, f"late-row injection acknowledgement was not observed before subject exit ({last})"
        return False, f"late-row injection acknowledgement was not observed before phase deadline ({last})"

    def _query(self, role: str, sql: str, params=()) -> list:
        conn = self._connect(role)
        try:
            cur = conn.cursor()
            try:
                cur.execute(sql, params)
                return cur.fetchall()
            finally:
                cur.close()
        finally:
            conn.close()

    def _execute(self, role: str, sql: str, params=()) -> None:
        conn = self._connect(role)
        try:
            cur = conn.cursor()
            try:
                cur.execute(sql, params)
            finally:
                cur.close()
        finally:
            conn.close()

    def _target_table(self) -> str:
        schema = _ident(self.tokens["PG_TARGET_SCHEMA"], "schema")
        return f'"{schema}"."{_ident(self.tokens["TID"] + "tgt", "table")}"'

    def _target_ids(self) -> list[int]:
        return [int(row[0]) for row in self._query("target", f"SELECT id FROM {self._target_table()}")]

    def prove_prior_sentinel_ignored(self, process) -> tuple[tuple[str, ...], str | None, dict]:
        """l2: an earlier attempt's ready sentinel sits in the run's target while readiness runs.

        The edge writes the prior row into the run's target as soon as the subject's DDL has created
        it, before deploy: a late delivery from an earlier attempt, as the hermetic proof delivers
        it. (PostgreSQLReader does not replay source rows committed before START, so a source
        insert is not delivered, as a live attempt showed.) The edge records the prior row in the
        target, then a second id (the current sentinel) beside it, and only then deletes the prior
        row, so the sample's empty golden still holds. Whether readiness ignored it is read from the
        envelope."""
        plan = _yaml(self.invocation.plan)
        prior = int(plan["sentinelId"])
        deadline = time.monotonic() + float(self.env.get("SLT_CONTROL_PHASE_TIMEOUT", "180"))
        events, observed = [], {"priorSentinelId": prior}

        def wait(what, step):
            last = "not yet observed"
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    outcome = step()
                    if outcome is not None:
                        return outcome, None
                    last = "not yet observed"
                except Exception as exc:  # recorded as the reason; never an acknowledgement
                    last = f"{type(exc).__name__}: {exc}"
                time.sleep(0.1)
            when = "subject exit" if process.poll() is not None else "phase deadline"
            return None, f"{what} was not observed before {when} ({last})"

        def insert_into_target():
            rows = self._query("target", "SELECT to_regclass(%s)", (self._target_table(),))
            if rows[0][0] is None:
                return None
            self._execute("target", f"INSERT INTO {self._target_table()} (id) VALUES (%s)", (prior,))
            return True

        def prior_in_target():
            return True if prior in self._target_ids() else None

        def current_beside_prior():
            ids = self._target_ids()
            if prior not in ids:
                raise ControlError("the prior row left the target before a current sentinel arrived")
            others = sorted(set(ids) - {prior})
            return others or None

        def prior_removed():
            return True if prior not in self._target_ids() else None

        _ok, why = wait("prior sentinel insert into the created target", insert_into_target)
        if why:
            return tuple(events), why, observed
        _ok, why = wait("prior sentinel in the target", prior_in_target)
        if why:
            return tuple(events), why, observed
        events.append(PRIOR_IN_TARGET)
        others, why = wait("a current sentinel beside the prior row", current_beside_prior)
        if why:
            return tuple(events), why, observed
        observed["currentSentinelIds"] = others
        events.append(CURRENT_BESIDE_PRIOR)
        self._execute("target", f"DELETE FROM {self._target_table()} WHERE id = %s", (prior,))
        _ok, why = wait("prior sentinel removal from the target", prior_removed)
        if why:
            return tuple(events), why, observed
        events.append(PRIOR_REMOVED)
        return tuple(events), None, observed

    def block_target_insert(self, process) -> tuple[bool, str | None, object | None]:
        plan = _yaml(self.invocation.plan)
        schema = self.tokens["PG_TARGET_SCHEMA"]
        table = self.tokens["TID"] + "tgt"
        deadline = time.monotonic() + float(self.env.get("SLT_CONTROL_PHASE_TIMEOUT", "90"))
        last = "target table was not ready"
        while process.poll() is None and time.monotonic() < deadline:
            conn = None
            try:
                schema, table = _ident(schema, "schema"), _ident(table, "table")
                conn = self._connect("target")
                conn.autocommit = False
                cur = conn.cursor()
                try:
                    cur.execute("SET lock_timeout = '2s'")
                    cur.execute(f'LOCK TABLE "{schema}"."{table}" IN ACCESS EXCLUSIVE MODE')
                finally:
                    cur.close()
                # Keep the transaction open until the real subject exits. Closing here would
                # turn the claimed control into a no-op and is the original live defect.
                return True, None, conn
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
                if conn is not None:
                    try:
                        conn.rollback()
                    finally:
                        conn.close()
                time.sleep(0.1)
        if process.poll() is not None:
            return False, f"target insert block was not observed before subject exit ({last})", None
        return False, f"target insert block was not observed before phase deadline ({last})", None


def _late_row_edge(invocation: Invocation, env=None) -> _PostgresControlEdge:
    return _PostgresControlEdge(invocation, env)


def _prior_sentinel_edge(invocation: Invocation, env=None) -> _PostgresControlEdge:
    return _PostgresControlEdge(invocation, env)


class _LiveControlEdge:
    """The external Striim/DB edge for controls that act after application RUNNING."""

    def __init__(self, invocation: Invocation, env=None):
        self.invocation = invocation
        self.env = os.environ if env is None else env
        self.tokens = _control_tokens(invocation, self.env)
        self.pg = _PostgresControlEdge(invocation, self.env)
        self._client = None

    def _client_for(self):
        if self._client is None:
            from livetest.striim import StriimClient

            url = self.env.get("STRIIM_URL") or "http://localhost:9080"
            self._client = StriimClient.from_url(
                url, self.env.get("STRIIM_USER") or "admin", self.env.get("STRIIM_PASS") or "striim"
            )
        return self._client

    def _wait_running(self, process) -> tuple[bool, str | None]:
        app = self.tokens["APP"]
        deadline = time.monotonic() + float(self.env.get("SLT_CONTROL_PHASE_TIMEOUT", "90"))
        last = "RUNNING was not observed"
        while process.poll() is None and time.monotonic() < deadline:
            try:
                status = self._client_for().current_status(app)
                if status == "RUNNING":
                    return True, None
                if status in {"CRASH", "HALT", "TERMINATED", "DEPLOY_FAILED"}:
                    return False, f"application reached terminal status {status}"
                last = f"application status {status}"
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
            time.sleep(0.2)
        if process.poll() is not None:
            return False, f"application RUNNING was not observed before subject exit ({last})"
        return False, f"application RUNNING was not observed before phase deadline ({last})"

    def _stop_after_running(self, process, action: str) -> tuple[bool, str | None]:
        ok, reason = self._wait_running(process)
        if not ok:
            return False, reason
        app = self.tokens["APP"]
        try:
            self._client_for().stop_app(app)
        except Exception as exc:
            return False, f"{action} was not observed: stop application failed ({type(exc).__name__}: {exc})"
        deadline = time.monotonic() + float(self.env.get("SLT_CONTROL_PHASE_TIMEOUT", "90"))
        last = "stop accepted but status remained RUNNING"
        while process.poll() is None and time.monotonic() < deadline:
            try:
                status = self._client_for().current_status(app)
                if status != "RUNNING":
                    return True, None
                last = f"application status {status}"
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
            time.sleep(0.2)
        return False, f"{action} was not observed before phase deadline ({last})"

    def suppress_capture(self, process) -> tuple[bool, str | None]:
        return self._stop_after_running(process, "capture suppression")

    def suppress_done_sentinel(self, process) -> tuple[bool, str | None]:
        source_schema = self.pg.tokens["PG_SOURCE_SCHEMA"]
        source_table = self.pg.tokens["TID"] + "src"
        deadline = time.monotonic() + float(self.env.get("SLT_CONTROL_PHASE_TIMEOUT", "120"))
        activity_seen, last = False, "delete-to-empty was not observed"
        while process.poll() is None and time.monotonic() < deadline:
            try:
                count = self.pg._count("source", source_schema, source_table)
                if count > 0:
                    activity_seen = True
                elif activity_seen:
                    return self._stop_after_running(process, "done-sentinel suppression")
                last = f"source count={count}"
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
            time.sleep(0.1)
        return False, f"done-sentinel suppression was not observed ({last})"

    def block_target_insert(self, process) -> tuple[bool, str | None, object | None]:
        ok, reason = self._wait_running(process)
        if not ok:
            return False, reason, None
        return self.pg.block_target_insert(process)


def _live_control_edge(invocation: Invocation, env=None) -> _LiveControlEdge:
    return _LiveControlEdge(invocation, env)


def _phase_result(outcome):
    if isinstance(outcome, tuple):
        if len(outcome) == 3:
            return bool(outcome[0]), outcome[1], outcome[2]
        if len(outcome) == 2:
            return bool(outcome[0]), outcome[1], None
    return bool(outcome), None, None


def _acknowledge_late_row(process) -> bool:
    """Record the row-verification instant only while the subject is still alive."""
    if process.poll() is not None:
        return False
    acknowledged_at = _utc()
    if process.poll() is not None:
        return False
    process._slt_late_row_ack_at = acknowledged_at
    return True


def _control_phase(invocation: Invocation, process=None, env=None) -> tuple[tuple[str, ...], str | None]:
    """Run the declared external setup phase and return only observed events plus a reason."""
    cid = invocation.control["id"]
    plan = _yaml(invocation.plan)
    env = os.environ if env is None else env
    if cid == "late-row":
        if process is None:
            return (), "late-row injection event was not observed: no running subject"
        edge = _late_row_edge(invocation, env)
        if getattr(edge, "tokens", None) is not None:
            process._slt_late_row_tokens = edge.tokens
        outcome = edge.inject_late_row(process)
        if isinstance(outcome, tuple):
            ok, reason = outcome
        else:
            ok, reason = bool(outcome), None
        if not ok and not reason:
            reason = "late-row injection event was not observed"
        if ok and not _acknowledge_late_row(process):
            return (), "late-row injection acknowledgement was observed after subject exit"
        return ((plan["ackEvent"],) if ok else ()), reason
    if cid == L2:
        if process is None:
            return (), "prior sentinel event was not observed: no running subject"
        events, reason, observed = _prior_sentinel_edge(invocation, env).prove_prior_sentinel_ignored(process)
        process._slt_control_observed = observed
        return tuple(events), reason
    if cid in {"l1-capture-suppressed", "l3-delete-empty-no-done", "l8-blocked-target-insert"}:
        if process is None:
            return (), f"{cid} setup/injection was not observed: no running subject"
        edge = _live_control_edge(invocation, env)
        method = {
            "l1-capture-suppressed": edge.suppress_capture,
            "l3-delete-empty-no-done": edge.suppress_done_sentinel,
            "l8-blocked-target-insert": edge.block_target_insert,
        }[cid]
        ok, reason, hold = _phase_result(method(process))
        if hold is not None:
            holds = getattr(process, "_slt_control_holds", [])
            holds.append(hold)
            process._slt_control_holds = holds
        return ((plan["ackEvent"],) if ok else ()), reason
    # Controls without a concrete external phase remain fail-closed through their required-event
    # check below; they must never acquire a synthetic acknowledgement here.
    return (), None


def _safe_control_phase(invocation: Invocation, process=None, env=None) -> tuple[tuple[str, ...], str | None]:
    try:
        return _control_phase(invocation, process, env)
    except Exception as exc:  # preserve the setup failure in the control result/log
        return (), f"{invocation.control['id']} setup/injection failed: {type(exc).__name__}: {exc}"


def _default_execute(invocation: Invocation) -> SubjectRun:
    if invocation.control["id"] in OVERLAP:
        return run_overlap_coordinator(invocation)
    project = invocation.output / "work" / "consumer" / "gold-targets.yaml"
    project.parent.mkdir(parents=True, exist_ok=True)
    project.write_text(
        "schemaVersion: 1\ntargets: []\nsuites:\n  live: cases/live\n"
        f"stateDir: {str((invocation.output / 'state').resolve())!r}\n"
    )
    argv = _subject_command(invocation, project)
    env = _service_env(_subject_env(dict(os.environ)), invocation)
    # The edge renders the subject's identity, so both must see one run id: striim-test keeps an
    # operator's SLT_RUN_EPOCH and would otherwise name it after its run directory.
    env.setdefault("SLT_RUN_EPOCH", f"control-{invocation.control['id']}-{uuid.uuid4().hex[:8]}")
    env["SLT_CONTROL_PLAN"] = str(invocation.plan)
    env.update((_yaml(invocation.plan).get("environment") or {}))
    phase_events, control_error = (), None
    control_tokens, control_ack_at, control_observed = None, None, None
    with open(invocation.output / "stdout.log", "wb") as stdout, open(invocation.output / "stderr.log", "wb") as stderr:
        if invocation.control["id"] in LIVE_PHASE:
            process = subprocess.Popen(argv, cwd=invocation.output, env=env, stdin=subprocess.DEVNULL,
                                       stdout=stdout, stderr=stderr)
            process._slt_control_output = str(invocation.output)
            phase_events, control_error = _safe_control_phase(invocation, process, env)
            exit_code = process.wait()
            control_tokens = getattr(process, "_slt_late_row_tokens", None)
            control_ack_at = getattr(process, "_slt_late_row_ack_at", None)
            control_observed = getattr(process, "_slt_control_observed", None)
            for hold in getattr(process, "_slt_control_holds", []):
                try:
                    hold.rollback()
                finally:
                    hold.close()
        else:
            phase_events, control_error = _safe_control_phase(invocation, env=env)
            exit_code = subprocess.run(argv, cwd=invocation.output, env=env, stdin=subprocess.DEVNULL,
                                       stdout=stdout, stderr=stderr).returncode
    junit = next((invocation.output / "state").rglob("junit.xml"), invocation.output / "missing-junit.xml")
    envelopes = tuple((invocation.output / "state").rglob("evidence.json"))
    events_path = invocation.output / "control-events.json"
    events = list(json.loads(events_path.read_text())) if events_path.is_file() else []
    events.extend(phase_events)
    if events:
        _write_json_atomic(events_path, events)
    recovery_exit = None
    if invocation.control["id"] == "l7-cleanup-fault-replay":
        docs = []
        for path in envelopes:
            try:
                docs.append(json.loads(path.read_text()))
            except (OSError, json.JSONDecodeError):
                pass
        if any(
            ((doc.get("lifecycle") or {}).get("cleanup") or {}).get("status") == "failed"
            and ((doc.get("lifecycle") or {}).get("faultInjected") or {}).get("kind") == "pg-table"
            for doc in docs
        ):
            events.append("cleanup-table-fault-injected")
        ledgers = list((invocation.output / "state").rglob("ledgers/*.json"))
        if len(ledgers) == 1:
            events.append("ownership-replay-started")
            replay = subprocess.run(
                [sys.executable, "-m", "livetest.ownership", "replay", str(ledgers[0])],
                cwd=invocation.output,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
            )
            recovery_exit = replay.returncode
            (invocation.output / "ownership-replay.log").write_bytes(replay.stdout + replay.stderr)
            if recovery_exit == 0:
                events.append("ownership-replay-completed")
    return SubjectRun(
        exit_code, junit, envelopes, tuple(events), recovery_exit, control_error,
        control_tokens=control_tokens, control_ack_at=control_ack_at, control_observed=control_observed,
    )


def _junit(path: Path) -> tuple[str, str]:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ControlError(f"missing or invalid JUnit: {exc}") from None
    nodes = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    failures = sum(int(node.attrib.get("failures", 0)) + int(node.attrib.get("errors", 0)) for node in nodes)
    skipped = sum(int(node.attrib.get("skipped", 0)) for node in nodes)
    outcome = "failed" if failures else "skipped" if skipped else "passed"
    text = " ".join((element.text or "") for element in root.iter() if element.tag in {"failure", "error", "skipped"})
    return outcome, text


def _comparison(doc: dict, kind: str):
    comparisons = ((doc.get("data") or {}).get("comparisons") or [])
    return next((row for row in comparisons if isinstance(row, dict) and row.get("type") == kind), None)


def _hash_pair(comparison: dict) -> bool:
    expected = (comparison.get("expected") or {}).get("sha256")
    actual = (comparison.get("actual") or {}).get("sha256")
    return isinstance(expected, str) and SHA_RE.fullmatch(expected) is not None \
        and isinstance(actual, str) and SHA_RE.fullmatch(actual) is not None


def _late_row_condition(tokens: dict | None) -> str | None:
    if not isinstance(tokens, dict):
        return None
    try:
        source_schema = _ident(tokens["PG_SOURCE_SCHEMA"], "schema")
        target_schema = _ident(tokens["PG_TARGET_SCHEMA"], "schema")
        tid = _ident(tokens["TID"], "table prefix")
    except (ControlError, KeyError, TypeError):
        return None
    return f'"{target_schema}"."{tid}tgt" count == "{source_schema}"."{tid}src" count, both > 0'


def _wall_time(value: str | None) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _late_row_stability_observed(doc: dict, tokens: dict | None, ack_at: str | None) -> bool:
    """Accept only this attempt's acknowledged source-ahead-of-target lifecycle failure."""
    lifecycle = doc.get("lifecycle") or {}
    completion = lifecycle.get("completion") or {}
    reason = completion.get("reason")
    if completion.get("kind") != "source-count" or reason not in {"stability-lost", "deadline"}:
        return False
    if completion.get("condition") != _late_row_condition(tokens):
        return False
    baseline = (lifecycle.get("baseline") or {}).get("count")
    observations = completion.get("observations")
    if type(baseline) is not int or baseline <= 0 or not isinstance(observations, list) \
            or not observations or not isinstance(observations[-1], dict):
        return False
    value = observations[-1].get("value")
    if reason == "stability-lost":
        # lifecycle.complete records the failed stability re-read in the sibling stability
        # witness; it is the final completion observation even though Watch keeps the initial
        # satisfied sample in completion.observations.
        stability = lifecycle.get("stability") or {}
        value = stability.get("value")
    if not isinstance(value, dict) or type(value.get("source")) is not int \
            or type(value.get("target")) is not int \
            or value["source"] != baseline + 1 or value["target"] != baseline:
        return False
    ended_at = _wall_time(completion.get("endedAt"))
    acknowledged_at = _wall_time(ack_at)
    if ended_at is None or acknowledged_at is None or acknowledged_at >= ended_at:
        return False
    run = doc.get("run") or {}
    failure = run.get("failure")
    return run.get("status") == "failed" and isinstance(failure, str) \
        and f"lifecycle completion source-count failed: {reason}" in failure


def _late_row_failure(control: dict, docs: list[dict], junit_outcome: str,
                      run: SubjectRun | None, events: list[str] | tuple[str, ...]) -> str | None:
    if control["id"] != "late-row" or run is None:
        return None
    required = control["expected"]["requiredEvents"]
    if run.control_error or run.exit_code == 0 or junit_outcome != "failed" \
            or any(event not in events for event in required):
        return None
    if run.control_tokens is None or run.control_ack_at is None:
        return None
    return "late-row-source-ahead-of-target" \
        if any(_late_row_stability_observed(doc, run.control_tokens, run.control_ack_at) for doc in docs) else None


def _failure(control: dict, docs: list[dict], junit_outcome: str, *, run: SubjectRun | None = None,
             events: list[str] | tuple[str, ...] = ()) -> str | None:
    if control["kind"] == "lifecycle-proof":
        return None
    cid = control["id"]
    if cid == "required-skip":
        return "required-case-skipped" if junit_outcome == "skipped" else None
    if cid == "late-row":
        return _late_row_failure(control, docs, junit_outcome, run, events)
    for doc in docs:
        if doc.get("evidenceVersion") != 2 or doc.get("kind") != "case":
            continue
        if cid in {"wrong-golden", "extra-duplicate"}:
            comparison = _comparison(doc, "data")
            samples = (comparison or {}).get("samples") or {}
            if comparison and comparison.get("equal") is False and not comparison.get("error") \
                    and _hash_pair(comparison) and (samples.get("missing") or samples.get("extra")):
                return "exact-data-mismatch"
        elif cid == "sequence-order":
            comparison = _comparison(doc, "file")
            samples = (comparison or {}).get("samples") or {}
            if comparison and comparison.get("order") == "sequence" and comparison.get("equal") is False \
                    and not comparison.get("error") and _hash_pair(comparison) \
                    and samples.get("missing") == [] and samples.get("extra") == [] \
                    and isinstance(samples.get("firstMismatch"), dict):
                return "sequence-order-mismatch"
        elif cid == "invalid-asset":
            comparison = _comparison(doc, "file")
            error = (comparison or {}).get("error")
            if isinstance(error, str) and error.startswith(("invalid-value:expected:", "exact-input-error")):
                return "invalid-input-asset"
        elif cid == "missing-required-service":
            run = doc.get("run") or {}
            services = (doc.get("runtime") or {}).get("services") or []
            comparisons = ((doc.get("data") or {}).get("comparisons") or [])
            if run.get("status") == "failed" and comparisons == [] \
                    and (doc.get("data") or {}).get("reason") == "no exact data assertion" \
                    and ((doc.get("lifecycle") or {}).get("ready") is None) \
                    and any(isinstance(service, dict) and service.get("name") == MISSING_SERVICE
                            for service in services) \
                    and MISSING_SERVICE_HOST in str(run.get("failure") or ""):
                return "required-service-unavailable"
        else:
            lifecycle = doc.get("lifecycle") or {}
            ready, completion = lifecycle.get("ready") or {}, lifecycle.get("completion") or {}
            if cid == "l1-capture-suppressed" and ready.get("kind") == "sentinel" \
                    and ready.get("reason") == "deadline":
                return "readiness-deadline"
            if cid == "l3-delete-empty-no-done" and completion.get("kind") == "sentinel" \
                    and completion.get("reason") == "deadline":
                return "current-done-sentinel-not-observed"
            if cid == "l7-cleanup-fault-replay" and (lifecycle.get("cleanup") or {}).get("status") == "failed" \
                    and (lifecycle.get("faultInjected") or {}).get("kind") == "pg-table":
                return "cleanup-fault"
            if cid == "l8-blocked-target-insert" and ready.get("kind") == "baseline-landed" \
                    and ready.get("reason") == "deadline":
                return "baseline-landed-deadline"
    return None


def _observed_detail(docs: list[dict], junit_text: str, run: SubjectRun) -> str:
    if run.control_error:
        return run.control_error
    for doc in docs:
        run_doc = doc.get("run") or {}
        for value in (run_doc.get("failure"), (doc.get("data") or {}).get("reason")):
            if isinstance(value, str) and value:
                return value
        lifecycle = doc.get("lifecycle") or {}
        for phase in ("ready", "completion", "cleanup"):
            value = lifecycle.get(phase)
            if isinstance(value, dict) and value.get("reason"):
                return str(value["reason"])
        for comparison in (doc.get("data") or {}).get("comparisons") or []:
            if isinstance(comparison, dict) and comparison.get("error"):
                return str(comparison["error"])
    if run.exit_code == 5:
        return "no tests selected (exit 5; no evidence envelope)"
    if not run.envelopes:
        return f"envelope-free run (subject exit {run.exit_code})"
    return junit_text or "subject failure had no structured reason"


def _observed_label(control: dict, docs: list[dict], junit_outcome: str, junit_text: str,
                    run: SubjectRun, missing_events: list[str], events: list[str] | tuple[str, ...]) -> str | None:
    if control["id"] == "late-row":
        if run.control_error and "after subject exit" in run.control_error:
            return "late-row-injection-ack-after-subject-exit"
        if run.control_error:
            return "late-row-injection-acknowledgement-failed"
        if missing_events:
            return "injection-event-not-observed"
        if run.exit_code == 0 or junit_outcome != "failed":
            return "late-row-subject-did-not-fail"
        if _late_row_failure(control, docs, junit_outcome, run, events) is None:
            return "late-row-source-ahead-of-target-not-observed"
        return None
    if missing_events and control["kind"] != "lifecycle-proof":
        return "injection-event-not-observed"
    if control["kind"] == "lifecycle-proof" and not run.control_error and not missing_events:
        return None
    detail = _observed_detail(docs, junit_text, run)
    lowered = detail.lower()
    if "witness-not-owned" in lowered:
        return "witness-not-owned"
    if run.exit_code == 5 or (not run.envelopes and control["id"] == "required-skip"):
        return "no-tests-selected"
    if control["id"] == "required-skip" and junit_outcome != "skipped":
        return "unexpected-subject-outcome"
    if detail and (run.control_error or not _failure(control, docs, junit_outcome, run=run, events=events)):
        return "unexpected-subject-failure"
    return None


def _prior_sentinel_proof(docs: list[dict], run: SubjectRun) -> str | None:
    """l2's proof, read from the envelope: readiness was satisfied by the current sentinel, the id
    the edge saw beside the prior attempt's row, and never by the prior row. None when shown."""
    observed = run.control_observed or {}
    prior, current = observed.get("priorSentinelId"), observed.get("currentSentinelIds") or []
    ready = next((((doc.get("lifecycle") or {}).get("ready") or {}) for doc in docs
                  if doc.get("kind") == "case"), {})
    if ready.get("kind") != "sentinel" or ready.get("reason") != "satisfied":
        return f"readiness was not satisfied (kind {ready.get('kind')!r}, reason {ready.get('reason')!r})"
    match = re.fullmatch(r"ready sentinel (\d+) observed present then absent", str(ready.get("witness") or ""))
    if not match:
        return f"the readiness witness names no sentinel id: {ready.get('witness')!r}"
    witness = int(match.group(1))
    if witness == prior:
        return f"the readiness witness is the prior attempt's sentinel {prior}"
    if witness not in current:
        return f"the readiness witness {witness} is not an id observed beside the prior row {current}"
    return None


def _relative_record(output: Path, path: Path) -> dict:
    if not _contained(output, path) or not path.is_file():
        raise ControlError(f"evidence path is missing or outside output: {path}")
    return {"path": path.resolve().relative_to(output.resolve()).as_posix(), "sha256": file_sha256(path)}


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def run_control(spec: dict, control_id: str, cases: Path, output: Path,
                *, executor: Callable[[Invocation], SubjectRun] | None = None) -> int:
    controls = {row["id"]: row for row in spec["controls"]}
    if control_id not in controls:
        raise ControlError(f"control id not declared: {control_id}")
    control, cases, output = controls[control_id], Path(cases).resolve(), Path(output).resolve()
    if output.exists():
        raise ControlError(f"control output already exists: {output}")
    output.mkdir(parents=True)
    started = _utc()
    before = tree_sha256(cases)
    copied = output / "work" / "consumer" / "cases" / "live"
    shutil.copytree(cases, copied)
    plan = _plan(control, output / "work", copied)
    run = (executor or _default_execute)(Invocation(control, cases, copied, output, plan))
    after = tree_sha256(cases)
    junit_outcome, _junit_text = _junit(run.junit)
    events = list(run.events)
    errors = []
    expected = control["expected"]
    if expected["subjectExit"] == "nonzero":
        if run.exit_code == 0:
            errors.append("subject exit was zero")
    elif run.exit_code != expected["subjectExit"]:
        errors.append(f"subject exit {run.exit_code} != {expected['subjectExit']}")
    junit_record = {**_relative_record(output, run.junit), "outcome": junit_outcome}
    envelope_records = [_relative_record(output, path) for path in run.envelopes]
    envelope_docs = []
    if run.control_error:
        errors.append(f"observed setup/injection failure: {run.control_error}")
    if not envelope_records:
        errors.append(f"no evidence envelopes (observed subject exit {run.exit_code})")
    for path in run.envelopes:
        try:
            envelope = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            errors.append(f"invalid evidence envelope {path.name}")
            continue
        envelope_docs.append(envelope)
        if control["kind"] == "negative" and (envelope.get("run") or {}).get("qualifies") is True:
            errors.append(f"negative/control envelope qualifies: {path.name}")
    missing_events = [event for event in expected["requiredEvents"] if event not in events]
    failure = _failure(control, envelope_docs, junit_outcome, run=run, events=events)
    observed_failure = None
    if failure is None:
        observed_failure = _observed_label(
            control, envelope_docs, junit_outcome, _junit_text, run, missing_events, events,
        )
    observed_detail = _observed_detail(envelope_docs, _junit_text, run)
    if control_id == L2:
        why = _prior_sentinel_proof(envelope_docs, run)
        if why:
            errors.append(f"prior-sentinel proof not shown: {why}")
            observed_failure = observed_failure or "prior-sentinel-proof-not-shown"
    if failure != expected["failure"]:
        errors.append(
            f"failure {failure!r} != {expected['failure']!r}; "
            f"observedFailure: {observed_failure!r}; observed reason: {observed_detail}"
        )
    if junit_outcome != expected["junit"]:
        errors.append(f"JUnit outcome {junit_outcome!r} != {expected['junit']!r}")
    if len(events) != len(set(events)):
        errors.append("events contain duplicates")
    if missing_events:
        errors.append(f"missing required events {missing_events}; observed: {_observed_detail(envelope_docs, _junit_text, run)}")
    if run.recovery_exit != expected["recoveryExit"]:
        errors.append(f"recovery exit {run.recovery_exit!r} != {expected['recoveryExit']!r}")
    if before != after:
        errors.append("positive assets changed")
    wrapper_exit = 0 if not errors else 1
    result = {
        "schemaVersion": SCHEMA_VERSION,
        "controlId": control_id,
        "case": control["case"],
        "startedAt": started,
        "finishedAt": _utc(),
        "wrapperExit": wrapper_exit,
        "subjectExit": run.exit_code,
        "failure": failure,
        "observedFailure": observed_failure,
        "junit": junit_record,
        "envelopes": envelope_records,
        "events": events,
        "positiveAssets": {"beforeSha256": before, "afterSha256": after},
        "recoveryExit": run.recovery_exit,
        "overlap": run.overlap_evidence,
        "observed": run.control_observed,
    }
    _write_json_atomic(output / "control-result.json", result)
    if errors:
        (output / "control-errors.log").write_text("\n".join(errors) + "\n")
    return wrapper_exit


def _record_refusal(output: Path, control_id: str, case, cases: Path, started: str, detail: str) -> None:
    output.mkdir(parents=True, exist_ok=True)
    try:
        digest = tree_sha256(cases)
    except (ControlError, OSError):
        digest = None
    result = {
        "schemaVersion": SCHEMA_VERSION,
        "controlId": control_id,
        "case": case,
        "startedAt": started,
        "finishedAt": _utc(),
        "wrapperExit": 2,
        "subjectExit": None,
        "failure": None,
        "observedFailure": None,
        "junit": None,
        "envelopes": [],
        "events": [],
        "positiveAssets": {"beforeSha256": digest, "afterSha256": digest},
        "recoveryExit": None,
        "overlap": None,
    }
    _write_json_atomic(output / "control-result.json", result)
    (output / "control-errors.log").write_text(detail + "\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="controls.py")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--spec", required=True, type=Path)
    run.add_argument("--id", required=True)
    run.add_argument("--cases", required=True, type=Path)
    run.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    started = _utc()
    spec = None
    output_preexisted = args.output.exists()
    try:
        spec = load_spec(args.spec)
        return run_control(spec, args.id, args.cases, args.output)
    except ControlError as exc:
        case = next((row["case"] for row in (spec or {}).get("controls", []) if row["id"] == args.id), None)
        if not output_preexisted:
            try:
                _record_refusal(args.output, args.id, case, args.cases, started, str(exc))
            except OSError:
                pass
        print(f"controls.py: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
