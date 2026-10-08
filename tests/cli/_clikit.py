"""Helpers for the striim-test CLI tests (ported from the legacy framework repo).

Every test drives a real entry point in a fresh interpreter: ``python -m striim_test`` (or
``$STRIIM_TEST_CMD``, for example an installed ``striim-test`` console script) and the pytest
children the CLI launches. The engines come from this clone (``PYTHONPATH`` on the three
package parents, which is what ``pip install -e .`` also resolves to).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "cli"
CONTRACT_FIXTURES = FIXTURES / "contracts"
PACKAGES = (("livetest", "scripts/live"), ("inttest", "scripts/integration"),
            ("striim_test", "scripts/cli"))
TRIO = [REPO / parent for _, parent in PACKAGES]

# Variables a developer shell may carry that would redirect the code under test.
_SCRUB = {
    "PYTHONPATH", "PYTEST_ADDOPTS", "GOLD_TARGETS", "STRIIM_HOME", "STRIIM_TEST_GUARD",
    "SLT_FRAMEWORK_MODE", "SLT_FRAMEWORK_HOME", "SLT_FRAMEWORK", "SLT_MODE",
    "SLT_FIELD", "SLT_FIELD_HOME", "SLT_STATE_DIR",
    "SLT_RUN_DISABLED", "SLT_PARALLEL", "SLT_KEEP_RESOURCES", "SLT_SERVICES_ROOTS",
    "INT_STATE_DIR", "INT_PERF_DIR", "PT_HOME", "PT_PRODUCT_HOME", "GGTRAIL_TESTDATA",
    "XTR_FAKE_LOG", "PYTEST_XDIST_WORKER", "PYTEST_PLUGINS", "PT_FIELD_HOME", "XTR_AUDIT_PROBE",
    # the path keys (livetest.paths / inttest.paths)
    "SLT_PROJECT_ROOT", "SLT_LIVE_CASES", "SLT_INT_CASES", "SLT_SERVICES_DIR",
    "SLT_INT_SERVICES_DIR", "STRIIM_URL", "STRIIM_USER", "STRIIM_PASS", "STRIIM_PASSWORD",
    "SLT_RUN_EPOCH", "SLT_INFRA_OWNERSHIP",
}

_ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".state")


def clean_env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for k, v in extra.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = str(v)
    return env


def package_dirs() -> dict:
    """Where this clone's packages live."""
    return {pkg: REPO / parent / pkg for pkg, parent in PACKAGES}


def framework_env(**extra) -> dict:
    """This clone's packages on PYTHONPATH and nothing else from the developer's shell.
    SLT_FRAMEWORK_HOME is left unset: it defaults to the clone."""
    base = {"PYTHONPATH": os.pathsep.join(map(str, TRIO))}
    base.update(extra)
    return clean_env(**base)


def cli_cmd() -> list[str]:
    raw = os.environ.get("STRIIM_TEST_CMD")
    return shlex.split(raw) if raw else [sys.executable, "-m", "striim_test"]


@dataclass
class Result:
    rc: int
    stdout: str
    stderr: str
    run_dir: Path | None

    def ids(self) -> list[str]:
        return [ln for ln in self.stdout.splitlines() if ln and not ln.startswith("#")]

    def part(self, label: str, name: str = "selection.json"):
        return json.loads((self.run_dir / label / name).read_text())


def run_cli(argv, *, cwd, env=None, cmd=None, timeout=900) -> Result:
    p = subprocess.run([*(cmd or cli_cmd()), *map(str, argv)], cwd=cwd,
                       env=framework_env() if env is None else env,
                       capture_output=True, text=True, timeout=timeout)
    m = re.search(r"^striim-test: run-dir: (.+)$", p.stderr, re.M)
    return Result(p.returncode, p.stdout, p.stderr, Path(m.group(1)) if m else None)


def copy_fixture(name: str, dest: Path) -> Path:
    shutil.copytree(FIXTURES / name, dest, ignore=_ignore)
    return dest


def site_layout(dest: Path, packages=PACKAGES) -> Path:
    """A site-packages-shaped copy of the packages (an installed tree without pip)."""
    site = dest / "lib" / "python3" / "site-packages"
    site.mkdir(parents=True)
    for pkg, parent in packages:
        shutil.copytree(REPO / parent / pkg, site / pkg, ignore=_ignore)
    return site


def clone_copy(dest: Path) -> Path:
    """The three packages of this clone copied to another location: a second clone."""
    for pkg, parent in PACKAGES:
        shutil.copytree(REPO / parent / pkg, dest / parent / pkg, ignore=_ignore)
    return dest


def runnable_clone(dest: Path) -> Path:
    """A second clone that can run the live tier: the packages, the striim_api client and the
    regression/hello cases. Its default state dir (``scripts/live``) is never this repository."""
    clone_copy(dest)
    shutil.copytree(REPO / "scripts/live/regression/hello", dest / "scripts/live/regression/hello",
                    ignore=_ignore)
    shutil.copy(REPO / "scripts/live/pyproject.toml", dest / "scripts/live/pyproject.toml")
    shutil.copytree(REPO / "tools/python", dest / "tools/python", ignore=_ignore)
    return dest


def clone_env(clone: Path, **extra) -> dict:
    """``clone``'s packages on PYTHONPATH and nothing else from the developer's shell."""
    return clean_env(PYTHONPATH=os.pathsep.join(str(clone / parent) for _, parent in PACKAGES),
                     **extra)


AUDIT_HOOK = '''\
import json
import os
import sys

_XTR_LOG = os.environ.get("XTR_AUDIT_LOG")
if _XTR_LOG:
    def _xtr_audit(event, args):
        if event not in ("subprocess.Popen", "socket.connect", "os.system"):
            return
        try:
            if event == "subprocess.Popen":
                argv = args[1] if isinstance(args[1], (list, tuple)) else [args[1]]
                detail = {"executable": str(args[0]), "argv": [str(a) for a in argv]}
            else:
                detail = {"args": [str(a) for a in args[:2]]}
            with open(_XTR_LOG, "a") as f:
                f.write(json.dumps({"event": event, "pid": os.getpid(), **detail}) + "\\n")
        except Exception:
            pass
    sys.addaudithook(_xtr_audit)
    if os.environ.get("XTR_AUDIT_PROBE"):
        # Positive control (final review 6): every instrumented process records itself and makes
        # one refused loopback connection, so a descendant's hook and socket events are visible.
        try:
            with open(_XTR_LOG, "a") as f:
                f.write(json.dumps({"event": "hook", "pid": os.getpid(),
                                    "argv": list(getattr(sys, "orig_argv", sys.argv))}) + "\\n")
            import socket
            _s = socket.socket()
            _s.settimeout(0.5)
            try:
                _s.connect(("127.0.0.1", 9))
            except OSError:
                pass
            finally:
                _s.close()
        except Exception:
            pass
'''


@dataclass
class Trap:
    """Fake docker/docker-compose/mvn/javac/java first on PATH (each logs and exits 97), plus an
    audit hook in every descendant Python process (subprocess spawns and socket connects)."""
    bin: Path
    log: Path
    audit_log: Path
    site: Path

    def env(self, base: dict) -> dict:
        env = dict(base)
        env["PATH"] = f"{self.bin}{os.pathsep}{env.get('PATH', '')}"
        env["PYTHONPATH"] = os.pathsep.join([str(self.site)] + ([env["PYTHONPATH"]]
                                                               if env.get("PYTHONPATH") else []))
        env["TRAP_LOG"] = str(self.log)
        env["XTR_AUDIT_LOG"] = str(self.audit_log)
        return env

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def audit(self) -> list[dict]:
        if not self.audit_log.exists():
            return []
        return [json.loads(ln) for ln in self.audit_log.read_text().splitlines() if ln]


def make_trap(tmp: Path) -> Trap:
    bin_dir = tmp / "trapbin"
    bin_dir.mkdir()
    for tool in ("docker", "docker-compose", "mvn", "javac", "java"):
        p = bin_dir / tool
        p.write_text('#!/bin/sh\nprintf "%s %s\\n" "$(basename "$0")" "$*" >> "$TRAP_LOG"\nexit 97\n')
        p.chmod(0o755)
    site = tmp / "audit-site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(AUDIT_HOOK)
    return Trap(bin_dir, tmp / "trap.log", tmp / "audit.jsonl", site)
