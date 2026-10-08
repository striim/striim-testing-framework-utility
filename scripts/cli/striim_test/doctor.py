"""``striim-test doctor``: check the setup a run depends on, one line per check.

    striim-test doctor [--targets PATH] [--case PATH ...]

1. ``.env`` (at the project root, ``livetest.paths.dotenv_path``) and the process environment:
   an ``SLT_*`` key the framework never reads is flagged as a likely typo, with the nearest
   known key; a known key that ``.env`` cannot supply (``.env`` supplies only
   ``livetest.paths.KEYS`` and ``SERVICE_KEYS``) is flagged as ignored; a set path key naming a
   missing path is flagged with its key and where it was set; ``STRIIM_URL`` must be
   ``http(s)://host[:port]``. The service settings a run gets (``SERVICE_KEYS``, e.g.
   ``SLT_PG_HOST``) are reported with where each is set. ``STRIIM_API_TIMEOUT``, when set, must
   parse as ``striim_api`` parses it.
   The effective infrastructure ownership (``SLT_INFRA_OWNERSHIP``, environment then ``.env``)
   is reported; unset, not ``exclusive``/``shared``, or ``shared`` without ``SLT_KEEP_SERVICES``
   is flagged, because ``livetest.infra`` then refuses every live run. Ownership ledgers an earlier
   run left unfinished are named with the ``python -m livetest.ownership replay`` command for each.
   With a project manifest (``--targets``, else ``GOLD_TARGETS``) the ownership and ledger checks
   read what a run's tier child reads: the project's ``.env`` (then the clone's) and its state dir.
2. Striim: with ``STRIIM_URL`` set, an authentication probe against it. Unset (Docker mode), a
   cluster already answering on the default endpoint is probed; otherwise Docker must answer,
   the release must resolve (``STRIIM_HOME``) and the license the cluster boots with must too.
   With ``STRIIM_URL`` set and a selected case that loads a jar or uploads a file (``op:``,
   ``udf:``, a ``server_files`` entry with ``load:``), ``$STRIIM_HOME/UploadedFiles`` must exist and
   be writable: the run copies them there, so ``STRIIM_HOME`` must be the running server's own
   install root, on this host.
3. Cases: each selected case's ``test.yaml`` must load as a run loads it (``load_manifest``,
   including the ``exact:`` and ``lifecycle:`` rules); an error is a configuration failure.
   Services: every service a selected case ``requires:`` is reached where the run would reach
   it. A customer-provided service (its ``live_override_env`` set, e.g. ``SLT_PG_HOST``) must
   accept a TCP connection, and Postgres a login as its admin user. A Docker service is
   connected to when its container runs; otherwise Docker must answer, since the run starts it.

The known ``SLT_*`` keys are read from this clone's own sources (every ``SLT_*`` literal under
``scripts/``, tests excluded), so a key a later change adds is known without a list to update.
Doctor starts nothing and writes nothing. Exit: 0 clean (warnings allowed), 2 when a
configuration check fails, else 3 when only reachability checks fail.
"""
from __future__ import annotations

import difflib
import re
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from striim_test.errors import CONFIG, INFRA, OK, aggregate

OK_, WARN, FAIL = "ok", "warn", "FAIL"
PATH_KEYS = ("SLT_FRAMEWORK_HOME", "SLT_PROJECT_ROOT", "SLT_LIVE_CASES", "SLT_INT_CASES",
             "SLT_SERVICES_DIR", "SLT_INT_SERVICES_DIR", "SLT_STATE_DIR")
# Paths read from the process environment only (not paths.KEYS). A pattern, so a family such
# as the per-release JDK homes is covered as a whole. A service driver adds its own
# (livetest.drivers ENV_PATH_KEYS; driver_path_keys below).
ENV_PATH_KEYS = re.compile(r"SLT_STRIIM_DEPS_MANIFEST|SLT_JDK[0-9]+_HOME")
_LITERAL = re.compile(r"(?<![A-Za-z0-9_])SLT_[A-Z0-9_]*(?:\{[^{}\n]*\}[A-Z0-9_]*)*")
_SCAN_SUFFIXES = {".py", ".yaml", ".yml", ".sh", ".env", ".toml", ".cfg", ".ini"}
_CONNECT_TIMEOUT = 5.0


@dataclass
class Check:
    status: str           # ok | warn | FAIL
    subject: str          # what was checked: "env", "striim", "service postgres", ...
    message: str
    code: int = OK        # CONFIG or INFRA when FAIL

    def line(self) -> str:
        return f"[{self.status:^4}] {self.subject}: {self.message}"


def _ok(subject, message):
    return Check(OK_, subject, message)


def _warn(subject, message):
    return Check(WARN, subject, message)


def _fail(subject, message, code=CONFIG):
    return Check(FAIL, subject, message, code)


# --- known keys ------------------------------------------------------------------------------

def known_keys(clone: Path) -> tuple[set, list]:
    """(exact keys, template patterns) from every SLT_* literal in the clone's scripts/ tree,
    tests excluded. ``f"SLT_{name.upper()}_VIEW_HOST"`` becomes a pattern; a template with no
    literal text after ``SLT_`` (a namespace such as ``f"SLT_{slug}"``) names no key and is
    skipped, as is a bare prefix such as ``"SLT_PG_"``."""
    exact, patterns = set(), set()
    root = Path(clone) / "scripts"
    for f in root.rglob("*"):
        if f.suffix not in _SCAN_SUFFIXES or "tests" in f.relative_to(root).parts \
                or not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for tok in _LITERAL.findall(text):
            if "{" not in tok:
                if not tok.endswith("_"):
                    exact.add(tok)
                continue
            literal = re.sub(r"\{[^{}]*\}", "", tok)[4:]
            if len(re.sub(r"[^A-Z0-9]", "", literal)) < 3:
                continue
            parts = re.split(r"\{[^{}]*\}", tok)
            patterns.add("[A-Z0-9_]+".join(map(re.escape, parts)))
    return exact, [re.compile(p + r"\Z") for p in sorted(patterns)]


def _is_known(key, exact, patterns) -> bool:
    return key in exact or bool(ENV_PATH_KEYS.fullmatch(key)) or any(p.match(key)
                                                                     for p in patterns)


# --- .env -----------------------------------------------------------------------------------

def declared_service_keys(env=None) -> set:
    """The settings every service definition the registry can see names (live_override_env,
    live_env, docker_env, opt_in_env, and the variables its compose file interpolates),
    consumer servicesRoots included: a consumer service may use SLT_ names of its own, and they
    are not typos. The same declarations govern settings forwarding."""
    from livetest.service_env import declarations
    return set(declarations(env))


def driver_settings() -> tuple[set, set, list]:
    """(path keys, other keys, failures) the visible services' drivers declare (livetest.drivers
    ENV_PATH_KEYS and ENV_KEYS). A driver that cannot be loaded is a failure naming the service and
    module, so its settings checks do not silently disappear."""
    from livetest import drivers, registry
    path_keys, keys, failures = set(), set(), []
    for name in registry.all_services():
        try:
            defn = registry.load_service(name)
        except Exception:           # a broken definition is reported where the service is used
            continue
        if not getattr(defn, "driver", None):
            continue
        try:
            module = drivers.load(defn)
        except Exception as e:
            failures.append(_fail("env", f"service {name}: driver {defn.driver!r} cannot be loaded from "
                                         f"{defn.dir}: {type(e).__name__}: {e}"))
            continue
        path_keys.update(getattr(module, "ENV_PATH_KEYS", ()))
        keys.update(getattr(module, "ENV_KEYS", ()))
    return path_keys, keys, failures


def driver_path_keys() -> set:
    """The path settings the visible services' drivers declare (livetest.drivers ENV_PATH_KEYS)."""
    return driver_settings()[0]


def dotenv_keys(path: Path) -> list[str]:
    """Every key assigned in ``path``, in the dialect ``paths.read_dotenv`` accepts."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return []
    keys = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export") and line[6:7].isspace():
            line = line[6:].lstrip()
        key, sep, _ = line.partition("=")
        if sep and key.strip():
            keys.append(key.strip())
    return keys


def check_env(env: dict, clone: Path) -> tuple[list, dict]:
    """The .env and environment checks; also returns the .env values (KEYS only)."""
    from livetest import paths
    checks = []
    try:
        dotenv = paths.dotenv_path(env)
    except paths.PathConfigError as e:
        return [_fail("env", str(e))], {}
    values = paths.dotenv_values(env)
    in_file = dotenv_keys(dotenv)
    if dotenv.is_file():
        checks.append(_ok("env", f"{dotenv} ({len(in_file)} keys)"))
    else:
        checks.append(_ok("env", f"no .env at {dotenv}; using the environment and defaults"))

    from striim_test._bootstrap import RETIRED
    exact, patterns = known_keys(clone)
    driver_paths, driver_keys, driver_failures = driver_settings()
    checks += driver_failures
    exact = (exact | set(paths.KEYS) | declared_service_keys(env) | driver_paths | driver_keys) - set(RETIRED)
    for source, keys in ((str(dotenv), in_file), ("environment", sorted(env))):
        for key in keys:
            if not key.startswith("SLT_"):
                continue
            if key in RETIRED:
                checks.append(_fail("env", f"{key} (set in {source}) is retired and never read; "
                                           f"use {RETIRED[key]}"))
            elif not _is_known(key, exact, patterns):
                near = difflib.get_close_matches(key, sorted(exact), n=1, cutoff=0.8)
                hint = f"; did you mean {near[0]}?" if near else ""
                checks.append(_fail("env", f"{key} (set in {source}) is not a key the framework "
                                           f"reads, likely a typo{hint}"))
            elif source != "environment" and key not in paths.KEYS and key not in paths.SERVICE_KEYS and key not in declared_service_keys(env):
                checks.append(_fail("env", f"{key} is set in {dotenv}, but .env supplies only "
                                           f"{', '.join(paths.KEYS)} and the service settings; "
                                           f"export {key} in the environment instead"))

    for key in PATH_KEYS:
        try:
            paths._path(key, Path("."), env, values)
        except paths.PathConfigError as e:
            checks.append(_fail("env", str(e)))
    for key in sorted(k for k in env if ENV_PATH_KEYS.fullmatch(k) or k in driver_paths):
        raw = (env.get(key) or "").strip()
        if raw and not Path(raw).expanduser().exists():
            checks.append(_fail("env", f"{key}={raw!r} (set in environment) does not exist: "
                                       f"{Path(raw).expanduser().resolve()}"))
    return checks, values


def check_api_timeout(env: dict) -> list:
    """STRIIM_API_TIMEOUT must parse as striim_api parses it; a run would otherwise fail only once
    its first Striim client is built, after provisioning, and read as a topology error."""
    from livetest import paths
    # The layers dispatch.striim_env hands a tier child, each named by the file it came from.
    key = "STRIIM_API_TIMEOUT"
    raw, source = (env.get(key) or "").strip(), "environment"
    if not raw:
        clone_env = {k: v for k, v in env.items() if k != "SLT_PROJECT_ROOT"}
        try:
            layers = [(paths.dotenv_path(e), paths.read_dotenv(paths.dotenv_path(e)))
                      for e in (env, clone_env)]
        except paths.PathConfigError:
            return []                   # reported by check_env
        layers.append((paths.machine_env_path(env), paths.machine_values(env)))
        raw, source = next(((v.get(key, "").strip(), str(p)) for p, v in layers
                            if v.get(key, "").strip()), ("", None))
    if not raw:
        return []
    try:
        from livetest.striim import striim_api
    except Exception as e:              # noqa: BLE001 - e.g. a bad SLT_FRAMEWORK_HOME, reported above
        return [_warn("env", f"STRIIM_API_TIMEOUT={raw} not checked: {e}")]
    try:
        striim_api.parse_timeout(raw)
    except ValueError as e:
        return [_fail("env", f"{e} (set in {source})")]
    return [_ok("env", f"STRIIM_API_TIMEOUT={raw} (set in {source})")]


def _lookup(key, env: dict, dotenv: dict):
    """(value, source) as striim-test hands ``key`` to a tier child: the shell, then the .env of
    ``env``'s project root, then the clone's .env (``dispatch.ownership_env``)."""
    from livetest import paths
    value, source = paths._lookup(key, env, dotenv)
    if not value and (env.get("SLT_PROJECT_ROOT") or "").strip():
        value, source = paths._lookup(key, {k: v for k, v in env.items() if k != "SLT_PROJECT_ROOT"})
    return value, source


def check_service_settings(env: dict) -> list:
    """The service settings (``livetest.paths.SERVICE_KEYS``) a run gets, and where each is set.
    The shell wins over .env; a .env value reaches a run through striim-test only."""
    from livetest import paths
    from striim_test.dispatch import service_env
    try:
        filled = service_env(env)
        clone_env = {k: v for k, v in env.items() if k != "SLT_PROJECT_ROOT"}
        layers = [(paths.dotenv_path(e), paths.dotenv_values(e)) for e in (env, clone_env)]
    except paths.PathConfigError:
        return []                       # reported by check_env
    parts = []
    for key in paths.SERVICE_KEYS:
        in_file = next((path for path, values in layers if (values.get(key) or "").strip()), None)
        if (env.get(key) or "").strip():
            over = f", overrides {in_file}" if in_file else ""
            parts.append(f"{key} (set in environment{over})")
        elif key in filled:
            parts.append(f"{key} (set in {_where(str(in_file))})")
    if not parts:
        return [_ok("service settings", "none set; each required service runs in Docker with "
                                        "its defaults")]
    return [_ok("service settings", "; ".join(parts))]


def check_ownership(env: dict, dotenv: dict) -> list:
    """The ownership a live run declares (``livetest.infra.declare``), read without declaring."""
    from livetest import infra
    key = infra.OWNERSHIP_ENV
    mode, source = _lookup(key, env, dotenv)
    if not mode:
        return [_fail("ownership", f"{key} is unset, so live runs will be refused; set {key}=shared "
                                   f"(with SLT_KEEP_SERVICES=1) to reuse a kept stack, or "
                                   f"{key}=exclusive for a stack this run owns")]
    if mode not in infra.MODES:
        return [_fail("ownership", f"{key}={mode!r} (set in {source}) is not an accepted value, so live "
                                   f"runs will be refused; use exclusive or shared")]
    keep, keep_source = _lookup("SLT_KEEP_SERVICES", env, dotenv)
    if mode == "shared" and not keep:
        return [_fail("ownership", f"{key}=shared (set in {source}) requires SLT_KEEP_SERVICES=1, so live "
                                   f"runs will be refused")]
    if mode == "exclusive" and keep:
        if keep_source == "environment":
            return [_warn("ownership", f"exclusive ({key}, set in {_where(source)}) with SLT_KEEP_SERVICES={keep} "
                                       f"(set in environment): the run keeps its own stack at session end, and "
                                       f"the next exclusive run is refused while it stands; unset "
                                       f"SLT_KEEP_SERVICES")]
        return [_warn("ownership", f"exclusive ({key}, set in {_where(source)}); SLT_KEEP_SERVICES={keep} in "
                                   f"{keep_source} is not used for an exclusive run (striim-test takes it from "
                                   f".env only for shared), so the run tears its stack down")]
    kept = f"; SLT_KEEP_SERVICES={keep} (set in {_where(keep_source)})" if keep else ""
    return [_ok("ownership", f"{mode} ({key}, set in {_where(source)}){kept}")]


def _where(source) -> str:
    """A .env source reaches a run through striim-test only: a direct pytest reads its environment."""
    return source if source == "environment" else f"{source}; striim-test only, export for direct pytest"


def check_ledgers(env: dict, dotenv: dict) -> list:
    """Ownership ledgers an earlier live run left unfinished (killed, timed out, failed or kept
    cleanup), each with the command that reclaims it; a warning, since the next run is not blocked."""
    from livetest import ownership, paths
    value, source = paths._lookup("SLT_STATE_DIR", env, dotenv)
    if value:
        p = Path(value).expanduser()
        if not p.is_absolute():
            p = (Path.cwd() if source == "environment" else Path(source).parent) / p
        if not p.exists():
            # a project that has not run yet; a mistyped key is check_env's FAIL
            return [_ok("ledgers", f"no leftover ownership ledgers (no state yet at {p.resolve()})")]
    try:
        state = paths.state_dir(env, dotenv)
        left = ownership.leftover_ledgers(state)
    except Exception as e:              # noqa: BLE001 - a set-but-missing state dir is reported by check_env
        return [_warn("ledgers", f"could not read the ownership ledgers: {e}")]
    if not left:
        return [_ok("ledgers", "no leftover ownership ledgers")]
    return [_warn("ledgers", ownership.leftover_notice(path, why)) for path, why in left]


def url_problem(url: str) -> str | None:
    """Why ``url`` is not a usable STRIIM_URL, or None."""
    if "://" not in url:
        return f"STRIIM_URL={url!r} has no scheme; write it as http://{url}"
    try:
        parts = urlsplit(url)
        parts.port                      # raises ValueError on a non-numeric or out-of-range port
    except ValueError as e:
        return f"STRIIM_URL={url!r} is not a valid URL: {e}"
    if parts.scheme not in ("http", "https"):
        return f"STRIIM_URL={url!r} must use http:// or https://, not {parts.scheme}://"
    if not parts.hostname:
        return f"STRIIM_URL={url!r} has no host"
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        root = f"{parts.scheme}://{parts.netloc}"
        return f"STRIIM_URL={url!r} must be the server root ({root}), with no path or query"
    return None


# --- Striim ---------------------------------------------------------------------------------

def _docker_answers(run=None) -> tuple[bool, str]:
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=20))
    try:
        p = run(["docker", "info", "--format", "{{.ServerVersion}}"])
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"docker not usable: {e}"
    if p.returncode != 0:
        err = (p.stderr or p.stdout or "").strip().splitlines()
        return False, f"'docker info' failed: {err[-1] if err else f'exit {p.returncode}'}"
    return True, f"Docker {(p.stdout or '').strip()}"


def check_striim(env: dict, dotenv: dict, probe=None, docker=None, build=None) -> list:
    """The Striim checks, with settings resolved as livetest.paths resolves them."""
    from livetest import paths
    probe = probe or _probe
    docker = docker or _docker_answers
    build = build or _docker_build
    url, source = paths._lookup("STRIIM_URL", env, dotenv)
    user = paths.setting("STRIIM_USER", env, dotenv) or "admin"
    pw = paths.setting("STRIIM_PASS", env, dotenv)
    if url:
        problem = url_problem(url)
        if problem:
            return [_fail("striim", f"{problem} (set in {source})")]
        ok, detail = probe(url, user, pw or "striim")
        if ok:
            return [_ok("striim", f"{url} authenticated as {user!r}")]
        code = CONFIG if detail.startswith("HTTP 401") else INFRA
        unset = "" if pw else " (STRIIM_PASS unset, so the Docker default was tried)"
        return [_fail("striim", f"{url} (STRIIM_URL, set in {source}) as {user!r}"
                                         f"{unset}: {detail}", code)]
    # Docker mode: the run provisions a cluster at the default endpoint.
    from livetest.infra import striim_endpoint
    default = striim_endpoint(env)[0]
    ok, detail = probe(default, user, pw or "striim")
    if ok:
        return [_ok("striim", f"Docker mode (STRIIM_URL unset); a cluster already answers at "
                              f"{default}")]
    if detail.startswith("HTTP"):
        return [_fail("striim", f"Docker mode (STRIIM_URL unset), but {default} answers "
                                f"{detail}; stop what is on that port, or set STRIIM_URL to it")]
    up, why = docker()
    if not up:
        return [_fail("striim", f"Docker mode (STRIIM_URL unset) needs Docker: {why}; start "
                                f"Docker, or set STRIIM_URL to your Striim server", INFRA)]
    problem, release = build(env)
    if problem:
        return [_fail("striim", f"Docker mode (STRIIM_URL unset): {problem}")]
    return [_ok("striim", f"Docker mode (STRIIM_URL unset); {why} answers, and the run builds "
                          f"Striim {release} and starts a cluster at {default}")]


_LICENSE_HELP = ("export STRIIM_HOME=<a Striim install> (read from its conf/startUp.properties), "
                 "or export COMPANY_NAME, CLUSTER_NAME, PRODUCT_KEY and LICENCE_KEY")


def _docker_build(env) -> tuple[str | None, str]:
    """(problem, release) for the image the run would build: the release from STRIIM_HOME (else
    the default), and the four license settings the cluster needs to boot, from the environment
    or derived from $STRIIM_HOME/conf/startUp.properties."""
    from livetest import releases, striim_provision
    try:
        release = releases.resolve_release(env)["STRIIM_VERSION"]
    except releases.ReleaseError as e:
        return f"STRIIM_HOME={env.get('STRIIM_HOME')!r} picks the Striim release: {e}", ""
    _, sources = striim_provision.enrich_license_env(env, home=env.get("STRIIM_HOME") or "")
    missing = [k for k, v in sources.items() if v == "unset"]
    if missing:
        return (f"Striim needs a license to boot, and {', '.join(missing)} "
                f"{'is' if len(missing) == 1 else 'are'} not set; {_LICENSE_HELP}"), release
    return None, release


def check_license(env: dict) -> list:
    """Docker mode: where each of the four license settings comes from, by name only. A missing
    one is the striim check's failure, so it is not repeated here."""
    from livetest import striim_provision
    home = env.get("STRIIM_HOME") or ""
    _, sources = striim_provision.enrich_license_env(env, home=home)
    if "unset" in sources.values():
        return []
    parts = []
    for where, label in (("env", "the environment"),
                         ("derived", f"{home}/conf/startUp.properties (STRIIM_HOME)")):
        names = [k for k, v in sources.items() if v == where]
        if names:
            parts.append(f"{', '.join(names)} from {label}")
    return [_ok("license", "; ".join(parts))]


def check_installers(env: dict) -> list:
    """Docker mode: where the image build gets the Striim installers. With a manifest, it is
    read and every required file is looked for (not hashed: the build does that)."""
    from livetest import releases, striim_provision as sp
    raw = (env.get(sp.DEPS_MANIFEST_ENV) or "").strip()
    if not raw:
        if not sp._in_checkout():
            return [_fail("installers", f"{sp.DEPS_MANIFEST_ENV} is unset, and an installed "
                                        f"framework builds Striim only from a manifest; see "
                                        f"the README's Docker mode")]
        return [_ok("installers", f"{sp.DEPS_MANIFEST_ENV} unset; the first build downloads "
                                  f"about 6.3 GB of Striim packages and drivers")]
    try:
        version = releases.resolve_release(env)["STRIIM_VERSION"]
    except releases.ReleaseError:
        return []                                   # reported by the striim check
    where = f"{raw} ({sp.DEPS_MANIFEST_ENV})"
    try:
        ddir, expected = sp.load_striim_deps_manifest(raw, env)
    except sp.StriimDepsError as e:
        return [_fail("installers", str(e))]
    names = sp._required_deps(version)
    for name in names:
        if name not in expected:
            return [_fail("installers", f"{where}: names no digest for {name}")]
        if not (ddir / name).is_file():
            return [_fail("installers", f"{where}: {name} is not in {ddir}")]
    return [_ok("installers", f"{where} names all {len(names)} files for Striim {version}, all "
                              f"in {ddir}; their digests are checked before the build")]


def check_docker_disk(env: dict, dotenv: dict, disk=None, present=None) -> list:
    """Docker mode only: is there room in Docker for the image build, or, with the image
    already built, for the running cluster (livetest.docker_disk has the floors)."""
    from livetest import docker_disk, paths, releases, striim_provision
    if paths._lookup("STRIIM_URL", env, dotenv)[0]:
        return []
    disk = disk or docker_disk.vm_free_bytes
    present = present or striim_provision.image_present
    try:
        release = releases.resolve_release(env)["STRIIM_VERSION"]
    except releases.ReleaseError:
        return []                                   # reported by the striim check
    free, how = disk()
    if free is None:
        return [_warn("docker disk", f"could not measure Docker's free space ({how}); "
                                     f"building the Striim image needs at least "
                                     f"{docker_disk.min_free_gb(True, env):g} GB")]
    building = not present(release)
    problem = docker_disk.shortfall(free, building, env)
    if problem:
        return [_fail("docker disk", problem, INFRA)]
    what = "building the Striim image" if building else "running the Striim cluster"
    return [_ok("docker disk", f"{docker_disk.describe(free)} free ({how}); {what} needs at "
                               f"least {docker_disk.min_free_gb(building, env):g} GB")]



def _probe(url, user, pw):
    from livetest.striim import probe_reachable_detail
    return probe_reachable_detail(url, user, pw)


def _uploads(f: Path) -> bool:
    """Whether a case copies files into the server's UploadedFiles/ (``op:``/``udf:`` jars and their
    uploads, or a ``server_files`` jar with ``load:``)."""
    try:
        raw = yaml.safe_load(f.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return False                    # reported by required_services
    if not isinstance(raw, dict):
        return False
    return bool(raw.get("op") or raw.get("udf")) or any(
        isinstance(e, dict) and e.get("load") for e in (raw.get("server_files") or []))


def check_uploads(env: dict, dotenv: dict, files: list) -> list:
    """With STRIIM_URL set (a native server), a selected case that uploads needs
    ``$STRIIM_HOME/UploadedFiles``, writable: livetest.opartifacts copies into it. STRIIM_HOME is
    read from the environment only, as the run reads it."""
    import os

    from livetest import paths
    url, _ = paths._lookup("STRIIM_URL", env, dotenv)
    cases = [f.parent.name for f in files if _uploads(f)]
    if not url or not cases:
        return []
    who, fix = ", ".join(cases), (f"export STRIIM_HOME=<the install root of the Striim server at {url}, "
                                  f"on this host>")
    home = (env.get("STRIIM_HOME") or "").strip()
    if not home:
        return [_fail("uploads", f"{who} upload(s) into the server's UploadedFiles/, and STRIIM_HOME is "
                                 f"unset; {fix}")]
    d = Path(home).expanduser() / "UploadedFiles"
    if not d.is_dir():
        return [_fail("uploads", f"{d} does not exist, and {who} upload(s) into it: STRIIM_HOME is not "
                                 f"the running server's install; {fix}")]
    if not os.access(d, os.W_OK | os.X_OK):
        return [_fail("uploads", f"{d} is not writable by this user, and {who} upload(s) into it")]
    return [_ok("uploads", f"{d} is writable ({who})")]


# --- services -------------------------------------------------------------------------------

def case_files(paths_: list) -> tuple[list, list]:
    """(test.yaml files, checks) for the --case arguments: a test.yaml, a case dir, or a dir of
    cases (searched recursively)."""
    found, checks = [], []
    for raw in paths_:
        p = Path(raw).expanduser()
        if p.is_file():
            found.append(p.resolve())
        elif (p / "test.yaml").is_file():
            found.append((p / "test.yaml").resolve())
        elif p.is_dir() and any(p.rglob("test.yaml")):
            found.extend(sorted(f.resolve() for f in p.rglob("test.yaml")))
        else:
            checks.append(_fail("case", f"{raw}: no test.yaml there ({p.resolve()})"))
    return found, checks


def required_services(files: list) -> tuple[dict, list]:
    """{service: [case dirs requiring it]} and the checks for unreadable manifests."""
    need, checks = {}, []
    for f in files:
        try:
            raw = yaml.safe_load(f.read_text()) or {}
        except (OSError, yaml.YAMLError) as e:
            checks.append(_fail("case", f"{f}: cannot read: {e}"))
            continue
        req = raw.get("requires", []) if isinstance(raw, dict) else None
        if not isinstance(req, list):
            checks.append(_fail("case", f"{f}: 'requires' must be a list of service names, got "
                                        f"{req!r}"))
            continue
        for svc in req:
            need.setdefault(str(svc), []).append(f.parent.name)
    return need, checks


def case_tiers(project):
    """``tier_of(test.yaml)``: the tier whose case root holds the case, or a reason it is not checked.
    Unlike ``run PATH``, which asks for ``--tier`` whenever two roots hold a path, nested roots
    resolve to the deepest one; two tiers sharing the very root that holds the case cannot be told
    apart, so that case is not checked. None when the case lies outside every root; ``tier_of.roots``
    lists the roots for that message."""
    from striim_test import project_io
    roots = [(t, r) for t in ("live", "integration", "perf") if t in project.suites
             for r in project_io.tier_roots(project, t)]

    def tier_of(f):
        p = Path(f).resolve().parent
        hits = [(len(r.parts), t) for t, r in roots if p == r or r in p.parents]
        if not hits:
            return None
        deepest = max(n for n, _ in hits)
        tiers = [t for n, t in hits if n == deepest]
        if len(tiers) > 1:
            return (f"not checked: in the case roots of {' and '.join(tiers)}; "
                    f"`run` needs --tier")
        return tiers[0]
    tier_of.roots = ", ".join(f"{t}: {r}" for t, r in roots) or "none declared"
    return tier_of


def _loaders():
    from inttest import manifest as int_manifest
    from inttest import perfmanifest
    from livetest import manifest as live_manifest
    return {"live": (live_manifest.load_manifest, live_manifest.ManifestError, ""),
            "integration": (int_manifest.load_manifest, int_manifest.ManifestError,
                            " as an integration case"),
            "perf": (perfmanifest.load_perf_manifest, perfmanifest.PerfManifestError,
                     " as a perf case")}


def check_manifests(files: list, tier_of=None) -> list:
    """One check per case: its test.yaml loads as a run of its tier loads it (the live loader with the
    ``exact:`` and ``lifecycle:`` rules; the integration and perf loaders for those tiers), so a typo
    fails here and not at run time. Without ``tier_of`` every case is live. A case outside every
    case root is not checked: ``run`` refuses it anyway, naming the roots."""
    loaders = _loaders()
    checks = []
    for f in files:
        subject = f"case {f.parent.name}"
        tier = tier_of(f) if tier_of is not None else "live"
        if tier is None:
            checks.append(_warn(subject, f"not checked: outside every case root ({tier_of.roots})"))
            continue
        if tier not in loaders:
            checks.append(_warn(subject, tier))
            continue
        load, error, how = loaders[tier]
        try:
            m = load(f)
        except error as e:
            checks.append(_fail(subject, str(e)))
        except (OSError, ValueError, TypeError, AttributeError, KeyError) as e:
            checks.append(_fail(subject, f"{f}: {type(e).__name__}: {e}"))
        else:
            checks.append(_ok(subject, f"test.yaml loads{how} ({getattr(m, 'name', None) or f.parent.name})"))
    return checks


def _tcp(host, port) -> str | None:
    try:
        with socket.create_connection((host, int(port)), timeout=_CONNECT_TIMEOUT):
            return None
    except (OSError, ValueError) as e:
        return str(e) or type(e).__name__


def _pg_login(base) -> str | None:
    import psycopg2
    try:
        psycopg2.connect(host=base["host"], port=int(base["port"]), dbname=base["dbname"],
                         user=base["admin_user"], password=base["admin_password"],
                         connect_timeout=int(_CONNECT_TIMEOUT)).close()
    except Exception as e:  # noqa: BLE001 -- any driver error is the message
        return " ".join(str(e).split()) or type(e).__name__
    return None


LOGINS = {"postgres": _pg_login}


def _port_publisher(port, env) -> str:
    """The name of a running container that publishes host ``port``, or ""."""
    try:
        out = subprocess.run(["docker", "ps", "--filter", f"publish={port}", "--format", "{{.Names}}"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    return (out.split() or [""])[0]


def _taken_host_port(defn, base: dict, tcp, env: dict, publisher):
    """(addr, setting) of the first host port a stopped Docker service would publish on that already
    answers, or None. `docker compose up` fails on it with "port is already allocated". A port published
    by a running container of this stack (a sibling of the stopped one in its compose project, such as
    slt-zookeeper beside slt-kafka) is not taken: `up` just starts the stopped container."""
    from livetest import stack
    ours = stack.prefixed("slt-", env)
    for key, envname in defn.docker_env.items():
        port = base.get(key)
        if (key == "port" or key.endswith("_port")) and port and base.get("host"):
            if tcp(base["host"], port) is None and not publisher(port, env).startswith(ours):
                return f"{base['host']}:{port}", envname
    return None


def check_services(need: dict, env: dict, tcp=None, login=None, docker=None,
                   running=None, publisher=None) -> list:
    from livetest import registry, resource_profiles, services
    tcp = tcp or _tcp
    logins = LOGINS if login is None else login
    docker = docker or _docker_answers
    running = running or services.container_running
    publisher = publisher or _port_publisher
    checks = []
    for name in sorted(need):
        subject = f"service {name}"
        cases = ", ".join(sorted(set(need[name])))
        try:
            defn = registry.load_service(name, env=env)
        except registry.RegistryError as e:
            checks.append(_fail(subject, f"required by {cases}: {e}"))
            continue
        if defn.opt_in_env and not (env.get(defn.opt_in_env) or env.get("SLT_EMULATORS")):
            checks.append(_warn(subject, f"opt-in: `start all` and other bring-ups not tied to a "
                                         f"case leave it out unless {defn.opt_in_env}=1 or "
                                         f"SLT_EMULATORS=1; a live case that requires it ({cases}) "
                                         f"still starts it"))
            continue
        live = defn.live_override_env and (env.get(defn.live_override_env) or "").strip()
        if live:
            base = {k: (env.get(v) or "").strip() or (getattr(defn, "live_defaults", None) or {}).get(
                k, defn.docker_defaults.get(k))
                    for k, v in defn.live_env.items()}
            where = f"customer-provided ({defn.live_override_env}={live})"
        else:
            base = dict(defn.docker_defaults)
            for key, envname in defn.docker_env.items():
                if (env.get(envname) or "").strip():
                    base[key] = env[envname].strip()
            host = (env.get("SLT_SERVICES_HOST") or "").strip()
            if host and base.get("host") in (None, "localhost", "127.0.0.1"):
                base["host"] = host
            where = f"Docker ({defn.container})"
            why = registry.unavailable(defn, env)
            if why and not defn.compose and not defn.container:
                checks.append(_warn(subject, f"required by {cases}: {why}; they skip until then"))
                continue
            try:
                prof = resource_profiles.select_profile("live", name, services_dir=Path(defn.dir).parent)
            except resource_profiles.ProfileError as e:
                checks.append(_fail(subject, f"required by {cases}: {e}"))
                continue
            for rel in resource_profiles.absent_directory_mounts(prof):
                checks.append(_warn(subject, f"bind-mount source {rel} does not exist in {defn.dir}; "
                                             f"Docker creates it as an empty directory when it "
                                             f"starts {name}"))
            if defn.container and not running(defn.container):
                up, why = docker()
                if not up:
                    checks.append(_fail(subject, f"{where} is not running and the run cannot "
                                                 f"start it: {why}; start Docker, or point "
                                                 f"{defn.live_override_env} at your own "
                                                 f"{name}", INFRA))
                else:
                    taken = _taken_host_port(defn, base, tcp, env, publisher)
                    if taken:
                        addr, envname = taken
                        from livetest import paths
                        how = (" in .env" if envname in paths.SERVICE_KEYS else
                               ": export it in the shell, since .env does not pass it to the run")
                        checks.append(_fail(subject, f"{where} is not running, but {addr} already "
                                                     f"answers: something else holds the host port "
                                                     f"the run publishes {name} on; set {envname} "
                                                     f"to a free port{how}"))
                    else:
                        checks.append(_ok(subject, f"{where} not running; {why} answers, and the "
                                                   f"run starts it"))
                continue
        if not base.get("host") or not base.get("port"):
            checks.append(_ok(subject, f"{where}; no host/port to probe"))
            continue
        addr = f"{base['host']}:{base['port']}"
        err = tcp(base["host"], base["port"])
        if err:
            checks.append(_fail(subject, f"{where}: cannot connect to {addr}: {err}", INFRA))
            continue
        probe = logins.get(name)
        err = probe(base) if probe else None
        if err:
            user = base.get("admin_user")
            checks.append(_fail(subject, f"{where}: {addr} refused login as {user!r}: {err}",
                                CONFIG if live else INFRA))
            continue
        checks.append(_ok(subject, f"{where} at {addr}" + (" (login ok)" if probe else "")))
    return checks


# --- command --------------------------------------------------------------------------------

def run_checks(env: dict, clone: Path, cases=(), probe=None, docker=None, tcp=None, login=None,
               running=None, location=None, disk=None, present=None, tier_of=None) -> list:
    """``location`` is a project manifest's ``dispatch.location_env``: the ownership, ledger and
    service checks then read what a tier child of that project reads."""
    from livetest import paths
    checks, dotenv = check_env(env, clone)
    run_env, run_dotenv = env, dotenv
    if location:
        run_env = {**env, **location}
        run_dotenv = paths.dotenv_values(run_env)
    checks += check_api_timeout(run_env)
    checks += check_service_settings(run_env)
    checks += check_ownership(run_env, run_dotenv)
    checks += check_ledgers(run_env, run_dotenv)
    striim_checks = check_striim(env, dotenv, probe=probe, docker=docker)
    checks += striim_checks
    if not [c for c in striim_checks if c.status == FAIL] and \
            not paths._lookup("STRIIM_URL", env, dotenv)[0]:
        checks += check_license(env)
        checks += check_installers(env)
        checks += check_docker_disk(env, dotenv, disk=disk, present=present)
    if cases:
        files, bad = case_files(list(cases))
        need, unreadable = required_services(files)
        checks += bad + unreadable
        checks += check_manifests(files, tier_of)
        checks += check_uploads(env, dotenv, files)
        if files and not need:
            checks.append(_ok("services", f"the {len(files)} selected case(s) require none"))
        from striim_test.dispatch import service_env
        try:
            svc_env = {**run_env, **service_env(run_env)}
        except paths.PathConfigError:
            svc_env = run_env           # reported by check_env
        checks += check_services(need, svc_env, tcp=tcp, login=login, docker=docker,
                                 running=running)
    return checks


def exit_code(checks) -> int:
    return aggregate([c.code for c in checks if c.status == FAIL] or [OK])


def project_location(targets, env: dict) -> tuple[dict, list]:
    """(location_env, checks) for ``--targets`` (else ``GOLD_TARGETS``): the path keys a run's
    tier child gets from the project manifest; ``{}`` without one, so nothing changes."""
    from types import SimpleNamespace

    from striim_test import project_io
    from striim_test.dispatch import location_env
    from striim_test.errors import CliError
    try:
        project = project_io.load(targets)
        if project.manifest is None:
            return {}, []
        state = project_io.state_root(project, env)
    except CliError as e:
        return {}, [_fail("targets", e.message)]
    # The service checks then see the manifest's servicesRoots, as the run does.
    from livetest import project as live_project
    try:
        live_project.load_and_activate(project.manifest)
    except Exception as e:                     # ProjectError, PathConfigError, LayoutError
        return {}, [_fail("targets", f"servicesRoots: {e}")]
    return location_env(SimpleNamespace(project=project, state=state)), []


def _tier_of(targets, cases):
    """``case_tiers`` of the project a run would use; None (every case live) when there are no
    --case arguments or the project does not load (``project_location`` reports that)."""
    if not cases:
        return None
    from striim_test import project_io
    from striim_test.errors import CliError
    try:
        return case_tiers(project_io.load(targets))
    except CliError:
        return None


def cmd_doctor(args, origins, env=None) -> int:
    import os
    env = dict(os.environ if env is None else env)
    location, checks = project_location(getattr(args, "targets", None), env)
    checks += run_checks(env, origins.home, cases=args.case or (), location=location,
                         tier_of=_tier_of(getattr(args, "targets", None), args.case))
    for c in checks:
        print(c.line(), flush=True)
    failed = [c for c in checks if c.status == FAIL]
    print(f"doctor: {len(failed)} problem(s)" if failed else "doctor: all checks passed")
    return exit_code(checks)
