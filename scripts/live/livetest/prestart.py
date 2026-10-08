"""Host-side pre-start hooks: a service's `pre_up` script (service.yaml), run before its bring-up.

A service whose container needs files git does not carry (VM disks, licensed installers) can
fetch them itself: `pre_up: download-dependencies.sh` runs that script, from the service dir, on
the host. It runs:
- only for a selected test that requires the service (the plugin's runtest, a test-id pre-flight,
  an integration case) or a start that names it; never for a derived `start all` / `start live`;
- never with the service's existing instance set (its `live_override_env`);
- never with SLT_PRE_UP=0 (the shell or .env): the framework's hermetic suites set it;
- not when every file the service declares in `required_files` is already present: the hook is
  then not needed, and no network is touched;
- one at a time per service name on this machine (a lock in the shared lock dir), so parallel
  workers never run it into the same directory at once. The files are checked again inside the
  lock, so the next one in finds the work done;
- for at most `pre_up_timeout` seconds (default DEFAULT_TIMEOUT); then its process group is
  killed and it counts as failed.
A failed hook is an error (PreUpError), never a quiet skip: the caller fails the test or the start.

This is the one runner. The integration tier (inttest.services.run_pre_up) calls run_hook too.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

OFF_SWITCH = "SLT_PRE_UP"
DEFAULT_TIMEOUT = 7200          # seconds: a 9 GB download on a slow link, with room to spare
_TAIL = 800                     # characters of the hook's output quoted in a failure


class PreUpError(Exception):
    """A pre_up hook failed (non-zero exit, timeout, missing script). The message says which."""


def enabled(env=None) -> bool:
    """False when SLT_PRE_UP=0, in the environment or the project .env."""
    from livetest import paths
    return (paths.setting(OFF_SWITCH, env) or "").strip() != "0"


def lock_path(name: str) -> Path:
    """The machine-wide lock for `name`'s hook, shared by both tiers."""
    from livetest import lockdir, stack
    return lockdir.ensure_dir(stack._LOCK_DIR) / f".slt-pre-up-{name}.lock"


def missing_files(files) -> list[str]:
    """The entries of `files` (absolute paths) that are not regular files."""
    return [str(f) for f in files if not Path(f).is_file()]


def _check_script(service_dir: Path, script: str) -> Path:
    if Path(script).is_absolute() or ".." in Path(script).parts:
        raise PreUpError(f"pre_up {script!r} must stay inside {service_dir}")
    path = Path(service_dir) / script
    if not path.is_file():
        raise PreUpError(f"pre_up {script!r} not found in {service_dir}")
    return path


def _execute(path: Path, service_dir: Path, env: dict, timeout: float) -> tuple[int | None, str]:
    """Run `path` in its own process group; (exit code or None on timeout, output tail)."""
    argv = [str(path)] if os.access(path, os.X_OK) else ["/bin/sh", str(path)]
    proc = subprocess.Popen(argv, cwd=str(service_dir), env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, (out or "")[-_TAIL:]
    except subprocess.TimeoutExpired:
        for sig in (signal.SIGTERM, signal.SIGKILL):   # the hook's own group: curl and all
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                break
            try:
                out, _ = proc.communicate(timeout=10)
                break
            except subprocess.TimeoutExpired:
                out = ""
        return None, (out or "")[-_TAIL:]


def run_hook(name: str, service_dir, script: str | None, *, required=(), timeout=None,
             extra_env=None, env=None, check="missing_files", unavailable_policy="skip", log=print) -> bool:
    """Run `script` for service `name` when it is needed and allowed. True when it ran and
    exited 0; False when it did not need to run (no script, switched off, every required file
    present). Raises PreUpError when it ran and failed, timed out, or cannot be found.
    `required` are absolute paths; the caller has already ruled out an existing instance."""
    if not script:
        return False
    if not enabled(env):
        if unavailable_policy == "fail":
            raise PreUpError(f"[{name}] pre_up disabled: {OFF_SWITCH}=0; enable it to provision this service")
        log(f"[{name}] pre_up not run: {OFF_SWITCH}=0")
        return False
    if check != "always" and required and not missing_files(required):
        return False
    from filelock import FileLock
    from livetest import lockdir
    service_dir = Path(service_dir)
    path = _check_script(service_dir, script)
    run_env = {**os.environ, **(env or {}), **(extra_env or {}),
               "SLT_SERVICE_DIR": str(service_dir), "SLT_FRAMEWORK_PYTHON": sys.executable}
    limit = float(timeout) if timeout else DEFAULT_TIMEOUT
    with FileLock(str(lock_path(name)), mode=lockdir.file_mode()):
        if check != "always" and required and not missing_files(required):      # another worker fetched them
            return False
        log(f"[{name}] pre_up: {path}")
        rc, tail = _execute(path, service_dir, run_env, limit)
    if rc is None:
        raise PreUpError(f"[{name}] pre_up {script} timed out after {limit:.0f}s "
                         f"(pre_up_timeout); last output: {tail.strip()!r}")
    if rc != 0:
        raise PreUpError(f"[{name}] pre_up {script} exited {rc}; last output: {tail.strip()!r}")
    missing = missing_files(required)
    if missing:
        raise PreUpError(f"[{name}] pre_up {script} exited 0 but left required files missing: "
                         f"{', '.join(missing)}")
    return True


def maybe_run(defn, env, log=print) -> bool:
    """The live tier: run `defn`'s pre_up when declared, allowed and needed. False when nothing
    ran; raises PreUpError when it failed."""
    from livetest import paths
    env = paths.effective_env(env)
    script = getattr(defn, "pre_up", None)            # test harnesses build definitions by hand
    if not script:
        return False
    if defn.live_override_env and (env.get(defn.live_override_env) or "").strip():
        return False
    from livetest.registry import required_paths
    required = required_paths(defn, env)
    return run_hook(defn.name, defn.dir, script, required=required,
                    timeout=getattr(defn, "pre_up_timeout", None), env=env,
                    check=getattr(defn, "pre_up_check", "missing_files"),
                    unavailable_policy=getattr(defn, "unavailable_policy", "skip"), log=log)


def eligibility(defn, mode, required_topology=None, topology=None):
    """Strict service eligibility, without resource probes or hooks."""
    from livetest import drivers
    if getattr(defn, 'unavailable_policy', 'skip') != 'fail':
        return
    unsupported = drivers.hook(defn, 'unsupported_mode')
    why = unsupported(mode) if unsupported else None
    if not why and required_topology and topology is not None:
        from livetest.topology import topology_satisfies
        ok, reason = topology_satisfies(required_topology, topology)
        why = reason if not ok else None
    if why:
        raise PreUpError(f'service {defn.name} unavailable: {why}')


def prepare_selected(defn, env, mode="docker", *, allow_hook=True, log=print):
    """Prepare resources and publish the selected volume to the actual Compose environment."""
    why = prepare(defn, env, mode, allow_hook=allow_hook, log=log)
    if not why:
        from livetest import drivers, paths
        compose_env = drivers.hook(defn, 'compose_env')
        for key, value in (compose_env(paths.effective_env(env)) if compose_env else {}).items():
            env[key] = value
            os.environ[key] = value
    return why


def prepare(defn, env, mode="docker", *, allow_hook=True, log=print):
    """Selected-service ordering shared by pytest and preflight; return a skip reason.

    Strict services raise on unavailable prerequisites. A derived start checks resources
    without running hooks and leaves unavailable services out, as before.
    """
    from livetest import registry, drivers, paths
    env = paths.effective_env(env)
    strict = getattr(defn, "unavailable_policy", "skip") == "fail" and allow_hook

    def reject(why):
        if why and strict:
            raise PreUpError(f"service {defn.name} unavailable: {why}")
        return why

    unsupported = drivers.hook(defn, "unsupported_mode")
    if unsupported:
        why = unsupported(mode)
        if why:
            return reject(why)
    if allow_hook:
        maybe_run(defn, env, log=log)
    why = registry.unavailable(defn, env)
    driver_unavailable = drivers.hook(defn, "unavailable")
    if not why and driver_unavailable:
        why = driver_unavailable(env, mode)
    return reject(why)
