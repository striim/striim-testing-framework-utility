"""Service drivers: the Python a service definition brings for what `docker compose up` cannot do.

A service.yaml may name ``driver: <module>``, a Python module in the service's own directory
(a consumer's service under ``servicesRoots`` usually). The framework imports it by that name,
with the service directory on ``sys.path`` for the import only, so the module and its own
sibling imports load once per process, and the consumer's tests can import the same module
object by the same name. Every hook is optional; the framework calls the ones the module defines:

``unsupported_mode(mode) -> str | None``
    Why the service cannot run in this run mode (``docker`` / ``native``), or None. Checked
    first, before any pre_up hook runs.
``unavailable(env, mode) -> str | None``
    Why the service cannot be brought up here, or None. Checked after the registry's own
    checks (``registry.unavailable``) pass.
``compose_env(env) -> dict``
    Settings to publish to compose (and this process) before the service is brought up.
``admins(base, defn) -> dict``
    The connections a test routes ``ddl:``/``data:``/``seed:`` to, keyed ``<service>-<role>``.
    ``base`` is the resolved connection; the driver may complete it in place.
``provision(admins, client, progress)``
    Setup on top of ``compose up``, given those admins and a Striim client. Run once across
    xdist workers, once per test otherwise; it must be idempotent.
``reader_mode(tql) -> str | None``
    The source reader mode a test's rendered TQL selects, or None when this service has no
    reader to wait for. A test with one waits for ``wait_reader_ready`` before ``post_start``
    seeds, and again after ``drop_recreate_app``.
``reader_mark()``
    A position in the reader's evidence, taken before the app is deployed.
``wait_reader_ready(mode, mark, timeout, progress=None) -> float``
    Block until the reader is reading (evidence past ``mark``); the seconds waited.
``ENV_PATH_KEYS``
    Settings ``striim-test doctor`` checks as existing paths when they are set.
``ENV_KEYS``
    Other settings the driver reads, so ``striim-test doctor`` knows them (not typos).
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path


class DriverError(RuntimeError):
    pass


def load(defn):
    """The driver module ``defn`` names, imported once per process; None when it names none."""
    name = getattr(defn, "driver", None) if defn is not None else None
    if not name:
        return None
    directory = Path(defn.dir).resolve()
    module = sys.modules.get(name)
    if module is None:
        sys.path.insert(0, str(directory))
        try:
            module = importlib.import_module(name)
        finally:
            sys.path.remove(str(directory))
    origin = getattr(module, "__file__", None)
    if not origin:
        raise DriverError(f"service {defn.name}: driver module {name!r} is not a module in {directory} "
                          f"(it resolves to one with no file, such as a built-in); give the driver "
                          f"a distinct module name")
    if directory not in Path(origin).resolve().parents:
        raise DriverError(f"service {defn.name}: driver module {name!r} is already loaded from "
                          f"{origin}, not from {directory}; give the driver a distinct module name")
    return module


def hook(defn, name):
    """``name`` from ``defn``'s driver, or None when there is no driver or it lacks the hook."""
    return getattr(load(defn), name, None)
