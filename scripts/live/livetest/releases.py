from __future__ import annotations
import re
from pathlib import Path

# The Striim release the gold OPs/UDFs build + test against is DETECTED from the install that
# STRIIM_HOME points at. The installed version is the version we build the jar against AND the
# version the live harness provisions + tests in Docker — one install, one version, end to end.
# There are no per-release files to select or keep in sync; a build/test version mismatch is
# impossible by construction.

# Java level per Striim series (the JDK the series ships/requires).
_SERIES_JAVA = {"5.4": "17", "5.0": "11", "5.2": "11"}
_DEFAULT_JAVA = "17"

# Fallback used ONLY when STRIIM_HOME is unset (e.g. a standalone `mvn package` that relies on the
# pom's own defaults). Kept in sync with the pom defaults.
_DEFAULT_VERSION = "5.4.2"

class ReleaseError(Exception):
    pass

def _series_of(version: str) -> str:
    parts = version.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else version

def _java_for_series(series: str) -> str:
    return _SERIES_JAVA.get(series, _DEFAULT_JAVA)

def _default_release() -> dict:
    s = _series_of(_DEFAULT_VERSION)
    return {"STRIIM_RELEASE": _DEFAULT_VERSION, "STRIIM_VERSION": _DEFAULT_VERSION,
            "STRIIM_SERIES": s, "JAVA_RELEASE": _java_for_series(s)}

def _version_from_jar(name: str, prefix: str) -> str:
    return name[len(prefix):-len(".jar")]

def detect_release(striim_home) -> dict:
    """Derive the release from a Striim install root.

    ``STRIIM_VERSION`` from ``${STRIIM_HOME}/lib/Platform-<ver>.jar``, ``STRIIM_SERIES`` parsed
    from it, ``JAVA_RELEASE`` from the series, and the release-specific ``mssql-jdbc`` version from
    its jar (when present). Returns a dict of KEY=value strings — the same shape the harness threads
    through the OP-jar build and the Docker provision."""
    lib = Path(striim_home) / "lib"
    plats = sorted(p for p in lib.glob("Platform-*.jar")
                   if not p.name.endswith(("-sources.jar", "-javadoc.jar")))
    if not plats:
        raise ReleaseError(
            f"no Platform-*.jar under {lib} — set STRIIM_HOME to a Striim install root")
    if len(plats) > 1:
        raise ReleaseError(
            f"ambiguous install: multiple Platform-*.jar under {lib}: {[p.name for p in plats]}")
    version = _version_from_jar(plats[0].name, "Platform-")
    series = _series_of(version)
    rel = {"STRIIM_RELEASE": version, "STRIIM_VERSION": version,
           "STRIIM_SERIES": series, "JAVA_RELEASE": _java_for_series(series)}
    mssql = sorted(p for p in lib.glob("mssql-jdbc-*.jar")
                   if not p.name.endswith(("-sources.jar", "-javadoc.jar")))
    if mssql:
        rel["MSSQL_JDBC_VERSION"] = _version_from_jar(mssql[0].name, "mssql-jdbc-")
    return rel

def resolve_release(env: dict) -> dict:
    """Detect the release from ``STRIIM_HOME`` (the strict model: the installed version IS the
    version we build + test). Falls back to the 5.4.2 default only when ``STRIIM_HOME`` is unset."""
    home = env.get("STRIIM_HOME")
    if home:
        return detect_release(home)
    return _default_release()


# A Striim release: dotted numbers (the patch LINE) plus an optional upper-case patch letter.
_RELEASE = re.compile(r"(\d+(?:\.\d+)+)([A-Z]?)")

def _parse(version: str) -> tuple[str, str]:
    m = _RELEASE.fullmatch(version)
    if not m:
        raise ReleaseError(f"{version!r} is not a Striim release (e.g. 5.4.0.6 or 5.4.0.6G)")
    return m.group(1), m.group(2)

def _entry_bounds(entry: str) -> tuple[str, str, str]:
    """(line, lowest letter, highest letter) for an exact release or a range ``A-B``. Letters order
    as '' (the unlettered base) < 'A' < ... . Patch lines ship in parallel, so a range may not span
    lines: a fix in 5.4.0.6G says nothing about 5.4.0.2G."""
    lo, sep, hi = entry.partition("-")
    lo_line, lo_letter = _parse(lo)
    if not sep:
        return lo_line, lo_letter, lo_letter
    hi_line, hi_letter = _parse(hi)
    if lo_line != hi_line:
        raise ReleaseError(f"range {entry!r} must stay within one patch line ({lo_line} vs {hi_line})")
    if lo_letter > hi_letter:
        raise ReleaseError(f"range {entry!r} is reversed")
    return lo_line, lo_letter, hi_letter

def check_release_entry(entry: str) -> None:
    """Raise ReleaseError unless ``entry`` is an exact release or a one-line range."""
    _entry_bounds(entry)

def release_matches(version: str, entries) -> bool:
    """True when ``version`` is one of ``entries`` (exact releases or one-line ranges)."""
    line, letter = _parse(version)
    for entry in entries:
        e_line, lo, hi = _entry_bounds(entry)
        if e_line == line and lo <= letter <= hi:
            return True
    return False
