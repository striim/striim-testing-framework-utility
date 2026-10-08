"""Project manifest access for the CLI parent process (thin wrappers over livetest.project).

Imported only after provenance is checked. The parent process never imports a pytest plugin.
With no project manifest (neither ``--targets`` nor ``GOLD_TARGETS``) the project is this
clone's own suites, from the path keys: ``SLT_PROJECT_ROOT``,
``SLT_LIVE_CASES``, ``SLT_INT_CASES`` and ``SLT_STATE_DIR``, each falling back to today's layout.
"""
from __future__ import annotations

import hashlib
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from striim_test.errors import CONFIG, CliError

_INSTALL_PARTS = ("site-packages", "dist-packages")
# C1 framework.mode values a clone cannot honour ("sibling" is what a clone used in place is).
RETIRED_FRAMEWORK_MODES = ("wheel", "legacy")


def _path_errors():
    from inttest import paths as int_paths
    from livetest import paths as live_paths
    from livetest import project as _project
    return (_project.ProjectError, _project.LayoutError, live_paths.PathConfigError,
            int_paths.PathConfigError)


def load(targets):
    """``--targets`` > ``GOLD_TARGETS`` > this clone's default project. A set location that is
    missing, or an invalid manifest, is a configuration error (exit 2)."""
    from livetest import project as _project
    try:
        project = _project.load_project(targets) or default_project()
    except _path_errors() as e:
        raise CliError(CONFIG, str(e)) from None
    mode = (project.framework or {}).get("mode")
    if mode in RETIRED_FRAMEWORK_MODES:
        raise CliError(CONFIG, f"{project.manifest}: framework.mode={mode} is retired: striim-test runs "
                               f"from a framework clone used in place (pip install -e); remove "
                               f"framework.mode or set it to sibling")
    return project


def default_project():
    """The project when no manifest is named: the live and integration case roots (and the perf
    root beside the integration one) that exist, under ``SLT_PROJECT_ROOT``."""
    from inttest import paths as int_paths
    from livetest import paths as live_paths
    from livetest.project import Project
    live = live_paths.live_case_roots()
    suites = {tier: p for tier, p in (("live", live[0]),
                                      ("integration", int_paths.int_cases()),
                                      ("perf", int_paths.perf_dir())) if p.is_dir()}
    extra = {"live": tuple(live[1:])} if "live" in suites and live[1:] else {}
    return Project(manifest=None, root=live_paths.project_root(), suites=suites, extra_suites=extra)


def describe(project) -> str:
    return str(project.manifest) if project.manifest is not None else \
        f"the default project at {project.root} (no project manifest)"


def load_runners(project, env=None) -> list:
    from striim_test import runner
    decls = [runner.load_runner(p, project.root, env) for p in project.runners]
    seen = {}
    for d in decls:
        if d.name in seen:
            raise CliError(CONFIG, f"runner name {d.name!r} is declared by both {seen[d.name]} "
                                   f"and {d.path}")
        seen[d.name] = d.path
    return decls


def state_root(project, env=None) -> Path:
    """Writable state: manifest ``stateDir`` > ``livetest.paths.state_dir()`` (``SLT_STATE_DIR``,
    else the live engine dir, where the engine keeps its own coordination files)."""
    if project.state_dir is not None:
        d = Path(project.state_dir)
    else:
        from livetest import paths as live_paths
        e = dict(os.environ if env is None else env)
        if project.manifest is not None:
            # resolve as the tier children will: their SLT_PROJECT_ROOT (and so .env) is the consumer's
            e["SLT_PROJECT_ROOT"] = str(project.root)
        try:
            d = live_paths.state_dir(e)
        except live_paths.PathConfigError as err:
            raise CliError(CONFIG, str(err)) from None
    d = d.resolve()
    if any(part in _INSTALL_PARTS for part in d.parts):
        raise CliError(CONFIG, f"state dir {d} is inside an install tree (package data is "
                               "read-only)")
    return d


def new_run_dir(state: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run = state / "runs" / f"{stamp}-{uuid.uuid4().hex[:8]}"
    run.mkdir(parents=True, exist_ok=False)
    return run


def run_epoch(run, env=None) -> str:
    """(C5/C7.5 1.7.0): the run id tier children receive as ``SLT_RUN_EPOCH``. An operator's
    non-empty value wins; otherwise the run directory basename ``<utc>-<uuid8>``, in full."""
    e = os.environ if env is None else env
    return e.get("SLT_RUN_EPOCH") or Path(run).name


def tier_root(project, tier: str) -> Path:
    if tier not in project.suites:
        raise CliError(CONFIG, f"tier {tier!r} is not declared in {describe(project)} suites "
                               f"(declared: {sorted(project.suites)})")
    p = Path(project.suites[tier])
    if not p.is_dir():
        raise CliError(CONFIG, f"suites.{tier} {p} is not a directory")
    return p.resolve()


def tier_roots(project, tier: str) -> list:
    """Every case root of ``tier``, primary (``tier_root``) first: suites.live (or
    SLT_LIVE_CASES) may list several."""
    roots = [tier_root(project, tier)]
    for p in (getattr(project, "extra_suites", None) or {}).get(tier, ()):
        if not Path(p).is_dir():
            raise CliError(CONFIG, f"suites.{tier} {p} is not a directory")
        roots.append(Path(p).resolve())
    return roots


def suite_path(project, tier: str, suite: str) -> Path:
    """``--suite`` is project-relative and must resolve inside a case root of ``suites.<tier>``."""
    roots = tier_roots(project, tier)
    if Path(suite).is_absolute():
        raise CliError(CONFIG, f"--suite {suite} must be relative to the project root {project.root}")
    cand = (project.root / suite).resolve()
    if not any(cand == root or root in cand.parents for root in roots):
        raise CliError(CONFIG, f"--suite {suite} resolves to {cand}, outside suites.{tier} "
                               f"({', '.join(map(str, roots))})")
    if not cand.exists():
        raise CliError(CONFIG, f"--suite {suite} does not exist ({cand})")
    return cand


def case_path(project, raw: str, tiers, tier=None) -> tuple:
    """(tier, dir) for ``run PATH``: PATH (a case dir, its test.yaml, or a folder of cases) is
    resolved against the working directory and must lie in exactly one of the declared case
    roots of ``tiers`` (with ``tier``, in that one). A file selects the case dir holding it."""
    p = Path(raw).expanduser().resolve()
    if not p.exists():
        raise CliError(CONFIG, f"{raw} does not exist ({p})")
    roots = [(t, r) for t in tiers if t in project.suites for r in tier_roots(project, t)]
    hits = list(dict.fromkeys(t for t, r in roots if p == r or r in p.parents))
    if not hits:
        listed = ", ".join(f"{t}: {r}" for t, r in roots) or "none declared"
        raise CliError(CONFIG, f"{raw} is outside every case root ({listed}); move it under "
                               f"one, or point SLT_LIVE_CASES (or the manifest's suites) at it")
    if tier is not None:
        if tier not in hits:
            raise CliError(CONFIG, f"{raw} is in the {' and '.join(hits)} case root, not {tier}")
        hits = [tier]
    if len(hits) > 1:
        raise CliError(CONFIG, f"{raw} lies in the case roots of {hits}; pass --tier")
    return hits[0], (p.parent if p.is_file() else p)


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def base_identity(project, origins) -> dict:
    import striim_test
    return {
        "schemaVersion": 1,
        "tool": "striim-test",
        "version": striim_test.__version__,
        "interpreter": sys.executable,
        "argv": sys.argv,
        **origins.as_json(),
        "manifest": ({"path": str(project.manifest), "sha256": sha256(project.manifest)}
                     if project.manifest is not None else None),
        "consumerRoot": str(project.root),
    }
