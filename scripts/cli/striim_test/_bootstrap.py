"""Engine provenance, checked before either engine is imported.

``check_provenance`` locates ``livetest``, ``inttest`` and ``striim_test`` with
``importlib.util.find_spec`` (which does not import a top-level package) and enforces ONE
origin: a framework clone, used in place (``pip install -e .`` or ``PYTHONPATH``).

* The three packages must be exactly ``<clone>/scripts/{live,integration,cli}/<package>`` of the
  same clone. An installed copy (site-packages) is refused: wheels are not a distribution form.
* ``SLT_FRAMEWORK_HOME`` is optional. Unset, the framework home is the clone
  the packages come from. Set, in the environment or ``.env``, it must name that same clone, so
  a stale variable never runs one checkout's CLI against another's engine.
* Every ``sys.path`` entry is scanned for another copy of a package, and already-imported
  ``livetest*``/``inttest*``/``striim_test*`` modules must come from the selected origins.

The same checks run again inside every pytest process (``striim_test.pytest_guard``).
"""
from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass
from pathlib import Path

ENV_HOME = "SLT_FRAMEWORK_HOME"
RETIRED = {"SLT_MODE": "SLT_FRAMEWORK_MODE", "SLT_FRAMEWORK": ENV_HOME}
PACKAGES = ("livetest", "inttest", "striim_test")
SIBLING_PARENTS = {"livetest": ("scripts", "live"), "inttest": ("scripts", "integration"),
                   "striim_test": ("scripts", "cli")}
MODE = "clone"
_INSTALL_PARTS = ("site-packages", "dist-packages")


class ProvenanceError(Exception):
    pass


@dataclass
class Origins:
    mode: str
    mode_source: str
    home: Path
    packages: dict

    def as_json(self) -> dict:
        return {"mode": self.mode, "modeSource": self.mode_source,
                "frameworkHome": str(self.home),
                "packages": {k: str(v) for k, v in sorted(self.packages.items())}}


def _installed(p: Path) -> bool:
    return any(part in _INSTALL_PARTS for part in p.parts)


def _package_dir(name: str) -> Path:
    mod = sys.modules.get(name)
    if mod is not None and getattr(mod, "__file__", None):
        return Path(mod.__file__).resolve().parent
    spec = importlib.util.find_spec(name)
    if spec is None:
        raise ProvenanceError(f"package {name} is not importable by {sys.executable}")
    if not spec.origin or spec.origin == "namespace":
        raise ProvenanceError(f"package {name} resolves to a namespace package (no __init__.py) "
                              f"at {list(spec.submodule_search_locations or [])}")
    return Path(spec.origin).resolve().parent


def _clone_of(packages: dict) -> Path:
    """The one clone all three packages come from, or ProvenanceError naming each origin."""
    described = "; ".join(f"{n} from {p}" for n, p in packages.items())
    installed = [n for n, p in packages.items() if _installed(p)]
    if installed:
        raise ProvenanceError(
            f"{', '.join(installed)} run from an installed copy ({described}); striim-test runs "
            f"from a framework clone: install it with 'pip install -e <clone>'")
    clones = {}
    for n, p in packages.items():
        parent = SIBLING_PARENTS[n]
        root = p.parents[len(parent)]
        if p != root.joinpath(*parent, n):
            raise ProvenanceError(f"{n} from {p} is not at <clone>/{'/'.join(parent)}/{n}")
        clones[n] = root
    if len(set(clones.values())) != 1:
        raise ProvenanceError(f"mixed provenance (packages from more than one clone): {described}")
    return next(iter(clones.values()))


def _decoys(packages: dict, entries) -> list[str]:
    """Another copy of a selected package reachable from ``entries`` (sys.path)."""
    found = []
    for entry in entries:
        base = Path(entry) if entry else Path(os.getcwd())
        for name, pkg in packages.items():
            cand = base / name / "__init__.py"
            try:
                if cand.is_file() and cand.parent.resolve() != Path(pkg):
                    found.append(f"{name} also at {cand.parent.resolve()} (selected {pkg})")
            except OSError:
                continue
    return sorted(set(found))


def _foreign_modules(packages: dict) -> list[str]:
    """Imported engine modules whose file lies outside the selected package directory."""
    bad = []
    for modname, mod in list(sys.modules.items()):
        top = modname.split(".")[0]
        f = getattr(mod, "__file__", None)
        if top not in packages or not f:
            continue
        rp, pkg = Path(f).resolve(), Path(packages[top])
        if rp.parent != pkg and pkg not in rp.parents:
            bad.append(f"{modname} imported from {rp} (selected {pkg})")
    return sorted(bad)


def check_provenance(env=None) -> Origins:
    e = os.environ if env is None else env
    for old, new in RETIRED.items():
        if e.get(old) is not None:
            raise ProvenanceError(f"{old} is retired and never read (use {new})")
    packages = {n: _package_dir(n) for n in PACKAGES}
    clone = _clone_of(packages)
    problems = _decoys(packages, sys.path) + _foreign_modules(packages)
    if problems:
        raise ProvenanceError("mixed provenance: " + "; ".join(problems))

    # Safe now: livetest is the selected clone's own package.
    from livetest import paths
    try:
        home = paths.framework_home(e if env is not None else None)
    except paths.PathConfigError as err:
        raise ProvenanceError(str(err)) from None
    if home.resolve() != (clone / "scripts").resolve():
        value, source = paths._lookup(ENV_HOME, e if env is not None else None)
        raise ProvenanceError(
            f"{ENV_HOME}={value!r} (set in {source}) names {home}, but striim-test runs from the "
            f"clone at {clone}; run that clone's striim-test, or unset {ENV_HOME}")
    source = "package-location" if not paths._lookup(ENV_HOME, e if env is not None else None)[0] \
        else ENV_HOME
    return Origins(MODE, source, clone, packages)
