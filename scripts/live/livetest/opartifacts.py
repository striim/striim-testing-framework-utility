from __future__ import annotations
import contextlib
import dataclasses
import filecmp
import hashlib
import os
import shutil
import tempfile
import re
import subprocess
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from livetest import paths, stack

# OP artifact provisioning: build an OpenProcessor's (or UDF's) jar AGAINST A GIVEN
# RELEASE (once per run, if missing/stale) and place it — plus its config JSON(s) —
# into the Striim server's UploadedFiles/, where the example's `LOAD OPEN PROCESSOR
# "UploadedFiles/<jar>"` / `ConfigFile: 'UploadedFiles/<json>'` resolve at deploy.
# Mode-aware: docker cp into the cluster containers, or a filesystem copy for a
# native Striim.
#
# A test.yaml `op.jar` field is a repo-relative MODULE reference (the module dir, or
# its pom.xml) — never a versioned jar path. The release (detected from STRIIM_HOME by
# livetest.releases) fixes STRIIM_SERIES, which the gold-standard poms bake into
# the shaded jar's finalName (`${artifactId}-${STRIIM_SERIES}.jar`); we glob for that
# rather than hardcoding a name, so the same code builds every gold OP/UDF.

# `jar:` module refs resolve against the project root (SLT_PROJECT_ROOT; this clone when unset),
# where a customer's own OP/UDF source lives.
def _root() -> Path:
    """Where `jar:` module refs resolve, at call time: the active project's consumer root, else
    SLT_PROJECT_ROOT, else this clone (livetest.project.example_root)."""
    from livetest import project as _project
    return _project.example_root()
# The jar lands in the cluster containers that run apps on the `default` group. Base names;
# docker targets resolve through stack.app_nodes() at call time (SLT_STACK_PREFIX-aware).
_CLUSTER_NODES = stack.APP_NODES
_UPLOADED = "/opt/striim/UploadedFiles"

class OpArtifactError(Exception):
    pass

@dataclass(frozen=True)
class BuiltArtifact:
    path: Path    # absolute path to the built, series-suffixed jar
    name: str     # OP_JAR  -- the jar's filename, e.g. "FooOpV8G-5.4.jar"
    op_name: str  # OP_NAME -- filename with the trailing "-<SERIES>.jar" removed,
                  #            e.g. "FooOpV8G"
    sha256: str = ""  # content digest, computed by the CALLER while it still holds the build
                      # lock (see plugin.build_modules). Empty when nobody computed one; the
                      # register path then hashes on demand. Carrying it exists so the digest
                      # provably describes the bytes `mvn` just produced -- hashing later, with
                      # the build lock released, can read a jar a sibling runner is rewriting.
                      # Empty for OP jars, which carry `fingerprint` instead.
    fingerprint: str = ""  # OP jars: the jar_content_fingerprint their content name is cut from
    built_name: str = ""   # OP jars: the name the build produced, before content naming
    module_name: str = ""  # OP jars: the manifest's Striim-Module-Name (the OP's identity)

    @property
    def content_tag(self) -> str:
        """OP jars: the content tag in `name` (see content_addressed); empty otherwise."""
        return _tag(self.fingerprint) if self.fingerprint else ""

def _module_dir(module_ref: str) -> Path:
    # module_ref is normally a repo-relative module dir (e.g.
    # "java/OpenProcessors/FooOp") or its pom.xml. Back-compat: also accepts
    # the old "<module>/target/<versioned>.jar" shape some callers may still pass.
    p = Path(module_ref)
    if p.name == "pom.xml":
        return p.parent
    parts = p.parts
    if "target" in parts:
        return Path(*parts[:parts.index("target")])
    return p

def module_name(module_ref: str) -> str:
    """The module's directory name, e.g. "FooOp" — used for per-module
    identification/guards."""
    return _module_dir(module_ref).name

def _release_jar_candidates(module_dir: Path, series: str) -> list[Path]:
    target = module_dir / "target"
    if not target.is_dir():
        return []
    return sorted(
        p for p in target.glob(f"*-{series}.jar")
        if not p.name.startswith("original-") and not p.name.endswith("-SNAPSHOT.jar"))

def op_name_for(jar_name: str, series: str) -> str:
    suffix = f"-{series}.jar"
    if not jar_name.endswith(suffix):
        raise OpArtifactError(
            f"built jar {jar_name!r} does not end with the expected {suffix!r} "
            f"suffix for STRIIM_SERIES={series!r}")
    return jar_name[: -len(suffix)]

# An OP jar is uploaded and loaded under a name that carries a tag of its contents, and a name
# once published on a cluster is never overwritten, so one name never holds two different sets of
# bytes there.
#
# Why: Striim keeps a copy of each loaded OP jar at .striim/OpenProcessor/<name>, overwrites it
# in place on UNLOAD with whatever UploadedFiles/<name> holds, and reads it at LOAD through the
# JDK's jar: URL cache, which is keyed by path and never evicted (Striim never closes its module
# class loaders). An UNLOAD while UploadedFiles/<name> holds other bytes than were loaded leaves
# a cached handle whose zip index no longer matches the file, and every later LOAD of that name
# fails with "ZipFile invalid LOC header (bad signature)" until the JVM restarts. Reproduced on
# 5.4.0.6C with a 1.3 MB jar; size plays no part.
#
# The tag comes from jar_content_fingerprint, not the file's sha256: these builds are not
# reproducible, so a raw digest would give every rebuild of unchanged sources a new name, a
# cluster-wide reload and another jar in UploadedFiles. Two builds with one fingerprint differ
# only in timestamps; whichever reached the cluster first stays under that name.
_CONTENT_DIGITS = 12
_CONTENT_KEEP = 5      # local copies kept per OP; older ones are pruned
_CONTENT_MIN_AGE_S = 24 * 3600   # never prune a copy used this recently: a run may still hold it


def jar_content_fingerprint(path) -> str:
    """sha256 over each entry's (name, CRC-32, uncompressed size), name-sorted. Falls back to
    the raw file digest if the jar cannot be read as a zip -- callers upstream (build_jar's
    _jar_unreadable probe) reject a truncated jar long before this, so the fallback only keeps
    a fingerprint available rather than papering over corruption."""
    h = hashlib.sha256()
    try:
        with zipfile.ZipFile(path) as z:
            for info in sorted(z.infolist(), key=lambda i: i.filename):
                h.update(info.filename.encode("utf-8"))
                h.update(b"\0")
                h.update(f"{info.CRC:08x}:{info.file_size}".encode("ascii"))
                h.update(b"\n")
    except (OSError, zipfile.BadZipFile):
        return file_sha256(path)
    return h.hexdigest()


def file_sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def jar_module_name(path) -> str:
    """The jar's `Striim-Module-Name` manifest entry, or "" if it has none."""
    try:
        with zipfile.ZipFile(path) as z:
            text = z.read("META-INF/MANIFEST.MF").decode("utf-8", "replace")
    except (OSError, KeyError, zipfile.BadZipFile):
        return ""
    m = re.search(r"^Striim-Module-Name:\s*(\S+)", text, re.MULTILINE)
    return m.group(1) if m else ""


def _tag(fingerprint: str) -> str:
    return fingerprint[:_CONTENT_DIGITS]


def content_addressed_name(jar_name: str, tag: str, series: str | None) -> str:
    """`jar_name` with `tag` inserted before its first dot, so Striim's temp copy (named from the
    jar name up to the first dot) differs per build too.

    A series-suffixed jar keeps the suffix last: "FooOpV2-5.4.jar" -> "FooOpV2-<tag>-5.4.jar".
    Any other name: "Dup_5.4.2.scm" -> "Dup_5-<tag>.4.2.scm"."""
    suffix = f"-{series}.jar" if series else None
    if not (suffix and jar_name.endswith(suffix)):
        # A version suffix of another series (a published jar from another release).
        m = re.search(r"-\d+(?:\.\d+)+\.jar$", jar_name)
        suffix = m.group(0) if m else None
    if suffix and jar_name.endswith(suffix) and "." not in jar_name[: -len(suffix)]:
        return f"{jar_name[: -len(suffix)]}-{tag}{suffix}"
    head, dot, tail = jar_name.partition(".")
    return f"{head}-{tag}{dot}{tail}"


_STORE_DIR = ".slt-op-jars"


def content_addressed_file(path: Path, series: str | None) -> BuiltArtifact:
    """A staged OP jar (a server_files `load: open_processor` entry) renamed in place to its
    content name and described like a built one. It is not in a store, so callers upload it with
    keep_existing (_register_op_jar does)."""
    fingerprint = jar_content_fingerprint(path)
    name = content_addressed_name(path.name, _tag(fingerprint), series)
    final = path.rename(path.with_name(name))
    try:
        op_name = op_name_for(path.name, series) if series else Path(path.name).stem
    except OpArtifactError:
        op_name = Path(path.name).stem          # not a series-suffixed name
    return BuiltArtifact(final, name, op_name, fingerprint=fingerprint, built_name=path.name,
                         module_name=jar_module_name(final))


def _link_no_clobber(tmp: Path, final: Path) -> None:
    """Publish `tmp` as `final` unless `final` exists: a concurrent writer's bytes stay. Where hard
    links are unsupported (SMB, exFAT, some NFS), an atomic replace, unless `final` appeared
    meanwhile. `tmp` is always removed."""
    try:
        os.link(tmp, final)
    except FileExistsError:
        pass
    except OSError:
        if not os.path.exists(final):
            os.replace(tmp, final)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def any_tag_pattern(content_name: str, tag: str):
    """A regex for `content_name` with any content tag in place of `tag`: every build of the same
    OP and series. None when the tag is not in the name exactly once."""
    escaped, t = re.escape(content_name.lower()), tag.lower()
    if not t or escaped.count(t) != 1:
        return None
    return re.compile(escaped.replace(t, f"[0-9a-f]{{{_CONTENT_DIGITS}}}"))


def plain_name(content_name: str, tag: str) -> str:
    """The name the build produced, before content naming: `content_name` without `-<tag>`."""
    return re.sub(re.escape(f"-{tag}"), "", content_name, count=1, flags=re.IGNORECASE)


def op_jar_store() -> Path:
    """Where content-named OP jar copies are staged: <state dir>/.slt-op-jars."""
    from livetest import layout
    return layout.state_dir() / _STORE_DIR


def content_addressed(built: BuiltArtifact, store: Path | None = None) -> BuiltArtifact:
    """`built` re-pointed at a content-named copy of its jar, made only if the store lacks it.

    Callers hold the module's build lock, so target/<jar> is not rewritten while it is read.
    `op_name` is unchanged: ${<TOKEN>_NAME} is the module name, not a file name."""
    try:
        return _content_addressed(built, store)
    except OpArtifactError:
        raise
    except Exception as e:      # noqa: BLE001 -- an unwritable state dir or full disk is a build failure
        raise OpArtifactError(f"could not stage a content-named copy of {built.name}: {e}") from e


# Per process: (source path, size, mtime, store) -> the content-addressed artifact. build_modules
# runs per test; an unchanged jar needs no new fingerprint, copy or prune.
_CONTENT_MEMO: dict = {}


def _content_addressed(built: BuiltArtifact, store: Path | None) -> BuiltArtifact:
    store = store or op_jar_store()
    st = os.stat(built.path)
    key = (str(built.path), st.st_size, st.st_mtime_ns, str(store))
    hit = _CONTENT_MEMO.get(key)
    if hit is not None:
        try:
            os.utime(hit.path)                   # still in use: keep it out of other lanes' prune
            return hit
        except FileNotFoundError:
            pass                                 # pruned meanwhile: make it again
        except OSError:
            return hit                           # another user's copy; it exists
    result = _content_addressed_uncached(built, store)
    _CONTENT_MEMO[key] = result
    return result


def _content_addressed_uncached(built: BuiltArtifact, store: Path) -> BuiltArtifact:
    store.mkdir(parents=True, exist_ok=True)
    ignore = store / ".gitignore"
    if not ignore.exists():
        # The state dir is usually inside the consumer's checkout; keep these copies out of git.
        ignore.write_text("*\n")
    series = built.name[len(built.op_name) + 1: -len(".jar")]
    fingerprint = jar_content_fingerprint(built.path)
    tag = _tag(fingerprint)
    name = content_addressed_name(built.name, tag, series)
    final = store / name
    try:
        os.utime(final)                          # most recently used, for pruning
    except OSError:
        pass                                     # absent, or another user's copy
    # Checked after the utime: a concurrent lane may have pruned the copy in between.
    if not final.exists():
        fd, tmp = tempfile.mkstemp(dir=store, prefix=".staging-")
        os.close(fd)
        try:
            shutil.copyfile(built.path, tmp)
            os.chmod(tmp, 0o644)                 # mkstemp's 0600 would shut out other users
            _link_no_clobber(Path(tmp), final)   # a concurrent lane's copy stays
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    _prune_store(store, tag, keep=final)
    return dataclasses.replace(built, path=final, name=name, fingerprint=fingerprint,
                               built_name=built.name, module_name=jar_module_name(final))


def _prune_store(store: Path, tag: str, keep: Path) -> None:
    # The same name with any tag, wherever content_addressed_name put it (dotted names too).
    pattern = any_tag_pattern(keep.name, tag)
    if pattern is None:
        return
    # Another lane may prune the same store concurrently, so a copy can vanish between calls.
    copies = []
    for p in store.iterdir():
        if pattern.fullmatch(p.name.lower()) and p != keep:
            try:
                copies.append((p.stat().st_mtime, p))
            except OSError:
                pass
    copies.sort(reverse=True)
    now = time.time()
    for stale in store.glob(".staging-*"):       # a copy cut short by a kill or a full disk
        try:
            if now - stale.stat().st_mtime > _CONTENT_MIN_AGE_S:
                stale.unlink()
        except OSError:
            pass
    for mtime, old in copies[_CONTENT_KEEP - 1:]:
        if now - mtime < _CONTENT_MIN_AGE_S:
            continue
        try:
            old.unlink()
        except OSError:
            pass


_ADD_SOURCE_RE = re.compile(r"<source>\s*([^<]+?)\s*</source>")


def add_source_roots(module_dir: Path) -> list[Path]:
    """Extra source roots this module's pom pulls in via build-helper `add-source`.

    A shared library (OpenProcessorCommon, SampleCommon) is compiled INTO this module's
    jar rather than depended on as one, so an edit there changes this artifact exactly
    like an edit under src/ — and must trigger the same rebuild.

    Read from the pom rather than hardcoded, so a new shared root is covered the moment
    a module add-sources it, and only for the modules that actually do.
    """
    try:
        text = (module_dir / "pom.xml").read_text()
    except OSError:
        return []
    roots = []
    for raw in _ADD_SOURCE_RE.findall(text):
        resolved = raw.replace("${project.basedir}", str(module_dir))
        if "${" in resolved:
            # An unresolved property would silently resolve to a non-existent dir and
            # scan nothing; skipping is equally wrong but at least not silent-by-design.
            continue
        path = Path(resolved)
        if path.is_dir():
            roots.append(path.resolve())
    return roots


def _stale_trigger(jar: Path, module_dir: Path) -> Path | None:
    # Return the SOURCE that is newest-newer than the built jar (so a rebuild note can name
    # what changed), or None when the jar is up to date. Rebuild if any source is newer —
    # otherwise an edit to the OP's Java (without `mvn clean`) is silently ignored and the
    # live suite uploads the OLD jar, reporting green over a real regression. Covers every
    # file under src/main (java AND resources), pom.xml (a dependency/version/shade change
    # alters the artifact), and every add-sourced shared root (see add_source_roots) —
    # shared code compiles into this jar, so it is this module's source for staleness.
    #
    # src/test is deliberately EXCLUDED: tests are compiled but never packaged, so no edit
    # there can change the jar's bytes, and including it made a test-only edit trigger a
    # full rebuild that could not alter the artifact. Trade-off: the build's `-DskipTests`
    # test COMPILE (which catches a Java-16+ construct in a test before it breaks a Java-11
    # release build) no longer re-runs for a test-only edit — it fires on the next rebuild
    # from any main/pom change, and in CI.
    try:
        jar_mtime = jar.stat().st_mtime
    except FileNotFoundError:
        return jar
    srcs = [p for p in (module_dir / "src" / "main").rglob("*") if p.is_file()]
    for root in add_source_roots(module_dir):
        srcs.extend(p for p in root.rglob("*") if p.is_file())
    pom = module_dir / "pom.xml"
    if pom.exists():
        srcs.append(pom)
    newer = [p for p in srcs if p.stat().st_mtime > jar_mtime]
    return max(newer, key=lambda p: p.stat().st_mtime) if newer else None

def _running_java_major(run=None) -> str | None:
    # Best-effort major version of whatever `java` the ambient JAVA_HOME/PATH resolves
    # to, so a build that already matches JAVA_RELEASE doesn't need a JAVA_HOME
    # override. Returns None if it can't be determined (java missing / unparseable).
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    java_bin = "java"
    home = os.environ.get("JAVA_HOME")
    if home:
        candidate = Path(home) / "bin" / "java"
        if candidate.exists():
            java_bin = str(candidate)
    try:
        r = run([java_bin, "-version"])
    except FileNotFoundError:
        return None
    text = (getattr(r, "stderr", "") or "") + (getattr(r, "stdout", "") or "")
    m = re.search(r'version "(\d+)(?:\.(\d+))?', text)
    if not m:
        return None
    return m.group(2) if m.group(1) == "1" else m.group(1)   # "1.8.0_x" -> "8"

def resolve_build_java_home(java_release: str, run=None, platform: str | None = None) -> str | None:
    """A JAVA_HOME override for the build subprocess, or None to leave the ambient
    JAVA_HOME/PATH java alone (it already matches JAVA_RELEASE). Raises
    OpArtifactError naming the missing JDK when the release needs a different major
    version and macOS's `java_home` can't find it.

    Off macOS the JDK comes only from SLT_JDK<release>_HOME:
    one variable, no /usr/lib/jvm guess; unset, or a home without bin/java, is an
    OpArtifactError naming it. ``platform`` takes the os.uname().sysname spelling."""
    if _running_java_major(run=run) == str(java_release):
        return None
    if (platform or os.uname().sysname) == "Darwin":
        probe = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
        r = probe(["/usr/libexec/java_home", "-v", str(java_release)])
        out = (getattr(r, "stdout", "") or "").strip()
        if getattr(r, "returncode", 1) != 0 or not out:
            raise OpArtifactError(
                f"build needs JDK {java_release} (JAVA_RELEASE) but it was not found via "
                f"'/usr/libexec/java_home -v {java_release}'; install it "
                f"(e.g. 'brew install openjdk@{java_release}' and register it with "
                f"/usr/libexec/java_home, or set JAVA_HOME yourself) and retry.")
        return out
    var = f"SLT_JDK{java_release}_HOME"
    home = os.environ.get(var)
    if not home:
        raise OpArtifactError(
            f"build needs JDK {java_release} (JAVA_RELEASE) but the running java is a different major "
            f"version and {var} is unset; set {var} to a JDK {java_release} home "
            f"(no /usr/lib/jvm guess is made)")
    # Absolute before the check: mvn runs in the module dir, and a quoted ~ is not expanded.
    home = str(Path(home).expanduser().resolve())
    if not (Path(home) / "bin" / "java").is_file():
        raise OpArtifactError(f"{var}={home} has no bin/java")
    return home

def _default_run(argv, cwd, env):
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True)

def _verify_striim_home(build_env: dict, release: dict) -> None:
    """Fail fast if STRIIM_HOME points at an install that does not contain STRIIM_VERSION.

    Guards the classic mismatch: a stale exported STRIIM_HOME (say Striim_5.4.0.6) combined
    with a release-set STRIIM_VERSION (say 5.4.0.6C) otherwise produces an impossible
    systemPath (`.../lib/Platform-5.4.0.6C.jar` under the 5.4.0.6 install) that surfaces as a
    confusing 'could not find artifact' deep in `mvn`. Platform-<version>.jar is present in
    every Striim install's lib/, so it is the marker. Skipped when STRIIM_HOME is unset (the
    pom then falls back to its own default)."""
    home = build_env.get("STRIIM_HOME")
    version = release.get("STRIIM_VERSION")
    if not home or not version:
        return
    marker = Path(home) / "lib" / f"Platform-{version}.jar"
    if not marker.exists():
        raise OpArtifactError(
            f"STRIIM_HOME={home} does not contain STRIIM_VERSION={version} (missing {marker}). "
            f"Point STRIIM_HOME at the {version} install, or set STRIIM_RELEASE to the release "
            f"matching your install.")

# --- release fingerprint + bytecode guards ---------------------------------------
# A built jar is only valid for the exact release it was compiled against. `_is_stale`
# catches SOURCE edits; these catch a changed BUILD ENVIRONMENT (Striim version/series or
# Java level), which leaves source mtimes untouched so mtime alone can't see it. Two guards:
# (1) each gold pom stamps STRIIM_VERSION/SERIES/JAVA_RELEASE into the jar's MANIFEST.MF
# (Striim-Build-* entries), so ANY builder's jar self-describes and reuse is refused when the
# stamped release differs from the current one; (2) a builder-agnostic check that the jar's own
# bytecode is compiled at the expected Java level -- catches a stale-repackage whose manifest
# lies, and any jar built before the manifest stamp existed. A jar with no stamp falls back to (2).

def _build_fingerprint(release: dict) -> dict:
    jr = release.get("JAVA_RELEASE")
    return {
        "STRIIM_VERSION": release.get("STRIIM_VERSION"),
        "STRIIM_SERIES": release.get("STRIIM_SERIES"),
        "JAVA_RELEASE": None if jr is None else str(jr),
    }

def _read_manifest_fingerprint(jar: Path) -> dict | None:
    # The release identity a jar was built against, read from the Striim-Build-* entries its pom
    # stamps into META-INF/MANIFEST.MF. None when the jar has no COMPLETE stamp -- built before the
    # entries existed, by another toolchain, or with an unresolved ${property}; callers treat None
    # as "unknown" (fall back to the bytecode guard), NOT as a mismatch.
    import zipfile
    keymap = {"Striim-Build-Version": "STRIIM_VERSION",
              "Striim-Build-Series": "STRIIM_SERIES",
              "Striim-Build-Java": "JAVA_RELEASE"}
    try:
        with zipfile.ZipFile(jar) as z:
            text = z.read("META-INF/MANIFEST.MF").decode("utf-8", "replace")
    except (zipfile.BadZipFile, OSError, KeyError):
        return None
    fp = {}
    for line in text.splitlines():
        key, sep, val = line.partition(":")
        if sep and key.strip() in keymap:
            fp[keymap[key.strip()]] = val.strip()
    if len(fp) != 3 or any(not v or v.startswith("${") for v in fp.values()):
        return None
    return fp

# class-file major = 44 + Java feature version (Java 8 -> 52, 11 -> 55, 17 -> 61).
_CLASS_MAJOR_BASE = 44

def _expected_class_major(java_release) -> int | None:
    try:
        return _CLASS_MAJOR_BASE + int(java_release)
    except (TypeError, ValueError):
        return None

def _own_class_pattern(module_dir: Path | None):
    # The module's own classes, from its sources: a jar class at a source file's package path,
    # where a path segment may carry a suffix (a versioned build relocates com/acme/FooOp to
    # com/acme/FooOpV2C). A path, not just a file name, because a shaded or bundled dependency
    # can share a simple name (com/google/cloud/spanner/Dialect beside Orm/Dialect.java).
    import re
    if module_dir is None:
        return None
    src = Path(module_dir) / "src" / "main" / "java"
    alts = set()
    for f in (src.rglob("*.java") if src.is_dir() else ()):
        parts = f.relative_to(src).with_suffix("").parts
        alts.add("/".join(re.escape(p) + "[A-Za-z0-9_]*" for p in parts[:-1])
                 + "/" * bool(parts[:-1]) + re.escape(parts[-1]) + r"\.class")
    return re.compile("(?:" + "|".join(sorted(alts)) + ")") if alts else None


def _jar_own_class_major(jar: Path, module_dir: Path | None = None) -> int | None:
    # Major version of one of the module's OWN classes -- NOT a shaded dependency, which may
    # legitimately be compiled at a different level. Own classes follow `module_dir`'s sources
    # (_own_class_pattern); when none match, the template's com/example/**. None if the jar has
    # no such class or can't be read.
    import zipfile, struct
    own = _own_class_pattern(module_dir)
    try:
        with zipfile.ZipFile(jar) as z:
            classes = [n for n in z.namelist()
                       if n.endswith(".class") and "-info" not in n.rsplit("/", 1)[-1]]
            names = sorted(n for n in classes if own and own.fullmatch(n)) or sorted(
                n for n in classes if n.startswith("com/example/"))   # the template's package
            if not names:
                return None
            data = z.read(names[0])
            if len(data) < 8 or data[:4] != b"\xca\xfe\xba\xbe":
                return None
            return struct.unpack(">H", data[6:8])[0]
    except (zipfile.BadZipFile, OSError, KeyError):
        return None


def _jar_unreadable(jar: Path) -> str | None:
    # A short human-readable reason when the jar on disk is not a readable zip archive
    # (truncated/corrupt -- typically an interrupted or concurrent `mvn`), or None when
    # it opens fine. This is deliberately a SEPARATE probe from the guards above: they
    # return None both for "corrupt" and for legitimately-unknowable cases (no manifest
    # stamp, no own classes in a valid jar), and those must stay reusable -- only actual
    # unreadability may force a rebuild.
    import zipfile
    try:
        with zipfile.ZipFile(jar):
            pass
    except (zipfile.BadZipFile, OSError) as e:
        return f"{type(e).__name__}: {e}"
    return None

def _assert_jar_java_version(jar: Path, java_release, module_dir: Path) -> None:
    expected = _expected_class_major(java_release)
    got = _jar_own_class_major(jar, module_dir)
    if expected is None or got is None:
        return   # can't determine -> don't block
    if got != expected:
        raise OpArtifactError(
            f"{jar.name} contains Java class-file major version {got}, but this release needs "
            f"JAVA_RELEASE={java_release} (major {expected}) -- the jar was compiled with the wrong "
            f"JDK (a stale build from another release). Run 'mvn clean' in {module_dir} "
            f"and retry.")

def build_jar(module_ref: str, release: dict, run=None, report=None) -> BuiltArtifact:
    """Build module_ref (a repo-relative module dir or its pom.xml) against `release`
    (a dict as returned by livetest.releases.resolve_release / detect_release) and return the resulting
    BuiltArtifact. A matching `*-<SERIES>.jar` is REUSED only when it is newer than every
    module source AND its fingerprint sidecar records the same release; otherwise it is
    (re)built with the release env merged into the subprocess so each pom's `release-from-env`
    profile activates. A jar whose MANIFEST is stamped for a DIFFERENT release fails fast (asking
    for `mvn clean`); a jar with no stamp (built before the entries existed / by another toolchain)
    is accepted so long as the builder-agnostic bytecode guard confirms the right Java level.

    When a jar is (re)built, `report` (a callable taking one string) is invoked once with a
    human-readable trigger — the reason the automatic rebuild fired (source changed / no jar
    for this series / stale target-classes cleared). It is NOT called when an up-to-date jar
    is reused. Defaults to a no-op so direct callers need not pass it."""
    module_dir = _root() / _module_dir(module_ref)
    notify = report or (lambda _reason: None)
    series = release.get("STRIIM_SERIES")
    if not series:
        raise OpArtifactError(f"release is missing STRIIM_SERIES: {release!r}")
    fingerprint = _build_fingerprint(release)
    java_release = release.get("JAVA_RELEASE")
    existing = _release_jar_candidates(module_dir, series)
    # >1 candidate is stale build output, not a broken repo: a version bump changes the
    # pom's finalName, and `mvn package` writes the new jar BESIDE the old one instead of
    # replacing it. Treated as a clean-rebuild trigger below rather than an error, because
    # (a) the harness cannot know which jar to upload, and (b) a plain rebuild would not
    # help -- only `mvn clean` removes the stale one. Erroring here instead made the test
    # SKIP, which reads like a pass in a summary line.
    ambiguous = len(existing) > 1
    jar = existing[0] if len(existing) == 1 else None
    # A jar that is not a readable zip (truncated by an interrupted/concurrent `mvn`) must
    # never be reused: the manifest/bytecode guards read it as None ("unknown", which is
    # legitimate for a VALID jar and must not block), and the mtime check cannot see
    # corruption -- so without this probe the truncated jar was uploaded to Striim, which
    # died with `ZipException: zip END header not found` at deploy: a test FAILURE where
    # a rebuild (or, if unbuildable, a skip) is the right outcome.
    corrupt = _jar_unreadable(jar) if jar is not None else None

    if jar is not None and corrupt is None:
        recorded = _read_manifest_fingerprint(jar)
        if recorded is not None and recorded != fingerprint:
            # The jar's MANIFEST is stamped for a different release -> clean rebuild required.
            clean = True
            reason = (f"{jar.name} was built for {recorded}, but this run resolves to "
                      f"{fingerprint} (different Striim release / Java level) -> clean rebuild")
        else:
            # recorded matches, OR is absent (no Striim-Build-* manifest stamp -- a jar built before the
            # entries existed or by another toolchain). Absent is NOT a failure -- but the builder-
            # agnostic bytecode guard must hold: it catches a wrong Java LEVEL no matter who built the
            # jar (the 5.4/Java17 <-> 5.0/Java11 switch) and a stale-repackage whose manifest lies.
            _assert_jar_java_version(jar, java_release, module_dir)
            trigger = _stale_trigger(jar, module_dir)
            if trigger is None:
                return BuiltArtifact(path=jar, name=jar.name, op_name=op_name_for(jar.name, series))
            # Any source change forces `mvn clean` -- this harness never does an
            # incremental rebuild. An incremental `package` reuses target/, and this pom
            # points shade's <outputFile> at the SAME path maven-jar-plugin writes (see
            # <finalName>), so what lands in target/ depends on what was already there.
            # A full rebuild from an empty target/ costs a slower build and makes the jar
            # a pure function of the sources.
            clean = True
            try:
                changed = trigger.relative_to(module_dir)
            except ValueError:
                changed = trigger
            reason = f"source changed ({changed} is newer than {jar.name}) -> clean rebuild"
    elif corrupt is not None:
        # The interrupted build that truncated the jar may have left target/classes
        # half-written too, and shade would fold those into a plain `package` -- only
        # `mvn clean` guarantees a known-good state.
        clean = True
        reason = f"{jar.name} is not a readable jar ({corrupt}) -> clean rebuild"
    elif ambiguous:
        # Several *-<series>.jar in target/ (typically a version bump: V1B alongside V1C).
        # `clean` is mandatory -- a plain `package` leaves the stale jar in place and the
        # next run is ambiguous again.
        clean = True
        reason = (f"multiple *-{series}.jar in target/ "
                  f"({', '.join(p.name for p in existing)}) -> clean rebuild")
    else:
        # No jar for this series. If target/ still holds a DIFFERENT release's compiled output,
        # clean it -- mvn's compiler skips unchanged sources, so a plain `package` could shade
        # stale .class files (e.g. Java-17 bytecode into a nominally-5.0 jar). A fresh checkout
        # (no target/classes) needs no clean.
        clean = (module_dir / "target" / "classes").is_dir()
        reason = (f"no *-{series}.jar and stale target/classes present -> clean rebuild"
                  if clean else f"no *-{series}.jar found -> initial build")

    run = run or _default_run
    build_env = {**os.environ, **{k: str(v) for k, v in release.items() if v is not None}}
    if "STRIIM_HOME" not in build_env:
        candidate_home = Path.home() / ".striim" / str(release.get("STRIIM_VERSION", ""))
        if (candidate_home / "lib" / f"Platform-{release.get('STRIIM_VERSION')}.jar").exists():
            build_env["STRIIM_HOME"] = str(candidate_home)
    _verify_striim_home(build_env, release)
    if java_release:
        java_home = resolve_build_java_home(str(java_release))
        if java_home:
            build_env["JAVA_HOME"] = java_home
    # -DskipTests (not -Dmaven.test.skip): COMPILE the test sources at the release's Java level
    # so a Java-16+ construct slipping into a test is caught here (it would otherwise break the
    # Java-11 release builds), but do NOT RUN the suites. `clean` is prepended only when a prior
    # different-release build must be discarded (see above).
    # Announce the automatic (re)build and the trigger that caused it, so a long silent
    # `mvn` step in the live console is explained (and a surprise rebuild is visible).
    notify(reason)
    goals = ["mvn", "-q"] + (["clean"] if clean else []) + ["package", "-DskipTests=true"]
    r = run(goals, str(module_dir), build_env)
    if getattr(r, "returncode", 1) != 0:
        out = (getattr(r, "stderr", "") or getattr(r, "stdout", "") or "")[-800:]
        raise OpArtifactError(f"mvn package failed in {module_dir}:\n{out}")
    existing = _release_jar_candidates(module_dir, series)
    if len(existing) != 1 and not clean:
        # A non-clean build can itself create the ambiguity the branch above only catches
        # on the NEXT run: a version bump changes the pom's finalName, so `mvn package`
        # writes the new jar BESIDE the old one instead of replacing it, and the run that
        # performed the bump-rebuild then dies here. Retrying with `clean` -- which empties
        # target/ -- resolves it in the SAME run, so a version bump costs one extra build
        # rather than one failed run plus a manual retry. Since a source change now forces
        # `clean`, the only path that still reaches here is an initial build into a
        # target/ that has no compiled output.
        notify(f"ambiguous *-{series}.jar after non-clean build "
               f"({', '.join(p.name for p in existing)}) -> clean rebuild")
        r = run(["mvn", "-q", "clean", "package", "-DskipTests=true"], str(module_dir), build_env)
        if getattr(r, "returncode", 1) != 0:
            out = (getattr(r, "stderr", "") or getattr(r, "stdout", "") or "")[-800:]
            raise OpArtifactError(f"mvn clean package failed in {module_dir}:\n{out}")
        existing = _release_jar_candidates(module_dir, series)
    if len(existing) != 1:
        raise OpArtifactError(
            f"build succeeded but no unambiguous *-{series}.jar found in "
            f"{module_dir / 'target'} (found: {[p.name for p in existing]})")
    jar = existing[0]
    # The MANIFEST stamp is written by the pom during the build, not here; verify what landed.
    _assert_jar_java_version(jar, java_release, module_dir)
    # The clean rebuild is the recovery, not a licence to guess -- same principle as the
    # ambiguity retry above. A release mismatch now triggers a clean rebuild instead of
    # refusing outright, which is the better behaviour, but it is only sound if the
    # rebuild actually FIXED the stamp. If the jar that landed is still stamped for a
    # different release, the environment is not delivering what this run resolved to
    # (a pom reading its version from elsewhere, STRIIM_RELEASE not reaching the build)
    # and reusing it would upload a jar for the wrong release -- exactly what the
    # pre-rebuild guard existed to prevent.
    #
    # _assert_jar_java_version above does NOT cover this: it catches a wrong Java LEVEL
    # (the 5.4/Java17 <-> 5.0/Java11 switch), so two releases at the same Java level
    # (5.4.0.6 vs 5.4.0.6C) pass it and would slip through.
    landed = _read_manifest_fingerprint(jar)
    if landed is not None and landed != fingerprint:
        raise OpArtifactError(
            f"{jar.name} in {module_dir / 'target'} is still stamped for {landed} after a "
            f"clean rebuild, but this run resolves to {fingerprint}. The build is not "
            f"producing the release it was asked for -- check that STRIIM_RELEASE reaches "
            f"the pom in {module_dir}.")
    return BuiltArtifact(path=jar, name=jar.name, op_name=op_name_for(jar.name, series))

def upload_artifacts(ctx, files: list[Path], run=None, keep_existing: bool = False) -> None:
    # Place each file into Striim's UploadedFiles/. Docker: docker cp into the cluster
    # nodes (chmod 644 — the in-container Striim user must read it). Native: copy into
    # $STRIIM_HOME/UploadedFiles (skip with a clear error if unset).
    # PUBLISHED ATOMICALLY, and skipped when the bytes already match.
    #
    # `docker cp` writes straight into the destination path, so a reader during the copy
    # sees a TRUNCATED file. The destination is a fixed, shared name (UploadedFiles/
    # <jar>), and LOAD OPEN PROCESSOR reads exactly that path -- so a concurrent runner
    # copying a 52MB OP jar over a live LOAD produced "ZipFile invalid LOC header (bad
    # signature)". Copy to a temp name in the SAME directory and rename into place: a
    # rename within one filesystem is atomic, so a reader sees either the whole old file
    # or the whole new one. This needs no coordination, so unlike the flock it also holds
    # across separate checkouts and users.
    #
    # The digest check is the cheaper half: if the node already holds these exact bytes
    # there is nothing to publish, which removes the write window entirely for the common
    # case (an unchanged jar re-uploaded every run) and skips a 52MB copy per node.
    #
    # keep_existing (content-named OP jars): a file already at the name is left as it is, even
    # with other bytes. Same name means same contents, and the bytes already there are the ones
    # Striim loaded; replacing them is what lets an UNLOAD poison its loader (see
    # content_addressed).
    import shutil, stat
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))

    if ctx.mode == "docker":
        for f in files:
            name = Path(f).name
            final = f"{_UPLOADED}/{name}"
            keep = keep_existing
            source, fetched, missing = f, None, None
            if keep:
                nodes = stack.app_nodes()
                present = [n for n in nodes if getattr(
                    run(["docker", "exec", n, "test", "-e", final]), "returncode", 1) == 0]
                missing = [n for n in nodes if n not in present]
                if not missing:
                    continue                      # a content name already there stays as it is
                if present:
                    # Give the missing nodes the bytes the others already hold, not this build's:
                    # one name must hold one set of bytes on every node.
                    fetched = Path(tempfile.mkdtemp(prefix="slt-op-jar-"))
                    source = fetched / name
            try:
                if fetched is not None:
                    got = run(["docker", "cp", f"{present[0]}:{final}", str(source)])
                    if getattr(got, "returncode", 0) != 0:
                        raise OpArtifactError(
                            f"upload failed: could not copy {name} from {present[0]}: "
                            f"{(got.stderr or '').strip()}")
                _publish_docker(run, f, name, final, source, keep, missing)
            finally:
                if fetched is not None:
                    shutil.rmtree(fetched, ignore_errors=True)
    else:  # native Striim
        home = os.environ.get("STRIIM_HOME")
        if not home:
            raise OpArtifactError(
                "native Striim resolved but STRIIM_HOME is unset — cannot place OP "
                "artifacts in UploadedFiles/ (set STRIIM_HOME to the install root)")
        dest = Path(home) / "UploadedFiles"
        _root = (f"STRIIM_HOME must be the install root of the running native server, on this host "
                 f"(STRIIM_HOME={home})")
        if not dest.is_dir():
            raise OpArtifactError(f"native-uploads-dir-missing: {dest} does not exist; {_root}")
        for f in files:
            target = dest / Path(f).name
            keep = keep_existing
            if target.exists() and (keep or file_sha256(target) == file_sha256(f)):
                continue
            tmp = target.with_name(f".{target.name}.tmp.{os.getpid()}")
            try:
                shutil.copy(f, tmp)
                tmp.chmod(tmp.stat().st_mode | stat.S_IROTH | stat.S_IRGRP)
                if keep:
                    # A concurrent uploader may have published this name since the check above.
                    _link_no_clobber(tmp, target)
                else:
                    os.replace(tmp, target)           # atomic within the filesystem
            except OSError as e:
                raise OpArtifactError(f"native-uploads-dir-unwritable: placing {target.name} in {dest} "
                                      f"failed ({e.strerror or e}); {_root}") from None

def restore_loaded_copy(ctx, name: str, run=None) -> None:
    """Put the bytes Striim loaded under `name` back into UploadedFiles/<name>, before an UNLOAD.

    Only when Striim's copy differs from UploadedFiles/<name> and is a complete zip; a copy that
    is not (a write cut short) raises, so the caller does not UNLOAD over other bytes.

    Striim's UNLOAD rewrites its own copy (.striim/OpenProcessor/<name>) in place from
    UploadedFiles/<name>, under a jar: URL handle it never closes. If UploadedFiles holds other
    bytes -- a newer build copied over the name, as the old harness did -- every later LOAD of
    the name fails with "ZipFile invalid LOC header", or Striim keeps the old main class beside
    the new jar's other classes. Copying Striim's own copy back makes the UNLOAD rewrite
    identical bytes. A node with no copy (nothing loaded there) is left alone."""
    loaded = f"/opt/striim/.striim/OpenProcessor/{name}"
    if ctx.mode == "docker":
        run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
        for node in stack.app_nodes():
            tmp = f"{_UPLOADED}/.{name}.restore.{os.getpid()}"
            result = run(["docker", "exec", "-u", "0", node, "sh", "-c",
                          '[ -f "$1" ] || exit 0; cmp -s "$1" "$3" && exit 0; '
                          'unzip -tq "$1" >/dev/null 2>&1 || { echo "not a complete zip: $1" >&2; exit 3; }; '
                          'cp -p "$1" "$2" && mv -f "$2" "$3"',
                          "sh", loaded, tmp, f"{_UPLOADED}/{name}"])
            if getattr(result, "returncode", 0) != 0:
                raise OpArtifactError(f"could not restore the loaded copy of {name} on {node}: "
                                      f"{(result.stderr or '').strip()}")
        return
    home = os.environ.get("STRIIM_HOME")
    if not home:
        return
    src = Path(home) / ".striim" / "OpenProcessor" / name
    target = Path(home) / "UploadedFiles" / name
    if src.is_file() and not (target.is_file() and filecmp.cmp(src, target, shallow=False)):
        try:
            with zipfile.ZipFile(src) as z:
                bad = z.testzip()
        except (OSError, zipfile.BadZipFile) as e:
            bad = str(e)
        if bad is not None:
            raise OpArtifactError(f"the loaded copy of {name} is not a complete zip ({bad}); "
                                  f"not restoring it")
        tmp = target.with_name(f".{name}.restore.{os.getpid()}")
        try:
            shutil.copy2(src, tmp)
            os.replace(tmp, target)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            raise OpArtifactError(f"could not restore the loaded copy of {name}: {e}") from e


def _publish_docker(run, f, name: str, final: str, source, keep: bool, missing) -> None:
    """Publish `source` as `final` on each app node that needs it, atomically: copy to a temp
    name, then move or link it into place. With `keep`, only the `missing` nodes, and without
    clobbering: the hard link fails if a concurrent uploader published the name first, whose
    bytes then stay."""
    want = None
    for node in stack.app_nodes():
        if keep:
            if node not in missing:
                continue
        else:
            got = run(["docker", "exec", node, "sha256sum", final])
            want = want or file_sha256(f)
            if getattr(got, "returncode", 1) == 0 and (got.stdout or "").split(" ")[0] == want:
                continue                  # already published, byte for byte
        tmp = f"{_UPLOADED}/.{name}.tmp.{os.getpid()}"
        # Placed last: the file is complete AND readable before it is visible. chmod as root, as
        # the agent path below does: Docker Desktop's `docker cp` keeps the host owner, which the
        # container's striim user may not chmod.
        # The link as root: `docker cp` keeps the host owner, and protected_hardlinks forbids
        # the striim user linking a file it does not own.
        # ln fails harmlessly if a concurrent uploader got there first; any other failure (no
        # hard links on the mount) falls back to a move that still will not clobber. Its stderr
        # is kept for the error message.
        place = (["docker", "exec", "-u", "0", node, "sh", "-c",
                  'ln "$1" "$2" || test -e "$2" || mv -n "$1" "$2"; rm -f "$1"; test -e "$2"',
                  "sh", tmp, final]
                 if keep else ["docker", "exec", node, "mv", "-f", tmp, final])
        for command in (["docker", "cp", str(source), f"{node}:{tmp}"],
                        ["docker", "exec", "-u", "0", node, "chmod", "644", tmp],
                        place):
            result = run(command)
            if getattr(result, "returncode", 0) != 0:
                raise OpArtifactError(
                    f"upload failed on {node} for {name}: "
                    f"{' '.join(command)}: {(result.stderr or '').strip()}")


def place_on_agent(ctx, files: list[Path], client=None, run=None, progress=None,
                   names: list[str] | None = None) -> None:
    """Put OP jars on the AGENT's own classpath and restart it, so an agent-deployed flow can
    instantiate their adapter classes.

    WHY THIS IS NOT upload_artifacts. `LOAD OPEN PROCESSOR` never reaches an agent. It
    distributes the module through `LoadSCMTask`, whose `call()` does
    `Server.server.striimClassLoader.addProcessComponentModule(...)` -- and `Server.server` does
    not exist on an agent (an agent runs `AgentNode`). The RemoteCall is dispatched to Hazelcast
    MEMBERS in any case, and an agent joins as a CLIENT. So an agent asked to deploy a flow whose
    source is an OP fails at DEPLOY with:

        cannot create adapter <class>
        Caused by: java.lang.ClassNotFoundException: <class>

    even though the same jar is loaded and working on every server. Verified on a 5.4.0.6
    dockerized cluster: the deploy fails before the flow starts, and the agent contributes
    nothing.

    WHY THE RESTART IS UNAVOIDABLE. Two independent reasons, and neither has a runtime path:
      * the agent launcher passes `-cp "$WA_HOME/lib/*"`, and the JVM expands that glob ONCE at
        startup;
      * `StriimClassLoader` builds its module map in its constructor (`addModulesFromDir`), and
        the only method that adds one afterwards is the server-side task above.
    So a jar copied into a running agent is invisible until the JVM is replaced. This is an
    operator-visible fact, not a harness quirk -- a customer deploying StatusReader (or any OP)
    to an agent must do exactly this copy and restart by hand.

    Only the AGENT container is restarted. The servers keep running, so their OP class loaders
    are untouched and `opregistry` records stay valid -- unlike
    striim_provision.restart_app_nodes, which must clear them.

    Skipped with a note in native mode: there is no agent container to copy into, and a native
    single-node install has no server/agent split to exercise.
    """
    if not files:
        return
    if ctx.mode != "docker":
        if progress:
            progress("agent", "skipping agent jar placement: not a dockerized cluster")
        return

    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    agent = stack.agent_container()

    if getattr(run(["docker", "inspect", agent]), "returncode", 1) != 0:
        if progress:
            progress("agent", f"skipping agent jar placement: {agent} is not present")
        return

    # Copy only what differs, and restart only if something was copied. The digest check is what
    # keeps this cheap on the common case (an unchanged jar across runs): a needless agent
    # restart costs ~90s of re-registration and buys nothing.
    placed = []
    for i, f in enumerate(files):
        # `names` places a file under another name: a content-named OP jar goes on the agent
        # under its built name, so a new build replaces the last one on the classpath.
        name = names[i] if names else Path(f).name
        final = f"{stack.AGENT_LIB_DIR}/{name}"
        got = run(["docker", "exec", agent, "sha256sum", final])
        if getattr(got, "returncode", 1) == 0 and (got.stdout or "").split(" ")[0] == file_sha256(f):
            continue
        # Same atomic publish as upload_artifacts: `docker cp` writes straight into the
        # destination, so a reader during the copy sees a truncated file. Here the reader would
        # be the agent JVM at startup, which is exactly when a truncated jar becomes
        # "ClassNotFoundException" again -- the symptom this function exists to remove.
        tmp = f"{stack.AGENT_LIB_DIR}/.{name}.tmp.{os.getpid()}"
        run(["docker", "cp", str(f), f"{agent}:{tmp}"])
        run(["docker", "exec", "-u", "0", agent, "chmod", "644", tmp])
        run(["docker", "exec", "-u", "0", agent, "mv", "-f", tmp, final])
        placed.append(name)

    if not placed:
        if progress:
            progress("agent", "agent already holds these jar(s); no restart needed")
        return

    if progress:
        progress("agent", f"placed {', '.join(placed)} in {stack.AGENT_LIB_DIR}; restarting agent")
    run(["docker", "restart", agent])

    if client is not None:
        # The agent must be back in its deployment group before anything deploys to it, or the
        # deploy resolves zero agent nodes and the flow silently lands nowhere.
        from livetest import striim_provision as _sp
        _sp._reauthenticate(client)
        _sp.wait_agent_registered(client, timeout=300)
        if progress:
            progress("agent", "agent re-registered")


def delete_artifacts(ctx, names: list[str], run=None) -> None:
    # Best-effort remove of per-test op.upload files (spec §B.3) from Striim's
    # UploadedFiles/ at teardown — upload_artifacts only ever copies/overwrites, so
    # without this a passing test's rendered config (e.g. customers_lookup.json)
    # sits there forever, same gap as the Postgres/server-file cleanup above.
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    if ctx.mode == "docker":
        for name in names:
            for node in stack.app_nodes():
                run(["docker", "exec", node, "rm", "-f", f"{_UPLOADED}/{name}"])
    else:
        home = os.environ.get("STRIIM_HOME")
        if not home:
            return
        dest = Path(home) / "UploadedFiles"
        for name in names:
            (dest / name).unlink(missing_ok=True)


@contextlib.contextmanager
def jar_staging():
    """A fresh ``slt-load-jar-*`` directory for a `server_files` `load:` jar, removed whole on exit
    (the staged jar and any fetched gs:// copy). Only that directory is ever removed."""
    root = Path(tempfile.mkdtemp(prefix="slt-load-jar-"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _gcs_download(url: str, dest: Path) -> None:
    """Download gs://<bucket>/<object> with application default credentials (`gcloud auth
    application-default login`, or GOOGLE_APPLICATION_CREDENTIALS)."""
    from google.cloud import storage
    bucket, _, obj = url[len("gs://"):].partition("/")
    storage.Client().bucket(bucket).blob(obj).download_to_filename(str(dest))


def jar_source(name: str, source_dir: Path, scratch: Path, fetch=_gcs_download) -> Path:
    """The local file a `server_files` `load:` entry uploads. `name` is already token-rendered:
    a test-dir-relative path, an absolute one, or a gs:// object, which is fetched into
    `scratch` first. A missing local file fails here, naming the path it looked for."""
    if name.startswith("gs://"):
        bucket, _, obj = name[len("gs://"):].partition("/")
        if not bucket or not obj:
            raise FileNotFoundError(f"server_files: {name!r} is not gs://<bucket>/<object>")
        dest = Path(scratch) / "fetched" / Path(obj).name
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            fetch(name, dest)
        except Exception as e:
            raise FileNotFoundError(f"server_files: could not fetch {name}: {e}") from e
        return dest
    path = Path(source_dir) / name          # an absolute name replaces source_dir
    if not path.is_file():
        raise FileNotFoundError(f"server_files: jar not found: {path}")
    return path
