"""C2 native runner declarations (schema v1): strict loading and execution.

Loading (any violation is a configuration error, exit 2, naming the file and field):
unknown fields; ``name``/``tier`` spelling; a non-empty string ``argv``; ``shell: true``
without ``shellReviewed: true``; ``result.policy.zeroTests``/``requiredSkip`` other than
``fail``; adapters and prerequisite kinds outside the frozen sets. Capabilities frozen in C2
but not implemented in 2.4 fail as named unsupported capabilities: shell runners, non-empty
``knownFailures``, ``testng-xml``/``chorus-xml``, ``selection.default`` injection and
``allowEmpty: true``.

Path expansion (contract set 1.3.0): ``$VAR``/``${VAR}`` expand from the process environment
only in ``cwd``, ``prereq[].path`` and ``artifacts.reports[].path``; an unset variable is a
configuration error. A relative value is manifest-relative and must stay inside the consumer
root; an absolute value is accepted only when it came from a leading variable. ``argv`` is
never expanded; ``env`` values expand only for keys listed in ``envExpand``.

Execution: prerequisites in order (first failure -> 3, runner not launched); argv launch in a
new process group (launch failure -> 3); timeout -> SIGTERM the group, wait ``cancelGrace``,
SIGKILL the group, confirm it is gone (-> 4); then reports. A nonzero process exit is 1 even
when reports pass; a missing, stale, malformed, zero-test, failing or skipping report is 1 even
when the exit is 0. Success is launch + exit 0 + every required report fresh and clean.
"""
from __future__ import annotations

import glob
import json
import os
import re
import signal
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from striim_test import junit
from striim_test.errors import CANCELLED, CONFIG, FAILED, INFRA, OK, CliError

_TOP = ("schemaVersion", "name", "tier", "description", "cwd", "argv", "env", "envExpand",
        "shell", "shellReviewed", "prereq", "selection", "timeout", "processGroup",
        "artifacts", "result")
_NAME_RE = re.compile(r"^[a-z0-9-]+$")
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
_ADAPTERS = ("junit-xml", "testng-xml", "chorus-xml", "none")
_REPORT_KINDS = ("junit-xml", "testng-xml", "chorus-xml", "file")
_NOT_IN_24 = ("testng-xml", "chorus-xml")
_PREREQ_KEYS = {"cmd": ("kind", "argv", "expectExit"), "file": ("kind", "path"),
                "http": ("kind", "url", "expectStatus")}
HTTP_PROBE_TIMEOUT = 5.0
PREREQ_CMD_TIMEOUT = 60.0
GROUP_EXIT_WAIT = 5.0
FRESHNESS_TOLERANCE = 1.0   # coarse filesystem mtimes; identity change is also required


class RunnerError(CliError):
    def __init__(self, path, message: str):
        super().__init__(CONFIG, f"runner {path}: {message}")


@dataclass
class RunnerDecl:
    path: Path
    name: str
    tier: str
    cwd: Path
    argv: list
    env: dict
    prereq: list
    timeout_total: float
    cancel_grace: float
    process_group: bool
    reports: list
    adapter: str


@dataclass
class RunnerOutcome:
    code: int
    reason: str
    detail: str
    result_path: Path | None = None
    facts: dict = field(default_factory=dict)


def _unknown(mapping: dict, allowed, where: str, path) -> None:
    extra = sorted(set(mapping) - set(allowed))
    if extra:
        raise RunnerError(path, f"unknown field(s) {extra} in {where} (allowed: {list(allowed)})")


def _mapping(raw, where: str, path) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise RunnerError(path, f"{where} must be a mapping")
    return raw


def _glob_anchor(p: Path) -> Path:
    parts = []
    for part in p.parts:
        if any(ch in part for ch in "*?["):
            break
        parts.append(part)
    return Path(*parts) if parts else Path(".")


def _expand_path(raw, where: str, root: Path, env: dict, path) -> Path:
    if not isinstance(raw, str) or not raw:
        raise RunnerError(path, f"{where} must be a non-empty string")
    for a, b in _VAR_RE.findall(raw):
        if (a or b) not in env:
            raise RunnerError(path, f"{where} uses unset environment variable ${a or b}")
    expanded = _VAR_RE.sub(lambda m: env[m.group(1) or m.group(2)], raw)
    p = Path(expanded)
    if p.is_absolute():
        if not raw.startswith("$"):
            raise RunnerError(path, f"{where} {raw!r} is absolute: a runner path is "
                                    "manifest-relative or starts with an environment variable")
        return p
    joined = root / p
    anchor = _glob_anchor(joined).resolve()
    if anchor != root and root not in anchor.parents:
        raise RunnerError(path, f"{where} {raw!r} escapes the consumer root {root}")
    return joined


def load_runner(path, project_root, env=None) -> RunnerDecl:
    path = Path(path)
    root = Path(project_root).resolve()
    e = dict(os.environ if env is None else env)
    try:
        raw = yaml.safe_load(path.read_text())
    except OSError as exc:
        raise RunnerError(path, f"cannot read: {exc}") from None
    except yaml.YAMLError as exc:
        raise RunnerError(path, f"invalid YAML: {exc}") from None
    if not isinstance(raw, dict):
        raise RunnerError(path, "top level must be a mapping")
    _unknown(raw, _TOP, "the declaration", path)

    sv = raw.get("schemaVersion")
    if isinstance(sv, bool) or sv != 1:
        raise RunnerError(path, f"schemaVersion must be 1, got {sv!r}")
    name = raw.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise RunnerError(path, f"name must match [a-z0-9-]+, got {name!r}")
    tier = raw.get("tier")
    if not isinstance(tier, str) or not _NAME_RE.match(tier):
        raise RunnerError(path, f"tier must match [a-z0-9-]+, got {tier!r}")
    argv = raw.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
        raise RunnerError(path, "argv is required: a non-empty list of non-empty strings")

    shell = raw.get("shell", False)
    reviewed = raw.get("shellReviewed", False)
    if not isinstance(shell, bool) or not isinstance(reviewed, bool):
        raise RunnerError(path, "shell and shellReviewed must be true/false")
    if shell and not reviewed:
        raise RunnerError(path, "shell: true requires shellReviewed: true with a named reviewer "
                                "(a token is never interpolated into a shell string)")
    if shell:
        raise RunnerError(path, "shell runners are an unsupported capability in 2.4 (the "
                                "reviewed wrapper is phase 3)")

    result = _mapping(raw.get("result"), "result", path)
    _unknown(result, ("adapter", "policy"), "result", path)
    adapter = result.get("adapter")
    if adapter not in _ADAPTERS:
        raise RunnerError(path, f"result.adapter {adapter!r} is not a supported capability "
                                f"({'|'.join(_ADAPTERS)})")
    if adapter in _NOT_IN_24:
        raise RunnerError(path, f"result.adapter {adapter} is unsupported in 2.4 (product "
                                "adapters are phase 3)")
    policy = _mapping(result.get("policy"), "result.policy", path)
    _unknown(policy, ("zeroTests", "requiredSkip", "knownFailures"), "result.policy", path)
    for key, meaning in (("zeroTests", "a selected suite with 0 tests is a failure"),
                         ("requiredSkip", "a required skip is a failure")):
        if policy.get(key) != "fail":
            raise RunnerError(path, f"result.policy.{key} must be fail ({meaning}), "
                                    f"got {policy.get(key)!r}")
    if policy.get("knownFailures"):
        raise RunnerError(path, "result.policy.knownFailures entries are unsupported in 2.4 "
                                "(phase 3 with the product adapters)")

    prereq = []
    raw_prereq = raw.get("prereq") or []
    if not isinstance(raw_prereq, list):
        raise RunnerError(path, "prereq must be a list")
    for i, pr in enumerate(raw_prereq):
        where = f"prereq[{i}]"
        pr = _mapping(pr, where, path)
        kind = pr.get("kind")
        if kind not in _PREREQ_KEYS:
            raise RunnerError(path, f"{where}.kind {kind!r} is not a supported capability "
                                    "(cmd|file|http)")
        _unknown(pr, _PREREQ_KEYS[kind], where, path)
        if kind == "cmd":
            pargv = pr.get("argv")
            if not isinstance(pargv, list) or not pargv or not all(isinstance(a, str) for a in pargv):
                raise RunnerError(path, f"{where}.argv must be a non-empty list of strings")
            expect = pr.get("expectExit", 0)
            if isinstance(expect, bool) or not isinstance(expect, int):
                raise RunnerError(path, f"{where}.expectExit must be an integer")
            prereq.append({"kind": kind, "argv": pargv, "expectExit": expect})
        elif kind == "file":
            prereq.append({"kind": kind,
                           "path": str(_expand_path(pr.get("path"), f"{where}.path", root, e, path))})
        else:
            url = pr.get("url")
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                raise RunnerError(path, f"{where}.url must be an http(s) URL")
            status = pr.get("expectStatus", 200)
            if isinstance(status, bool) or not isinstance(status, int):
                raise RunnerError(path, f"{where}.expectStatus must be an integer")
            prereq.append({"kind": kind, "url": url, "expectStatus": status})

    env_raw = _mapping(raw.get("env"), "env", path)
    for k, v in env_raw.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise RunnerError(path, f"env.{k} must be a string value (literal)")
    expand_keys = raw.get("envExpand") or []
    if not isinstance(expand_keys, list) or not all(isinstance(k, str) for k in expand_keys):
        raise RunnerError(path, "envExpand must be a list of env key names")
    runner_env = {}
    for k, v in env_raw.items():
        if k in expand_keys:
            for a, b in _VAR_RE.findall(v):
                if (a or b) not in e:
                    raise RunnerError(path, f"env.{k} uses unset environment variable ${a or b}")
            v = _VAR_RE.sub(lambda m: e[m.group(1) or m.group(2)], v)
        runner_env[k] = v
    for k in expand_keys:
        if k not in env_raw:
            raise RunnerError(path, f"envExpand names {k!r}, which env does not declare")

    selection = _mapping(raw.get("selection"), "selection", path)
    _unknown(selection, ("default", "allowEmpty"), "selection", path)
    if selection.get("default"):
        raise RunnerError(path, "selection.default injection is unsupported in 2.4 (use [] for "
                                "the runner's own default scope)")
    if selection.get("allowEmpty", False) is not False:
        raise RunnerError(path, "selection.allowEmpty must be false (zero selected is never a "
                                "success, C5 exit 5)")

    artifacts = _mapping(raw.get("artifacts"), "artifacts", path)
    _unknown(artifacts, ("reports",), "artifacts", path)
    reports = []
    raw_reports = artifacts.get("reports") or []
    if not isinstance(raw_reports, list):
        raise RunnerError(path, "artifacts.reports must be a list")
    for i, rep in enumerate(raw_reports):
        where = f"artifacts.reports[{i}]"
        rep = _mapping(rep, where, path)
        _unknown(rep, ("kind", "path", "required", "freshness"), where, path)
        kind = rep.get("kind")
        if kind not in _REPORT_KINDS:
            raise RunnerError(path, f"{where}.kind {kind!r} is not a supported capability "
                                    f"({'|'.join(_REPORT_KINDS)})")
        if kind in _NOT_IN_24:
            raise RunnerError(path, f"{where}.kind {kind} is unsupported in 2.4 (phase 3)")
        if kind == "junit-xml" and adapter != "junit-xml":
            raise RunnerError(path, f"{where}.kind junit-xml needs result.adapter junit-xml")
        required = rep.get("required", True)
        if not isinstance(required, bool):
            raise RunnerError(path, f"{where}.required must be true/false")
        if rep.get("freshness", "run") != "run":
            raise RunnerError(path, f"{where}.freshness must be run")
        pattern = _expand_path(rep.get("path"), f"{where}.path", root, e, path)
        reports.append({"kind": kind, "pattern": str(pattern), "required": required})
    if not any(r["required"] for r in reports):
        raise RunnerError(path, "artifacts.reports needs at least one required report: a process "
                                "exit alone is never success (C2)")
    if adapter == "junit-xml" and not any(r["kind"] == "junit-xml" for r in reports):
        raise RunnerError(path, "result.adapter junit-xml needs an artifacts.reports entry of "
                                "kind junit-xml")

    timeout = _mapping(raw.get("timeout"), "timeout", path)
    _unknown(timeout, ("total", "cancelGrace"), "timeout", path)
    total = timeout.get("total")
    grace = timeout.get("cancelGrace", 30)
    for key, val in (("total", total), ("cancelGrace", grace)):
        if isinstance(val, bool) or not isinstance(val, (int, float)) or val < 0 or \
                (key == "total" and val == 0):
            raise RunnerError(path, f"timeout.{key} must be a {'positive' if key == 'total' else 'non-negative'} "
                                    f"number of seconds, got {val!r}")
    group = raw.get("processGroup", True)
    if not isinstance(group, bool):
        raise RunnerError(path, "processGroup must be true/false")

    cwd = _expand_path(raw["cwd"], "cwd", root, e, path) if raw.get("cwd") is not None else root
    return RunnerDecl(path=path.resolve(), name=name, tier=tier, cwd=Path(cwd), argv=list(argv),
                      env=runner_env, prereq=prereq, timeout_total=float(total),
                      cancel_grace=float(grace), process_group=group, reports=reports,
                      adapter=adapter)


def _check_prereq(pr: dict, cwd: Path, env: dict) -> tuple[bool, str]:
    kind = pr["kind"]
    if kind == "cmd":
        try:
            r = subprocess.run(pr["argv"], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=PREREQ_CMD_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"{pr['argv'][0]}: {exc}"
        if r.returncode != pr["expectExit"]:
            return False, f"{pr['argv'][0]} exited {r.returncode} (expected {pr['expectExit']})"
        return True, ""
    if kind == "file":
        return (True, "") if Path(pr["path"]).exists() else (False, f"{pr['path']} does not exist")
    try:
        with urllib.request.urlopen(pr["url"], timeout=HTTP_PROBE_TIMEOUT) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except (urllib.error.URLError, OSError) as exc:
        return False, f"{pr['url']}: {exc}"
    if status != pr["expectStatus"]:
        return False, f"{pr['url']} answered {status} (expected {pr['expectStatus']})"
    return True, ""


def _identity(p: str):
    try:
        st = os.stat(p)
    except OSError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns)


def _signal_group(proc, decl: RunnerDecl, sig) -> None:
    try:
        if decl.process_group:
            os.killpg(proc.pid, sig)
        else:
            proc.send_signal(sig)
    except (ProcessLookupError, PermissionError):
        pass


def _cancel(proc, decl: RunnerDecl) -> bool:
    """SIGTERM, grace, SIGKILL; True when no member of the runner's group remains."""
    _signal_group(proc, decl, signal.SIGTERM)
    try:
        proc.wait(timeout=decl.cancel_grace)
    except subprocess.TimeoutExpired:
        pass
    _signal_group(proc, decl, signal.SIGKILL)
    try:
        proc.wait(timeout=GROUP_EXIT_WAIT)
    except subprocess.TimeoutExpired:
        return False
    if not decl.process_group:
        return True
    deadline = time.monotonic() + GROUP_EXIT_WAIT
    while time.monotonic() < deadline:
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.05)
    return False


def _check_reports(decl: RunnerDecl, before: dict, run_start: float):
    facts, failure = [], None

    def fail(reason, detail):
        nonlocal failure
        if failure is None:
            failure = (reason, detail)

    for rep in decl.reports:
        matches = sorted(glob.glob(rep["pattern"]))
        if not matches:
            if rep["required"]:
                fail("report-missing", rep["pattern"])
            facts.append({"pattern": rep["pattern"], "matches": 0})
            continue
        for m in matches:
            entry = {"path": m, "kind": rep["kind"]}
            facts.append(entry)
            ident = _identity(m)
            st_mtime = os.stat(m).st_mtime if ident else 0
            fresh = ident is not None and before.get(m) != ident and \
                st_mtime >= run_start - FRESHNESS_TOLERANCE
            entry["fresh"] = fresh
            if not fresh:
                fail("report-stale", f"{m} was not written by this run")
                continue
            if rep["kind"] == "file":
                if os.path.getsize(m) == 0:
                    fail("report-empty", m)
                continue
            try:
                f = junit.parse_junit(m)
            except junit.JunitError as exc:
                fail(exc.reason, exc.detail)
                continue
            entry.update(tests=f.tests, failures=f.failures, errors=f.errors, skipped=f.skipped)
            if f.tests == 0:
                fail("report-zero-tests", m)
            elif f.failures + f.errors:
                fail(f"report-failures:{f.failures + f.errors}", ", ".join(f.failing))
            elif f.skipped:
                fail(f"required-skip:{f.skipped}", ", ".join(f.skipped_ids))
    return facts, failure


def execute(decl: RunnerDecl, run_dir, env=None) -> RunnerOutcome:
    out = Path(run_dir) / "runners" / decl.name
    out.mkdir(parents=True, exist_ok=False)
    child_env = dict(os.environ if env is None else env)
    child_env.update(decl.env)
    record = {"runner": decl.name, "tier": decl.tier, "declaration": str(decl.path),
              "argv": decl.argv, "cwd": str(decl.cwd)}

    def finish(code, reason, detail, **extra) -> RunnerOutcome:
        record.update(exit=code, reason=reason, detail=detail, **extra)
        (out / "result.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        return RunnerOutcome(code, reason, detail, out / "result.json", record)

    for i, pr in enumerate(decl.prereq):
        ok, detail = _check_prereq(pr, decl.cwd, child_env)
        if not ok:
            return finish(INFRA, f"prereq[{i}]:{pr['kind']}", detail, launched=False)

    before = {m: _identity(m) for rep in decl.reports for m in glob.glob(rep["pattern"])}
    run_start = time.time()
    with open(out / "stdout.log", "wb") as so, open(out / "stderr.log", "wb") as se:
        try:
            proc = subprocess.Popen(decl.argv, cwd=decl.cwd, env=child_env, stdin=subprocess.DEVNULL,
                                    stdout=so, stderr=se, start_new_session=decl.process_group)
        except (FileNotFoundError, PermissionError, NotADirectoryError) as exc:
            return finish(INFRA, "launch-failed", f"{decl.argv[0]}: {exc}", launched=False)
        try:
            rc = proc.wait(timeout=decl.timeout_total)
        except subprocess.TimeoutExpired:
            cleared = _cancel(proc, decl)
            return finish(CANCELLED, "timeout",
                          f"exceeded timeout.total={decl.timeout_total:g}s",
                          launched=True, runStart=run_start, processGroupCleared=cleared)
        except KeyboardInterrupt:
            cleared = _cancel(proc, decl)
            return finish(CANCELLED, "cancelled", "interrupted", launched=True,
                          runStart=run_start, processGroupCleared=cleared)

    facts, failure = _check_reports(decl, before, run_start)
    common = dict(launched=True, runStart=run_start, returncode=rc, reports=facts)
    if rc != 0:
        return finish(FAILED, f"process-exit:{rc}",
                      "a nonzero process exit is a failure whatever the reports say", **common)
    if failure is not None:
        return finish(FAILED, failure[0], failure[1], **common)
    return finish(OK, "ok", f"{len(facts)} fresh report(s)", **common)
