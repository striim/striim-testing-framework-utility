"""Where the live engine finds things (spec section 2).

Every location the engine used to derive from ``Path(__file__).parents[N]`` comes from here.
Precedence per key: the process environment, checkout ``<project root>/.env``,
then the machine settings file, then today's relative default. Nothing is exported
to the process by this module. Machine settings cannot set checkout paths or lane keys.
A key that is set but names a missing path raises PathConfigError naming the key -- a set key
is never ignored. Empty values count as unset. The integration engine carries a twin,
``inttest/paths.py``, whose core block is byte-identical (test_int_paths.py checks it).
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# --- core (byte-identical in inttest/paths.py) ---------------------------------------------
KEYS = (
    "SLT_FRAMEWORK_HOME", "SLT_PROJECT_ROOT", "SLT_LIVE_CASES", "SLT_INT_CASES",
    "SLT_SERVICES_DIR", "SLT_INT_SERVICES_DIR", "SLT_STATE_DIR",
    "STRIIM_URL", "STRIIM_USER", "STRIIM_PASS", "STRIIM_PASSWORD",
    "SLT_INFRA_OWNERSHIP", "SLT_KEEP_SERVICES", "STRIIM_API_TIMEOUT",
)
# Service settings .env may carry as well: each service.yaml's live_override_env,
# live_env and docker_env names in both tiers, plus the per-service and Striim view hosts. The
# engines read them from the process environment; striim-test fills them in from .env.
_LIVE_SERVICE_KEYS = (
    "SLT_GCS_HOST", "SLT_GCS_HOST_PORT", "SLT_GCS_PORT", "SLT_GCS_PROJECT",
    "SLT_GCS_SRC_BUCKET", "SLT_GCS_TGT_BUCKET", "SLT_GCS_TOKEN_HOST_PORT", "SLT_GCS_VIEW_HOST",
    "SLT_KAFKA_BROKER_PORT", "SLT_KAFKA_DOCKER_HOST_PORT", "SLT_KAFKA_HOST",
    "SLT_KAFKA_HOST_PORT", "SLT_KAFKA_PORT", "SLT_KAFKA_REGISTRY_PORT", "SLT_KAFKA_SRC_TOPIC",
    "SLT_KAFKA_TGT_TOPIC", "SLT_KAFKA_VIEW_HOST", "SLT_MSSQL_DB", "SLT_MSSQL_HOST", "SLT_MSSQL_HOST_PORT",
    "SLT_MSSQL_PASSWORD", "SLT_MSSQL_PORT", "SLT_MSSQL_SOURCE_PASSWORD",
    "SLT_MSSQL_SOURCE_SCHEMA", "SLT_MSSQL_SOURCE_USER", "SLT_MSSQL_TARGET_PASSWORD",
    "SLT_MSSQL_TARGET_SCHEMA", "SLT_MSSQL_TARGET_USER", "SLT_MSSQL_USER", "SLT_MSSQL_VIEW_HOST",
    "SLT_MYSQL_ADMIN_PASSWORD", "SLT_MYSQL_ADMIN_USER", "SLT_MYSQL_HOST", "SLT_MYSQL_HOST_PORT",
    "SLT_MYSQL_PORT", "SLT_MYSQL_SOURCE_PASSWORD", "SLT_MYSQL_SOURCE_SCHEMA",
    "SLT_MYSQL_SOURCE_USER", "SLT_MYSQL_TARGET_PASSWORD", "SLT_MYSQL_TARGET_SCHEMA",
    "SLT_MYSQL_TARGET_USER", "SLT_MYSQL_VIEW_HOST", "SLT_ORACLE_VIEW_HOST",
    "SLT_ORA_CDB_SERVICE", "SLT_ORA_CDC_PASSWORD", "SLT_ORA_CDC_USER", "SLT_ORA_HOST",
    "SLT_ORA_HOST_PORT", "SLT_ORA_PORT", "SLT_ORA_SERVICE", "SLT_ORA_SOURCE_PASSWORD",
    "SLT_ORA_SOURCE_SCHEMA", "SLT_ORA_SOURCE_USER", "SLT_ORA_TARGET_PASSWORD",
    "SLT_ORA_TARGET_SCHEMA", "SLT_ORA_TARGET_USER", "SLT_PG_ADMIN_PASSWORD",
    "SLT_PG_ADMIN_USER", "SLT_PG_DB", "SLT_PG_HOST", "SLT_PG_HOST_PORT", "SLT_PG_PORT",
    "SLT_PG_SOURCE_PASSWORD", "SLT_PG_SOURCE_SCHEMA", "SLT_PG_SOURCE_USER",
    "SLT_PG_TARGET_PASSWORD", "SLT_PG_TARGET_SCHEMA", "SLT_PG_TARGET_USER",
    "SLT_POSTGRES_VIEW_HOST", "SLT_SCHEMA_REGISTRY_HOST_PORT",
    "SLT_SERVICENOW_CLIENT_ID", "SLT_SERVICENOW_CLIENT_SECRET", "SLT_SERVICENOW_HOST",
    "SLT_SERVICENOW_PASSWORD", "SLT_SERVICENOW_PORT", "SLT_SERVICENOW_SCHEME",
    "SLT_SERVICENOW_USER", "SLT_SERVICENOW_VIEW_HOST", "SLT_SPANNER_ADMIN_PORT",
    "SLT_SPANNER_GRPC_HOST_PORT", "SLT_SPANNER_GSQL_DB", "SLT_SPANNER_HOST",
    "SLT_SPANNER_INSTANCE", "SLT_SPANNER_PG_DB", "SLT_SPANNER_PORT", "SLT_SPANNER_PROJECT",
    "SLT_SPANNER_REST_HOST_PORT", "SLT_SPANNER_VIEW_HOST", "SLT_STRIIM_VIEW_HOST",
    "SLT_TERADATA_HOST", "SLT_TERADATA_PASSWORD", "SLT_TERADATA_PORT",
    "SLT_TERADATA_SOURCE_PASSWORD", "SLT_TERADATA_SOURCE_SCHEMA", "SLT_TERADATA_SOURCE_USER",
    "SLT_TERADATA_TARGET_PASSWORD", "SLT_TERADATA_TARGET_SCHEMA",
    "SLT_TERADATA_TARGET_USER", "SLT_TERADATA_USER", "SLT_TERADATA_VIEW_HOST",
    "SLT_VERTICA_ADMIN_PASSWORD", "SLT_VERTICA_ADMIN_USER", "SLT_VERTICA_DB", "SLT_VERTICA_HOST",
    "SLT_VERTICA_HOST_PORT", "SLT_VERTICA_PORT", "SLT_VERTICA_SOURCE_PASSWORD",
    "SLT_VERTICA_SOURCE_SCHEMA", "SLT_VERTICA_SOURCE_USER", "SLT_VERTICA_TARGET_PASSWORD",
    "SLT_VERTICA_TARGET_SCHEMA", "SLT_VERTICA_TARGET_USER", "SLT_VERTICA_VIEW_HOST",
    "SLT_ZOOKEEPER_CLIENT_PORT", "SLT_ZOOKEEPER_PORT",
)
_INT_SERVICE_KEYS = (
    "INT_GCS_BUCKET", "INT_GCS_HOST", "INT_GCS_HOST_PORT", "INT_GCS_PORT", "INT_GCS_PROJECT",
    "INT_MSSQL_ADMIN_PASSWORD", "INT_MSSQL_ADMIN_USER", "INT_MSSQL_DB", "INT_MSSQL_HOST",
    "INT_MSSQL_HOST_PORT", "INT_MSSQL_PORT", "INT_MSSQL_SOURCE_PASSWORD",
    "INT_MSSQL_SOURCE_SCHEMA", "INT_MSSQL_SOURCE_USER", "INT_MSSQL_TARGET_PASSWORD",
    "INT_MSSQL_TARGET_SCHEMA", "INT_MSSQL_TARGET_USER", "INT_MYSQL_ADMIN_PASSWORD",
    "INT_MYSQL_ADMIN_USER", "INT_MYSQL_DB", "INT_MYSQL_HOST", "INT_MYSQL_HOST_PORT",
    "INT_MYSQL_PORT", "INT_MYSQL_SOURCE_PASSWORD", "INT_MYSQL_SOURCE_SCHEMA",
    "INT_MYSQL_SOURCE_USER", "INT_MYSQL_TARGET_PASSWORD", "INT_MYSQL_TARGET_SCHEMA",
    "INT_MYSQL_TARGET_USER", "INT_ORA_HOST", "INT_ORA_HOST_PORT", "INT_ORA_PORT",
    "INT_ORA_SERVICE", "INT_ORA_SOURCE_PASSWORD", "INT_ORA_SOURCE_SCHEMA",
    "INT_ORA_SOURCE_USER", "INT_ORA_TARGET_PASSWORD", "INT_ORA_TARGET_SCHEMA",
    "INT_ORA_TARGET_USER", "INT_PG_ADMIN_PASSWORD", "INT_PG_ADMIN_USER", "INT_PG_DB",
    "INT_PG_HOST", "INT_PG_HOST_PORT", "INT_PG_PORT", "INT_PG_SOURCE_PASSWORD",
    "INT_PG_SOURCE_SCHEMA", "INT_PG_SOURCE_USER", "INT_PG_TARGET_PASSWORD",
    "INT_PG_TARGET_SCHEMA", "INT_PG_TARGET_USER",
    "INT_SERVICENOW_CLIENT_ID", "INT_SERVICENOW_CLIENT_SECRET", "INT_SERVICENOW_HOST",
    "INT_SERVICENOW_PASSWORD", "INT_SERVICENOW_PORT", "INT_SERVICENOW_SCHEME",
    "INT_SERVICENOW_USER", "INT_SPANNER_ADMIN_PORT",
    "INT_SPANNER_GRPC_HOST_PORT", "INT_SPANNER_GSQL_DB", "INT_SPANNER_HOST",
    "INT_SPANNER_INSTANCE", "INT_SPANNER_PG_DB", "INT_SPANNER_PORT", "INT_SPANNER_PROJECT",
    "INT_SPANNER_REST_HOST_PORT",
    "INT_TERADATA_ADMIN_PASSWORD", "INT_TERADATA_ADMIN_USER", "INT_TERADATA_DB",
    "INT_TERADATA_HOST", "INT_TERADATA_PORT",
    "INT_TERADATA_SOURCE_PASSWORD", "INT_TERADATA_SOURCE_SCHEMA", "INT_TERADATA_SOURCE_USER",
    "INT_TERADATA_TARGET_PASSWORD", "INT_TERADATA_TARGET_SCHEMA", "INT_TERADATA_TARGET_USER",
    "INT_VERTICA_ADMIN_PASSWORD", "INT_VERTICA_ADMIN_USER", "INT_VERTICA_DB", "INT_VERTICA_HOST",
    "INT_VERTICA_HOST_PORT", "INT_VERTICA_PORT", "INT_VERTICA_SOURCE_PASSWORD",
    "INT_VERTICA_SOURCE_SCHEMA", "INT_VERTICA_SOURCE_USER", "INT_VERTICA_TARGET_PASSWORD",
    "INT_VERTICA_TARGET_SCHEMA", "INT_VERTICA_TARGET_USER",
)
# Service knobs no service.yaml names: SLT_PRE_UP=0 turns off every service's pre_up hook
# (livetest.prestart, inttest.services.run_pre_up).
_SERVICE_EXTRA_KEYS = (
    "SLT_PRE_UP", "SLT_STACK_PREFIX", "INT_STACK_PREFIX",
    "SLT_STRIIM_PRIMARY_CPUS", "SLT_STRIIM_NODE_CPUS", "SLT_STRIIM_MEM_MAX",
    "SLT_STRIIM_MEM_LIMIT", "SLT_STRIIM_JAVA_SYSTEM_PROPERTIES",
    "SLT_STRIIM_HTTP_HOST_PORT", "SLT_STRIIM_HTTPS_HOST_PORT",
    "SLT_STRIIM_JMX_HOST_PORT", "SLT_STRIIM_DEBUG_HOST_PORT",
    "SLT_STRIIM_NODE_JMX_HOST_PORT", "SLT_STRIIM_AGENT_JMX_HOST_PORT",
)
SERVICE_KEYS = _LIVE_SERVICE_KEYS + _INT_SERVICE_KEYS + _SERVICE_EXTRA_KEYS
_ALIASES = {"STRIIM_PASS": ("STRIIM_PASS", "STRIIM_PASSWORD")}
# The KEYS that name locations; only these are made absolute against the .env's dir.
_PATH_KEYS = ("SLT_FRAMEWORK_HOME", "SLT_PROJECT_ROOT", "SLT_LIVE_CASES", "SLT_INT_CASES",
              "SLT_SERVICES_DIR", "SLT_INT_SERVICES_DIR", "SLT_STATE_DIR")
# The path KEYS that may name several locations (an os.pathsep list).
_LIST_PATH_KEYS = ("SLT_LIVE_CASES",)


class PathConfigError(RuntimeError):
    """A path key is set but unusable. The message names the key and where it was set."""


LICENCE_KEYS = ("COMPANY_NAME", "CLUSTER_NAME", "PRODUCT_KEY", "LICENCE_KEY")
_MACHINE_WARNED = set()


def lane_key(key):
    return (key in ("SLT_STACK_PREFIX", "INT_STACK_PREFIX", "STRIIM_URL", "CONSOLE_PORT",
                    "PYTHON", "VIRTUAL_ENV", "MAVEN_OPTS", "XDG_CACHE_HOME", "GOLD_TARGETS")
            or key.endswith(("_HOST_PORT", "_CLIENT_PORT"))
            or key in ("SLT_FRAMEWORK_HOME", "SLT_PROJECT_ROOT", "SLT_LIVE_CASES", "SLT_INT_CASES",
                       "SLT_SERVICES_DIR", "SLT_INT_SERVICES_DIR", "SLT_STATE_DIR"))


def machine_env_path(env=None):
    e = os.environ if env is None else env
    config = e.get("XDG_CONFIG_HOME") or str(Path(e.get("HOME") or Path.home()) / ".config")
    return Path(e.get("SLT_MACHINE_ENV") or str(Path(config) / "striim-test/machine.env")).expanduser()


def machine_values(env=None, allowed=None):
    allowed = allowed if allowed is not None else (*SERVICE_KEYS, *LICENCE_KEYS, "STRIIM_USER", "STRIIM_PASS", "STRIIM_PASSWORD",
                                                      "STRIIM_API_TIMEOUT")
    path = machine_env_path(env)
    try:
        mode = path.stat().st_mode
    except (FileNotFoundError, NotADirectoryError):
        return {}
    if mode & 0o077 and path not in _MACHINE_WARNED:
        _MACHINE_WARNED.add(path)
        print("[machine.env] group/world-readable; use chmod 600 on the machine settings file", file=sys.stderr)
    values = read_dotenv(path, machine=True)
    out = {}
    for key, value in values.items():
        if lane_key(key):
            print(f"[machine.env] refusing lane key {key}", file=sys.stderr)
        elif key in allowed and value.strip():
            out[key] = value
    return out


def read_dotenv(path, *, machine=False, allowed=None) -> dict:
    """KEY=VALUE pairs for KEYS and SERVICE_KEYS (all keys when machine=True); absent means {}. Accepts a BOM,
    CRLF, comments, blank lines, an `export` prefix, one level of quotes (anything after the
    closing quote, such as a comment, is dropped) and a whitespace-led ` # comment` on an
    unquoted value. No interpolation, no multi-line values."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return {}
    out = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export") and line[6:7].isspace():
            line = line[6:].lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or (not machine and key not in (allowed if allowed is not None else (*KEYS, *SERVICE_KEYS))):
            continue
        if value[:1] in ("\"", "'") and value.find(value[0], 1) > 0:
            value = value[1:value.index(value[0], 1)]
        else:
            value = re.split(r"\s#", value, maxsplit=1)[0].rstrip()
        out[key] = value
    return out


def dotenv_path(env=None) -> Path:
    """<project root>/.env, the project root taken from the process env only (never .env).
    A set SLT_PROJECT_ROOT that is not a dir raises, rather than silently hiding the .env.
    Without one, the clone .env: SLT_FRAMEWORK_DOTENV (process env only) or the checkout's own."""
    e = os.environ if env is None else env
    raw = (e.get("SLT_PROJECT_ROOT") or "").strip()
    if not raw:
        # The clone .env: the framework checkout's own, unless SLT_FRAMEWORK_DOTENV (process env only, never a .env)
        # names another file. A file that does not exist reads as empty, so naming a missing one reads none.
        clone = (e.get("SLT_FRAMEWORK_DOTENV") or "").strip()
        return Path(clone).expanduser() if clone else _default_project_root() / ".env"
    root = Path(raw).expanduser()
    if not root.is_dir():
        raise PathConfigError(f"SLT_PROJECT_ROOT={raw!r} (set in environment) does not exist: "
                              f"{root.resolve()}. Fix it, or unset SLT_PROJECT_ROOT to use the "
                              f"default {_default_project_root()}.")
    return root / ".env"


def dotenv_values(env=None) -> dict:
    """Machine file < project .env. With the live engine importable, the keys its services declare
    are read too (livetest.service_env); the integration engine alone reads its own keys."""
    try:
        from livetest import service_env
    except ImportError:
        values = machine_values(env)
        checkout = read_dotenv(dotenv_path(env))
        for group in _ALIASES.values():
            if any((checkout.get(k) or "").strip() for k in group):
                for k in group:
                    values.pop(k, None)
        values.update({k: v for k, v in checkout.items() if v.strip()})
        return values
    return service_env.values(sys.modules[__name__], env)


def _lookup(key, env=None, dotenv=None):
    """(value, source) -- the process env first, then .env; ("", None) when unset."""
    e = os.environ if env is None else env
    names = _ALIASES.get(key, (key,))
    for name in names:
        v = (e.get(name) or "").strip()
        if v:
            return v, "environment"
    d = dotenv_values(env) if dotenv is None else dotenv
    for name in names:
        v = (d.get(name) or "").strip()
        if v:
            return v, str(dotenv_path(env))
    return "", None


def setting(key, env=None, dotenv=None):
    """A non-path key (STRIIM_URL/USER/PASS), or None when unset at every layer."""
    return _lookup(key, env, dotenv)[0] or None


def effective_env(env=None, dotenv=None) -> dict:
    """The process env with .env's KEYS filled in where the process env leaves them unset.
    An alias group counts as set when any of its names is (as in setting()), and a relative
    path key from .env is made absolute against the .env's dir (as in _path())."""
    src = os.environ if env is None else env
    e = dict(src)
    d = dotenv_values(env) if dotenv is None else dotenv
    for k, v in d.items():
        group = next((g for g in _ALIASES.values() if k in g), (k,))
        if any((src.get(n) or "").strip() for n in group):
            continue
        if k in _LIST_PATH_KEYS:
            # Several locations, an os.pathsep list: each relative entry is relative to the .env.
            v = os.pathsep.join(e if not e.strip() or Path(e).expanduser().is_absolute()
                                else str(dotenv_path(env).parent / e) for e in v.split(os.pathsep))
        elif k in _PATH_KEYS and v.strip() and not Path(v).expanduser().is_absolute():
            v = str(dotenv_path(env).parent / v)
        e[k] = v
    return e


def _path(key, default: Path, env=None, dotenv=None) -> Path:
    value, source = _lookup(key, env, dotenv)
    if not value:
        return default
    p = Path(value).expanduser()
    if not p.is_absolute():
        p = (Path.cwd() if source == "environment" else Path(source).parent) / p
    p = p.resolve()
    if not p.exists():
        raise PathConfigError(f"{key}={value!r} (set in {source}) does not exist: {p}. "
                              f"Fix it, or unset {key} to use the default {default}.")
    return p
# --- end core ------------------------------------------------------------------------------

_HERE = Path(__file__).resolve()          # <scripts>/live/livetest/paths.py


def _default_project_root() -> Path:
    return _HERE.parents[3]


def framework_home(env=None, dotenv=None) -> Path:
    """The dir holding live/livetest (in a test repo that vendors it: scripts/). A repo root holding
    scripts/live/livetest is accepted and normalised to its scripts/ dir."""
    home = _path("SLT_FRAMEWORK_HOME", _HERE.parents[2], env, dotenv)
    if (home / "live" / "livetest").is_dir():
        return home
    if (home / "scripts" / "live" / "livetest").is_dir():
        return home / "scripts"
    source = _lookup("SLT_FRAMEWORK_HOME", env, dotenv)[1] or "the default"
    raise PathConfigError(f"SLT_FRAMEWORK_HOME={home} (set in {source}) "
                          f"holds neither live/livetest nor scripts/live/livetest; "
                          f"point it at the framework checkout.")


def project_root(env=None, dotenv=None) -> Path:
    return _path("SLT_PROJECT_ROOT", _default_project_root(), env, dotenv)


def live_dir(env=None, dotenv=None) -> Path:
    return framework_home(env, dotenv) / "live"


def live_cases(env=None, dotenv=None) -> Path:
    """The primary live case root: the first of live_case_roots()."""
    return live_case_roots(env, dotenv)[0]


def live_case_roots(env=None, dotenv=None) -> list[Path]:
    """Every live case root, primary first. SLT_LIVE_CASES is one path or an os.pathsep list
    (empty entries ignored); each entry resolves as a single path key does, a relative one
    against the working directory or the .env that set it. Unset, this engine's own regression/.

    Case ids must stay unambiguous across roots, so a root inside another and two roots with
    the same directory name (the name ids of a root outside the project carry) are refused."""
    default = live_dir(env, dotenv) / "regression"
    value, source = _lookup("SLT_LIVE_CASES", env, dotenv)
    if os.pathsep not in value:
        return [_path("SLT_LIVE_CASES", default, env, dotenv)]
    entries = [e.strip() for e in value.split(os.pathsep) if e.strip()]
    if not entries:
        return [default]
    roots = []
    for entry in entries:
        p = _entry_path(entry, source, default)        # as _path() resolves one value
        if p not in roots:
            roots.append(p)
    problem = case_roots_problem(roots)
    if problem:
        raise PathConfigError(f"SLT_LIVE_CASES={value!r} (set in {source}): {problem}")
    return roots


def case_roots_problem(roots) -> str | None:
    """Why ``roots`` (resolved) cannot be case roots together, or None: a root inside another
    would collect its cases twice, and ids name a root outside the project by its directory
    name, so two roots may not share one."""
    for i, a in enumerate(roots):
        for b in roots[i + 1:]:
            if a in b.parents or b in a.parents:
                return f"{b if a in b.parents else a} is inside another case root; list each case tree once."
            if a.name == b.name:
                return f"{a} and {b} have the same name {a.name!r}; case ids name a root by its directory name."
    return None


def _entry_path(entry: str, source, default: Path) -> Path:
    p = Path(entry).expanduser()
    if not p.is_absolute():
        p = (Path.cwd() if source == "environment" else Path(source).parent) / p
    p = p.resolve()
    if not p.exists():
        raise PathConfigError(f"SLT_LIVE_CASES entry {entry!r} (set in {source}) does not exist: {p}. "
                              f"Fix it, or unset SLT_LIVE_CASES to use the default {default}.")
    return p


def services_dir(env=None, dotenv=None) -> Path:
    return _path("SLT_SERVICES_DIR", live_dir(env, dotenv) / "services", env, dotenv)


def state_dir(env=None, dotenv=None) -> Path:
    return _path("SLT_STATE_DIR", live_dir(env, dotenv), env, dotenv)


def tools_python(env=None, dotenv=None) -> Path:
    return framework_home(env, dotenv).parent / "tools" / "python"
