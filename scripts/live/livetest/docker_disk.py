"""Free space in the disk Docker writes images and containers to.

The slt-striim image is about 22 GB (21.9 GB for 5.4.2, measured on a Mac run). On
that run Docker Desktop's VM had 22.8 GB free before the first build and 0 bytes after it: the
build succeeded, and the primary's Derby then could not create /var/striim/wactionrepos/tmp,
so the cluster never became reachable and nothing said "disk full". So the build is gated on
free space, and `striim-test doctor` reports it.

On Linux the Docker root dir is a local path and its free space is read directly. Docker
Desktop keeps it inside a VM, where the host cannot see it; there, a throwaway container from
an image that is ALREADY LOCAL (``--pull never``: no download) runs `df` on its own root, which
is the VM's disk. When neither works the answer is None ("unknown"), never a guess.
"""
from __future__ import annotations

import os
import shutil
import subprocess

# A first build needs the image (~22 GB) plus the staged installers in the build context
# (~5.2 GB, on the HOST disk on Docker Desktop, the same disk on Linux), BuildKit's
# intermediate layers, and room for the running cluster's Derby, logs and Kafka. 35 GB is
# the documented floor; 22.8 GB was measured as not enough.
MIN_FREE_BUILD_GB = 35
# With the image already built, only the running cluster writes: Derby, logs, Kafka.
MIN_FREE_RUN_GB = 5
MIN_FREE_ENV = "SLT_STRIIM_MIN_FREE_GB"

_GB = 1000 ** 3
# Tried in order; the first one present locally is used. Small images first.
_PROBE_PREFERENCE = ("alpine", "busybox", "ubuntu", "debian", "amd64/ubuntu")


def _default_run(argv):
    return subprocess.run(argv, capture_output=True, text=True, timeout=120)


def _out(r) -> str:
    if getattr(r, "returncode", 1) != 0:
        return ""
    out = getattr(r, "stdout", "")
    return out if isinstance(out, str) else ""


def _df_available(text: str) -> int | None:
    """Bytes available from `df -Pk` output (its last line, 4th column), or None."""
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    cols = lines[-1].split()
    if len(cols) < 4 or not cols[3].isdigit():
        return None
    return int(cols[3]) * 1024


def _probe_image(run) -> str | None:
    images = [ln.strip() for ln in _out(run(["docker", "image", "ls", "--format",
                                             "{{.Repository}}:{{.Tag}}"])).splitlines()
              if ln.strip() and "<none>" not in ln]
    for want in _PROBE_PREFERENCE:
        for ref in images:
            if ref.split(":", 1)[0] == want:
                return ref
    return None


def vm_free_bytes(run=None) -> tuple[int | None, str]:
    """(bytes free where Docker stores images, how it was measured). (None, why) if unknown."""
    run = run or _default_run
    try:
        info = _out(run(["docker", "info", "--format",
                         "{{.DockerRootDir}}|{{.OperatingSystem}}"])).strip()
        if not info:
            return None, "'docker info' did not answer"
        root, _, osname = info.partition("|")
        if "Docker Desktop" not in osname and root and os.path.isdir(root):
            return shutil.disk_usage(root).free, f"free space of {root}"
        ref = _probe_image(run)
        if not ref:
            return None, ("no small local image to run df in (docker pull alpine makes one "
                          "available)")
        free = _df_available(_out(run(["docker", "run", "--rm", "--pull", "never",
                                       "--entrypoint", "df", ref, "-Pk", "/"])))
        if free is None:
            return None, f"df in {ref} gave no answer"
        return free, f"df in a {ref} container"
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"docker not usable: {e.__class__.__name__}"


def min_free_gb(building: bool, env=None) -> float:
    """The floor for a first build or for a run on an already-built image. SLT_STRIIM_MIN_FREE_GB
    overrides the build floor (for a Docker whose images are smaller or live elsewhere)."""
    env = os.environ if env is None else env
    if not building:
        return MIN_FREE_RUN_GB
    raw = (env.get(MIN_FREE_ENV) or "").strip()
    try:
        return float(raw) if raw else MIN_FREE_BUILD_GB
    except ValueError:
        return MIN_FREE_BUILD_GB


def shortfall(free: int | None, building: bool, env=None) -> str | None:
    """A message when ``free`` is below the floor, else None (unknown free space is not a
    shortfall: the caller says it could not measure)."""
    if free is None:
        return None
    need = min_free_gb(building, env)
    if free >= need * _GB:
        return None
    what = "building the Striim image" if building else "running the Striim cluster"
    return (f"Docker has {free / _GB:.1f} GB free, and {what} needs at least {need:g} GB "
            f"(the image alone is about 22 GB); free space in Docker (docker system df shows "
            f"what uses it), or set {MIN_FREE_ENV} if you know this Docker needs less")


def describe(free: int | None) -> str:
    return "unknown" if free is None else f"{free / _GB:.1f} GB"
