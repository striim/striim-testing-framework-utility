"""OP jar build-or-reuse, adapted from scripts/live/livetest/opartifacts.py (docs/INTEGRATION-TESTS.md).

Build an OpenProcessor's jar AGAINST A GIVEN RELEASE (once, if missing/stale) and hand
back its path. Behavior mirrors the live tier's build-or-reuse exactly -- fingerprint
guard, bytecode Java-level guard, mtime staleness, ambiguous-output and corrupt-jar
clean-rebuild triggers, `mvn` invocation -- with the live-only pieces dropped (there is
no docker-cp / UploadedFiles upload step in this tier: the harness takes the built jar
path directly, per SPEC §6/§7).

A test.yaml `op.jar`/`udf.jar` field is a repo-relative MODULE reference (the module dir, or its
pom.xml) -- never a versioned jar path. The release (detected from STRIIM_HOME by
`inttest.releases`) fixes STRIIM_SERIES, which the gold-standard poms bake into the
shaded jar's finalName (`${artifactId}-${STRIIM_SERIES}.jar`); we glob for that rather
than hardcoding a name, so the same code builds every gold OP.

**The OpenProcessorCommon staleness fold (SPEC §8's "build-staleness gotcha").** The
shared contracts (`EventProcessor`, `Logger`, `TypeResolver`, ...) are add-source'd from
`java/OpenProcessors/OpenProcessorCommon/src/main/java` into EVERY op's own build and
shaded into its single output jar -- they compile as if they were the op's own sources,
but they live outside the op's `src/` tree. If `_stale_trigger` only looked at the op's
own `src/**` + `pom.xml`, editing a shared interface would leave every dependent op's
jar's mtime untouched and stale, so a real regression would be silently built over. We
fix this the simplest-correct way: `_stale_trigger` ALWAYS folds `OpenProcessorCommon`'s
own `src/**` mtimes into the comparison for every module (not just ones we know add-source
it) -- see the module docstring for `_stale_trigger` below.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from inttest import paths

def _root() -> Path:
    """Where `jar:` module refs resolve, at call time: the live engine's active project (its
    consumer root) when installed, else SLT_PROJECT_ROOT, else this clone."""
    try:
        from livetest import project as _project
    except ImportError:
        return paths.project_root()
    return _project.example_root()


def _common_dir() -> Path:
    return _root() / "java" / "OpenProcessors" / "OpenProcessorCommon"


class OpArtifactError(Exception):
    pass


class OpBuildFailed(OpArtifactError):
    """The build ran and failed: the module's code or pom is broken, not the environment."""


@dataclass(frozen=True)
class BuiltArtifact:
    path: Path    # absolute path to the built, series-suffixed jar
    name: str     # OP_JAR  -- the jar's filename, e.g. "ReferenceOpV1-5.4.jar"
    op_name: str  # OP_NAME -- filename with the trailing "-<SERIES>.jar" removed,
                  #            e.g. "ReferenceOpV1"


def _module_dir(module_ref: str) -> Path:
    # module_ref is normally a repo-relative module dir (e.g.
    # "java/OpenProcessors/ReferenceOp") or its pom.xml. Back-compat: also accepts
    # the old "<module>/target/<versioned>.jar" shape some callers may still pass.
    p = Path(module_ref)
    if p.name == "pom.xml":
        return p.parent
    parts = p.parts
    if "target" in parts:
        return Path(*parts[:parts.index("target")])
    return p


def module_name(module_ref: str) -> str:
    """The module's directory name, e.g. "ReferenceOp" -- used for per-module
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
        raise OpBuildFailed(
            f"built jar {jar_name!r} does not end with the expected {suffix!r} "
            f"suffix for STRIIM_SERIES={series!r}")
    return jar_name[: -len(suffix)]


def _files_under(directory: Path) -> list[Path]:
    return [p for p in directory.rglob("*") if p.is_file()] if directory.is_dir() else []


def _stale_trigger(jar: Path, module_dir: Path) -> Path | None:
    # Return the SOURCE that is newest-newer than the built jar (so a rebuild note can
    # name what changed), or None when the jar is up to date. Rebuild if any source is
    # newer -- otherwise an edit (without `mvn clean`) is silently ignored and this tier
    # would drive the OLD jar, reporting green over a real regression. Covers every file
    # under the module's own src/ (java AND resources) plus its pom.xml (a
    # dependency/version/shade change alters the artifact) -- AND, per SPEC §8's
    # "build-staleness gotcha", every file under OpenProcessorCommon's src/ tree, which
    # is add-source'd into (and shaded into the single jar of) every op, module docstring
    # above. We fold Common's tree in unconditionally rather than only for ops we know
    # declare the add-source plugin execution: it is the simplest approach that is
    # correct for every current AND future op that depends on Common, and the cost is
    # cheap (Common is a small module) -- a false-positive rebuild trigger for an op that
    # somehow does NOT depend on Common is harmless (mvn package is a no-op rebuild),
    # whereas a false negative (missing a real dependency) silently ships a stale jar.
    # The one module exempted is Common itself -- its own build already covers its own
    # tree, and folding it in again is a no-op.
    try:
        jar_mtime = jar.stat().st_mtime
    except FileNotFoundError:
        return jar
    # src/test is deliberately EXCLUDED: tests are compiled but never packaged, so no edit
    # there can change the jar's bytes, and including it made a test-only edit trigger a
    # full rebuild that could not alter the artifact. Trade-off: the build's `-DskipTests`
    # test COMPILE (which catches a Java-16+ construct in a test before it breaks a Java-11
    # release build) no longer re-runs for a test-only edit — it fires on the next rebuild
    # from any main/pom change, and in CI.
    srcs = _files_under(module_dir / "src" / "main")
    pom = module_dir / "pom.xml"
    if pom.exists():
        srcs.append(pom)
    common = _common_dir()
    if module_dir.resolve() != common.resolve():
        srcs.extend(_files_under(common / "src" / "main"))
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
# A built jar is only valid for the exact release it was compiled against. `_stale_trigger`
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
            f"JDK (a stale build from another release). Run 'mvn clean' in {module_dir} and retry.")


def build_jar(module_ref: str, release: dict, run=None, report=None) -> BuiltArtifact:
    """Build module_ref (a repo-relative module dir or its pom.xml) against `release`
    (a dict as returned by inttest.releases.resolve_release / detect_release) and return the
    resulting BuiltArtifact. A matching `*-<SERIES>.jar` is REUSED only when it is newer than
    every module source (including OpenProcessorCommon's, see module docstring) AND its
    fingerprint sidecar records the same release; otherwise it is (re)built with the release
    env merged into the subprocess so each pom's `release-from-env` profile activates. A jar
    whose MANIFEST is stamped for a DIFFERENT release fails fast (asking for `mvn clean`); a
    jar with no stamp (built before the entries existed / by another toolchain) is accepted so
    long as the builder-agnostic bytecode guard confirms the right Java level.

    When a jar is (re)built, `report` (a callable taking one string) is invoked once with a
    human-readable trigger -- the reason the automatic rebuild fired (source changed / no jar
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
    # (a) the harness cannot know which jar to drive, and (b) a plain rebuild would not
    # help -- only `mvn clean` removes the stale one. Erroring here instead made the test
    # SKIP (plugin.py turns OpArtifactError into pytest.skip), which reads like a pass in
    # a summary line. Same rationale and shape as the live tier (commit 2f313977a).
    ambiguous = len(existing) > 1
    jar = existing[0] if len(existing) == 1 else None
    # A jar that is not a readable zip (truncated by an interrupted/concurrent `mvn`) must
    # never be reused: the manifest/bytecode guards read it as None ("unknown", which is
    # legitimate for a VALID jar and must not block), and the mtime check cannot see
    # corruption -- so without this probe the truncated jar was handed to the Java harness,
    # which died with `ZipException: zip END header not found` and reported a test FAILURE
    # where PERF_SPEC.md §2 wants a rebuild (or, if unbuildable, a skip).
    corrupt = _jar_unreadable(jar) if jar is not None else None

    if jar is not None and corrupt is None:
        recorded = _read_manifest_fingerprint(jar)
        if recorded is not None and recorded != fingerprint:
            # The jar's MANIFEST is stamped for a different release -> refuse to reuse it.
            raise OpArtifactError(
                f"{jar.name} in {module_dir / 'target'} was built for {recorded}, but this run "
                f"resolves to {fingerprint}. Refusing to reuse a jar from a different Striim "
                f"release / Java level. Run 'mvn clean' in {module_dir} and retry.")
        # recorded matches, OR is absent (no Striim-Build-* manifest stamp -- a jar built before the
        # entries existed or by another toolchain). Absent is NOT a failure -- but the builder-
        # agnostic bytecode guard must hold: it catches a wrong Java LEVEL no matter who built the
        # jar (e.g. a 5.4/Java17 <-> 5.0/Java11 switch) and a stale-repackage whose manifest lies.
        _assert_jar_java_version(jar, java_release, module_dir)
        trigger = _stale_trigger(jar, module_dir)
        if trigger is None:
            return BuiltArtifact(path=jar, name=jar.name, op_name=op_name_for(jar.name, series))
        clean = False   # a source edit in the same environment -> incremental rebuild
        try:
            changed = trigger.relative_to(module_dir)
        except ValueError:
            changed = trigger
        reason = f"source changed ({changed} is newer than {jar.name}) -> incremental rebuild"
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
    _verify_striim_home(build_env, release)
    if java_release:
        java_home = resolve_build_java_home(str(java_release))
        if java_home:
            build_env["JAVA_HOME"] = java_home
    # -DskipTests (not -Dmaven.test.skip): COMPILE the test sources at the release's Java level
    # so a Java-16+ construct slipping into a test is caught here, but do NOT RUN the suites.
    # `clean` is prepended only when a prior different-release build must be discarded (above).
    # Announce the automatic (re)build and the trigger that caused it, so a long silent `mvn`
    # step isn't a silent surprise.
    notify(reason)
    goals = ["mvn", "-q"] + (["clean"] if clean else []) + ["package", "-DskipTests=true"]
    r = run(goals, str(module_dir), build_env)
    if getattr(r, "returncode", 1) != 0:
        out = (getattr(r, "stderr", "") or getattr(r, "stdout", "") or "")[-800:]
        raise OpBuildFailed(f"mvn package failed in {module_dir}:\n{out}")
    existing = _release_jar_candidates(module_dir, series)
    if len(existing) != 1 and not clean:
        # An INCREMENTAL build can itself create the ambiguity the branch above only
        # catches on the NEXT run: a version bump changes the pom's finalName, so
        # `mvn package` writes the new jar BESIDE the old one instead of replacing it,
        # and the run that performed the bump-rebuild then dies here. Retrying with
        # `clean` -- which empties target/ -- resolves it in the SAME run, so a version
        # bump costs one extra build rather than one failed run plus a manual retry.
        notify(f"ambiguous *-{series}.jar after incremental build "
               f"({', '.join(p.name for p in existing)}) -> clean rebuild")
        r = run(["mvn", "-q", "clean", "package", "-DskipTests=true"], str(module_dir), build_env)
        if getattr(r, "returncode", 1) != 0:
            out = (getattr(r, "stderr", "") or getattr(r, "stdout", "") or "")[-800:]
            raise OpBuildFailed(f"mvn clean package failed in {module_dir}:\n{out}")
        existing = _release_jar_candidates(module_dir, series)
    if len(existing) != 1:
        raise OpBuildFailed(
            f"build succeeded but no unambiguous *-{series}.jar found in "
            f"{module_dir / 'target'} (found: {[p.name for p in existing]})")
    jar = existing[0]
    # The MANIFEST stamp is written by the pom during the build, not here; verify what landed.
    _assert_jar_java_version(jar, java_release, module_dir)
    return BuiltArtifact(path=jar, name=jar.name, op_name=op_name_for(jar.name, series))
