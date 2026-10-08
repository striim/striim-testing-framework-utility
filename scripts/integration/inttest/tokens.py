"""`${TOKEN}` substitution engine + per-test token table assembly (docs/INTEGRATION-TESTS.md).

Consolidates the token-engine pieces prototyped inline in `inttest/plugin.py`
(`_TOKEN_RE`, `render`, `SubstitutionError`, `missing_tokens`, `_parallel`, and the
`_postgres_tokens`/`_oracle_tokens`/`_spanner_tokens` os.getenv precedence) into one
place, generalized to read the *real* `services/<name>/service.yaml` shape on disk
instead of hardcoding one dict of token names per service.

Pure Python: no Docker, no live services, no jar building. Reading `service.yaml` files
off disk is just parsing -- it never talks to a running container. `pytest` is never
imported here (top-level or otherwise) so this module stays usable from plain scripts
and unit tests alike; the pytest-side parallel-flag plumbing lives in `plugin.py`,
which calls `isolation_tokens()`/`build_tokens()` with a plain bool.

Service `provides:` resolution, adapted from `scripts/live/livetest/services.py`
(`resolve()`) + `scripts/live/livetest/plugin.py` (`build_service_tokens()`): each
`services/<name>/service.yaml` declares

    docker_defaults: {key: default, ...}   # flat keys, e.g. host/port/source_user/...
    live_env:        {key: ENV_VAR_NAME}   # per-key env override name
    provides:        {TOKEN_NAME: "{key}-style format template", ...}

`provides:` values are Python `str.format()` templates over the resolved
`docker_defaults`/`live_env` keys (e.g. `ORACLE_URL: "jdbc:oracle:thin:@//{view_host}:{port}/{service}"`),
not over the published token names themselves -- so token assembly here is a two-step
resolve-then-format, matching live's `resolve()` + `build_service_tokens()` split, not the
one-token-per-os.getenv-call shape `plugin.py`'s stub used inline. The **precedence** is
still the stub's: an env var override wins over `docker_defaults`, always -- this module
resolves that override per-key using the *actual* env var name each service.yaml declares
in `live_env` (e.g. `INT_PG_HOST`), rather than inventing one named after the published
token (there is no `INT_POSTGRES_URL` to override, because `POSTGRES_URL` is a template,
not a key).

Deliberately NOT implemented (out of scope for this slice, and for pure token assembly):
the full docker-vs-live *mode* switch keyed on `live_override_env` (e.g. reusing an
already-running external Postgres wholesale when `INT_PG_HOST` is set) -- that is a
later-phase broker concern per `plugin.py`'s module docstring (`INT_SHARED_SERVICES`).
Here every `docker_defaults` key is independently env-overridable via its `live_env`
name, which is sufficient for a test (or a developer's shell) to redirect one field
without a live broker owning container lifecycle. `view_host` (the host the Striim
*app* would use to reach the service) is always the same as `host`: this tier has no
containerized Striim -- `IntegrationProcessor` runs as a local subprocess against the
docker-published ports -- so there is no separate docker-internal hostname to model.
"""
from __future__ import annotations

import os
import re
import secrets
from pathlib import Path
from typing import Mapping


try:
    import yaml
except ImportError:  # pragma: no cover - pyyaml is a declared dependency (pyproject.toml)
    yaml = None


# ============================================================================
# 1. Substitution engine
# ============================================================================

_TOKEN_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class SubstitutionError(Exception):
    """Raised by `render()` when one or more `${TOKEN}` placeholders have no value.
    Every unresolved token is named in the message -- no silent passthrough (SPEC §6)."""


class ServiceConfigError(Exception):
    """Raised when a required service's `service.yaml` is missing/unreadable, or its
    `provides:` template references a key `docker_defaults`/`live_env` didn't resolve."""


def missing_tokens(template: str, tokens: Mapping[str, str]) -> set[str]:
    """Every `${NAME}` in `template` whose NAME is not a key of `tokens`."""
    return {m.group(1) for m in _TOKEN_RE.finditer(template) if m.group(1) not in tokens}


def render(template: str, tokens: Mapping[str, str]) -> str:
    """Substitute every `${NAME}` in `template` from `tokens`. Raises SubstitutionError
    naming EVERY unresolved token (sorted, comma-joined) if any are missing."""
    missing = missing_tokens(template, tokens)
    if missing:
        raise SubstitutionError(
            f"missing token value(s): {', '.join(sorted(missing))}"
        )
    return _TOKEN_RE.sub(lambda m: str(tokens[m.group(1)]), template)


# ============================================================================
# 2. ${TID} / ${TID_ORACLE} isolation tokens
# ============================================================================


def _random_id() -> str:
    """9 lowercase hex chars, fresh every call via `secrets` (cryptographic
    strength is overkill here, but it's stdlib and free of the weak-default-seed
    footguns of `random`) -- same length as scripts/live/livetest/plugin.py's
    `_tid_oracle` ("T" + 9 hex chars), so ${TID}/${TID_ORACLE} have the same
    shape in both harnesses, just random here instead of hashed-from-name."""
    return secrets.token_hex(5)[:9]


def isolation_tokens(*, parallel: bool) -> dict[str, str]:
    """${TID} / ${TID_ORACLE} per SPEC §6 / §13: empty in a serial run, a short
    RANDOM id with a trailing "_" separator in a parallel run -- fresh every call,
    NOT derived from the test's own name/nodeid (the original design, since
    replaced: isolation only needs two DIFFERENT concurrent tests/runs to get
    DIFFERENT values, which random already guarantees without a name's length
    coming along for the ride). A name-derived id has no natural length bound --
    a nested regression/ nodeid slugifies to 100+ characters -- which blows past
    Spanner's 128-byte (GoogleSQL) / 63-byte (PostgreSQL-dialect) identifier
    limits the instant it prefixes a real table name; ${TID_ORACLE} avoided this
    historically only because it hashed the name down first, but ${TID} itself
    never did, and nothing about EITHER token actually needs to be reproducible
    across separate runs (a failed run's leftover resources are already kept
    for manual debugging, never auto-matched by a later run's prefix-drop
    teardown).

    Deliberately takes no arguments but `parallel` -- this module must stay
    usable outside pytest (plain scripts, unit tests); the pytest-side
    `_parallel(os.environ)` -> `parallel` flag plumbing belongs to plugin.py.
    ${TID} and ${TID_ORACLE} are the SAME random id, just cased differently --
    lowercase "t"-led for ${TID} (Postgres/Spanner's lowercase-friendly
    identifier conventions), uppercase "T"-led for ${TID_ORACLE} (matching
    Oracle's own unquoted-identifier uppercase fold) -- one `secrets` call, not
    two independent ones, since there's no reason for a single test's two
    isolation tokens to carry unrelated random bodies.
    """
    if not parallel:
        return {"TID": "", "TID_ORACLE": ""}
    rid = _random_id()
    return {"TID": "t" + rid + "_", "TID_ORACLE": "T" + rid.upper() + "_"}


# ============================================================================
# 3. Service `provides:` token resolution
# ============================================================================


def _load_service_def(name: str, services_dir: Path) -> dict:
    """Load `services/<name>/service.yaml` as a raw dict. Raises ServiceConfigError
    (never silently falls back) if it's missing, unreadable, or malformed -- a required
    service with no service.yaml is a hard error in this slice; see module docstring."""
    if yaml is None:
        raise ServiceConfigError(
            f"service {name!r}: PyYAML is not installed; cannot load its service.yaml"
        )
    path = services_dir / name / "service.yaml"
    if not path.exists():
        raise ServiceConfigError(f"service {name!r}: no service.yaml found at {path}")
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ServiceConfigError(f"service {name!r}: could not parse {path}: {e}") from e
    if not isinstance(raw, dict):
        raise ServiceConfigError(f"service {name!r}: {path} must be a YAML mapping")
    from livetest.registry import hook_policy, RegistryError
    try:
        raw.update(hook_policy(raw, path))
    except RegistryError as e:
        raise ServiceConfigError(str(e)) from e
    return raw


def _resolve_service_base(name: str, raw: dict, env: Mapping[str, str]) -> dict:
    """Merge `docker_defaults` with per-key env overrides named in `docker_env` and
    `live_env`, then add `view_host`. Precedence per key: `env[live_env[key]]` >
    `env[docker_env[key]]` > `docker_defaults[key]` -- the same "env override first"
    precedence `plugin.py`'s stub used, generalized to the real per-key env-var names
    each service.yaml declares (see module docstring for why the full
    `live_override_env` mode switch is out of scope here).

    `docker_env` names the SAME variables `compose.yaml` interpolates for its published
    host ports (`INT_PG_HOST_PORT` and friends), so remapping a busy port -- or standing
    up a second stack from another checkout -- moves the container AND the client that
    dials it. Without it the two halves drift silently: the container publishes on
    :15532 while every `requires: [postgres]` test keeps dialing docker_defaults' :15432,
    which either hangs on a refused socket or, worse, quietly drives the OTHER stack.
    Ported from `scripts/live/livetest/services.py::resolve`, where the identical gap was
    found and fixed first; `tests/test_services.py` carries the guard that keeps every
    service declaring one.
    """
    from livetest.service_env import require_env, RegistryError
    try:
        require_env(name, raw.get("required_env") or (), env)
    except RegistryError as exc:
        raise ServiceConfigError(str(exc)) from exc
    base = dict(raw.get("docker_defaults") or {})
    for key, envname in (raw.get("docker_env") or {}).items():
        override = (env.get(envname) or "").strip()
        if override:
            base[key] = override
    gate = raw.get("live_override_env")
    if gate and env.get(gate):
        # The existing instance is used: its own fallbacks (live_defaults) replace the container's,
        # a remapped container port included; an explicit live_env setting below still wins.
        base.update(raw.get("live_defaults") or {})
    live_env = raw.get("live_env") or {}
    for key, envname in live_env.items():
        if env.get(envname):
            base[key] = env[envname]
        # Unset, with no default (an OAuth client in a basic-auth case): its token renders empty,
        # as in the live tier, instead of failing the case on a setting it does not use.
        base.setdefault(key, "")
    # No containerized Striim in this tier -- the app-visible host is always the same
    # host the harness itself connects to.
    base.setdefault("view_host", base.get("host", "localhost"))
    return base


def service_tokens(name: str, env: Mapping[str, str] | None = None,
                   services_dir: Path | None = None) -> dict[str, str]:
    """One service's `provides:` token map, for callers outside the `requires:` pipeline.

    Public because `inttest.cli` needs it: reaching into `plugin.py` for the same values
    drags `pytest` in, which is a `[dev]`-only extra -- so `start postgres` (and therefore
    every `start integration`, whose default set always contains postgres) died
    with ModuleNotFoundError in a runtime-only checkout, the exact case the lazy import in
    `cli._postgres_setup` was written to protect."""
    return _service_tokens(name, os.environ if env is None else env,
                           _services_root(name, services_dir))


def _services_root(name: str, services_dir) -> Path:
    """The services root `name`'s definition is read from, at call time. An explicit
    `services_dir` is a deliberate single origin; otherwise the integration profile seam
    (SLT_INT_SERVICES_DIR, else scripts/integration/services), never an import-time constant."""
    if services_dir is not None:
        return Path(services_dir)
    from inttest import resources as _resources
    return _resources.service_dir(name).parent


def _checked_profile(name: str, services_dir: Path) -> None:
    """Tokens are published only for a COMPLETE profile, checked in the same origin the
    definition is read from (explicit services_dir or the call-time default root)."""
    from inttest import resources as _resources
    try:
        _resources.select_profile(name, services_dir=services_dir)
    except _resources.ResourceError as e:
        raise ServiceConfigError(f"service {name!r}: {e}") from e


def _service_tokens(name: str, env: Mapping[str, str], services_dir: Path) -> dict[str, str]:
    """The resolved `${TOKEN}` -> value map a single `requires:` entry publishes, per
    its `service.yaml`'s `provides:` templates (SPEC §6)."""
    raw = _load_service_def(name, services_dir)
    _checked_profile(name, services_dir)
    base = _resolve_service_base(name, raw, env)
    provides = raw.get("provides") or {}
    try:
        return {token: str(template).format(**base) for token, template in provides.items()}
    except (KeyError, IndexError) as e:
        raise ServiceConfigError(
            f"service {name!r}: provides template references unknown key: {e}"
        ) from e


# ============================================================================
# 4. Per-test token table assembly
# ============================================================================


def build_tokens(
    test_dir,
    requires=(),
    *,
    parallel: bool = False,
    env: Mapping[str, str] = os.environ,
    services_dir: Path | None = None,
) -> dict[str, str]:
    """Assemble the full per-test token table (SPEC §6):

    - `${TEST_DIR}`: absolute path of `test_dir`.
    - `${TID}` / `${TID_ORACLE}`: `isolation_tokens(parallel=parallel)`.
    - every `requires:` service's `provides:` tokens (merged in list order; a later
      service's token of the same name wins, matching dict.update semantics -- SPEC
      does not define a collision policy and none of postgres/oracle/spanner today
      publish overlapping names).

    `parallel` is a plain value, not a pytest fixture -- see `isolation_tokens`.
    `env` defaults to `os.environ` (live, not a snapshot) so a var set after import is
    still honored; pass an explicit dict in tests for determinism.
    """
    if isinstance(requires, str):
        requires = [requires]
    services_dir = Path(services_dir) if services_dir is not None else None

    tokens: dict[str, str] = {"TEST_DIR": str(Path(test_dir).resolve())}
    tokens.update(isolation_tokens(parallel=parallel))
    for name in requires:
        tokens.update(_service_tokens(name, env, _services_root(name, services_dir)))
    return tokens
