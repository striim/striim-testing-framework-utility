"""The environment a test's child pytest runs with: what a Python process needs, plus what the test sets.

A child that inherits the host's environment writes host-dependent output. Its redactor treats every secret-named
value as a known secret (a licence COMPANY_NAME of "Striim" masks the product's name in a reason), GOLD_TARGETS loads
the host's project, and the machine settings file and the framework checkout's .env add values of their own. So the
child gets an allowlist, not the host minus a denylist: anything the host carries beyond it stays out, and the two
settings files are pointed at files that do not exist.

Host-installed pytest plugins stay out as well (PYTEST_DISABLE_PLUGIN_AUTOLOAD=1): a child loads what it names with
``-p``. pytest-cov's plugin therefore does not load in a child; its COV_CORE_*/COVERAGE_PROCESS_START variables are
still passed through for the versions that start subprocess coverage without the plugin.

The test's own values win: run_case's ``env``, a ``monkeypatch.setenv`` of an ``SLT_`` variable, or ``overrides``
here. A variable outside the allowlist that the child needs goes through one of those; a child that needs
autoloading passes PYTEST_DISABLE_PLUGIN_AUTOLOAD="".
"""
from __future__ import annotations

import os
from pathlib import Path

ALLOW = frozenset({
    "PATH", "HOME", "TMPDIR", "TEMP", "TMP", "LANG", "TZ", "USER", "LOGNAME",
    "PYTHONPATH", "PYTHONHASHSEED", "PYTHONDONTWRITEBYTECODE", "PY_COLORS", "PYTEST_DEBUG_TEMPROOT",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    # Windows: a process cannot start without the first three; home and user-site resolution need the rest
    "SYSTEMROOT", "COMSPEC", "PATHEXT", "SYSTEMDRIVE", "WINDIR",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
    "COVERAGE_PROCESS_START",                    # subprocess coverage under pytest --cov
    "STRIIM_TEST_GUARD",                         # set by run_case(guard=True) for striim-test's guard
})
# SLT_ in the parent is the test's own: scripts/live/conftest.py removes the host's before each test. A caller that
# runs before that fixture (a module-scoped one) passes a base without SLT_. XTR_ is not allowed through: harness
# variables are set after apply() or passed as overrides, so a host XTR_ never reaches the child.
ALLOW_PREFIXES = ("LC_", "SLT_", "COV_CORE_")


def child_env(missing: Path, base=None, **overrides) -> dict:
    """The child's environment: `base` (default os.environ) narrowed to the allowlist, the machine settings file and
    the clone .env pointed at `missing` (a path that does not exist), then `overrides`."""
    base = os.environ if base is None else base
    env = {k: v for k, v in base.items() if k in ALLOW or k.startswith(ALLOW_PREFIXES)}
    env["SLT_MACHINE_ENV"] = str(missing)
    env["SLT_FRAMEWORK_DOTENV"] = str(missing)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env.update({k: str(v) for k, v in overrides.items()})
    return env


def apply(monkeypatch, tmp_path: Path, **overrides) -> None:
    """Make os.environ the child's environment for the rest of the test (pytester copies os.environ into its child)."""
    env = child_env(tmp_path / "no-such-settings-file", **overrides)
    for key in [k for k in os.environ if k not in env]:
        monkeypatch.delenv(key)
    for key, value in env.items():
        if os.environ.get(key) != value:
            monkeypatch.setenv(key, value)
