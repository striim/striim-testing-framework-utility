"""Project manifest resolution — the C1 contract (schema v1), on top of ``livetest.paths``.

The project manifest (working filename ``gold-targets.yaml``) is the single
consumer-owned entry point. It is a PROJECT manifest, distinct from the per-case
``test.yaml`` (which ``livetest.manifest`` parses — unchanged).

Frozen rules implemented here (C1):

* Paths in the manifest resolve **relative to the manifest file's own location**
  (the consumer root), never the invocation cwd, never a machine absolute path.
* The manifest's directory is the default consumer root and, for app-only
  cases, the default asset root (a case's ``example: .`` selects the
  manifest's directory).
* Unknown top-level fields (and inside ``targets[]``/``suites``/``scan``) are
  rejected, naming the field and listing the allowed fields.
* ``schemaVersion`` mismatch with the supported major rejects before any side
  effect.
* App-only = ``targets: []`` (or absent) with at least one of ``suites.live`` /
  ``suites.integration`` present; no target may require a Maven build.
* Target kinds: ``app`` (build none), ``op``/``udf`` (build maven|prebuilt);
  ``prebuilt`` requires ``prebuilt.jar`` + ``prebuilt.sha256`` (64-hex).
* Path policy: every relative manifest path stays within the consumer root
  after symlink resolution; an escaping path or an ambiguous (duplicate) target
  name fails OFFLINE, before provisioning.
* Environment expansion is permitted ONLY in ``servicesRoots`` and
  ``stateDir`` entries (``$VAR`` / ``${VAR}``); any other field containing an
  undeclared ``$`` is rejected, not guessed. A ``livetest.paths`` key is looked up
  the way the engine looks it up (environment, then ``<project root>/.env``).
* Unset means default. An absent ``SLT_FRAMEWORK_HOME`` (in neither the environment nor
  ``.env``) is the running framework checkout. An entry that references any other unset
  variable, or any empty one, is itself unset: a ``servicesRoots`` entry is
  dropped and ``stateDir`` falls back to the ``livetest.paths`` default.
* Locating the manifest: explicit path > ``GOLD_TARGETS`` env. There is no
  implicit filesystem search. Neither given means no project manifest, and every
  location takes its ``livetest.paths`` default. ``GOLD_TARGETS`` set to a missing
  file raises ``paths.PathConfigError`` naming it.

``apply_project()`` is the only side effect: it feeds the manifest's
``servicesRoots``/``stateDir`` into ``livetest.layout`` (idempotent,
call-time). Engine selection is a SEPARATE contract — this module never
chooses an engine.
"""
from __future__ import annotations

import dataclasses
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from livetest import layout as _layout
from livetest import paths as _paths
from livetest.layout import LayoutError

SCHEMA_VERSION = 1
MANIFEST_FILENAME = "gold-targets.yaml"
ENV_TARGETS = "GOLD_TARGETS"

_ALLOWED_TOP = ("schemaVersion", "framework", "targets", "suites", "servicesRoots",
                "stateDir", "scan", "runners")
_ALLOWED_TARGET = ("name", "kind", "path", "build", "prebuilt")
_ALLOWED_PREBUILT = ("jar", "sha256")
_ALLOWED_SUITE = ("unit", "integration", "live", "perf")
_ALLOWED_SCAN = ("moduleRoots",)
_ALLOWED_FRAMEWORK = ("mode", "wheel", "lock", "sibling")
_SUITE_TIERS = {"unit", "integration", "live", "perf"}
_TARGET_KINDS = {"app": "none", "op": ("maven", "prebuilt"), "udf": ("maven", "prebuilt")}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
_EXPANSION_FIELDS = ("servicesRoots", "stateDir")


class ProjectError(Exception):
    """C1 validation / resolution failure. Offline: raised before provisioning."""


@dataclass(frozen=True)
class Target:
    name: str
    kind: str
    path: Path | None = None
    build: str = "none"
    prebuilt_jar: Path | None = None
    prebuilt_sha256: str | None = None


@dataclass(frozen=True)
class Project:
    manifest: Path            # absolute manifest path
    root: Path                # consumer root = manifest's directory (resolved)
    targets: tuple[Target, ...] = ()
    suites: dict = field(default_factory=dict)   # tier -> Path (resolved): the primary root
    extra_suites: dict = field(default_factory=dict)   # tier -> tuple[Path, ...]: further roots (live only)
    services_roots: tuple[Path, ...] = ()        # consumer service roots (ordered)
    state_dir: Path | None = None                # explicit writable state (resolved)
    module_roots: tuple[Path, ...] = ()
    runners: tuple[Path, ...] = ()               # resolved runner.yaml paths (C2 files)
    framework: dict = field(default_factory=dict)


def _reject_unknown(mapping: dict, allowed: tuple[str, ...], where: str) -> None:
    unknown = sorted(set(mapping) - set(allowed))
    if unknown:
        raise ProjectError(
            f"{where}: unknown field(s) {unknown}; allowed: {list(allowed)}")


def _var(name: str) -> str:
    """A referenced variable's value, "" when unset or empty. livetest.paths keys resolve as
    the engine resolves them (environment, then <project root>/.env)."""
    if name in _paths.KEYS:
        return _paths._lookup(name)[0]
    return (os.environ.get(name) or "").strip()


def _absent(name: str, env=None) -> bool:
    """True when ``name`` is set nowhere: not in the environment (or ``env``) and, for the
    environment, not in <project root>/.env either. An empty value is set, not absent."""
    if env is not None:
        return name not in env
    return name not in os.environ and name not in _paths.dotenv_values()


def _expand_env(value: str, env=None) -> str | None:
    """Expand $VAR/${VAR}. An absent SLT_FRAMEWORK_HOME is the checkout this module runs from.
    None when any referenced variable is unset or empty (SLT_FRAMEWORK_HOME set to an empty
    value included): the entry is then unset, and the location falls back to its default."""
    names = [m.group(1) or m.group(2) for m in _ENV_RE.finditer(value)]
    values = {n: (env.get(n) or "").strip() if env is not None else _var(n) for n in names}
    if "SLT_FRAMEWORK_HOME" in values and _absent("SLT_FRAMEWORK_HOME", env):
        values["SLT_FRAMEWORK_HOME"] = str(_paths._HERE.parents[3])    # the running checkout
    if not all(values.values()):
        return None
    return _ENV_RE.sub(lambda m: values[m.group(1) or m.group(2)], value)


def _manifest_relative(p: str, root: Path, where: str) -> Path:
    """Resolve a manifest path against the consumer root; refuse absolute paths
    and anything escaping the root after symlink resolution (C1 path policy)."""
    rp = Path(p)
    if rp.is_absolute():
        raise ProjectError(f"{where}: {p!r} must be manifest-relative, not absolute")
    cand = (root / rp)
    root_r = root.resolve()
    cand_r = cand.resolve()
    if not (cand_r == root_r or root_r in cand_r.parents):
        raise ProjectError(
            f"{where}: {p!r} escapes the consumer root {str(root_r)!r} "
            f"(resolved to {str(cand_r)!r})")
    return cand_r


def _state_path(value: str, root: Path, where: str) -> Path:
    """C1 stateDir: manifest-relative (contained to the consumer root by the
    path policy) or absolute (a machine state location). An absolute stateDir
    inside an install tree is refused — writable state never lives in
    site-packages (C6)."""
    rp = Path(value)
    if rp.is_absolute():
        resolved = rp.resolve()
        if any(part in ("site-packages", "dist-packages") for part in resolved.parts):
            raise ProjectError(
                f"{where}: stateDir {value!r} is inside an install tree; writable "
                f"state must live outside site-packages")
        return resolved
    return _manifest_relative(value, root, where)


def _check_expansion_only_where_allowed(raw: dict) -> None:
    """A '$' in any field that is not an expansion field is undeclared
    interpolation — reject instead of guessing a private path."""
    def _walk(value, where):
        if isinstance(value, str):
            if "$" in value:
                raise ProjectError(
                    f"{where}: environment expansion is only permitted in "
                    f"{list(_EXPANSION_FIELDS)}; got {value!r}")
        elif isinstance(value, dict):
            for k, v in value.items():
                _walk(v, f"{where}.{k}")
        elif isinstance(value, list):
            for i, v in enumerate(value):
                _walk(v, f"{where}[{i}]")
    for key, value in raw.items():
        if key in _EXPANSION_FIELDS:
            continue
        if key == "suites" and isinstance(value, dict) and isinstance(value.get("live"), list):
            # suites.live's further roots may expand (a shared case tree's machine location).
            value = {**value, "live": value["live"][:1]}
        _walk(value, f"field {key!r}")


def load_project(manifest: str | os.PathLike | None = None, *, env=None) -> Project | None:
    """Load and validate the C1 project manifest.

    Location: explicit ``manifest`` > ``$GOLD_TARGETS``. No implicit search. None when
    neither names one (no project: every location takes its default). Raises ProjectError
    (offline) on any schema/path/ambiguity violation, and paths.PathConfigError when
    ``GOLD_TARGETS`` names a missing file.
    """
    if manifest is None:
        raw_env = (os.environ.get(ENV_TARGETS) or "").strip()
        if not raw_env:
            return None
        mpath = Path(raw_env).expanduser()
        if not mpath.is_file():
            raise _paths.PathConfigError(
                f"{ENV_TARGETS}={raw_env!r} (set in environment) does not exist: "
                f"{mpath.resolve()}. Fix it, or unset {ENV_TARGETS} to run without a "
                f"project manifest.")
    else:
        mpath = Path(manifest).expanduser()
    if not mpath.is_file():
        raise ProjectError(f"project manifest not found: {mpath}")
    try:
        raw = yaml.safe_load(mpath.read_text()) or {}
    except yaml.YAMLError as e:
        raise ProjectError(f"{mpath}: invalid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise ProjectError(f"{mpath}: top level must be a mapping")

    _reject_unknown(raw, _ALLOWED_TOP, f"manifest {mpath}")
    _check_expansion_only_where_allowed(raw)

    sv = raw.get("schemaVersion")
    if sv is None:
        raise ProjectError(
            f"manifest {mpath}: schemaVersion is required (supported: {SCHEMA_VERSION})")
    if isinstance(sv, bool) or not isinstance(sv, int) or sv != SCHEMA_VERSION:
        raise ProjectError(
            f"manifest {mpath}: unsupported schemaVersion {sv!r} (supported: {SCHEMA_VERSION})")

    root = mpath.resolve().parent

    fw_raw = raw.get("framework", {}) or {}
    _reject_unknown(fw_raw, _ALLOWED_FRAMEWORK, f"manifest {mpath} framework")
    fw_mode = fw_raw.get("mode")
    if fw_mode is not None and fw_mode not in ("legacy", "sibling", "wheel"):
        raise ProjectError(
            f"manifest {mpath}: framework.mode must be legacy|sibling|wheel, got {fw_mode!r}")
    if fw_mode == "wheel" and not (fw_raw.get("wheel") and fw_raw.get("lock")):
        raise ProjectError(
            f"manifest {mpath}: framework.mode=wheel requires framework.wheel AND framework.lock")

    # ---- targets ----------------------------------------------------------
    targets_raw = raw.get("targets") or []
    if not isinstance(targets_raw, list):
        raise ProjectError(f"manifest {mpath}: targets must be a list")
    targets: list[Target] = []
    seen_names: set[str] = set()
    for i, t in enumerate(targets_raw):
        where = f"targets[{i}]"
        if not isinstance(t, dict):
            raise ProjectError(f"manifest {mpath}: {where} must be a mapping")
        _reject_unknown(t, _ALLOWED_TARGET, f"manifest {mpath} {where}")
        name = t.get("name")
        if not name or not isinstance(name, str):
            raise ProjectError(f"manifest {mpath}: {where}.name is required (string)")
        if name in seen_names:
            raise ProjectError(
                f"manifest {mpath}: ambiguous target name {name!r} (declared more than "
                f"once); target names must be unique")
        seen_names.add(name)
        kind = t.get("kind")
        if kind not in _TARGET_KINDS:
            raise ProjectError(
                f"manifest {mpath}: {where}.kind must be one of {sorted(_TARGET_KINDS)}, got {kind!r}")
        allowed_builds = _TARGET_KINDS[kind]
        if kind == "app":
            build = t.get("build", "none")
            allowed = ("none",)
        else:
            if not t.get("build"):
                raise ProjectError(
                    f"manifest {mpath}: {where}.build is required for kind {kind!r} "
                    f"(explicit over implicit: {list(allowed_builds)})")
            build = t["build"]
            allowed = allowed_builds
        if build not in allowed:
            raise ProjectError(
                f"manifest {mpath}: {where}.build {build!r} not allowed for kind {kind!r} "
                f"(allowed: {list(allowed)})")
        tpath = None
        if kind in ("op", "udf"):
            if not t.get("path"):
                raise ProjectError(f"manifest {mpath}: {where}.path is required for kind {kind!r}")
            tpath = _manifest_relative(t["path"], root, f"manifest {mpath} {where}.path")
        if build == "prebuilt":
            pb = t.get("prebuilt")
            if not isinstance(pb, dict):
                raise ProjectError(
                    f"manifest {mpath}: {where} build=prebuilt requires a prebuilt mapping")
            _reject_unknown(pb, _ALLOWED_PREBUILT, f"manifest {mpath} {where}.prebuilt")
            jar = pb.get("jar")
            sha = pb.get("sha256")
            if not jar:
                raise ProjectError(f"manifest {mpath}: {where}.prebuilt.jar is required")
            if not sha or not _SHA256_RE.match(str(sha)):
                raise ProjectError(
                    f"manifest {mpath}: {where}.prebuilt.sha256 must be 64 lowercase hex chars")
            targets.append(Target(name, kind, tpath, build,
                                  _manifest_relative(jar, root, f"manifest {mpath} {where}.prebuilt.jar"),
                                  str(sha)))
        else:
            targets.append(Target(name, kind, tpath, build))

    # ---- suites -----------------------------------------------------------
    suites_raw = raw.get("suites") or {}
    if not isinstance(suites_raw, dict):
        raise ProjectError(f"manifest {mpath}: suites must be a mapping")
    _reject_unknown(suites_raw, _ALLOWED_SUITE, f"manifest {mpath} suites")
    suites: dict[str, Path] = {}
    extra_suites: dict[str, tuple] = {}
    for tier, p in suites_raw.items():
        if tier == "live" and isinstance(p, list):
            # Several live case roots: the first manifest-relative (the primary root), further
            # ones manifest-relative or absolute, with $VAR expansion; an unset variable drops one (SLT_FRAMEWORK_HOME: see _expand_env).
            if not p or not isinstance(p[0], str) or not p[0]:
                raise ProjectError(f"manifest {mpath}: suites.live[0] must be a manifest-relative path string")
            extra = []
            for i, entry in enumerate(p[1:], 1):
                if not isinstance(entry, str) or not entry:
                    raise ProjectError(f"manifest {mpath}: suites.live[{i}] must be a path string")
                expanded = _expand_env(entry, env)
                if expanded is None:
                    continue
                rp = Path(expanded)
                extra.append(rp.resolve() if rp.is_absolute()
                             else _manifest_relative(expanded, root, f"manifest {mpath} suites.live[{i}]"))
            suites[tier] = _manifest_relative(p[0], root, f"manifest {mpath} suites.live[0]")
            problem = _paths.case_roots_problem([suites[tier], *extra])
            if problem:
                raise ProjectError(f"manifest {mpath}: suites.live: {problem}")
            extra_suites[tier] = tuple(extra)
            continue
        if not isinstance(p, str) or not p:
            raise ProjectError(
                f"manifest {mpath}: suites.{tier} must be a manifest-relative path string "
                f"(got {p!r}); a registry tri-state value (true/false/null) must be "
                f"preserved by the private adapter, never coerced into a manifest path")
        suites[tier] = _manifest_relative(p, root, f"manifest {mpath} suites.{tier}")

    if not targets and "live" not in suites and "integration" not in suites:
        raise ProjectError(
            f"manifest {mpath}: app-only projects (targets: []) require at least one of "
            f"suites.live / suites.integration")

    # ---- servicesRoots / stateDir (expansion allowed) ----------------------
    # C1 schema: these two fields accept manifest-relative OR absolute values
    # (env expansion typically yields a machine path). Relative values are
    # contained to the consumer root by the path policy; absolute values are
    # the consumer's explicit machine choice (stateDir must stay out of an
    # install tree — that is the C6 "never in site-packages" rule).
    services_roots: list[Path] = []
    for i, entry in enumerate(raw.get("servicesRoots") or []):
        if not isinstance(entry, str):
            raise ProjectError(f"manifest {mpath}: servicesRoots[{i}] must be a string")
        expanded = _expand_env(entry, env)
        if expanded is None:
            continue    # unset -> no consumer root; the base services dir still applies
        where = f"manifest {mpath} servicesRoots[{i}]"
        rp = Path(expanded)
        services_roots.append(rp.resolve() if rp.is_absolute()
                              else _manifest_relative(expanded, root, where))
    state_dir = None
    if raw.get("stateDir") is not None:
        sd = raw["stateDir"]
        if not isinstance(sd, str):
            raise ProjectError(f"manifest {mpath}: stateDir must be a string (single path)")
        expanded = _expand_env(sd, env)
        if expanded is not None:    # unset -> the livetest.paths state dir
            state_dir = _state_path(expanded, root, f"manifest {mpath} stateDir")

    # ---- scan / runners ----------------------------------------------------
    scan_raw = raw.get("scan") or {}
    _reject_unknown(scan_raw, _ALLOWED_SCAN, f"manifest {mpath} scan")
    module_roots = tuple(_manifest_relative(p, root, f"manifest {mpath} scan.moduleRoots[{i}]")
                         for i, p in enumerate(scan_raw.get("moduleRoots") or []))
    runners = tuple(_manifest_relative(p, root, f"manifest {mpath} runners[{i}]")
                    for i, p in enumerate(raw.get("runners") or []))

    return Project(manifest=mpath.resolve(), root=root, targets=tuple(targets),
                   suites=suites, extra_suites=extra_suites, services_roots=tuple(services_roots),
                   state_dir=state_dir, module_roots=module_roots, runners=runners,
                   framework=dict(fw_raw))


def apply_project(project: Project) -> None:
    """Feed the manifest's servicesRoots/stateDir into livetest.layout (the ONLY
    side effect; idempotent, call-time). The values go into the MANIFEST slot
    (``layout.set_manifest_roots``), which C6 resolves below an explicit
    programmatic ``set_roots(...)`` and above env — and which is REPLACED on
    every activation (an axis the manifest omits is cleared), so a previous
    consumer's roots never persist. The base services dir is never
    configured (layout refuses it). Engine selection is a separate contract and is
    NOT touched here."""
    from livetest import layout
    # layout refuses a configured root that expands to the base services dir; drop it
    # rather than failing a manifest that names the consumer's own base tree.
    try:
        base = layout.builtin_services().resolve()
    except Exception:
        base = None
    services = [r for r in project.services_roots
                if base is None or layout._services_dir_for(r.resolve()).resolve() != base]
    # ALWAYS called (never skipped): replaces both manifest axes so a manifest
    # with neither servicesRoots nor stateDir clears a previous consumer's.
    layout.set_manifest_roots(services=services, state=project.state_dir)


def resolve_in_project(project: Project, rel: str) -> Path:
    """Resolve a manifest-relative path within the project, applying the C1 path
    policy (escaping/absolute rejected). The returned path is resolved."""
    return _manifest_relative(rel, project.root, f"project {project.manifest}")


def asset_root(project: Project, example: str | None) -> Path:
    """C1 legacy ``example`` mapping: ``example: .`` (or a manifest-relative
    subdir) selects the consumer root / the mapped subdir as the asset root.
    Never the invocation cwd."""
    if example is None:
        return project.root
    if example in (".", ""):
        return project.root
    return resolve_in_project(project, example)


def example_root(example: str | None = None) -> Path:
    """Call-time asset root for the legacy ``example:`` field of a case manifest
    (livetest.manifest source_dir arithmetic).

    * project loaded (``_ACTIVE``) -> consumer mapping (asset_root above).
    * no project -> ``paths.project_root()`` (``SLT_PROJECT_ROOT``, else the repo
      root three levels above the livetest package, as the old ``_REPO`` constant).
    """
    if _ACTIVE is not None:
        return asset_root(_ACTIVE, example)
    repo = _paths.project_root()
    if example is None:
        return repo
    cand = (repo / example).resolve()
    if not (cand == repo or repo in cand.parents):
        raise LayoutError(
            f"legacy example {example!r} escapes the project root {repo}")
    return cand


def load_and_activate(manifest: str | os.PathLike | None = None) -> Project | None:
    """Load, validate, and activate a project (roots wired + example mapping in
    effect for this process). Repeated calls replace the active project; two
    consumer roots exercised sequentially in one process never persist the
    first (roots config is re-applied on every activation). With no manifest
    named, any previous project is deactivated and None is returned."""
    global _ACTIVE
    project = load_project(manifest)
    if project is None:
        _layout.set_manifest_roots(services=(), state=None)
    else:
        apply_project(project)
    _ACTIVE = project
    return project


def identity(project: Project | None = None) -> dict:
    """Resolved identity record (evidence): interpreter, module
    origins, resource roots, consumer root, state root. Never includes
    credentials or full secret-bearing environment values (only names)."""
    import sys
    import livetest
    p = project if project is not None else _ACTIVE
    env_names = sorted(n for n in os.environ
                       if n.startswith(("SLT_", "INT_", "GOLD_")))
    return {
        "interpreter": sys.executable,
        "livetest_origin": str(Path(livetest.__file__).resolve()),
        "consumer_root": str(p.root) if p is not None else None,
        "manifest": str(p.manifest) if p is not None else None,
        "services_roots": [str(r) for r in _layout.services_roots()],
        "state_dir": str(_layout.state_dir()) if _has_writable_state() else None,
        "env_names": env_names,
    }


def _has_writable_state() -> bool:
    try:
        _layout.state_dir()
        return True
    except (_layout.LayoutError, _paths.PathConfigError):
        return False


_ACTIVE: Project | None = None
