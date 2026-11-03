"""Per-case run records: what a consumer's incremental certification reuses (field T1).

Every live and integration session writes one JSON-lines file per pytest process, whether or not
``--junitxml`` is given, so a run started by hand or by a script counts as much as one a tool
started. Path: ``<store>/<session>/<process>.jsonl``, where ``<store>`` is ``SLT_CASE_RECORDS``
(``off`` writes nothing), else ``<project root>/.local-evidence/case-records``; ``<session>`` is
shared by every xdist worker of one run and ``<process>`` is ``main`` or the worker id.

Lines, in order:

- ``session``, read and written just before the process's first case runs: the project root case
  paths are relative to (``caseRoot``), the source repo's HEAD and dirty paths, the framework
  checkout's HEAD and dirty paths, the recorded settings (``RECORDED_ENV``), host, tier, start time.
  A run that runs no case (collection, a dry run) spawns nothing and writes nothing.
- ``item``, one per case item at its teardown: case dir (relative to ``caseRoot``), item name,
  nodeid, the item names its case file collected in this process (its variants), status
  (``passed``/``failed``/``error``/``skipped``), skip or xfail reason, start and end, and
  ``striim``: the release the engine resolved (from ``STRIIM_HOME``; ``STRIIM_VERSION`` does not
  choose it), ``STRIIM_HOME`` and ``SLT_STRIIM_TRYOUT_JARS``.
- ``end``, at session finish: exit status, the nodeids an evidence error marked, the failed
  teardowns of owned infrastructure, and the source and framework state read again (a change
  during the run shows as a difference from the header).

The engine computes no fingerprint: the consumer recomputes its own from the recorded commit, so a
record never has to know how the consumer builds. A file without an ``end`` line is a process
that did not finish (it crashed or was interrupted after its first case started); a consumer must
not reuse that session. Writing is best effort and never changes a run's
outcome: a record that cannot be written leaves a case without reusable evidence, which is safe.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pytest

SCHEMA = 1
ENV_STORE = "SLT_CASE_RECORDS"
DEFAULT_SUBDIR = Path(".local-evidence") / "case-records"
# Settings that change what a case does, recorded as they were (none holds a secret). The Striim
# release is not among them: each item records the release the engine resolved (``striim``).
RECORDED_ENV = ("STRIIM_VERSION", "STRIIM_HOME", "SLT_UI_FORM", "SLT_PARALLEL",
                "SLT_STRIIM_JAVA_SYSTEM_PROPERTIES", "SLT_STRIIM_TRYOUT_JARS")
GIT_TIMEOUT_S = 5.0
_PLUGIN_NAME = "slt-case-records"


def store_dir(project_root, env=None) -> Path | None:
    """Where records go, or None when ``SLT_CASE_RECORDS`` is ``off``/``0``/``false``."""
    env = os.environ if env is None else env
    value = (env.get(ENV_STORE) or "").strip()
    if value.lower() in ("off", "0", "false", "no"):
        return None
    if value:
        return Path(value).expanduser().resolve()
    return Path(project_root).resolve() / DEFAULT_SUBDIR


def git_state(root) -> dict:
    """``{"head", "dirty"}`` of the worktree at ``root``, or ``{"unreadable": why}``."""
    def run(*args):
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                              timeout=GIT_TIMEOUT_S)
    try:
        head = run("rev-parse", "HEAD")
        if head.returncode != 0:
            return {"unreadable": (head.stderr or "").strip()[:200] or f"git rc {head.returncode}"}
        top = run("rev-parse", "--show-toplevel")
        status = run("status", "--porcelain=v1", "-z", "--untracked-files=all")
        if status.returncode != 0:
            return {"unreadable": (status.stderr or "").strip()[:200] or f"git rc {status.returncode}"}
    except Exception as e:              # noqa: BLE001 -- OSError, a timeout, undecodable output
        return {"unreadable": f"git: {type(e).__name__}: {e}"}
    entries = iter((status.stdout or "").split("\0"))
    dirty = []
    for entry in entries:
        if len(entry) < 4:
            continue
        dirty.append(entry[3:])
        if entry[0] in "RC" or entry[1] in "RC":
            dirty.append(next(entries, ""))     # a rename's source left its old place
    return {"root": (top.stdout or "").strip() or str(root), "head": (head.stdout or "").strip(),
            "dirty": sorted(filter(None, dirty))}


def _framework_state() -> dict:
    try:
        return _framework_state_unguarded()
    except Exception as e:              # noqa: BLE001
        return {"unreadable": f"{type(e).__name__}: {e}"}


def _framework_state_unguarded() -> dict:
    import livetest
    prov = getattr(livetest, "__provenance__", {}) or {}
    if prov.get("BUILT_FROM") not in (None, "checkout"):
        sha = prov.get("GIT_SHA")
        return {"head": sha, "dirty": ["<built dirty>"] if prov.get("DIRTY") else []} if sha else \
            {"unreadable": "an installed framework without GIT_SHA"}
    return git_state(Path(livetest.__file__).resolve().parent)


def _session_id(config) -> str:
    worker = getattr(config, "workerinput", None)
    if worker and worker.get("testrunuid"):
        return worker["testrunuid"]
    # The controller (or a plain run) fixes the id; xdist hands --testrunuid to every worker.
    option = getattr(config, "option", None)
    if option is not None and hasattr(option, "testrunuid"):
        if not option.testrunuid:
            option.testrunuid = uuid.uuid4().hex
        return option.testrunuid
    return uuid.uuid4().hex


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Recorder:
    """A pytest plugin writing one process's records. ``is_case(item)`` picks the engine's items;
    ``case_dir(item)`` is the item's case directory."""

    def __init__(self, config, tier: str, source_root, is_case, case_dir, env=None, release=None):
        self.config = config
        self.tier = tier
        self.is_case = is_case
        self.case_dir = case_dir
        self.source_root = Path(source_root)
        self.env = os.environ if env is None else env
        self.release = release          # () -> the engine's resolved release dict
        self._striim: dict | None = None
        self.path: Path | None = None
        self.variants: dict[str, list[str]] = {}
        self.phases: dict[str, dict] = {}
        self.failed = False
        self.started = False
        self.source: dict = {}
        self.header_written = False
        worker = getattr(config, "workerinput", None)
        self.process = (worker or {}).get("workerid") or "main"
        self._worker = worker
        store = store_dir(source_root, self.env)
        if store is not None:
            self.path = store / _session_id(config) / f"{self.process}.jsonl"

    @property
    def controller(self) -> bool:
        """An xdist controller runs no case: its workers record them; it records only its own end.
        Read late: xdist registers its session after this recorder is installed."""
        return self._worker is None and self.config.pluginmanager.hasplugin("dsession")

    def _write(self, doc: dict) -> None:
        """Append one line. Nothing is written before the first case starts (``start``), so a run
        that runs no case leaves no file; a process that dies after that leaves a file without an
        ``end`` line, which a consumer must not reuse."""
        if self.path is None or self.failed:
            return
        try:
            line = json.dumps(doc, sort_keys=True, default=str) + "\n"
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
            if doc["kind"] == "session":
                self.header_written = True
        except Exception as e:          # noqa: BLE001 -- never change a run's outcome
            self.failed = True
            print(f"[slt] case records not written to {self.path}: {type(e).__name__}: {e}")

    def _striim_identity(self) -> dict:
        """The Striim the cases ran against: the release the engine resolved (from STRIIM_HOME, not
        STRIIM_VERSION), STRIIM_HOME, and any tryout jars patched into the cluster."""
        if self._striim is None:
            try:
                rel = self.release() if self.release is not None else None
                version = (rel or {}).get("STRIIM_VERSION") if isinstance(rel, dict) else None
                release = version or {"unreadable": "the engine resolved no release"}
            except Exception as e:      # noqa: BLE001
                release = {"unreadable": f"{type(e).__name__}: {e}"}
            self._striim = {"release": release, "home": self.env.get("STRIIM_HOME"),
                            "tryouts": self.env.get("SLT_STRIIM_TRYOUT_JARS") or None}
        return self._striim

    def _relative(self, path) -> str:
        """`path` relative to the project root (``caseRoot`` in the header), else absolute."""
        try:
            return Path(path).resolve().relative_to(self.source_root.resolve()).as_posix()
        except ValueError:
            return Path(path).resolve().as_posix()

    def start(self) -> None:
        """Read the source and framework state, once, just before the first case runs (a run that
        runs no case, a dry run or a collection, spawns nothing)."""
        if self.started:
            return
        self.started = True
        self.source = git_state(self.source_root)
        if self.path is None:
            return
        self._write({"kind": "session", "schema": SCHEMA, "tier": self.tier, "process": self.process,
                     "startedAt": _now(), "host": socket.gethostname(), "pid": os.getpid(),
                     "caseRoot": str(self.source_root.resolve()), "source": self.source,
                     "framework": _framework_state(),
                     "env": {k: self.env.get(k) for k in RECORDED_ENV},
                     "args": list(getattr(getattr(self.config, "invocation_params", None), "args", ()) or ())})

    # -- hooks ---------------------------------------------------------------------------------

    def _guarded(self, what, fn, *args) -> None:
        try:
            fn(*args)
        except Exception as e:          # noqa: BLE001 -- never change a run's outcome
            self.failed = True
            print(f"[slt] case records stopped ({what}): {type(e).__name__}: {e}")

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_setup(self, item):
        if self.is_case(item):
            self._guarded("start", self.start)

    def pytest_collectreport(self, report):
        self._guarded("collect", self._collected, report)

    def _collected(self, report):
        items = [i for i in (report.result or []) if hasattr(i, "nodeid") and self.is_case(i)]
        for item in items:
            names = self.variants.setdefault(self._relative(self.case_dir(item)), [])
            if item.name not in names:
                names.append(item.name)

    def pytest_runtest_logreport(self, report):
        self._guarded("report", self._reported, report)

    def _reported(self, report):
        if self.path is None or self.controller or not getattr(report, "_slt_case_record", False):
            return
        state = self.phases.setdefault(report.nodeid, {"status": "passed", "reason": None,
                                                       "startedAt": _now(), "xfail": None})
        if report.failed:
            state["status"] = "failed" if report.when == "call" else "error"
        elif report.skipped and state["status"] == "passed":
            state["status"] = "skipped"
            if hasattr(report, "wasxfail"):
                state["xfail"] = report.wasxfail or ""
            else:
                longrepr = report.longrepr
                state["reason"] = str(longrepr[2] if isinstance(longrepr, tuple) and len(longrepr) == 3
                                      else longrepr)[:500]
        if report.when == "teardown":
            info = getattr(report, "_slt_case_info", None) or {}
            case = info.get("case")
            self._write({"kind": "item", "case": case, "item": info.get("name"), "nodeid": report.nodeid,
                         "variants": list(self.variants.get(case) or []), "status": state["status"],
                         "skipReason": state["reason"], "xfail": state["xfail"],
                         "striim": self._striim_identity(),
                         "startedAt": state["startedAt"], "endedAt": _now()})
            self.phases.pop(report.nodeid, None)

    def mark(self, item, report) -> None:
        """Tag a case item's report (the engine's makereport wrapper calls this): xdist carries the
        attributes to the controller, and only tagged reports are recorded."""
        if not self.is_case(item):
            return
        report._slt_case_record = True
        report._slt_case_info = {"case": self._relative(self.case_dir(item)), "name": item.name}

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session, exitstatus):
        self._guarded("finish", self.finish, session)

    def finish(self, session) -> None:
        # An xdist controller ends a session its workers recorded: its own teardown is part of it.
        joins = self.controller and self.path is not None and self.path.parent.is_dir()
        if not (self.header_written or joins):
            return
        self.start()
        config = session.config
        # Read again: a change made while the run was going (an edit, a commit, a checkout) means
        # the header no longer describes what every case ran on. A consumer compares the two.
        source = git_state(self.source_root)
        framework = _framework_state()
        errors = sorted({e.get("nodeid") for e in (getattr(config, "_slt_evidence_errors", None) or [])
                         if e.get("nodeid")})
        self._write({"kind": "end", "endedAt": _now(), "exitstatus": int(getattr(session, "exitstatus", 1) or 0),
                     "evidenceErrors": errors, "source": source, "framework": framework,
                     "teardownFailures": list(getattr(config, "_slt_teardown_failures", None) or [])})


def install(config, tier: str, source_root, is_case, case_dir, release=None) -> Recorder | None:
    """Register one recorder per process (both engines may load in one session; the first wins).
    None, and nothing registered, when records are off or anything here fails."""
    try:
        if store_dir(source_root) is None:
            return None
        pm = config.pluginmanager
        existing = pm.get_plugin(_PLUGIN_NAME)
        if existing is not None:
            return existing
        rec = Recorder(config, tier, source_root, is_case, case_dir, release=release)
        pm.register(rec, _PLUGIN_NAME)
        return rec
    except Exception as e:              # noqa: BLE001 -- never fail a run over its records
        print(f"[slt] case records disabled: {type(e).__name__}: {e}")
        return None


def recorder(config) -> Recorder | None:
    pm = getattr(config, "pluginmanager", None)
    return pm.get_plugin(_PLUGIN_NAME) if pm is not None else None
