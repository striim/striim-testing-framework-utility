"""Tier service-resource profiles: selection, preflight, materialization.

One seam for BOTH tiers (``inttest.resources`` is the thin integration facade).
Stdlib-only at import time — PyYAML and filelock are imported where they are used — so
the integration facade can load this file by path when the integration suite runs
without ``livetest`` importable.

* :func:`select_profile` resolves ONE whole profile from an ordered root list. A name
  found under more than one root is a collision, refused unless ``override=True``
  (C6 first-hit). ``services_dir`` is the deliberate single-origin override. Every
  dependent file resolves from the SAME origin: an asset missing from the selected
  origin is a named failure, never a fall-through to another root.
* :func:`check_profile` is the preflight. It follows service.yaml ``compose:``, compose
  ``build`` contexts/Dockerfiles, relative bind mounts and ``env_file``s, and Dockerfile
  COPY/ADD and bind-mount sources, naming tier/service/path for anything missing.
  Consumer-supplied build inputs (the striim recipe's ``deps/``, and the exact COPY sources
  in ``CONSUMER_SUPPLIED_SOURCES``) are external, not assets.
* :func:`materialize_profile` copies a profile's exact asset set into an owned state
  workspace keyed by tier / stack prefix / content hash — atomic, mode-preserving. The
  origin (possibly a read-only installed package) is never written.

Tier identities are never merged or aliased: live ``mssql`` and integration
``sqlserver`` are different profiles found under different root lists.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import posixpath
import re
import shlex
import shutil
import stat as _stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

TIERS = ("live", "integration")

KIND_SERVICE = "service"                 # has service.yaml
KIND_CLUSTER_RECIPE = "cluster-recipe"   # compose files only (live striim), provisioned elsewhere
KIND_MISSING_DEFINITION = "missing-definition"   # compose files but no service.yaml: incomplete

# The ONLY ratified cluster recipes: the live striim cluster is provisioned by
# striim_provision, not the service registry. Every other profile, in both tiers, requires
# service.yaml -- deleting it never turns a service into a recipe.
_CLUSTER_RECIPES = frozenset({("live", "striim")})

# The integration tier's lifecycle and cleanup address `<service>/compose.yaml` directly, so
# any other declared compose filename is refused at preflight (no census profile declares one).
_DEFAULT_COMPOSE = "compose.yaml"

# Runtime downloads, caches, bytecode and logs are never profile assets (census policy), nor
# are the folder-view files macOS Finder writes into any directory it opens (the repo's
# .gitignore ignores them too).
_EXCLUDED_DIRS = frozenset({"deps", "__pycache__"})
_EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".log", ".DS_Store")

# Build inputs a Dockerfile bind-mounts that the CONSUMER supplies (verified through SLT_STRIIM_DEPS_MANIFEST and staged into the owned workspace).
_EXTERNAL_BUILD_INPUTS = frozenset({"deps"})

# The rule for deps/ applied to single files: COPY sources an image's Dockerfile names that the
# CONSUMER supplies (a licensed binary its repo never tracks). They are declared COPY sources, not
# missing assets. Exact origin-relative paths per ratified (tier, name), never a pattern: any other
# absent COPY source is still a named preflight failure. The census never ships them. None today.
CONSUMER_SUPPLIED_SOURCES: dict = {}

_PREFIX_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_GLOB_CHARS = set("*?[")


class ProfileError(Exception):
    pass


@dataclass(frozen=True)
class Profile:
    tier: str
    name: str
    kind: str
    origin: Path            # the selected service directory: the ONE origin of every asset
    root: Path              # the services root it was found under
    definition_hash: str    # content_hash(assets)
    assets: tuple           # sorted (relpath, mode, sha256) of every file in the origin

    def file(self, rel: str) -> Path:
        """An asset of THIS profile's origin. Never falls back to another root."""
        rel = posixpath.normpath(str(rel).replace(os.sep, "/"))
        if not any(a[0] == rel for a in self.assets):
            raise ProfileError(
                f"{self.tier}/{self.name}: {rel} is not an asset of the selected origin "
                f"{self.origin}")
        return self.origin / rel


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

def _within(path: Path, base: Path) -> bool:
    return path == base or base in path.parents


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def content_hash(assets) -> str:
    """SHA-256 over sorted (relpath, mode, sha256) triples (same shape as roots.content_hash)."""
    acc = hashlib.sha256()
    for rel, mode, h in sorted(assets):
        acc.update(f"{rel}\x00{mode:o}\x00{h}\x00".encode())
    return acc.hexdigest()


def _check_tier(tier: str) -> None:
    if tier not in TIERS:
        raise ProfileError(f"unknown tier {tier!r}: expected one of {TIERS}")


def _check_name(tier: str, name: str) -> None:
    if (not isinstance(name, str) or not name or name.startswith(".")
            or "/" in name or "\\" in name):
        raise ProfileError(f"{tier}: {name!r} is not a service profile name")


def profile_kind(d: Path, tier: str | None = None) -> str | None:
    """``service`` (service.yaml); ``cluster-recipe`` (a RATIFIED recipe: compose files, no
    service.yaml); ``missing-definition`` (compose files but no service.yaml for any other
    name); None when the directory is not a profile at all."""
    d = Path(d)
    if (d / "service.yaml").is_file():
        return KIND_SERVICE
    if not (d.is_dir() and any(p.is_file() for p in d.glob("compose*.yaml"))):
        return None
    if (tier, d.name) in _CLUSTER_RECIPES:
        return KIND_CLUSTER_RECIPE
    return KIND_MISSING_DEFINITION


def scan_assets(origin: Path, *, tier: str = "?", name: str = "?", skip=frozenset()) -> tuple:
    """Every file of one profile origin as sorted (relpath, mode, sha256) triples.

    Runtime downloads/caches/bytecode/logs are skipped, and so is every origin-relative
    directory in ``skip`` (the compose files' bind-mounted directories: a container writes
    there, so it is volume content, not an asset). A symlink that escapes the origin, a
    symlinked directory, a dangling link or a non-regular file is refused; a file that cannot
    be read is a ProfileError."""
    origin = Path(origin)
    base = origin.resolve()
    out = []
    for dirpath, dirnames, filenames in os.walk(origin):
        d = Path(dirpath)
        keep = []
        for n in sorted(dirnames):
            if n in _EXCLUDED_DIRS:
                continue
            p = d / n
            if p.relative_to(origin).as_posix() in skip:
                continue
            if p.is_symlink():
                rel = p.relative_to(origin).as_posix()
                if not _within(p.resolve(), base):
                    raise ProfileError(
                        f"{tier}/{name}: symlinked directory {rel} escapes the profile "
                        f"origin {origin}")
                raise ProfileError(
                    f"{tier}/{name}: symlinked directory {rel} is not allowed in a profile")
            keep.append(n)
        dirnames[:] = keep
        for n in sorted(filenames):
            if n.endswith(_EXCLUDED_SUFFIXES):
                continue
            p = d / n
            rel = p.relative_to(origin).as_posix()
            if p.is_symlink():
                if not p.exists():
                    raise ProfileError(f"{tier}/{name}: dangling symlink {rel}")
                if not _within(p.resolve(), base):
                    raise ProfileError(
                        f"{tier}/{name}: symlink {rel} escapes the profile origin {origin}")
            try:
                st = p.stat()
                if not _stat.S_ISREG(st.st_mode):
                    raise ProfileError(f"{tier}/{name}: {rel} is not a regular file")
                out.append((rel, _stat.S_IMODE(st.st_mode), _sha256_file(p)))
            except OSError as e:
                raise ProfileError(f"{tier}/{name}: cannot read {rel} in the profile origin "
                                   f"{origin}: {e}") from e
    out.sort()
    return tuple(out)


# ---------------------------------------------------------------------------
# Dependency references (preflight)
# ---------------------------------------------------------------------------

def _yaml():
    try:
        import yaml
    except ImportError as e:  # pragma: no cover - pyyaml is a declared dependency
        raise ProfileError("PyYAML is required to read service profiles") from e
    return yaml


def _load_yaml(path: Path, tier: str, name: str):
    yaml = _yaml()
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        raise ProfileError(f"{tier}/{name}: cannot read {path.name}: {e}") from e


def _external(value: str) -> bool:
    """Interpolated, absolute or remote values are not profile-relative references."""
    return ("$" in value or "://" in value or value.startswith(("/", "~", "git@")))


def _join(tier: str, name: str, base: str, rel: str) -> str:
    joined = posixpath.normpath(posixpath.join(base, rel))
    if joined == ".." or joined.startswith("../") or joined.startswith("/"):
        raise ProfileError(f"{tier}/{name}: reference {rel!r} escapes the profile origin")
    return joined


def _dockerfile_refs(text: str, ctx: str, tier: str, name: str) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    lines, buf = [], ""
    for raw in text.splitlines():
        if raw.lstrip().startswith("#"):
            continue
        s = raw.rstrip()
        if s.endswith("\\"):
            buf += s[:-1] + " "
            continue
        buf += s
        if buf.strip():
            lines.append(buf.strip())
        buf = ""
    if buf.strip():
        lines.append(buf.strip())
    for line in lines:
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        instr, rest = parts[0].upper(), parts[1]
        if instr in ("COPY", "ADD"):
            if "<<" in rest:
                continue                     # heredoc: inline content, no source file
            if rest.lstrip().startswith("["):
                try:
                    tokens = [str(t) for t in json.loads(rest)]
                except ValueError:
                    raise ProfileError(f"{tier}/{name}: malformed {instr} in {ctx}/Dockerfile")
            else:
                try:
                    tokens = shlex.split(rest)
                except ValueError:
                    raise ProfileError(f"{tier}/{name}: malformed {instr} in {ctx}/Dockerfile")
            flags = [t for t in tokens if t.startswith("--")]
            if any(f.startswith("--from") for f in flags):
                continue                     # copies from another stage/image
            args = [t for t in tokens if not t.startswith("--")]
            for src in args[:-1]:
                if _external(src):
                    continue
                refs.append((_join(tier, name, ctx, src), f"{instr} source"))
        elif instr == "RUN":
            for m in re.finditer(r"--mount=(\S+)", rest):
                opts = dict(kv.split("=", 1) for kv in m.group(1).split(",") if "=" in kv)
                if opts.get("type", "bind") != "bind" or "from" in opts or "source" not in opts:
                    continue
                src = opts["source"]
                if _external(src) or src.strip("./").split("/", 1)[0] in _EXTERNAL_BUILD_INPUTS:
                    continue
                refs.append((_join(tier, name, ctx, src), "RUN bind-mount source"))
    return refs


def profile_references(origin: Path, kind: str, *, tier: str = "?",
                       name: str = "?") -> list[tuple[str, str]]:
    """(origin-relative path, what) for every file the definition requires."""
    origin = Path(origin)
    refs: list[tuple[str, str]] = []
    composes: list[str] = []
    if kind == KIND_SERVICE:
        refs.append(("service.yaml", "service definition"))
        data = _load_yaml(origin / "service.yaml", tier, name)
        if not isinstance(data, dict):
            raise ProfileError(f"{tier}/{name}: service.yaml must be a YAML mapping")
        compose = data.get("compose")
        if not compose and not data.get("container"):
            pass            # connection only (the shipped teradata): an existing instance, nothing to start
        elif tier == "integration":
            if isinstance(compose, str) and compose and posixpath.normpath(compose) != _DEFAULT_COMPOSE:
                raise ProfileError(
                    f"{tier}/{name}: service.yaml declares compose {compose!r}; the integration "
                    f"tier constructs and cleans up {_DEFAULT_COMPOSE}, so a non-default "
                    f"compose filename is refused")
            composes.append(_DEFAULT_COMPOSE)
        elif isinstance(compose, str) and compose:
            composes.append(_join(tier, name, ".", compose))
    elif kind == KIND_MISSING_DEFINITION:
        raise ProfileError(f"{tier}/{name}: missing service definition service.yaml "
                           f"(origin {origin})")
    elif kind == KIND_CLUSTER_RECIPE:
        composes.extend(sorted(p.name for p in origin.glob("compose*.yaml") if p.is_file()))
    else:
        raise ProfileError(f"{tier}/{name}: {origin} is not a service profile")

    for compose in composes:
        refs.append((compose, "compose file"))
        cpath = origin / compose
        if not cpath.is_file():
            continue
        cdir = posixpath.dirname(compose) or "."
        data = _load_yaml(cpath, tier, name) or {}
        services = data.get("services") if isinstance(data, dict) else None
        for svc in (services or {}).values():
            if not isinstance(svc, dict):
                continue
            build = svc.get("build")
            ctx = dockerfile = None
            if isinstance(build, str):
                ctx, dockerfile = build, "Dockerfile"
            elif isinstance(build, dict):
                ctx = str(build.get("context", "."))
                dockerfile = str(build.get("dockerfile", "Dockerfile"))
            if ctx is not None and not _external(ctx):
                ctx_rel = _join(tier, name, cdir, ctx)
                df_rel = _join(tier, name, ctx_rel, dockerfile)
                refs.append((df_rel, "build Dockerfile"))
                if (origin / df_rel).is_file():
                    try:
                        text = (origin / df_rel).read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError) as e:
                        raise ProfileError(f"{tier}/{name}: cannot read {df_rel}: {e}") from e
                    refs.extend(_dockerfile_refs(text, ctx_rel, tier, name))
            for vol in svc.get("volumes") or []:
                src = None
                if isinstance(vol, str):
                    src = vol.split(":", 1)[0]
                elif isinstance(vol, dict) and vol.get("type") == "bind":
                    src = str(vol.get("source", ""))
                if src and (src == "." or src.startswith(("./", "../"))):
                    refs.append((_join(tier, name, cdir, src), "bind-mount source"))
            env_files = svc.get("env_file") or []
            if isinstance(env_files, str):
                env_files = [env_files]
            for ef in env_files:
                path = ef.get("path") if isinstance(ef, dict) else ef
                if isinstance(path, str) and path and not _external(path):
                    refs.append((_join(tier, name, cdir, path), "env_file"))
    return refs


_BIND = "bind-mount source"


def _looks_like_dir(rel: str) -> bool:
    """An absent bind-mount source Docker creates as a directory: no file extension."""
    return not posixpath.splitext(posixpath.basename(rel))[1]


def bind_mount_dirs(origin: Path, kind: str, *, tier: str = "?", name: str = "?") -> frozenset:
    """The compose bind-mount sources that exist as directories in ``origin``:
    accepted whether empty or unreadable, and never scanned as assets."""
    origin = Path(origin)
    return frozenset(rel for rel, what in profile_references(origin, kind, tier=tier, name=name)
                     if what == _BIND and rel != "." and (origin / rel).is_dir())


def absent_directory_mounts(profile: Profile) -> list[str]:
    """Absent bind-mount sources that look like directories (no file extension). Docker
    creates them empty at bring-up, so preflight allows them; doctor warns about each."""
    names = {a[0] for a in profile.assets}
    return [rel for rel, what in profile_references(profile.origin, profile.kind,
                                                    tier=profile.tier, name=profile.name)
            if what == _BIND and rel not in names and not (profile.origin / rel).exists()
            and _looks_like_dir(rel)]


def check_profile(profile: Profile) -> None:
    """Named preflight failure for any required file absent from the profile's origin. A
    bind-mount source may be an existing directory, or an absent one that looks like a
    directory; an absent source that looks like a file (``init.sql``) is refused."""
    names = {a[0] for a in profile.assets}
    supplied = CONSUMER_SUPPLIED_SOURCES.get((profile.tier, profile.name), frozenset())
    dirs = {"."}
    for rel in names:
        parent = posixpath.dirname(rel)
        while parent:
            dirs.add(parent)
            parent = posixpath.dirname(parent)
    for rel, what in profile_references(profile.origin, profile.kind,
                                        tier=profile.tier, name=profile.name):
        if _GLOB_CHARS & set(rel):
            if any(fnmatch.fnmatch(a, rel) or fnmatch.fnmatch(a, rel + "/*") for a in names):
                continue
        elif rel in names or rel in dirs or rel in supplied:
            continue
        elif what == _BIND and ((profile.origin / rel).is_dir()
                                or (not (profile.origin / rel).exists() and _looks_like_dir(rel))):
            continue
        raise ProfileError(
            f"{profile.tier}/{profile.name}: missing {what} {rel} "
            f"(selected origin {profile.origin})")


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def _dedupe(roots) -> tuple[Path, ...]:
    out: list[Path] = []
    seen: set[Path] = set()
    for r in roots:
        p = Path(r)
        key = p.resolve()
        if key not in seen:
            seen.add(key)
            out.append(p)
    return tuple(out)


def select_profile(tier: str, name: str, *, roots=(), override: bool = False,
                   services_dir=None, check: bool = True) -> Profile:
    """Resolve ONE whole profile.

    ``services_dir`` — a deliberate single origin (it replaces ``roots``). Otherwise the
    ordered ``roots`` are searched; a name under more than one root is refused unless
    ``override=True`` selects the first hit. The selected origin supplies every asset."""
    _check_tier(tier)
    _check_name(tier, name)
    search = (Path(services_dir),) if services_dir is not None else _dedupe(roots)
    if not search:
        raise ProfileError(f"{tier}: no services root is configured")
    hits = [r for r in search if profile_kind(r / name, tier)]
    if not hits:
        searched = ", ".join(str(r) for r in search)
        raise ProfileError(
            f"{tier}: unknown service profile {name!r} (searched, in order: {searched})")
    if len(hits) > 1 and not override:
        named = ", ".join(str(h / name) for h in hits)
        raise ProfileError(
            f"{tier}: service profile {name!r} is defined under multiple roots ({named}); "
            f"remove the duplicate, select one origin with services_dir, or pass "
            f"override=True to accept the first hit")
    root = hits[0]
    origin = root / name
    kind = profile_kind(origin, tier)
    if kind == KIND_MISSING_DEFINITION:
        raise ProfileError(
            f"{tier}/{name}: missing service definition service.yaml (selected origin {origin}); "
            f"only {sorted(n for t, n in _CLUSTER_RECIPES if t == tier)} may omit it as "
            f"cluster recipes")
    try:
        skip = bind_mount_dirs(origin, kind, tier=tier, name=name)
    except ProfileError:
        if check:
            raise
        skip = frozenset()
    assets = scan_assets(origin, tier=tier, name=name, skip=skip)
    profile = Profile(tier, name, kind, origin, root, content_hash(assets), assets)
    if check:
        check_profile(profile)
    return profile


def list_profiles(tier: str, *, roots, kinds=(KIND_SERVICE,)) -> list[str]:
    """Sorted profile names across ``roots``; a duplicated name is refused. Offline and
    read-only: nothing is materialized to list."""
    _check_tier(tier)
    found: dict[str, list[str]] = {}
    for root in _dedupe(roots):
        if not root.is_dir():
            continue
        for p in root.iterdir():
            if profile_kind(p, tier) in kinds:
                found.setdefault(p.name, []).append(str(root))
    dupes = {n: rs for n, rs in found.items() if len(rs) > 1}
    if dupes:
        detail = "; ".join(f"{n!r} under {', '.join(rs)}" for n, rs in sorted(dupes.items()))
        raise ProfileError(f"{tier}: service profile name(s) defined under multiple roots: {detail}")
    return sorted(found)


# ---------------------------------------------------------------------------
# Materialization into declared state
# ---------------------------------------------------------------------------

def _copy_asset(src: Path, dst: Path, mode: int) -> None:
    """Copy one asset and apply its recorded mode (seam for the atomicity test)."""
    shutil.copyfile(src, dst)
    os.chmod(dst, mode)


def _workspace_base(state_dir, tier: str, prefix: str, *, create: bool) -> Path:
    """``<state>/services/<tier>/<prefix|default>`` inside the CANONICAL declared state dir.

    Every component below the state dir must be a real directory. A symlink there would
    redirect writes or cleanup outside the declared boundary, so it is refused before any
    directory, lock, copy or removal operation (components are created only when ``create``)."""
    _check_tier(tier)
    if prefix and not _PREFIX_RE.match(prefix):
        raise ProfileError(f"invalid stack prefix {prefix!r}: must match [a-z0-9][a-z0-9-]*")
    state = Path(state_dir).resolve()
    if create:
        state.mkdir(parents=True, exist_ok=True)
    base = state
    for part in ("services", tier, prefix or "default"):
        base = base / part
        _refuse_redirect(base, "workspace path")
        if base.exists():
            if not base.is_dir():
                raise ProfileError(f"workspace path {base} is not a directory")
        elif create:
            base.mkdir()
    return base


def _refuse_redirect(path: Path, what: str) -> None:
    if path.is_symlink():
        raise ProfileError(
            f"{what} {path} is a symlink; materialization and cleanup never follow a redirect "
            f"out of the declared state dir")


def materialize_profile(profile: Profile, *, state_dir, prefix: str = "") -> Path:
    """Materialize ``profile`` into ``<state_dir>/services/<tier>/<prefix|default>/``.

    The exact asset set is copied (mode-preserving, bytes re-verified) into a
    content-addressed ``.cas/<name>-<hash>`` directory published by rename, then a stable
    ``<name>`` symlink is repointed atomically. A failed copy leaves no version dir and no
    pointer. The origin is only read, and must still match the selected hash."""
    base = _workspace_base(state_dir, profile.tier, prefix, create=False)
    for protected in (profile.root.resolve(), profile.origin.resolve()):
        if _within(base, protected):
            raise ProfileError(
                f"state dir {base} resolves inside the service resources at {protected}; "
                f"materialization writes only to a declared state dir outside them")
    base = _workspace_base(state_dir, profile.tier, prefix, create=True)
    lock_path = base / ".profile-workspace.lock"
    _refuse_redirect(lock_path, "workspace lock")
    from filelock import FileLock
    with FileLock(str(lock_path)):
        skip = bind_mount_dirs(profile.origin, profile.kind, tier=profile.tier, name=profile.name)
        assets = scan_assets(profile.origin, tier=profile.tier, name=profile.name, skip=skip)
        if content_hash(assets) != profile.definition_hash:
            raise ProfileError(
                f"{profile.tier}/{profile.name}: origin {profile.origin} changed after it "
                f"was selected; select the profile again")
        cas_root = base / ".cas"
        _refuse_redirect(cas_root, "content store")
        cas = cas_root / f"{profile.name}-{profile.definition_hash[:16]}"
        _refuse_redirect(cas, "content-store entry")
        if not cas.is_dir():
            cas_root.mkdir(exist_ok=True)
            tmp = Path(tempfile.mkdtemp(prefix=f".tmp-{profile.name}-", dir=base))
            try:
                for rel, mode, sha in assets:
                    dst = tmp / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    _copy_asset(profile.origin / rel, dst, mode)
                    if _sha256_file(dst) != sha:
                        raise ProfileError(
                            f"{profile.tier}/{profile.name}: copied bytes of {rel} do not "
                            f"match the selected origin")
                (tmp / ".content-sha256").write_text(
                    "".join(f"{rel} {mode:o} {sha}\n" for rel, mode, sha in assets))
                os.chmod(tmp, 0o755)
                os.replace(tmp, cas)
            except BaseException:
                shutil.rmtree(tmp, ignore_errors=True)
                raise
        pointer = base / profile.name
        if pointer.exists() and not pointer.is_symlink():
            raise ProfileError(
                f"{pointer} exists and is not a workspace pointer; refusing to replace it")
        if not (pointer.is_symlink() and Path(os.readlink(pointer)) == cas):
            tmp_link = base / f".tmp-link-{profile.name}-{os.getpid()}-{os.urandom(4).hex()}"
            os.symlink(cas, tmp_link)
            os.replace(tmp_link, pointer)
    return pointer


def discard_workspaces(*, state_dir, tier: str, prefix: str = "") -> list[Path]:
    """Remove ONLY what materialization created for this tier/prefix: pointers into its
    ``.cas``, the ``.cas`` tree and interrupted ``.tmp-*`` staging. Anything else under
    the base (another owner's file) is left alone."""
    base = _workspace_base(state_dir, tier, prefix, create=False)
    removed: list[Path] = []
    if not base.is_dir():
        return removed
    cas_root = base / ".cas"
    _refuse_redirect(cas_root, "content store")
    for p in sorted(base.iterdir()):
        if p.is_symlink():
            if _within(Path(os.readlink(p)), cas_root):
                p.unlink()
                removed.append(p)
        elif p.name.startswith(".tmp-"):
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            removed.append(p)
    if cas_root.is_dir():
        shutil.rmtree(cas_root)
        removed.append(cas_root)
    return removed


# ---------------------------------------------------------------------------
# Live-tier adapter (import livetest lazily: this module stays stdlib-only)
# ---------------------------------------------------------------------------

def live_roots(env=None, dotenv=None) -> tuple[Path, ...]:
    """The built-in live service root: ``livetest.paths.services_dir()`` (``SLT_SERVICES_DIR``,
    else ``scripts/live/services``). The registry searches consumer roots before it
    (``registry._roots``); a selected service's profile is checked from its own origin."""
    from livetest import paths
    return (paths.services_dir(env, dotenv),)
