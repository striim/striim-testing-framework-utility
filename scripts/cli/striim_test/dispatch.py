"""List / run: per-tier pytest processes and C2 runners (C5).

Each pytest tier runs in its OWN process, with plugin autoload disabled and exactly one
engine plugin (``livetest.plugin`` for live, ``inttest.plugin`` for integration and perf) plus
``striim_test.pytest_guard``, which selects, deselects and re-checks provenance. A generated
``pytest.ini`` keeps a consumer's own ``addopts`` out. Roots and state reach the child through
its environment before its interpreter starts (the livetest/inttest path keys, set from the
project manifest when there is one); the parent never imports a plugin.

Run directory: ``<stateDir>/runs/<utc>-<uuid8>/`` (its basename is every tier child's
``SLT_RUN_EPOCH``) holding ``identity.json``, ``outcome.json``
and per tier ``pytest.ini``, ``guard.json``, ``selection.json``, ``results.json``,
``junit.xml`` (run only), ``stdout.log``, ``stderr.log``, ``outcome.json``; runners write
``runners/<name>/``.

Case IDs: ``<tier>:<consumer-root-relative case dir>::<pytest item name>``.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from striim_test import project_io
from striim_test.errors import (CANCELLED, CONFIG, FAILED, INFRA, NO_TESTS, OK, CliError,
                                aggregate)

PYTEST_TIERS = ("live", "integration", "perf")
DEFAULT_TIERS = ("live", "integration")
PLUGINS = {"live": "livetest.plugin", "integration": "inttest.plugin", "perf": "inttest.plugin"}
PARALLEL_WORKERS = 3
_LIVE_MARKERS = ["live: end-to-end test requiring a Striim server"] + [
    f"{s}: live test whose manifest requires the {s} service"
    for s in ("postgres", "oracle", "mssql", "spanner", "gcs", "kafka", "mysql", "vertica")] + [
    # Every live case carries depth_<its depth:>; unlisted, each printed a
    # PytestUnknownMarkWarning into stdout.log. The names follow livetest.manifest.VALID_DEPTHS.
    f"depth_{d}: live test whose manifest says depth: {d}"
    for d in ("smoke", "gate", "regression", "fault", "customer", "measure", "canary")]
_INT_MARKERS = ["integration: integration test (requires Docker services)",
                "postgres: requires Postgres", "oracle: requires Oracle",
                "spanner: requires Spanner", "gcs: requires the GCS emulator",
                "perf: performance test (PERF_SPEC.md, run under --perf)"]
MARKERS = {"live": _LIVE_MARKERS, "integration": _INT_MARKERS, "perf": _INT_MARKERS}
# Ambient pytest inputs that bypass the generated pytest.ini and PYTEST_DISABLE_PLUGIN_AUTOLOAD
# (a shell's PYTEST_ADDOPTS=--collect-only would otherwise turn `run` into a collection).
PYTEST_AMBIENT = ("PYTEST_ADDOPTS", "PYTEST_PLUGINS")
_OUTCOME_KEYS = ("passed", "failed", "skipped", "xfailed", "errors")


@dataclass
class TierOutcome:
    tier: str
    label: str
    code: int
    reason: str
    returncode: int | None
    dir: str
    selected: list = field(default_factory=list)
    deselected: list = field(default_factory=list)
    skipped: list = field(default_factory=list)


@dataclass
class Ctx:
    project: object
    origins: object
    state: Path
    run: Path


def _say(msg: str) -> None:
    print(f"striim-test: {msg}", file=sys.stderr, flush=True)


def _read_json(p: Path):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def _write_json(p: Path, payload) -> None:
    p.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")


def child_env(origins, base=None, location=None) -> dict:
    """Environment of a tier child. PYTHONPATH is kept: the parent's provenance check has
    already refused any sys.path entry that carries another engine copy, and every pytest child
    checks again (``pytest_guard``). SLT_FRAMEWORK_HOME is the clone the parent runs from.
    ``location`` is the child's ``location_env``: the ownership keys, service settings and Striim
    settings come from the .env of the project root the child gets, not the parent's."""
    env = dict(os.environ if base is None else base)
    env.update(ownership_env({**env, **(location or {})}))
    env.update(service_env({**env, **(location or {})}))
    env.update(striim_env({**env, **(location or {})}))
    env["SLT_FRAMEWORK_MODE"] = origins.mode
    env["SLT_FRAMEWORK_HOME"] = str(origins.home)
    return env


# The engine reads these from its process environment (livetest.infra, services, the session
# teardown), so a value given only in .env is handed to the tier child. The shell still wins.
OWNERSHIP_KEYS = ("SLT_INFRA_OWNERSHIP", "SLT_KEEP_SERVICES")


def ownership_env(env) -> dict:
    """The infrastructure ownership keys .env supplies where ``env`` leaves them unset: the shell, then
    the .env of ``env``'s project root, then the clone's .env (a key the project's .env leaves unset).
    SLT_KEEP_SERVICES is taken from .env only when the effective ownership is ``shared``: an
    exclusive run opted into from the shell must not inherit .env's kept stack."""
    from livetest import paths
    clone_env = {k: v for k, v in env.items() if k != "SLT_PROJECT_ROOT"}

    def dotenv(key):
        return paths.setting(key, env) or paths.setting(key, clone_env)

    own, keep = OWNERSHIP_KEYS
    out = {}
    owner = (env.get(own) or "").strip()
    if not owner:
        owner = dotenv(own) or ""
        if owner:
            out[own] = owner
    if owner == "shared" and not (env.get(keep) or "").strip():
        value = dotenv(keep)
        if value:
            out[keep] = value
    return out


def service_env(env) -> dict:
    """The service settings (``livetest.paths.SERVICE_KEYS``, e.g. SLT_PG_HOST) .env supplies where
    ``env`` leaves them unset, with the precedence of ``ownership_env``: the shell, then the .env
    of ``env``'s project root, then the clone's .env, then machine settings. The engines
    read them from their process environment."""
    from livetest import paths
    from livetest.service_env import declarations, values
    resolved = paths.effective_env(env, values(paths, env))
    keys = set(paths.SERVICE_KEYS) | set(paths.LICENCE_KEYS) | set(declarations(env))
    return {k: resolved[k] for k in keys if (resolved.get(k) or "").strip()
            and not (env.get(k) or "").strip()}




# Read by the engine from its process environment as well as its .env (livetest.infra and
# ownership read only the environment), so the child gets the value the parent resolved.
STRIIM_KEYS = ("STRIIM_URL", "STRIIM_USER", "STRIIM_PASS", "STRIIM_API_TIMEOUT")


def striim_env(env) -> dict:
    """STRIIM_URL/USER/PASS/API_TIMEOUT that .env supplies where ``env`` leaves them unset, with the layers of
    ``service_env``: the shell, then the .env of ``env``'s project root, then the clone's .env,
    then machine settings. STRIIM_PASSWORD is STRIIM_PASS's alias: either name in the shell wins, and a value found
    under either name in a .env is handed on as STRIIM_PASS."""
    from livetest import paths
    clone_env = {k: v for k, v in env.items() if k != "SLT_PROJECT_ROOT"}
    layers = (paths.read_dotenv(paths.dotenv_path(env)),
              paths.read_dotenv(paths.dotenv_path(clone_env)), paths.machine_values(env))
    out = {}
    for key in STRIIM_KEYS:
        names = paths._ALIASES.get(key, (key,))
        if any((env.get(n) or "").strip() for n in names):
            continue
        value = next((v for v in ((d.get(n) or "").strip() for d in layers for n in names) if v), "")
        if value:
            out[key] = value
    return out


def location_env(ctx) -> dict:
    """The path keys a tier child reads. With a project manifest, its root and
    suites become SLT_PROJECT_ROOT / SLT_LIVE_CASES / SLT_INT_CASES, and the run's state dir
    (manifest stateDir, else SLT_STATE_DIR as the consumer resolves it) becomes SLT_STATE_DIR, so
    parent and children agree (both tiers keep distinct coordination files there, assumption A7).
    Without a manifest the child inherits or defaults exactly as it would without striim-test.
    SLT_STATE_DIR is exported only when the manifest declares stateDir or SLT_STATE_DIR is set (in
    the shell or the project's .env); otherwise each tier keeps its own default (plan A7), so the
    integration child's coordination files stay in scripts/integration."""
    env = {}
    project = ctx.project
    if project.manifest is None:
        return env
    from livetest import paths as live_paths
    declared = project.state_dir is not None or live_paths._lookup(
        "SLT_STATE_DIR", {**os.environ, "SLT_PROJECT_ROOT": str(project.root)})[0]
    if declared:
        env["SLT_STATE_DIR"] = str(ctx.state)
    env["SLT_PROJECT_ROOT"] = str(project.root)
    if "live" in project.suites:
        env["SLT_LIVE_CASES"] = os.pathsep.join(map(str, project_io.tier_roots(project, "live")))
    if "integration" in project.suites:
        env["SLT_INT_CASES"] = str(project_io.tier_root(project, "integration"))
    return env


def check_perf_layout(project) -> None:
    """The integration engine finds perf cases at ``inttest.paths.perf_dir()`` of the integration
    cases, so a manifest perf suite must be exactly there (checked only when the perf tier runs)."""
    if project.manifest is None or "perf" not in project.suites:
        return
    from inttest import paths as int_paths
    perf = project_io.tier_root(project, "perf")
    integ = (project_io.tier_root(project, "integration")
             if "integration" in project.suites else None)
    want = int_paths.perf_dir({**os.environ, "SLT_INT_CASES": str(integ)}) if integ else None
    if want is None or Path(want).resolve() != perf:
        raise CliError(CONFIG, f"suites.perf {perf} must be {want or '<suites.integration>/../perf'}: "
                               f"the integration engine finds perf cases beside suites.integration "
                               f"({integ or 'not declared'})")


def _prepare(args, origins):
    project = project_io.load(args.targets)
    runners = project_io.load_runners(project)
    for tier, p in sorted(project.suites.items()):
        if tier in PYTEST_TIERS:
            project_io.tier_root(project, tier)
    if getattr(args, "path", None):
        _path_tier(args, project)
    state = project_io.state_root(project)
    run = project_io.new_run_dir(state)
    _say(f"run-dir: {run}")
    ctx = Ctx(project, origins, state, run)
    # (C5/C7.5 1.7.0): the run id every tier child receives, and the declared ownership
    # (as the tier child gets it: the shell, else the .env of the child's project root).
    env = {**os.environ, **location_env(ctx)}
    owner = (env.get("SLT_INFRA_OWNERSHIP") or "").strip() or ownership_env(env).get("SLT_INFRA_OWNERSHIP")
    _write_json(run / "identity.json", {**project_io.base_identity(project, origins),
                                        "runEpoch": project_io.run_epoch(run),
                                        "infraOwnership": owner or None})
    return ctx, runners


def _path_tier(args, project):
    """``run PATH``: (tier, case dir or folder), or CliError before any run dir exists."""
    if getattr(args, "suite", None):
        raise CliError(CONFIG, "PATH and --suite both select a folder; pass one of them")
    return project_io.case_path(project, args.path, PYTEST_TIERS, getattr(args, "tier", None))


def _plan(args, project, runners):
    tier = getattr(args, "tier", None)
    suite = getattr(args, "suite", None)
    cases = getattr(args, "case", None) or []
    by_name = {r.name: r for r in runners}

    if getattr(args, "path", None):
        return [_path_tier(args, project)], []

    if suite and suite in by_name:
        r = by_name[suite]
        if tier and tier != r.tier:
            raise CliError(CONFIG, f"--suite {suite} is a runner of tier {r.tier}, not {tier}")
        if cases:
            raise CliError(CONFIG, "--case cannot select inside a C2 runner (no selection "
                                   "injection in 2.4)")
        return [], [r]

    if tier:
        tier_runners = [r for r in runners if r.tier == tier]
        if tier in PYTEST_TIERS and tier in project.suites:
            path = (project_io.suite_path(project, tier, suite) if suite
                    else project_io.tier_root(project, tier))
            if cases and tier_runners and not suite:
                raise CliError(CONFIG, f"--case cannot select inside the {tier} runners "
                                       f"{[r.name for r in tier_runners]}; pass --suite")
            return [(tier, path)], ([] if suite else tier_runners)
        if suite:
            raise CliError(CONFIG, f"--suite {suite} is neither a runner name nor a suite of a "
                                   f"declared pytest tier")
        if not tier_runners:
            if tier in project.suites:
                raise CliError(CONFIG, f"suites.{tier} is declared but no engine runs it in "
                                       "striim-test 2.4 (declare a C2 runner for it)")
            raise CliError(CONFIG, f"unknown tier {tier!r}: not a declared suite tier "
                                   f"{sorted(project.suites)} and no runner declares it")
        if cases:
            raise CliError(CONFIG, "--case cannot select inside a C2 runner (no selection "
                                   "injection in 2.4)")
        return [], tier_runners

    if suite:
        hits = []
        for t in PYTEST_TIERS:
            if t in project.suites:
                try:
                    hits.append((t, project_io.suite_path(project, t, suite)))
                except CliError:
                    pass
        if len(hits) != 1:
            raise CliError(CONFIG, f"--suite {suite} must lie inside exactly one declared tier "
                                   f"root; matched {[t for t, _ in hits] or 'none'} (pass --tier)")
        return hits, []

    tiers = [(t, project_io.tier_root(project, t)) for t in DEFAULT_TIERS if t in project.suites]
    if not tiers:
        raise CliError(CONFIG, f"{project_io.describe(project)} has no live or integration "
                               "suite; pass --tier")
    return tiers, []


def build_pytest_argv(ctx: Ctx, tier: str, path: Path, tier_dir: Path, *, cases, collect_only,
                      parallel=False, keep=False):
    ini = tier_dir / "pytest.ini"
    ini.write_text("[pytest]\nmarkers =\n" + "".join(f"    {m}\n" for m in MARKERS[tier]))
    root = ctx.project.root
    argv = [sys.executable, "-m", "pytest", "-c", str(ini), "--rootdir", str(root),
            "--confcutdir", str(root), "-p", "no:cacheprovider",
            "-p", "striim_test.pytest_guard", "-p", PLUGINS[tier],
            "--import-mode=importlib", "-rfEs"]
    location = location_env(ctx)
    if ctx.project.manifest is not None:
        location["GOLD_TARGETS"] = str(ctx.project.manifest)
    env = child_env(ctx.origins, location=location)
    for name in PYTEST_AMBIENT:
        env.pop(name, None)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    if ctx.project.manifest is not None:
        env["GOLD_TARGETS"] = str(ctx.project.manifest)
    else:
        env.pop("GOLD_TARGETS", None)
    env.update(location_env(ctx))
    env["SLT_RUN_EPOCH"] = project_io.run_epoch(ctx.run, env)   # run + case + worker identity
    env["SLT_INVOCATION_ID"] = uuid.uuid4().hex                # this child's invocation
    env["SLT_RUN_IDENTITY"] = str(ctx.run / "identity.json")
    if tier == "perf":
        argv.append("--perf")
    if collect_only:
        argv += ["--collect-only", "-q"]
    else:
        argv += ["--junitxml", str(tier_dir / "junit.xml")]
        if parallel and tier == "live":
            argv += ["-p", "xdist.plugin", "--parallel", str(PARALLEL_WORKERS)]
        elif parallel and tier == "integration":
            argv += ["-p", "xdist.plugin", "-n", str(PARALLEL_WORKERS)]
            env["SLT_PARALLEL"] = "1"
        if keep and tier == "live":
            env["SLT_KEEP_RESOURCES"] = "1"
        elif keep:
            argv.append("--slt-keep-resources")
    guard = tier_dir / "guard.json"
    _write_json(guard, {
        "tier": tier, "suiteRoot": str(path), "consumerRoot": str(root),
        # A whole primary case root collects every root of the tier (livetest's plugin).
        "suiteRoots": [str(r) for r in (project_io.tier_roots(ctx.project, tier)
                                        if tier in ctx.project.suites
                                        and Path(path).resolve() == project_io.tier_root(ctx.project, tier)
                                        else [path])],
        # Every declared case root of the tier: case ids, whatever the selection narrows to.
        "caseRoots": [str(r) for r in (project_io.tier_roots(ctx.project, tier)
                                       if tier in ctx.project.suites else [])],
        "manifest": str(ctx.project.manifest) if ctx.project.manifest is not None else None,
        "cases": list(cases),
        "packages": {k: str(v) for k, v in ctx.origins.packages.items()},
        "selectionOut": str(tier_dir / "selection.json"),
        "resultsOut": str(tier_dir / "results.json"),
    })
    env["STRIIM_TEST_GUARD"] = str(guard)
    argv.append(str(path))
    return argv, env


def _selection(tier_dir: Path):
    sel = _read_json(tier_dir / "selection.json")
    if sel is not None:
        return sel
    workers = sorted(tier_dir.glob("selection-*.json"))
    if not workers:
        return None
    merged = _read_json(workers[0]) or {}
    violations = set(merged.get("provenanceViolations") or [])
    for w in workers[1:]:
        violations |= set((_read_json(w) or {}).get("provenanceViolations") or [])
    merged["provenanceViolations"] = sorted(violations)
    return merged


def result_lines(label: str, sel, res) -> list[str]:
    """What a finished tier says besides its exit line: how many tests passed, failed and were
    skipped, and each failed test with its one-line message, so the console tells what happened
    without opening stdout.log. A tier that passes says so too, instead of going quiet."""
    res = res or {}
    by_node = {e["nodeid"]: e["id"] for e in (sel or {}).get("selected", [])}
    counts = ", ".join(f"{len(res[k])} {k}" for k in _OUTCOME_KEYS if res.get(k))
    lines = [f"{label}: {counts}"] if counts else []
    for key, word in (("failed", "FAILED"), ("errors", "ERROR")):
        for e in res.get(key) or []:
            name = by_node.get(e["nodeid"], e["nodeid"])
            lines.append(f"{label}: {word} {name}: {e.get('message') or 'see stdout.log'}")
    return lines


def not_executed(sel, res) -> list:
    """Selected case IDs with no reported outcome (passed/failed/skipped/xfailed/errors)."""
    reported = {e["nodeid"] for key in _OUTCOME_KEYS for e in (res or {}).get(key, [])}
    return [e["id"] for e in (sel or {}).get("selected", []) if e["nodeid"] not in reported]


def map_exit(rc, sel, res, collect_only) -> tuple[int, str]:
    if sel is None:
        return {0: (CONFIG, "guard-selection-missing"), 1: (FAILED, "tests-failed"),
                2: (CANCELLED, "interrupted"), 5: (NO_TESTS, "no-tests-collected")}.get(
                    rc, (CONFIG, f"pytest-exit:{rc}"))
    if sel.get("provenanceViolations") or (res or {}).get("provenanceViolations"):
        return CONFIG, "provenance-violation"
    if sel.get("duplicates"):
        return CONFIG, "duplicate-case"
    if sel.get("collectionErrors"):
        return CONFIG, "collection-error"
    if not sel.get("selected"):
        return NO_TESTS, "no-tests-selected"
    if rc == 0:
        if collect_only:
            return OK, "collected"
        if res is None:
            return CONFIG, "guard-results-missing"
        if res.get("collectOnly") or not_executed(sel, res):
            return CONFIG, "selected-not-executed"
        if res.get("skipped"):
            return INFRA, "selected-item-skipped"
        return OK, "ok"
    if rc == 1:
        r = res or {}
        if not r.get("failed") and not r.get("errors") and r.get("skipped"):
            return INFRA, "selected-item-skipped"
        return FAILED, "tests-failed"
    if rc == 2:
        return CANCELLED, "interrupted"
    if rc == 5:
        return NO_TESTS, "no-tests-collected"
    return CONFIG, f"pytest-exit:{rc}"


def run_tier(ctx: Ctx, tier: str, path: Path, *, label: str, cases=(), collect_only: bool,
             parallel=False, keep=False) -> TierOutcome:
    tier_dir = ctx.run / label
    tier_dir.mkdir()
    argv, env = build_pytest_argv(ctx, tier, path, tier_dir, cases=cases,
                                  collect_only=collect_only, parallel=parallel, keep=keep)
    _write_json(tier_dir / "command.json", {"argv": argv, "cwd": str(tier_dir),
                                            "env": {k: env[k] for k in ("SLT_RUN_EPOCH", "SLT_INVOCATION_ID",
                                                                        "SLT_RUN_IDENTITY")}})
    if not collect_only:
        # The tier's output goes to its log, not the console: say how to watch it while it runs.
        _say(f"{label}: follow it with: tail -f {shlex.quote(str(tier_dir / 'stdout.log'))}")
    with open(tier_dir / "stdout.log", "wb") as so, open(tier_dir / "stderr.log", "wb") as se:
        try:
            rc = subprocess.run(argv, cwd=tier_dir, env=env, stdin=subprocess.DEVNULL,
                                stdout=so, stderr=se).returncode
        except KeyboardInterrupt:
            rc = None
    sel = _selection(tier_dir)
    res = _read_json(tier_dir / "results.json")
    code, reason = (CANCELLED, "interrupted") if rc is None else map_exit(rc, sel, res, collect_only)
    by_node = {e["nodeid"]: e["id"] for e in (sel or {}).get("selected", [])}
    skipped = [{"id": by_node.get(s["nodeid"], s["nodeid"]), "reason": s.get("reason", "")}
               for s in (res or {}).get("skipped", [])]
    outcome = TierOutcome(tier, label, code, reason, rc, str(tier_dir),
                          selected=[e["id"] for e in (sel or {}).get("selected", [])],
                          deselected=[{"id": e["id"], "reason": e["reason"]}
                                      for e in (sel or {}).get("deselected", [])],
                          skipped=skipped)
    _write_json(tier_dir / "outcome.json", asdict(outcome))
    if not collect_only and code != NO_TESTS:
        for line in result_lines(label, sel, res):
            _say(line)
    if code == OK and not collect_only:
        _say(f"{label}: {reason} (exit {code}) [logs: {tier_dir}]")
    if code not in (OK, NO_TESTS):
        detail = ""
        if reason == "provenance-violation":
            detail = "; ".join((sel or {}).get("provenanceViolations") or
                               (res or {}).get("provenanceViolations") or [])
        elif reason == "duplicate-case":
            detail = json.dumps(sel.get("duplicates"))
        elif reason == "collection-error":
            detail = "; ".join(e["nodeid"] for e in sel.get("collectionErrors"))
        elif reason == "selected-not-executed":
            detail = "; ".join(not_executed(sel, res)) or "pytest ran in collect-only mode"
        elif reason == "pytest-exit:4":
            detail = usage_error(tier_dir)
        elif reason.startswith("pytest-exit:"):
            detail = internal_error(tier_dir)
        elif skipped:
            detail = "; ".join(f"{s['id']}: {s['reason']}" for s in skipped)
        _say(f"{label}: {reason} (exit {code}) {detail} [logs: {tier_dir}]")
        if not collect_only:
            _say(f"{label}: the whole output is {tier_dir / 'stdout.log'}; docs/TROUBLESHOOTING.md, "
                 f"'Where the logs are', lists every log and the service containers' logs")
    return outcome


def usage_error(tier_dir: Path) -> str:
    """The usage error (pytest rc 4) a tier child printed -- for example the C7.1 ownership refusal -- from a
    bounded tail of its stderr.log, so the operator sees the reason and not only ``pytest-exit:4``."""
    try:
        with open(tier_dir / "stderr.log", "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 65536))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return "; ".join(ln[len("ERROR: "):] for ln in text.splitlines() if ln.startswith("ERROR: "))[-2000:]


_EXCEPTION_LINE = re.compile(r"^INTERNALERROR> ([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt)\b.*)$")


def internal_error(tier_dir: Path) -> str:
    """The first exception line of the INTERNALERROR block a crashed tier child (pytest rc 3) printed to its
    stdout.log -- for example a PermissionError on the lock dir -- so the operator sees the reason and not
    only ``pytest-exit:3``. Bounded read of the head of the log."""
    try:
        with open(tier_dir / "stdout.log", "rb") as f:
            text = f.read(262144).decode("utf-8", "replace")
    except OSError:
        return ""
    for line in text.splitlines():
        m = _EXCEPTION_LINE.match(line)
        if m:
            return m.group(1)[:2000]
    return ""


def _print_selection(o: TierOutcome) -> None:
    for cid in o.selected:
        print(cid)
    for d in o.deselected:
        if d["reason"].startswith("disabled"):
            print(f"# deselected {d['id']} ({d['reason']})")


def _finish(ctx: Ctx, codes, parts) -> int:
    code = aggregate(codes)
    _write_json(ctx.run / "outcome.json", {"exit": code, "parts": parts})
    if code == NO_TESTS:
        _say("no tests selected (exit 5)")
    return code


def _plan_checked(args, ctx, runners):
    tiers, selected_runners = _plan(args, ctx.project, runners)
    if any(t == "perf" for t, _ in tiers):
        check_perf_layout(ctx.project)
    return tiers, selected_runners


def cmd_list(args, origins) -> int:
    ctx, runners = _prepare(args, origins)
    tiers, selected_runners = _plan_checked(args, ctx, runners)
    codes, parts = [], []
    for tier, path in tiers:
        o = run_tier(ctx, tier, path, label=tier, collect_only=True)
        _print_selection(o)
        codes.append(o.code)
        parts.append(asdict(o))
    for r in selected_runners:
        print(f"runner:{r.name}")
        codes.append(OK)
        parts.append({"runner": r.name, "tier": r.tier, "exit": OK, "reason": "declared"})
    return _finish(ctx, codes, parts)


def cmd_run(args, origins) -> int:
    ctx, runners = _prepare(args, origins)
    tiers, selected_runners = _plan_checked(args, ctx, runners)
    if args.parallel and any(t == "perf" for t, _ in tiers):
        raise CliError(CONFIG, "--parallel is not allowed for the perf tier (performance mode "
                               "runs serially, PERF_SPEC.md §9)")
    cases = args.case or []
    codes, parts = [], []

    if cases and not args.tier and len(tiers) > 1:
        probes = []
        for t, p in tiers:
            o = run_tier(ctx, t, p, label=f"{t}-probe", cases=cases, collect_only=True)
            parts.append(asdict(o))
            if o.code not in (OK, NO_TESTS):
                return _finish(ctx, [o.code], parts)
            if o.code == OK:
                probes.append((t, p, o))
        if len(probes) > 1:
            _say(f"--case {' '.join(cases)} is ambiguous: it matches in tiers "
                 f"{[t for t, _, _ in probes]} (pass --tier)")
            return _finish(ctx, [CONFIG], parts)
        if not probes:
            return _finish(ctx, [NO_TESTS], parts)
        t, p, o = probes[0]
        if args.dry_run:
            _print_selection(o)
            return _finish(ctx, [OK], parts)
        tiers = [(t, p)]

    for t, p in tiers:
        o = run_tier(ctx, t, p, label=t, cases=cases, collect_only=args.dry_run,
                     parallel=args.parallel, keep=args.keep_resources)
        if args.dry_run:
            _print_selection(o)
        codes.append(o.code)
        parts.append(asdict(o))
    from striim_test import runner as runner_mod
    for r in selected_runners:
        if args.dry_run:
            print(f"runner:{r.name}")
            codes.append(OK)
            parts.append({"runner": r.name, "exit": OK, "reason": "not-executed-dry-run"})
            continue
        ro = runner_mod.execute(r, ctx.run)
        if ro.code != OK:
            _say(f"runner {r.name}: {ro.reason} (exit {ro.code}) {ro.detail}")
        codes.append(ro.code)
        parts.append({"runner": r.name, "exit": ro.code, "reason": ro.reason,
                      "result": str(ro.result_path)})
    return _finish(ctx, codes, parts)
