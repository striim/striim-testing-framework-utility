"""Services overlay and state dir -- a thin adapter over ``livetest.paths``.

Two concepts, two accessors, never collapsed:

* ``services_roots()`` -- ORDERED overlay of service-definition roots, always ending with
  ``paths.services_dir()`` (the engine's own services, or ``SLT_SERVICES_DIR``).
* ``state_dir()``      -- SINGLE-VALUED writable state dir. Never overlaid.

Every accessor computes its answer when called: ``set_roots()`` may be called AFTER
``livetest.plugin`` is imported and still takes effect.

Precedence, highest first:

1. ``set_roots(services=[...], state=...)`` -- the programmatic seam a consumer
   ``conftest.py`` calls.
2. ``set_manifest_roots(...)`` -- the C1 project manifest (``project.apply_project``).
3. ``livetest.paths``: ``SLT_SERVICES_DIR`` / ``SLT_STATE_DIR`` from the process environment,
   then ``<project root>/.env``, then today's default. Unset falls back; set
   but missing raises ``paths.PathConfigError`` naming the key.

The overlay is additive: the base services dir is always searched last, and configuring it
as a consumer root is a ``LayoutError``.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from livetest import paths


class LayoutError(Exception):
    pass


def builtin_services(env=None, dotenv=None) -> Path:
    """The base services dir, always last in the overlay: ``paths.services_dir()``."""
    return paths.services_dir(env, dotenv)


# ---------------------------------------------------------------------------
# Configuration: the explicit seam and the manifest slot.
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_cfg_services: tuple[Path, ...] = ()
_cfg_state: Path | None = None
_cfg_manifest_services: tuple[Path, ...] = ()   # C1 manifest servicesRoots (below explicit)
_cfg_manifest_state: Path | None = None         # C1 manifest stateDir (below explicit)


def _reset() -> None:
    """Test seam: drop all programmatic and manifest configuration."""
    global _cfg_services, _cfg_state, _cfg_manifest_services, _cfg_manifest_state
    with _lock:
        _cfg_services = ()
        _cfg_state = None
        _cfg_manifest_services = ()
        _cfg_manifest_state = None


def _as_path_list(value, what: str) -> tuple[Path, ...]:
    if isinstance(value, (str, os.PathLike)):
        value = [value]
    try:
        return tuple(Path(p) for p in value)
    except TypeError:
        raise LayoutError(f"{what} roots must be a path or an iterable of paths, got {value!r}")


def _refuse_state_collection(state) -> None:
    if state is not None and isinstance(state, (list, tuple, set, frozenset)):
        raise LayoutError(
            f"state dir is SINGLE-VALUED and is never overlaid; got a collection: {state!r}")


def set_roots(*, services=None, state=None) -> None:
    """Programmatic seam a consumer conftest.py calls. Idempotent; may be called AFTER
    livetest.plugin is imported and still take effect. ``None`` leaves that axis
    unchanged; pass an empty list to clear services."""
    global _cfg_services, _cfg_state
    _refuse_state_collection(state)
    with _lock:
        if services is not None:
            _cfg_services = _as_path_list(services, "services")
        if state is not None:
            _cfg_state = Path(state)


def set_manifest_roots(*, services=(), state=None) -> None:
    """Manifest-sourced roots (C1 ``apply_project``).

    A SEPARATE slot from ``set_roots(...)``, resolved below it and above livetest.paths.
    Activation REPLACES both manifest axes on every call, so a manifest that omits
    servicesRoots/stateDir clears them and a previous consumer's roots never persist.
    """
    global _cfg_manifest_services, _cfg_manifest_state
    _refuse_state_collection(state)
    with _lock:
        _cfg_manifest_services = _as_path_list(services, "services")
        _cfg_manifest_state = None if state is None else Path(state)


# ---------------------------------------------------------------------------
# Resolution.
# ---------------------------------------------------------------------------

def _canon(paths_, *, base: Path) -> list[Path]:
    """Canonicalize (resolve()) and de-duplicate, order-preserving: $X, $X/ and a symlink
    to $X collapse to one root. Refuse a configured root that IS the base services dir."""
    out: list[Path] = []
    for p in paths_:
        rp = Path(p).resolve()
        if rp == base:
            raise LayoutError(
                f"configured services root {str(p)!r} is the base services dir {str(base)!r}: "
                f"the base dir is always searched last and cannot be configured or "
                f"repositioned (the overlay is additive)")
        if rp not in out:
            out.append(rp)
    return out


def _services_dir_for(root: Path) -> Path:
    """A configured services entry is a ROOT under which the services tree lives:
    ``<root>/services`` or ``<root>/scripts/live/services`` (whichever exists), else the
    entry itself is taken to BE a services dir."""
    for cand in (root / "services", root / "scripts" / "live" / "services"):
        if cand.is_dir():
            return cand
    return root


def services_roots(env=None, dotenv=None) -> tuple[Path, ...]:
    """ORDERED overlay, computed now: explicit, then manifest, then the base services dir."""
    base = builtin_services(env, dotenv).resolve()
    entries = [_services_dir_for(Path(p).resolve())
               for p in list(_cfg_services) + list(_cfg_manifest_services)]
    return tuple(_canon(entries, base=base) + [base])


def planned_state_dir(env=None, dotenv=None) -> Path | None:
    """The CONFIGURED state dir -- set_roots(state=...) > manifest stateDir > SLT_STATE_DIR
    (environment, then .env) -- resolved but never created or checked; None when nothing is
    configured. The default is not a plan. Lets a caller refuse an unsuitable location
    before state_dir() creates it."""
    if _cfg_state is not None:
        return Path(_cfg_state).resolve()
    if _cfg_manifest_state is not None:
        return Path(_cfg_manifest_state).resolve()
    value, source = paths._lookup("SLT_STATE_DIR", env, dotenv)
    if not value:
        return None
    p = Path(value).expanduser()
    if not p.is_absolute():
        p = (Path.cwd() if source == "environment" else Path(source).parent) / p
    return p.resolve()


def state_dir(env=None, dotenv=None) -> Path:
    """SINGLE-VALUED write target. A set_roots() or manifest state dir is created on demand;
    otherwise ``paths.state_dir()`` (SLT_STATE_DIR, else the live engine dir)."""
    configured = _cfg_state if _cfg_state is not None else _cfg_manifest_state
    if configured is not None:
        d = Path(configured).resolve()
        d.mkdir(parents=True, exist_ok=True)
        return d
    return paths.state_dir(env, dotenv)


def in_install_tree(p) -> bool:
    """True when ``p`` resolves inside a site-packages or dist-packages tree."""
    parts = Path(p).resolve().parts
    return "site-packages" in parts or "dist-packages" in parts
