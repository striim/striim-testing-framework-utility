"""Reuse manifest service tokens until their discovery inputs change."""
from contextvars import ContextVar
from functools import wraps
import hashlib
import os
from pathlib import Path
import sys
import stat as stat_module
from threading import RLock

_dependencies = ContextVar("service_token_dependencies", default=None)


def _identity(path, *, content=False):
    resolved = path.resolve()
    try:
        stat = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return resolved, None
    identity = resolved, stat.st_mtime_ns, stat.st_size
    # Declarations and Compose inputs are small; hash them even on filesystems
    # whose timestamps cannot distinguish equal-size edits.
    if content and stat_module.S_ISREG(stat.st_mode):
        return (*identity, hashlib.sha256(path.read_bytes()).digest())
    return identity


def watch(path, *, content=False):
    """Record files read and directories searched during service discovery."""
    dependencies = _dependencies.get()
    if dependencies is not None:
        path = Path(path).absolute()
        content = content or path.name == "service.yaml" or dependencies.get(path, (False,))[0]
        dependencies[path] = content, _identity(path, content=content)
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
    # Consumer declarations and project paths may reference arbitrary environment
    # names. A stock-key allowlist would omit those inputs, so retain the whole
    # environment rather than trying to predict which names discovery will use.
    # Function identities deliberately invalidate test-time monkeypatches; equal
    # settings must not reuse tokens discovered through different implementations.
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
                if (context == previous and all(_identity(path, content=content) == identity
                                                for path, (content, identity) in dependencies.items())):
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
