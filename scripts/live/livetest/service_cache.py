"""Reuse manifest service tokens until their discovery inputs change."""
from contextvars import ContextVar
from functools import wraps
import os
from pathlib import Path
import sys
from threading import RLock

_dependencies = ContextVar("service_token_dependencies", default=None)


def _identity(path):
    resolved = path.resolve()
    try:
        stat = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return resolved, None
    return resolved, stat.st_mtime_ns, stat.st_size


def watch(path):
    """Record files read and directories searched during service discovery."""
    dependencies = _dependencies.get()
    if dependencies is not None:
        path = Path(path).absolute()
        dependencies[path] = _identity(path)
    return path


def watch_services(root):
    """Directory identities detect new services and new YAML in existing directories."""
    if _dependencies.get() is None:
        return
    root = Path(watch(root))
    if root.is_dir():
        for child in root.iterdir():
            watch(child)


def _context():
    from livetest import layout, paths, registry
    int_paths = sys.modules.get("inttest.paths")
    return (tuple(sorted(os.environ.items())), Path.cwd(),
            layout._cfg_services, layout._cfg_manifest_services,
            layout._cfg_state, layout._cfg_manifest_state,
            str(registry._SERVICES_DIR), paths._HERE, getattr(int_paths, "_HERE", None),
            paths.dotenv_values, layout.services_roots, registry.all_services, registry.load_service)


def cached_tokens(load):
    """One bounded entry; callers receive a fresh set, never the stored value."""
    entry = None
    lock = RLock()

    @wraps(load)
    def cached():
        nonlocal entry
        with lock:
            context = _context()
            if entry is not None:
                previous, dependencies, tokens = entry
                if (context == previous and all(_identity(path) == identity
                                                for path, identity in dependencies.items())):
                    return set(tokens)
            dependencies = {}
            marker = _dependencies.set(dependencies)
            try:
                tokens = frozenset(load())
            finally:
                _dependencies.reset(marker)
            entry = context, dependencies, tokens
            return set(tokens)

    return cached
