from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import importlib.util
import math
import sys
import yaml

from livetest import paths
from livetest import stack

_SERVICES_DIR = paths.services_dir()

from livetest.service_env import RegistryError


def _roots(services_dir: Path | None = None) -> list[Path]:
    """Where a service name is looked up, in order. ``services_dir`` is one explicit origin.
    Otherwise: the consumer roots (``layout.services_roots()`` -- ``set_roots`` and the project
    manifest's ``servicesRoots``, in their order), then the built-in services dir, last."""
    if services_dir:
        return [Path(services_dir)]
    from livetest import layout
    base = Path(_SERVICES_DIR).resolve()
    consumer = [r for r in layout.services_roots()[:-1] if Path(r).resolve() != base]
    return consumer + [_SERVICES_DIR]


_REPORTED: set = set()


def _hits(name: str, services_dir: Path | None = None) -> list[Path]:
    return [r / name for r in _roots(services_dir) if (r / name / "service.yaml").is_file()]


def _stub(d: Path) -> bool:
    """A connection-only definition: neither ``compose`` nor ``container``. A consumer service
    of the same name that ships a container is what it waits for, so replacing it is no news."""
    try:
        raw = yaml.safe_load((d / "service.yaml").read_text()) or {}
    except (OSError, yaml.YAMLError):
        return False
    return isinstance(raw, dict) and not raw.get("compose") and not raw.get("container")


def _reportable(hits: list[Path]) -> bool:
    """An override worth a line: something real is shadowed, not only connection-only stubs."""
    return len(hits) > 1 and not all(_stub(h) for h in hits[1:])


def _override_line(name: str, hits: list[Path]) -> str:
    return (f"[services] {name}: using {hits[0]}, which overrides "
            f"{', '.join(str(h) for h in hits[1:])}")


def overrides() -> list[str]:
    """One line per service name a consumer root overrides, now (a connection-only stub
    replaced is not reported). The live plugin puts these in
    the run's report header, which pytest never captures (a test's stderr is dropped when it
    passes); they then count as reported, so _find does not print them again."""
    lines = []
    for name in all_services():
        hits = _hits(name)
        if _reportable(hits):
            _REPORTED.add((name, hits[0]))
            lines.append(_override_line(name, hits))
    return lines


def _find(name: str, services_dir: Path | None = None) -> Path | None:
    """The service dir ``name`` resolves to: the FIRST root that defines it. A consumer root
    therefore overrides a built-in (or a later consumer root) of the same name. That is
    reported once per process: in the live run's report header (``overrides``), or else here
    on stderr, naming both paths."""
    hits = _hits(name, services_dir)
    if not hits:
        return None
    if (name, hits[0]) not in _REPORTED and _reportable(hits):
        _REPORTED.add((name, hits[0]))
        print(_override_line(name, hits), file=sys.stderr, flush=True)
    return hits[0]


def all_services(services_dir: Path | None = None) -> list[str]:
    """Every registered service name across the roots, sorted. Membership is "has a
    service.yaml", which is what makes `striim/` (compose files only — the cluster is
    provisioned by striim_provision, not by the service registry) fall out on its own."""
    names = set()
    for base in _roots(services_dir):
        if base.is_dir():
            names.update(p.name for p in base.iterdir() if (p / "service.yaml").is_file())
    return sorted(names)

@dataclass
class ServiceDef:
    name: str
    dir: Path
    isolation: str
    compose: str | None = None
    container: str | None = None
    live_override_env: str | None = None
    docker_defaults: dict = field(default_factory=dict)
    # Env vars that override a docker_defaults entry when the stack is brought up by us.
    # compose.yaml publishes host ports as ${SLT_x_HOST_PORT:-default}; without this, a user who
    # remaps a busy port gets a service on the new port and an admin client still dialling the
    # old one, and every `requires:` test errors on a connection that cannot succeed.
    docker_env: dict = field(default_factory=dict)
    live_env: dict = field(default_factory=dict)
    required_env: list = field(default_factory=list)
    # What a live_env setting falls back to when the existing instance is used (live_override_env
    # set) and the setting is unset, ahead of docker_defaults: for a definition whose container
    # listens on another port or scheme than a real instance does.
    live_defaults: dict = field(default_factory=dict)
    provides: dict = field(default_factory=dict)
    post_up: str | None = None   # path (in-container) to a script run via docker exec after up
    # Bare opt-in flag: a test that `requires` this service is skipped unless this env var (or the
    # SLT_EMULATORS umbrella) is set. For services whose provisioning is heavy or has cluster-global
    # side effects (Spanner's emulator redirect is JVM-global); None => runs by default (pg/oracle).
    opt_in_env: str | None = None
    # Files the Docker service needs that are not in git (VM disks, licensed installers), relative
    # to the service dir. When one is missing and no existing instance is set (live_override_env),
    # a test that requires the service skips with the files named, instead of failing inside
    # `compose up`; a derived bring-up leaves it out. Obtaining them is the service's own business
    # (its README, or a script beside it): the framework never downloads them.
    required_files: list = field(default_factory=list)
    required_files_env: dict = field(default_factory=dict)
    # A host-side script, relative to the service dir, run before the service is brought up
    # (livetest.prestart): fetching the `required_files`, say. It runs only for a selected test
    # that requires the service or a start that names it, never for a derived `start all`, and
    # not at all with the existing instance set or with SLT_PRE_UP=0.
    pre_up: str | None = None
    # Seconds the pre_up hook may run before its process group is killed (livetest.prestart,
    # default DEFAULT_TIMEOUT there).
    pre_up_timeout: float | None = None
    pre_up_env: dict = field(default_factory=dict)
    pre_up_check: str = "missing_files"
    unavailable_policy: str = "skip"
    # A shell command run in the container (`docker exec <c> sh -c`) to stop it gracefully, for
    # an image whose PID 1 is a wrapper that ignores SIGTERM (service_outage signal TERM).
    graceful_stop: str | None = None
    # Seconds service_outage waits for the container to exit after `graceful_stop`.
    graceful_stop_timeout: float | None = None
    # A shell command run in the container (`docker exec <c> sh -c`) that restarts the server
    # in place and exits 0 once it is back (service_outage signal RESTART).
    restart_in_place: str | None = None
    # Seconds service_outage waits for `restart_in_place` to return.
    restart_in_place_timeout: float | None = None
    # A Python client module the framework's own connection to the service needs and that is
    # not installed by default (an optional extra, a separately licensed driver). Without it a
    # test that requires the service skips, with `python_module_hint` saying how to install it.
    python_module: str | None = None
    python_module_hint: str | None = None
    # A Python module in the service dir with the hooks `docker compose up` cannot express
    # (livetest.drivers lists them). None => the service needs no code of its own.
    driver: str | None = None

def _exec_hook(raw, key: str, path: Path) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise RegistryError(f"{path}: '{key}' must be a shell command run in the container")
    return raw


def _exec_hook_timeout(raw, hook, key: str, path: Path) -> float | None:
    if raw is None:
        return None
    if hook is None:
        raise RegistryError(f"{path}: '{key}_timeout' needs '{key}'")
    if (isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw)
            or raw <= 0):
        raise RegistryError(f"{path}: '{key}_timeout' must be a positive number of seconds")
    return float(raw)


def _pre_up(raw, path: Path) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise RegistryError(f"{path}: 'pre_up' must be a script path relative to the service dir")
    if Path(raw).is_absolute() or ".." in Path(raw).parts:
        raise RegistryError(f"{path}: 'pre_up' {raw!r} must stay inside the service dir")
    return raw


def _pre_up_timeout(raw, path: Path) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        raise RegistryError(f"{path}: 'pre_up_timeout' must be a positive number of seconds")
    return float(raw)


def _choice(raw, key, choices, path):
    if raw is None:
        return choices[0]
    if raw not in choices:
        raise RegistryError(f"{path}: '{key}' must be one of {', '.join(choices)}")
    return raw


def _live_defaults(raw, path) -> dict:
    value = raw.get("live_defaults")
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RegistryError(f"{path}: 'live_defaults' must map live_env keys to values")
    live_env = raw.get("live_env") or {}
    for key in value:
        if key not in live_env:
            raise RegistryError(f"{path}: 'live_defaults' key {key} is not a live_env key")
    return dict(value)


def hook_policy(raw, path):
    """Shared schema validation for live and integration service definitions."""
    from livetest.service_env import pre_up_env, required_env
    return {
        "required_env": required_env(raw.get("required_env"), path),
        "live_defaults": _live_defaults(raw, path),
        "pre_up_env": pre_up_env(raw.get("pre_up_env"), path),
        "pre_up_check": _choice(raw.get("pre_up_check"), "pre_up_check",
                                ("missing_files", "always"), path),
        "unavailable_policy": _choice(raw.get("unavailable_policy"), "unavailable_policy",
                                     ("skip", "fail"), path),
    }


def _required_files(raw, path: Path) -> list:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(f, str) and f.strip() for f in raw):
        raise RegistryError(f"{path}: 'required_files' must be a list of paths relative to the service dir")
    for f in raw:
        if Path(f).is_absolute() or ".." in Path(f).parts:
            raise RegistryError(f"{path}: 'required_files' entry {f!r} must stay inside the service dir")
    return list(raw)


def _optional_str(raw, key: str, path: Path) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise RegistryError(f"{path}: '{key}' must be a non-empty string")
    return raw.strip()


def _driver(raw, path: Path) -> str | None:
    name = _optional_str(raw, "driver", path)
    if name is not None and not all(part.isidentifier() for part in name.split(".")):
        raise RegistryError(f"{path}: 'driver' must be a Python module name in the service dir")
    return name


def missing_python_module(module: str | None, hint: str | None = None) -> str | None:
    """Why ``module`` cannot be imported here, or None. Found by name only; never imported."""
    if not module or importlib.util.find_spec(module) is not None:
        return None
    return f"Python module {module} is not installed" + (f": {hint}" if hint else "")


def unavailable(defn: ServiceDef, env) -> str | None:
    """Why ``defn`` cannot be brought up here, or None. With its existing-instance setting
    (``live_override_env``) set, nothing is brought up, so None. Otherwise: a connection-only
    definition (no ``compose`` and no ``container``, as the shipped teradata) has nothing to start, and a
    ``required_files`` entry may be missing. Checked before compose, so its tests skip with the
    reason instead of failing inside it. A missing ``python_module`` comes first: even an
    existing instance cannot be reached without it."""
    why = missing_python_module(getattr(defn, "python_module", None), getattr(defn, "python_module_hint", None))
    if why:
        return why
    if defn.live_override_env and (env.get(defn.live_override_env) or "").strip():
        return None
    if not defn.compose and not defn.container:
        how = (f"set {defn.live_override_env} and its settings to your own instance, or add"
               if defn.live_override_env else "add")
        return (f"no container ships for {defn.name}: {how} a {defn.name} service that has a "
                f"compose file through servicesRoots")
    missing = [str(p) for p in required_paths(defn, env) if not p.is_file()]
    if not missing:
        return None
    how = "see its README.md" if (Path(defn.dir) / "README.md").is_file() else "supply them"
    alt = f", or set {defn.live_override_env} to use an existing instance" if defn.live_override_env else ""
    return f"required files missing from {defn.dir}: {', '.join(missing)} ({how}{alt})"


def load_service(name: str, services_dir: Path | None = None, env=None) -> ServiceDef:
    found = _find(name, services_dir)
    if found is None:
        searched = ", ".join(str(r) for r in _roots(services_dir))
        raise RegistryError(f"unknown service {name!r} (no {name}/service.yaml under {searched})")
    path = found / "service.yaml"
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise RegistryError(f"{path}: invalid YAML: {e}") from e
    if raw.get("name") != name:
        raise RegistryError(f"{path}: 'name' must equal {name!r}")
    if not raw.get("isolation"):
        raise RegistryError(f"{path}: 'isolation' is required")
    # see livetest.stack) HERE, in the one place every consumer reads it from — docker
    # exec/cp targets, the provision-registry keys, compose teardown bookkeeping and the
    # console's log allowlist all inherit the resolved name. service.yaml keeps the base name;
    # `env` (default os.environ) is a seam so the console can resolve with ITS configured
    # prefix (console.env) rather than the process environment.
    container = raw.get("container")
    if container:
        container = stack.prefixed(container, env)
    return ServiceDef(
        name=name,
        dir=path.parent,
        isolation=raw["isolation"],
        compose=raw.get("compose"),
        container=container,
        live_override_env=raw.get("live_override_env"),
        docker_defaults=dict(raw.get("docker_defaults", {})),
        docker_env=dict(raw.get("docker_env", {})),
        live_env=dict(raw.get("live_env", {})),
        provides=dict(raw.get("provides", {})),
        post_up=raw.get("post_up"),
        opt_in_env=raw.get("opt_in_env"),
        required_files=_required_files(raw.get("required_files"), path),
        required_files_env=_required_files_env(raw.get('required_files_env'), path),
        **hook_policy(raw, path),
        pre_up=_pre_up(raw.get("pre_up"), path),
        pre_up_timeout=_pre_up_timeout(raw.get("pre_up_timeout"), path),
        graceful_stop=_exec_hook(raw.get("graceful_stop"), "graceful_stop", path),
        graceful_stop_timeout=_exec_hook_timeout(raw.get("graceful_stop_timeout"),
                                                 raw.get("graceful_stop"), "graceful_stop", path),
        restart_in_place=_exec_hook(raw.get("restart_in_place"), "restart_in_place", path),
        restart_in_place_timeout=_exec_hook_timeout(raw.get("restart_in_place_timeout"),
                                                    raw.get("restart_in_place"),
                                                    "restart_in_place", path),
        python_module=_optional_str(raw.get("python_module"), "python_module", path),
        python_module_hint=_optional_str(raw.get("python_module_hint"), "python_module_hint", path),
        driver=_driver(raw.get("driver"), path),
    )


def _required_files_env(raw, path):
    """Optional env→relative-root mappings for read-only prerequisite paths."""
    import re
    if raw is None:
        return {}
    if not isinstance(raw, dict) or any(not isinstance(k, str) or not re.fullmatch('[A-Z][A-Z0-9_]*', k) for k in raw):
        raise RegistryError(f'{path}: required_files_env must map environment names to relative paths')
    _required_files(list(raw.values()), path)
    return raw


def required_path(directory, relative, env, mapping=None):
    path = Path(relative)
    for key, root in (mapping or {}).items():
        value = (env.get(key) or '').strip()
        if value and (path == Path(root) or Path(root) in path.parents):
            selected = Path(value).expanduser()
            if not selected.is_absolute():
                selected = Path(directory) / selected
            return selected / path.relative_to(root)
    return Path(directory) / path


def required_paths(defn, env):
    return [required_path(defn.dir, f, env, getattr(defn, 'required_files_env', {}))
            for f in getattr(defn, 'required_files', [])]
