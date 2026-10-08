from __future__ import annotations
import contextlib
import hashlib
import inspect
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from livetest import docker_disk, stack
from livetest.topology import parse_deployment_groups

_DEFAULT_STRIIM_VERSION = "5.4.2"


def striim_dir():
    """The Striim recipe's directory (compose files, Dockerfile, deps/), resolved at call time:
    `<services dir>/striim` (SLT_SERVICES_DIR, else the framework's live services). Public, for
    callers outside the engine (the console's environment catalog, console impact F2)."""
    from livetest import paths
    return paths.services_dir() / "striim"

def _required_deps(version: str = _DEFAULT_STRIIM_VERSION) -> list[str]:
    return [
        f"striim-dbms-{version}-Linux.deb", f"striim-node-{version}-Linux.deb",
        f"striim-agent-{version}-Linux.deb", f"striim-samples-{version}-Linux.deb",
        "sqljdbc_6.0.8112.200_enu.tar.gz", "mysql-connector-java-8.0.30.zip",
        "vertica-jdbc-25.3.0-0.jar",
        "jmx_prometheus_javaagent-0.16.1.jar",
        "instantclient-basic-linux.x64-21.6.0.0.0dbru.zip",
    ]

# Back-compat constant (the 5.4.2 default release's dep list) — some callers /
# hermetic tests still check against this directly.
REQUIRED_DEPS = _required_deps(_DEFAULT_STRIIM_VERSION)


# Consumer-supplied Striim installer inputs, named by one JSON manifest.
DEPS_MANIFEST_ENV = "SLT_STRIIM_DEPS_MANIFEST"
DEPS_MANIFEST_SCHEMA_VERSION = 1
_DEPS_MANIFEST_KEYS = frozenset({"schemaVersion", "directory", "sha256"})
# The verified installer identity, recorded in the recipe's files/ (-> /slt-build-inputs).
INSTALLER_IDENTITY_FILE = "slt-installer-identity.json"


class StriimDepsError(Exception):
    """Consumer-supplied Striim installer inputs are absent, malformed or do not match
    their expected digests. The slt-striim build is refused before
    ``docker build``, naming the offending file (never a secret value)."""


class StriimDiskError(RuntimeError):
    """Docker has too little free space to build the Striim image (livetest.docker_disk)."""


def _sha256_of(path: Path) -> str:
    from livetest import opartifacts
    return opartifacts.file_sha256(path)


def _is_hex64(value) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdefABCDEF" for c in value))


def load_striim_deps_manifest(path=None, env=None) -> tuple:
    """Parse the installer-input manifest; returns (directory, {name: sha256}).

    ``path`` defaults to ``$SLT_STRIIM_DEPS_MANIFEST``. The JSON object has exactly
    ``schemaVersion`` (1), ``directory`` (relative to the manifest file; it may not escape
    the manifest's own directory by ``..`` or a symlink) and ``sha256`` (plain filename ->
    64-hex digest)."""
    import json
    e = os.environ if env is None else env
    if path is None:
        raw = (e.get(DEPS_MANIFEST_ENV) or "").strip()
        if not raw:
            raise StriimDepsError(
                f"{DEPS_MANIFEST_ENV} is not set: building slt-striim from an installed "
                "framework requires a consumer-supplied installer-input manifest")
        path = raw
    mpath = Path(path).expanduser()
    try:
        text = mpath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise StriimDepsError(
            f"cannot read Striim deps manifest {mpath}: {exc.__class__.__name__}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise StriimDepsError(f"Striim deps manifest {mpath} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise StriimDepsError(f"Striim deps manifest {mpath} must be a JSON object")
    unknown = sorted(set(data) - _DEPS_MANIFEST_KEYS)
    if unknown:
        raise StriimDepsError(f"Striim deps manifest {mpath}: unknown field(s) {unknown}")
    version = data.get("schemaVersion")
    if type(version) is not int or version != DEPS_MANIFEST_SCHEMA_VERSION:
        raise StriimDepsError(
            f"Striim deps manifest {mpath}: unsupported schemaVersion {version!r} "
            f"(expected {DEPS_MANIFEST_SCHEMA_VERSION})")
    directory = data.get("directory")
    if not isinstance(directory, str) or not directory.strip():
        raise StriimDepsError(
            f"Striim deps manifest {mpath}: directory must be a non-empty relative path")
    if Path(directory).is_absolute():
        raise StriimDepsError(
            f"Striim deps manifest {mpath}: directory must be relative to the manifest file")
    base = mpath.resolve().parent
    ddir = (base / directory).resolve()
    if ddir != base and base not in ddir.parents:
        raise StriimDepsError(
            f"Striim deps manifest {mpath}: directory {directory!r} escapes the manifest "
            f"directory {base}")
    if not ddir.is_dir():
        raise StriimDepsError(
            f"Striim deps manifest {mpath}: directory {directory!r} does not exist")
    digests = data.get("sha256")
    if not isinstance(digests, dict) or not digests:
        raise StriimDepsError(
            f"Striim deps manifest {mpath}: sha256 must be a non-empty object")
    expected = {}
    for name, digest in digests.items():
        if (not name or name in (".", "..") or "/" in name or "\\" in name):
            raise StriimDepsError(
                f"Striim deps manifest {mpath}: sha256 key {name!r} is not a plain filename")
        if not _is_hex64(digest):
            raise StriimDepsError(
                f"Striim deps manifest {mpath}: sha256 value for {name} is not a 64-hex digest")
        expected[name] = digest.lower()
    return ddir, expected


def _staged_ok(final: Path, digest: str) -> bool:
    return final.is_file() and not final.is_symlink() and _sha256_of(final) == digest


def _verified_sources(manifest=None, version=_DEFAULT_STRIIM_VERSION, env=None,
                      staged: Path | None = None) -> tuple:
    """({name: source path}, {name: digest}, {names whose staged copy in ``staged`` already
    matches}). A source whose staged copy already matches its digest is not re-hashed: the staged
    bytes are what gets built. Every other source is hashed before anything is copied."""
    ddir, expected = load_striim_deps_manifest(manifest, env)
    sources, already = {}, set()
    for name in _required_deps(version):
        if name not in expected:
            raise StriimDepsError(f"no expected digest for Striim build dependency: {name}")
        p = ddir / name
        if p.is_symlink() and ddir not in p.resolve().parents:
            raise StriimDepsError(
                f"Striim build dependency {name} is a symlink escaping the deps directory")
        if not p.is_file():
            raise StriimDepsError(f"missing Striim build dependency: {name}")
        if staged is not None and _staged_ok(staged / name, expected[name]):
            already.add(name)
        elif _sha256_of(p) != expected[name]:
            raise StriimDepsError(f"digest mismatch for Striim build dependency: {name}")
        sources[name] = p
    return sources, expected, already


def verify_striim_deps(manifest=None, version: str = _DEFAULT_STRIIM_VERSION, env=None) -> dict:
    """Verify every required installer file named by the manifest BEFORE
    anything is copied. Returns {filename: source path}; raises StriimDepsError naming
    the first offending file. Never downloads, never falls back to a checkout path."""
    return _verified_sources(manifest, version, env)[0]


def stage_striim_deps(striim_dir: Path, version: str = _DEFAULT_STRIIM_VERSION,
                      manifest=None, env=None) -> Path:
    """Copy the verified required files into the owned build context's ``deps/`` (the
    only place the Dockerfile bind-mounts them from) and verify the STAGED bytes. Files
    outside the required set are never staged."""
    import shutil
    dest = _deps_dir(striim_dir)
    sources, expected, already = _verified_sources(manifest, version, env, staged=dest)
    dest.mkdir(parents=True, exist_ok=True)
    for name, src in sources.items():
        if name in already:
            continue
        final = dest / name
        tmp = dest / f".{name}.staging-{os.getpid()}"
        try:
            shutil.copyfile(src, tmp)
            if _sha256_of(tmp) != expected[name]:
                raise StriimDepsError(
                    f"digest mismatch for staged Striim build dependency: {name}")
            os.replace(tmp, final)
        finally:
            if tmp.exists():
                tmp.unlink()
    _write_installer_identity(striim_dir, version, expected)
    return dest


def _identity_dir(striim_dir: Path) -> Path:
    """The recipe's files/ dir, reached only through real directories below the canonical
    build context (the redirect rule of resource_profiles._workspace_base): a symlinked or
    non-directory component would send identity writes or cleanup outside owned state."""
    d = Path(striim_dir).resolve()
    for part in ("images", "striim", "files"):
        d = d / part
        if d.is_symlink():
            raise StriimDepsError(
                f"build-context path {d} is a symlink; the installer identity is never "
                "written or removed through a redirect")
        if d.exists() and not d.is_dir():
            raise StriimDepsError(f"build-context path {d} is not a directory")
    return d


def installer_identity(version: str, expected: dict) -> bytes:
    """Canonical record of the VERIFIED installer set (required files only) for one release.
    Staged into the recipe's files/ so the Dockerfile's COPY of files/ embeds it at
    /slt-build-inputs, where image_inputs_match compares it byte for byte."""
    import json
    record = {"schemaVersion": 1, "striimVersion": version,
              "sha256": {n: expected[n] for n in sorted(_required_deps(version))}}
    return (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _write_installer_identity(striim_dir: Path, version: str, expected: dict) -> None:
    directory = _identity_dir(striim_dir)
    path = directory / INSTALLER_IDENTITY_FILE
    if path.is_symlink():
        raise StriimDepsError(f"installer identity {path} is a symlink; refusing to follow it")
    data = installer_identity(version, expected)
    if path.is_file() and path.read_bytes() == data:
        return
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / f".{INSTALLER_IDENTITY_FILE}.tmp-{os.getpid()}-{os.urandom(4).hex()}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(tmp, flags, 0o644)
    except FileExistsError as exc:
        raise StriimDepsError(
            f"installer identity temp file {tmp} already exists; refusing to follow or "
            "reuse it") from exc
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        raise


def _clear_installer_identity(striim_dir: Path) -> None:
    """A legacy checkout build records no installer identity: drop a stale record so it never
    matches an image built from a verified manifest (or the reverse)."""
    path = _identity_dir(striim_dir) / INSTALLER_IDENTITY_FILE
    if path.is_file() or path.is_symlink():
        path.unlink()


def _in_checkout() -> bool:
    """True unless this module runs from a site-packages tree: a clone, an editable install or a
    plain copy of the repo is a checkout (the one place installers may still be downloaded)."""
    from livetest import layout
    return not layout.in_install_tree(__file__)


def candidate_mode() -> bool:
    """Installed/relocated tree, or SLT_STRIIM_DEPS_MANIFEST set: installer inputs must be
    verified and bound into image identity."""
    return bool((os.environ.get(DEPS_MANIFEST_ENV) or "").strip()) or not _in_checkout()


def verify_current_inputs(striim_dir: Path, release: dict | None = None) -> bool:
    """Before an automatic reuse decision on a running Docker cluster: in candidate mode,
    verify + stage the CURRENT manifest's installer set and record its identity, so the
    comparison that follows is against today's inputs, never a persisted record. Raises
    StriimDepsError on a missing or invalid manifest. Returns True iff candidate mode applied."""
    if not candidate_mode():
        return False
    _gate_striim_deps(striim_dir, (release or {}).get("STRIIM_VERSION", _DEFAULT_STRIIM_VERSION))
    return True


def running_image_reason(container: str, version: str, run=None):
    """Candidate-mode cluster reuse: after the gate verified the current inputs against this
    stack's ``image_ref(version)`` tag, the running container must run that exact image. Returns a
    redeploy reason when the container's immutable image ID differs from the tag's image ID,
    or when either ID is unavailable (never a match); None when they are the same image."""
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    ids = []
    for argv in (["docker", "container", "inspect", "-f", "{{.Image}}", container],
                 ["docker", "image", "inspect", "-f", "{{.Id}}", image_ref(version)]):
        try:
            r = run(argv)
        except OSError:
            return "the running cluster's image identity is unavailable"
        value = (getattr(r, "stdout", "") or "").strip()
        if getattr(r, "returncode", 1) != 0 or not value:
            return "the running cluster's image identity is unavailable"
        ids.append(value)
    if ids[0] != ids[1]:
        return "the running cluster's container image is not the image verified for the current inputs"
    return None


def _gate_striim_deps(striim_dir: Path, version: str) -> None:
    """Pre-build gate. With SLT_STRIIM_DEPS_MANIFEST set, the manifest's
    inputs are verified, staged into the build context (staged bytes re-verified) and the
    verified installer identity recorded. An installed/relocated tree REQUIRES the manifest:
    deps already present in its build context are never trusted. Only a source checkout
    without the variable keeps the legacy presence check (it downloads in ensure_deps)."""
    if (os.environ.get(DEPS_MANIFEST_ENV) or "").strip():
        stage_striim_deps(striim_dir, version)
        return
    if not _in_checkout():
        raise StriimDepsError(
            f"{DEPS_MANIFEST_ENV} is not set: an installed framework builds slt-striim only "
            "from a verified installer-input manifest; build-context deps are never trusted "
            "without it")
    d = _deps_dir(striim_dir)
    if not deps_present(d, version):
        missing = [n for n in _required_deps(version) if not (d / n).is_file()]
        raise StriimDepsError(
            f"no Striim build dependencies available and {DEPS_MANIFEST_ENV} is unset; "
            f"missing: {', '.join(missing)}. An installed framework requires a "
            "consumer-supplied installer-input manifest")

def _stream(argv, cwd=None):
    # inherit stdout/stderr so download/build progress shows live
    r = subprocess.run(argv, cwd=cwd)
    if r.returncode != 0:
        raise RuntimeError(f"command failed ({r.returncode}): {' '.join(argv)}")

@contextlib.contextmanager
def _env_override(extra: dict):
    # Temporarily set env vars for the duration of a `docker compose` subprocess call
    # (compose interpolates ${VAR} in compose.yaml/build args from the process env,
    # falling back to services/striim/.env). The live suite is hard-serial (see
    # plugin.pytest_configure), so a brief process-wide os.environ mutation is safe.
    prior = {k: os.environ.get(k) for k in extra}
    os.environ.update({k: str(v) for k, v in extra.items() if v is not None})
    try:
        yield
    finally:
        for k, v in prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

# startUp.properties field -> the compose env var the Striim image expects. The cluster
# image's entrypoint injects these into its own conf/startUp.properties from the env; compose
# interpolates ${VAR} from the process env (see _env_override). Without them the primary
# refuses to boot ("startUp.properties is missing required fields: WAClusterName, CompanyName").
_STARTUP_TO_ENV = {
    "WAClusterName": "CLUSTER_NAME",
    "CompanyName":   "COMPANY_NAME",
    "ProductKey":    "PRODUCT_KEY",
    "LicenceKey":    "LICENCE_KEY",
}

# The four compose vars, in a stable canonical order (CLUSTER_NAME, COMPANY_NAME, PRODUCT_KEY,
# LICENCE_KEY) — the single source of truth for "which vars the license fallback covers".
_LICENSE_VARS = tuple(_STARTUP_TO_ENV.values())

def _read_startup_license(home: str | None) -> dict:
    """Parse the four license/cluster fields out of ``<home>/conf/startUp.properties`` into
    their compose-var names (``_STARTUP_TO_ENV``). The file is java-properties style
    (``Key=Value``); a line whose first non-space char is ``#`` is a COMMENT and is skipped —
    the file ships commented duplicates like ``# ProductKey=`` that must never be read — and the
    FIRST uncommented occurrence of a field wins. Only non-blank values are returned. ``{}`` when
    ``home`` is falsy or the file is absent."""
    if not home:
        return {}
    props = Path(home) / "conf" / "startUp.properties"
    if not props.is_file():
        return {}
    out: dict = {}
    for raw in props.read_text(errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        env = _STARTUP_TO_ENV.get(key.strip())
        if env and env not in out and val.strip():   # first uncommented, non-blank occurrence wins
            out[env] = val.strip()
    return out

def cluster_config_from_home(home: str | None = None) -> dict:
    """Gather the cluster/license config the compose file needs from the Striim install's
    conf/startUp.properties (``$STRIIM_HOME`` by default). Returns only fields present and
    non-empty in the file; missing/blank ones are omitted so an explicit env value still wins
    (see cluster_up). No-op ({}) when STRIIM_HOME is unset or the file is absent — the caller
    then relies on the ambient env, exactly as before."""
    return _read_startup_license(home or os.environ.get("STRIIM_HOME"))

def enrich_license_env(env, home: str | None = None) -> tuple[dict, dict]:
    """Return ``(enriched_env, sources)`` for the four Striim license/cluster compose vars
    (``COMPANY_NAME``, ``CLUSTER_NAME``, ``PRODUCT_KEY``, ``LICENCE_KEY``).

    ``docker compose`` interpolates these ``${VAR}`` from the process env; when they are unset it
    warns ("variable is not set. Defaulting to a blank string") and the cluster is doomed. A user
    with a configured local Striim install already has the values in
    ``$STRIIM_HOME/conf/startUp.properties`` (mapped via ``_STARTUP_TO_ENV``), so a hand-written
    env file is unnecessary: for every one of the four vars that is unset/blank in ``env`` — and
    only when ``STRIIM_HOME`` is set and that file exists — the value is filled from the file. An
    explicit, non-blank value in ``env`` ALWAYS wins (only gaps are filled), so a deliberate
    override still takes effect and this is a safe no-op when the vars are already set.

    ``enriched_env`` is a COPY of ``env`` with the gaps filled (``env`` — e.g. ``os.environ`` — is
    never mutated); pass it as the ``env=`` of the compose subprocess. ``sources`` maps each var
    name to its origin — ``"env"``, ``"derived"``, or ``"unset"`` — so a caller can log WHICH vars
    were supplied and from where. Only var NAMES and origins ever live in ``sources``; the secret
    VALUES are never placed there or logged (the same names-only discipline stack-doctor uses for
    its license section). ``home`` overrides the ``STRIIM_HOME`` lookup (the ``env`` mapping first,
    then ``os.environ``)."""
    def _blank(v):
        return v is None or not str(v).strip()
    enriched = dict(env)
    missing = [v for v in _LICENSE_VARS if _blank(enriched.get(v))]
    if home is None:
        home = enriched.get("STRIIM_HOME") or os.environ.get("STRIIM_HOME")
    derived = _read_startup_license(home) if missing else {}
    sources: dict = {}
    for var in _LICENSE_VARS:
        if not _blank(enriched.get(var)):
            sources[var] = "env"
        elif var in derived:
            enriched[var] = derived[var]
            sources[var] = "derived"
        else:
            sources[var] = "unset"
    return enriched, sources

def _deps_dir(striim_dir: Path) -> Path:
    return Path(striim_dir) / "images" / "striim" / "deps"

def deps_present(deps_dir: Path, version: str = _DEFAULT_STRIIM_VERSION) -> bool:
    d = Path(deps_dir)
    return all((d / n).exists() and (d / n).stat().st_size > 0 for n in _required_deps(version))

def ensure_deps(striim_dir: Path, release: dict | None = None, run=None, progress=None) -> None:
    # release fixes STRIIM_VERSION, whose debs (striim-{dbms,node,agent,samples}-<ver>-Linux.deb)
    # download-dependencies.sh fetches — it already reads STRIIM_VERSION from the environment
    # (falling back to services/striim/.env), so passing it through _env_override is enough.
    run = run or _stream
    version = (release or {}).get("STRIIM_VERSION", _DEFAULT_STRIIM_VERSION)
    if (os.environ.get(DEPS_MANIFEST_ENV) or "").strip():
        # Consumer-supplied installer inputs -- verify + stage, never download.
        if progress: progress("cluster", "verifying consumer-supplied dependencies")
        stage_striim_deps(striim_dir, version)
        return
    if not _in_checkout():
        # An installed framework never downloads installers and never
        # trusts build-context deps without the manifest.
        _gate_striim_deps(striim_dir, version)   # raises, naming SLT_STRIIM_DEPS_MANIFEST
    if deps_present(_deps_dir(striim_dir), version):
        return
    if progress: progress("cluster", "downloading dependencies (one-time)")
    print(f"[striim] downloading build dependencies for {version} (one-time)…")
    with _env_override({"STRIIM_VERSION": version}):
        run(["./download-dependencies.sh"], cwd=str(striim_dir))

def image_ref(version: str = _DEFAULT_STRIIM_VERSION, env=None) -> str:
    """The image tag for THIS stack: ``ec`` + ``slt-striim:5.4.0.6C`` -> ``ec-slt-striim:5.4.0.6C``.

    The tag used to be ``slt-striim:<version>`` for every stack, which made it shared state
    between checkouts that are not the same: two working copies of this repo build their own
    Dockerfile and ``files/`` into one tag, so whichever ran last won and the other correctly
    detected "built from different sources" and rebuilt -- roughly 20 minutes, every alternate
    run, for as long as both were in use. Measured 2026-09-23 between two checkouts
    that differ deliberately (one installs a Teradata JDBC driver).

    Prefixing matches what compose.yaml already does for the project and container names. It does
    NOT deduplicate: two stacks whose build inputs are identical still get two images, and one
    checkout switching between branches with different inputs still rebuilds. Tagging by a hash
    of the inputs would fix both, and would let image_inputs_match below be deleted outright,
    since the tag would then identify what it was built from. That is the intended follow-up.
    """
    return f"{stack.prefixed('slt-striim', env)}:{version}"


def image_present(version: str = _DEFAULT_STRIIM_VERSION, run=None) -> bool:
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    out = run(["docker", "images", "-q", image_ref(version)])
    return bool((getattr(out, "stdout", "") or "").strip())

# --------------------------------------------------------------------------------------------
# Is the built image made from the checkout we have?
#
# The image carries a copy of its own build inputs at /slt-build-inputs (Dockerfile + files/,
# see the Dockerfile's last COPY). Comparing those bytes with the checkout answers "is this
# image stale?" exactly, and -- unlike encoding a digest in the tag -- tells no other part of
# the repo anything it has to agree with. The tag is `<prefix>-slt-striim:<version>`.
# --------------------------------------------------------------------------------------------
_BUILD_INPUTS = "/slt-build-inputs"
# The inputs the image carries a copy of. The Dockerfile must COPY exactly these, each keeping
# its relative path -- a contract test binds the two together, because a mismatch makes every
# image read as stale (a rebuild on every run) with nothing to notice.
_BUILD_INPUT_NAMES = ("Dockerfile", "files")


def _local_build_inputs(striim_dir: Path) -> dict:
    """{relative path: bytes} for the inputs the Dockerfile copies into the image."""
    root = Path(striim_dir) / "images" / "striim"
    out = {}
    for rel in _BUILD_INPUT_NAMES:
        target = root / rel
        paths = sorted(target.rglob("*")) if target.is_dir() else [target]
        for path in paths:
            if path.is_file():
                out[str(path.relative_to(root))] = path.read_bytes()
    return out


def image_inputs_match(version: str, striim_dir: Path, run=None) -> bool:
    """True iff the image's embedded build inputs equal the checkout's, byte for byte.

    `docker create` + `docker cp`: the container is never STARTED, so this costs about a second
    and no amd64 emulation. An image built before this directory existed has nothing to extract
    and reads as stale -- correct, since its inputs are unknowable.

    Failing to REACH docker (absent, `create` refused) reads as MATCHING: a 20-minute rebuild is
    the wrong answer to "docker hiccupped", and a genuinely stale image survives only until the
    next successful check. A failed `cp` is different and reads as STALE -- that is how an image
    built before /slt-build-inputs existed announces itself, and it is indistinguishable from a
    transient cp error, so the safe reading is the one that rebuilds."""
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    # Absent locally: `docker create` would try to PULL (there is no registry for this image),
    # which fails after a network timeout -- inside the provisioning FileLock, on what is
    # supposed to be the fast reuse path. Nothing to compare either way; ensure_image handles
    # absence by building.
    want = _local_build_inputs(striim_dir)
    # A NAMED probe (pid-suffixed, so concurrent callers cannot collide): a container leaked by
    # a SIGINT between create and rm is invisible to `docker ps` and to `--remove-orphans` --
    # it belongs to no compose project -- yet it pins the 21 GB image, so `docker rmi` then
    # fails with a confusing reference error. Named, it is greppable
    # (`docker ps -a --filter name=slt-image-probe`) and the next run clears it.
    # In candidate mode an image whose build inputs cannot be read has an
    # unverifiable installer identity, so a failed inspection is a non-match (rebuild).
    strict = candidate_mode()
    probe = f"slt-image-probe-{os.getpid()}"
    cid = None
    try:
        if not image_present(version, run=run):
            return True          # nothing to compare; ensure_image handles absence by building
        try:
            run(["docker", "rm", "-f", probe])       # clear a leak from an interrupted run
        except OSError:
            pass
        # --platform matches compose.yaml's `platform: linux/amd64`. Without it a Docker
        # Desktop using the containerd image store (the default on newer installs) refuses to
        # create a container from a foreign-platform image; `create` then fails, this returns
        # "current", and the staleness check silently never fires again.
        made = run(["docker", "create", "--platform", "linux/amd64",
                    "--name", probe, image_ref(version)])
        cid = (getattr(made, "stdout", "") or "").strip()
        if getattr(made, "returncode", 1) != 0 or not cid:
            return not strict
        with tempfile.TemporaryDirectory() as tmp:
            got = run(["docker", "cp", f"{cid}:{_BUILD_INPUTS}/.", tmp])
            if getattr(got, "returncode", 1) != 0:
                return False          # no embedded inputs: built before they existed -> stale
            here = {}
            for path in sorted(Path(tmp).rglob("*")):
                if path.is_file():
                    here[str(path.relative_to(tmp))] = path.read_bytes()
        return here == want
    except OSError:
        return not strict
    finally:
        if cid:
            try:
                run(["docker", "rm", "-f", probe])
            except OSError:
                pass


def _gate_docker_disk(query, progress=None, cold: bool = True) -> None:
    """Refuse a build Docker has no room for: a full disk surfaces much later as a primary
    that never answers. ``cold`` (no image of this tag at all) is gated at the
    build floor; a present-but-stale image rebuilds from the build cache at ~0 GB (measured
    on Mac re-runs), so it needs only the run floor -- the rule doctor uses too.
    Unknown free space warns and builds."""
    free, how = docker_disk.vm_free_bytes(run=query)
    problem = docker_disk.shortfall(free, building=cold)
    if problem:
        raise StriimDiskError(problem)
    if free is None:
        print(f"[striim] could not measure Docker's free space ({how}); the build needs about "
              f"{docker_disk.min_free_gb(cold):g} GB")
    elif progress:
        how_built = "" if cold else "; rebuilding from cache"
        progress("cluster", f"Docker has {docker_disk.describe(free)} free ({how}){how_built}")


def ensure_image(striim_dir: Path, release: dict | None = None, run=None, progress=None,
                 query_run=None) -> None:
    # compose.yaml's image is stack-prefixed and version-tagged, so each
    # release gets its own image. Presence alone is not enough to skip the build, though: an
    # edit to entrypoint.sh or the Dockerfile leaves the tag untouched, so a machine that had
    # built once kept a stale image forever -- and when the entrypoint's baked cluster address
    # changed, its cluster silently never formed. The image carries its own build inputs, so
    # ask it. Build args (STRIIM_VERSION, JDK_VERSION) come from the process env via
    # _env_override -- compose.yaml declares them as build.args.
    run = run or _stream
    query = query_run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    version = (release or {}).get("STRIIM_VERSION", _DEFAULT_STRIIM_VERSION)
    java_release = (release or {}).get("JAVA_RELEASE")
    manifest_mode = bool((os.environ.get(DEPS_MANIFEST_ENV) or "").strip())
    candidate = manifest_mode or not _in_checkout()
    if candidate:
        # Verify + stage the consumer inputs and record the verified
        # installer identity in the build inputs BEFORE any reuse; image_inputs_match
        # compares it, so other installer bytes (or no recorded identity) rebuild.
        _gate_striim_deps(striim_dir, version)
    else:
        _clear_installer_identity(striim_dir)
    present = image_present(version, run=query)
    if present and image_inputs_match(version, striim_dir, run=query):
        return
    _gate_docker_disk(query, progress, cold=not present)
    if progress: progress("cluster", "building image (first build is slow)")
    print(f"[striim] building {image_ref(version)} (first build is slow under emulation)…")
    with _env_override({"STRIIM_VERSION": version, "JDK_VERSION": java_release}):
        run(["docker", "compose", "build", "slt-striim"], cwd=str(striim_dir))

# Bring the cluster up with the Spanner emulator redirect baked in. The framework's
# cluster is emulator-only, so SPANNER_EMULATOR_HOST is always safe here (only Spanner
# clients read it — harmless for non-Spanner tests) and it makes SLT_SPANNER tests
# self-provisioning like GCS/Kafka. It stays out of the base compose so a plain
# `docker compose up` still targets real Spanner.
_COMPOSE_FILES = ["-f", "compose.yaml", "-f", "compose.spanner-emulator.yaml"]

def compose_files(striim_dir: Path) -> list[Path]:
    """The cluster's compose files as paths, for callers that read them rather than run them
    (services.declared_containers). Derived from the same list the compose calls use, so the
    two cannot drift."""
    return [Path(striim_dir) / f for f in _COMPOSE_FILES if f != "-f"]

def cluster_up(striim_dir: Path, release: dict | None = None, run=None, progress=None) -> None:
    if progress: progress("cluster", "starting containers")
    run = run or _stream
    version = (release or {}).get("STRIIM_VERSION", _DEFAULT_STRIIM_VERSION)
    # Fill the cluster/license config from the STRIIM_HOME install (conf/startUp.properties)
    # so a bare live run "just works" without the operator exporting
    # COMPANY_NAME/PRODUCT_KEY/LICENCE_KEY/CLUSTER_NAME by hand. An explicit, non-empty value
    # already in the environment WINS (only gaps are filled), so a deliberate override still
    # takes effect and this is a safe no-op when those vars are already set. Only the DERIVED
    # vars need _env_override — the env-supplied ones are already in os.environ.
    enriched, sources = enrich_license_env(os.environ)
    derived = {v: enriched[v] for v in _LICENSE_VARS if sources[v] == "derived"}
    if progress and derived:
        progress("cluster", f"license config derived from STRIIM_HOME: {', '.join(sorted(derived))}")
    with _env_override({"STRIIM_VERSION": version, **derived}):
        # --remove-orphans: renaming a compose SERVICE strands its container exactly as
        # renaming the project does -- compose stops managing the old one but leaves it
        # RUNNING. Upgrading past the slt-striim-node -> slt-node rename would otherwise
        # leave the old node up beside the new one: two nodes in the cluster, one of them
        # invisible to compose. Orphans are containers whose SERVICE is gone from the file,
        # so naming a subset of services here does not make the rest orphans.
        run(["docker", "compose", *_COMPOSE_FILES, "up", "-d", "--remove-orphans",
             "slt-striim", "slt-node", "slt-agent"],
            cwd=str(striim_dir))

def cluster_has_containers(striim_dir: Path, release: dict | None = None) -> bool:
    """True iff this checkout's Striim compose project owns any container, running or not.

    The ownership question `cluster_down` needs answered first: `down -v` removes the
    slt-striim-shared volume (the MDR), and a native / externally-run Striim leaves this
    project empty, so an unguarded teardown would delete state it never created. `ps -aq`,
    not `ps -q`: a stopped-but-present container is still ours to take down.

    RAISES when Docker cannot answer (daemon down, binary missing, compose error) rather than
    reporting False. "Docker did not answer" is not "the cluster is not ours" -- collapsing
    the two would let `stop live` print that it found a native Striim and exit 0 while the
    containers it should have removed are merely unreachable. No injectable `run` seam, on
    purpose: cluster_up/cluster_down take a streaming one that returns None, and a shared
    runner here would silently read as "no containers"."""
    version = (release or {}).get("STRIIM_VERSION", _DEFAULT_STRIIM_VERSION)
    with _env_override({"STRIIM_VERSION": version}):
        r = subprocess.run(["docker", "compose", *_COMPOSE_FILES, "ps", "-aq"],
                           cwd=str(striim_dir), capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"could not ask Docker what the Striim project owns: "
                           f"{(r.stderr or r.stdout).strip()[-300:]}")
    return bool(r.stdout.strip())

def cluster_down(striim_dir: Path, release: dict | None = None, run=None) -> None:
    run = run or _stream
    version = (release or {}).get("STRIIM_VERSION", _DEFAULT_STRIIM_VERSION)
    with _env_override({"STRIIM_VERSION": version}):
        # -v so teardown removes volumes (full reset semantics).
        #
        # --remove-orphans for the same reason cluster_up has it, and teardown needs it MORE:
        # a container whose compose SERVICE no longer exists survives a plain `down`, because
        # down only removes the services the file still declares. Upgrading across the
        # slt-striim-node -> slt-node rename therefore left two Striim JVMs (MEM_MAX defaults
        # to 40 GB each) running while `stop live` reported a clean teardown and exited 0.
        # preflight's orphan check cannot catch these either: it only runs when
        # `cluster_has_containers()` is False, and the still-declared slt-striim keeps it True.
        run(["docker", "compose", *_COMPOSE_FILES, "down", "-v", "--remove-orphans"],
            cwd=str(striim_dir))

# Every spelling seen or plausible: ProductKey=, LicenceKey=, "License Key:", LicenseKey,
# PRODUCT_KEY=, LICENCE_KEY=, any case.
_LICENSE_LINE = re.compile(r"(?i)((?:product[ _]?key|licen[cs]e[ _]?key)\s*[:=]\s*).*$", re.M)


def _redact_license(text: str) -> str:
    """Blank the license lines, and any raw license value that appears elsewhere. The values
    come from the environment or, on the STRIIM_HOME route, from its startUp.properties
    (enrich_license_env): cluster_up puts derived values in os.environ only while compose runs."""
    text = _LICENSE_LINE.sub(r"\1<redacted>", text)
    values, _ = enrich_license_env(os.environ)
    for var in ("PRODUCT_KEY", "LICENCE_KEY"):
        value = (values.get(var) or "").strip()
        if len(value) >= 4:
            text = text.replace(value, "<redacted>")
    return text


def save_cluster_logs(dest: Path, striim_dir: Path, run=None, tail: int = 300) -> Path | None:
    """Save ``docker logs --tail`` of each container in the cluster's compose project to
    ``dest/<container>.log``, license redacted. For a cluster that never became reachable:
    the primary's own log is where the cause is (a full disk showed there as a Derby
    NoSuchFileException). Best effort: returns ``dest``, or None when nothing was saved."""
    run = run or (lambda argv, cwd=None: subprocess.run(argv, cwd=cwd, stdout=subprocess.PIPE,
                                                        stderr=subprocess.STDOUT, text=True,
                                                        timeout=60))
    try:
        ps = run(["docker", "compose", *_COMPOSE_FILES, "ps", "-a", "--format", "{{.Name}}"],
                 cwd=str(striim_dir))
        names = [n.strip() for n in (getattr(ps, "stdout", "") or "").splitlines() if n.strip()]
        if not names:
            return None
        Path(dest).mkdir(parents=True, exist_ok=True)
        for name in names:
            r = run(["docker", "logs", "--tail", str(tail), name])
            text = (getattr(r, "stdout", "") or "") + (getattr(r, "stderr", "") or "")
            (Path(dest) / f"{name}.log").write_text(_redact_license(text))
        return Path(dest)
    except Exception:
        return None


# The app-group nodes that run OP jars. (The agent is a separate container and is left
# alone — the OP class-loader lives on these.) Base names; docker targets go through
# stack.app_nodes() so a prefixed stack (SLT_STACK_PREFIX) restarts ITS OWN nodes.
_APP_NODES = stack.APP_NODES

# Signatures a POISONED OP class-loader emits when re-LOADing a jar after a prior OP
# crash: the loader's in-memory copy of the jar is corrupt, so re-extracting it fails
# and every subsequent OP deploy in the session cascades to DEPLOY_FAILED until a node
# restart clears the loader. Matched against the node server log to decide whether a
# restart+retry is worth attempting (vs a genuine bad-TQL failure, which must NOT trigger
# a restart).
OP_LOADER_POISON_SIGNATURES = (
    "invalid LOC header",
    "LOC header invalid",
    "File copying failed during dependency verification",
    "error in opening zip file",
    "ZipException",
)

def op_loader_poisoned(log_text: str) -> bool:
    return any(sig in (log_text or "") for sig in OP_LOADER_POISON_SIGNATURES)

def app_nodes_generation(run=None) -> str:
    """A short token that CHANGES whenever an app node's JVM has restarted, or "" when it
    cannot be determined (native Striim -- no containers to inspect).

    Folded into the OP/UDF registry key so a restart invalidates every record automatically.
    This is the staleness the loaded-jar probe cannot see on its own: `LOAD` records the
    library in the MDR, which lives in a volume and survives a restart, while the class
    loaders it built live only in the JVM that died. `restart_app_nodes` clears the registry
    explicitly, but it is far from the only way to restart a node -- a host reboot, a
    `docker compose stop/start` (cluster_up runs `up -d`, which starts stopped containers), a
    Docker-daemon restart, an OOM kill someone `docker start`s, or a container recreated by a
    STRIIM_VERSION bump all leave the libraries listed and the loaders empty. Keying on the
    start time turns every one of those into a re-register instead of a skipped load.

    Best-effort by design: any failure yields "", which simply means records are not
    generation-scoped on that setup rather than that they are all discarded.
    """
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    stamps = []
    for node in stack.app_nodes():
        try:
            r = run(["docker", "inspect", "-f", "{{.State.StartedAt}}", node])
        except Exception:
            return ""
        if getattr(r, "returncode", 1) != 0:
            return ""
        stamps.append((getattr(r, "stdout", "") or "").strip())
    if not stamps or not all(stamps):
        return ""
    return hashlib.sha256("|".join(stamps).encode()).hexdigest()[:8]


def restart_app_nodes(client, run=None, settle_env="SLT_CLUSTER_SETTLE", timeout: int = 300) -> None:
    # Restart the app-group nodes to clear a poisoned OP class-loader, then wait until the
    # cluster is deploy-ready again (+ a brief settle, as node join settles async). The
    # agent container is left running so only the OP loaders reset.
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True,
                                             timeout=timeout))
    result = run(["docker", "restart", *stack.app_nodes()])
    if getattr(result, "returncode", 0) != 0:
        raise RuntimeError(f"docker restart failed: {(result.stderr or '').strip()}")
    # Every OP registration the framework recorded is now stale, and -- crucially -- NOT
    # detectably stale: `LOAD OPEN PROCESSOR` puts a PropertyTemplate in the MDR, which is
    # persistent and survives this restart, while the ModuleClassLoader it registered lives
    # only in the JVM that just died. So the loaded-OP probe would still answer "yes" for
    # every OP on a node that can no longer instantiate any of them, and a test trusting that
    # would deploy against nothing. Dropping the records is the only honest move; it costs one
    # re-register per jar, which is exactly what a restart implies.
    #
    # Done HERE rather than in the callers (plugin's poison recovery, preflight
    # --restart-app-nodes, the console's restart job) so no future caller can forget it.
    from livetest import opregistry
    opregistry.clear()
    if client is not None:
        # The token died with the JVMs. StriimApi authenticates ONCE, in __init__, and
        # getHeader() signs every later call with that cached token -- so without this,
        # every call from here on returns 401 and the recovery can never observe the
        # cluster it just restarted. wait_cluster_ready swallows the 401s and reports a
        # timeout, which sends the operator to look at node health while the nodes are
        # fine and only the credential is stale.
        #
        # Here rather than in the callers, for the same reason opregistry.clear() is:
        # plugin's poison recovery, preflight --restart-app-nodes and the console's
        # restart job all take this path, and any of them could forget it.
        # Caller's budget flows down, UNCAPPED: without it a manifest asking for `timeout: 30`
        # still paid the full 120s default here, and a 120s cap was just as wrong the other way
        # -- after a SIGKILL an emulated Mac cluster keeps its API port closed ~161s and first
        # answers authenticated at ~171s, so the cap expired ~10s after the kill and
        # wait_cluster_ready spent its whole window polling on the dead token.
        started = time.monotonic()
        reauth_failure = _reauthenticate(client, timeout=float(timeout))
        if reauth_failure is None:
            reauth_note = (f"Re-authentication after the restart succeeded at "
                           f"{time.monotonic() - started:.0f}s.")
        else:
            reauth_note = (f"The RE-AUTHENTICATION deadline ({float(timeout):.0f}s) expired first "
                           f"(last error: {reauth_failure!r}), so every poll carried the "
                           f"pre-restart token.")
        wait_cluster_ready(client, timeout=timeout, context=reauth_note)
        import os
        time.sleep(float(os.environ.get(settle_env, "20")))


def _reauthenticate(client, timeout: float = 120.0, poll: float = 3.0) -> OSError | None:
    """Refresh the cached API token, RETRYING while the node is still coming back.

    Returns None when the token was refreshed (or the client cannot refresh), and the last
    OSError when the deadline expired first, so the caller can name that deadline in its own
    timeout message instead of reporting a generic one.

    Tolerates a client that cannot (a fake in a test, or a future transport that
    authenticates per-call) -- re-auth is a repair, and failing to repair must not be louder
    than the thing being repaired.

    IT MUST ALSO TOLERATE THE CALL FAILING, which an earlier version did not. The only caller
    that matters here is restart_app_nodes, which reaches this line microseconds after
    `docker restart`: the HTTP endpoint is mid-restart, so the token fetch gets
    `ConnectionResetError(54)` or a refused connection. That escaped uncaught and failed the
    run before `wait_cluster_ready` -- which exists precisely to wait for this -- ever got to
    poll. Observed by running the recover phase's `kill` mode for the first time; no unit test
    could see it, because a stubbed client's getAuthToken never fails.

    Retries rather than merely swallowing, because a silently un-refreshed token is the
    failure mode the caller's own comment warns about: every later call 401s and
    wait_cluster_ready reports a cluster timeout while the nodes are fine and only the
    credential is stale. On exhaustion this returns quietly and lets wait_cluster_ready be
    the thing that reports a genuinely dead cluster.

    ONLY OSError IS RETRIED, AND THE NARROWNESS IS THE POINT. An earlier version of this fix
    caught bare `Exception`, which is a REGRESSION rather than extra safety, because of how
    StriimApi.getAuthToken fails: EVERY HTTP-level auth failure -- a wrong STRIIM_PASS, a
    503 -- arrives here as RuntimeError("Striim authentication failed: ..."). Retrying that turns a
    one-second, correct error into 120s of futile polling followed by a 300s
    wait_cluster_ready timeout whose message says the token merely expired and the nodes are
    fine. That is a worse outcome than the crash it replaced, on the two operator-facing
    callers (preflight --restart-app-nodes, and opartifacts.place_on_agent).

    OSError is exactly the transient set and nothing more: ConnectionResetError is an OSError,
    and requests' ConnectionError/Timeout subclass it via RequestException(IOError).
    RuntimeError is not an OSError, so a permanent auth failure fails fast.

    THE DEADLINE BOUNDS THE CALLS TOO, roughly: each getAuthToken attempt gets what is left of
    it (at least 1 s) as its request timeout, so a node that completes the TCP handshake and
    then never answers cannot hold one call much past the deadline. requests' read timeout is
    per socket read, so a server that trickles bytes can still stretch one call.
    """
    api = getattr(client, "api", None)
    get_token = getattr(api, "getAuthToken", None)
    if not callable(get_token):
        return None
    try:   # a getAuthToken without `timeout` (a test double, another transport) still works
        bounded = "timeout" in inspect.signature(get_token).parameters
    except (TypeError, ValueError):
        bounded = False
    deadline = time.monotonic() + timeout
    while True:
        left = max(1.0, deadline - time.monotonic())
        try:
            if bounded:
                get_token(timeout=(min(10.0, left), left))   # bounded by the deadline
            else:
                get_token()
            return None
        except OSError as e:
            if time.monotonic() >= deadline:
                return e
            time.sleep(poll)

def wait_agent_registered(client, timeout: int = 300, poll: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            t = parse_deployment_groups(client.list_deployment_groups())
            if t.has_agent:
                return
        except Exception:
            pass
        if time.monotonic() >= deadline:
            raise TimeoutError(f"agent did not register within {timeout}s")
        time.sleep(poll or 0.05)

def wait_cluster_ready(client, timeout: int = 300, poll: float = 10, progress=None,
                       context: str = "") -> None:
    # `context` is appended to either timeout message: restart_app_nodes uses it to say whether
    # its re-authentication deadline had already expired, so the two deadlines are never confused.
    # The full provisioned topology is ready only when BOTH the 2nd node has joined
    # `default` (has_cluster) AND the agent has registered (has_agent). Waiting only
    # for the agent races: a `cluster` test would see has_cluster=False and skip.
    started = time.monotonic()
    deadline = started + timeout
    # Whether ANY poll got far enough to read a deployment-group list. If none did, the
    # cluster's readiness was never actually observed and "not ready" is the wrong story:
    # the usual cause is a stale token after a node restart (401 on every poll), and
    # reporting it as a cluster problem sends the operator to look at healthy nodes.
    answered = False
    last_error: Exception | None = None
    while True:
        try:
            t = parse_deployment_groups(client.list_deployment_groups())
            answered = True
            if t.has_cluster and t.has_agent:
                return
        except Exception as e:
            last_error = e
        if time.monotonic() >= deadline:
            if not answered:
                raise TimeoutError(
                    f"CLUSTER-READY deadline ({timeout}s) expired with no authenticated reply "
                    f"from the cluster (every LIST DEPLOYMENTGROUPS poll failed; last error: "
                    f"{last_error}). The nodes may be healthy -- an expired token after a node "
                    f"restart looks exactly like this. {context}".rstrip())
            raise TimeoutError(f"CLUSTER-READY deadline ({timeout}s) expired: cluster "
                               f"(>=2 nodes + agent) not ready within {timeout}s. "
                               f"{context}".rstrip())
        if progress:
            elapsed = time.monotonic() - started
            progress("cluster", f"waiting for node + agent to register ({elapsed:.0f}s/{timeout}s)")
        time.sleep(poll or 0.05)
