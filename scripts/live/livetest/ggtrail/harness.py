from __future__ import annotations
import json
import os
import shutil
import subprocess
from pathlib import Path

from livetest import paths

# Python wrapper around the standalone Java decode harness
# (tools/ggtrail-harness/TrailDumpHarness.java). GGTrailParser is read-only -- it has no
# serializer -- so the ONLY way to prove a generated trail file is well-formed is to run
# it through the real decoder and diff the decoded records against what we encoded.
# Compiles once against "$STRIIM_HOME/lib/*" (the same pre-built-jar convention
# opartifacts.py:129-146 uses for OP builds), then shells out per call.

# Consumer-provided harness: resolves under the project root (SLT_PROJECT_ROOT, default the repo)
def _harness_dir() -> Path:
    """tools/ggtrail-harness in the project (consumer content), resolved at call time."""
    from livetest import project as _project
    return _project.example_root() / "tools" / "ggtrail-harness"
_MAIN_CLASS = "com.example.ggtrail.TrailDumpHarness"


def missing_reason() -> str | None:
    """Why the harness cannot run from this project, or None: its source is consumer content
    (tools/ggtrail-harness in your test repo), absent from the framework's own checkout."""
    src = _harness_dir() / "TrailDumpHarness.java"
    if src.is_file():
        return None
    return f"ggtrail harness not in this project: no {src} (tools/ggtrail-harness in your test repo)"


def _striim_home() -> Path:
    home = os.environ.get("STRIIM_HOME")
    if not home:
        raise RuntimeError("STRIIM_HOME must point at a Striim install with lib/*.jar "
                           "to build/run the ggtrail validation harness")
    return Path(home)


def ensure_built() -> str:
    """Compile the harness if needed; return the classpath to use for `java`."""
    hdir = _harness_dir()
    src = hdir / "TrailDumpHarness.java"
    cls = hdir / "com" / "example" / "ggtrail" / "TrailDumpHarness.class"
    classpath = f"{_striim_home()}/lib/*:{hdir}"
    if not cls.exists() or cls.stat().st_mtime < src.stat().st_mtime:
        if shutil.which("javac") is None:
            raise RuntimeError("javac not found on PATH -- the ggtrail validation harness needs a JDK")
        subprocess.run(["javac", "-cp", classpath, "-d", str(hdir), str(src)],
                       check=True, capture_output=True, text=True)
    return classpath


def decode_dir(directory: Path, def_file: Path, wildcard: str = "rt*") -> list[dict]:
    classpath = ensure_built()
    proc = subprocess.run(
        ["java", "-cp", classpath, _MAIN_CLASS, str(directory), str(def_file), wildcard],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ggtrail harness failed: {proc.stderr}")
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
