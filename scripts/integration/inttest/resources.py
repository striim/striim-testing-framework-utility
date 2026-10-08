"""Integration-tier facade over the shared service-profile seam.

The implementation lives in ``livetest.resource_profiles``. The integration suite may run
without ``livetest`` importable (its pytest config puts only ``scripts/integration`` on the
path), so a checkout then loads the sibling ``scripts/live/livetest/resource_profiles.py`` by
path (it is stdlib-only at import time).

Everything is computed at call time from ``inttest.paths``: the services root is
``SLT_INT_SERVICES_DIR`` (else ``scripts/integration/services``) and coordination state is
``SLT_STATE_DIR`` (else ``scripts/integration``). Unset falls back to today's layout; a set key
that names a missing path raises ``paths.PathConfigError`` naming it. The explicit
``services_dir`` argument the token and lifecycle entry points already accept is the
deliberate single-origin override. Lookup is read-only: nothing here materializes a profile.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from inttest import paths

TIER = "integration"
_HERE = Path(__file__).resolve().parent   # .../inttest


class ResourceError(Exception):
    """An integration service profile could not be selected (unknown, duplicated,
    or missing a dependency in its selected origin)."""


def _profiles():
    try:
        from livetest import resource_profiles
        return resource_profiles
    except ImportError:
        pass
    modname = "_inttest_resource_profiles"
    if modname in sys.modules:
        return sys.modules[modname]
    path = _HERE.parents[1] / "live" / "livetest" / "resource_profiles.py"
    if not path.is_file():
        raise ImportError(
            "livetest.resource_profiles is not importable and no source-checkout copy "
            f"exists at {path}")
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


def _integration_dir_for(entry: Path) -> Path | None:
    """The integration services dir a ``servicesRoots`` entry holds: ``<its live services
    dir>/integration`` (``services/integration/<name>``, beside the live ``services/<name>``),
    where the live services dir is the one the live tier takes for that entry. None when there is
    no such folder: the entry then supplies live services only."""
    # The entry's live services dir, by the live tier's own rule, so both tiers read an entry
    # the same way (review H4).
    try:
        from livetest import layout as _layout
        live = _layout._services_dir_for(entry)
    except ImportError:
        live = next((d for d in (entry / "services", entry / "scripts" / "live" / "services")
                     if d.is_dir()), entry)
    return live / "integration" if (live / "integration").is_dir() else None


def consumer_roots(env=None) -> tuple[Path, ...]:
    """The project manifest's ``servicesRoots`` (``GOLD_TARGETS``) as integration services
    dirs, in order. Read through the live engine's manifest loader; without it installed, none."""
    try:
        from livetest import project as _project
    except ImportError:
        return ()
    manifest = ((env if env is not None else os.environ).get("GOLD_TARGETS") or "").strip()
    if not manifest:
        return ()
    proj = _project.load_project(manifest)
    base = paths.services_dir(env).resolve()
    out = []
    for entry in (proj.services_roots if proj is not None else ()):
        d = _integration_dir_for(Path(entry))
        if d is not None and d.resolve() != base and d.resolve() not in out:
            out.append(d.resolve())
    return tuple(out)


def services_roots(env=None, dotenv=None) -> tuple[Path, ...]:
    """Where an integration service is looked up, in order: the manifest's ``servicesRoots``
    (as :func:`consumer_roots`), then ``paths.services_dir()``. As in the live tier, the first
    root that defines a name wins, and the override is reported once."""
    return consumer_roots(env) + (paths.services_dir(env, dotenv),)


_REPORTED: set = set()


def _hits(name: str, roots) -> list[Path]:
    return [r / name for r in roots if (r / name / "service.yaml").is_file()]


def _stub(d: Path) -> bool:
    """A connection-only definition (neither ``compose`` nor ``container``), as in the live
    tier: a consumer service that replaces it is its intended use, so that is not reported."""
    import yaml
    try:
        raw = yaml.safe_load((d / "service.yaml").read_text()) or {}
    except (OSError, yaml.YAMLError):
        return False
    return isinstance(raw, dict) and not raw.get("compose") and not raw.get("container")


def _report_override(name: str, hits: list[Path]) -> None:
    if (len(hits) > 1 and (name, hits[0]) not in _REPORTED
            and not all(_stub(h) for h in hits[1:])):
        _REPORTED.add((name, hits[0]))
        print(f"[services] {name}: using {hits[0]}, which overrides "
              f"{', '.join(str(h) for h in hits[1:])}", file=sys.stderr, flush=True)


def service_dir(name: str, *, services_dir=None, env=None, dotenv=None) -> Path:
    """The origin directory of service ``name``: the first root that defines it. An unknown
    name resolves under the base root anyway, so the caller's own missing-file error names
    the path it looked for."""
    if services_dir is not None:
        return Path(services_dir) / name
    roots = services_roots(env, dotenv)
    hits = _hits(name, roots)
    if hits:
        _report_override(name, hits)
        return hits[0]
    return roots[-1] / name


def select_profile(name: str, *, services_dir=None, env=None, dotenv=None, override=False):
    """The whole integration profile ``name`` (preflight-checked)."""
    rp = _profiles()
    try:
        roots = services_roots(env, dotenv)
        # A consumer root overrides the base by name (first hit), as in the live tier.
        return rp.select_profile(TIER, name, roots=roots, services_dir=services_dir,
                                 override=override or len(roots) > 1)
    except rp.ProfileError as e:
        raise ResourceError(str(e)) from e


def list_profiles(env=None, dotenv=None) -> list[str]:
    rp = _profiles()
    try:
        names = []
        for root in services_roots(env, dotenv):     # first hit wins; no cross-root collision
            names += [n for n in rp.list_profiles(TIER, roots=(root,)) if n not in names]
        return sorted(names)
    except rp.ProfileError as e:
        raise ResourceError(str(e)) from e


def state_root(env=None, dotenv=None) -> Path:
    """Coordination state: ``paths.state_dir()`` (``SLT_STATE_DIR``, else scripts/integration)."""
    return paths.state_dir(env, dotenv)
