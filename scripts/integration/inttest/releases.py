"""Striim release detection from STRIIM_HOME, adapted from scripts/live/livetest/releases.py.

The release the OP jars build + this suite's Java harness runs against is DETECTED from
the install that STRIIM_HOME points at -- one install, one version, end to end, so a
build/test version mismatch is impossible by construction. There are no per-release
files to select or keep in sync.

Behavior is identical to the live tier: STRIIM_VERSION comes from
``${STRIIM_HOME}/lib/Platform-<ver>.jar``, STRIIM_SERIES is the first two dotted parts
of that version, and JAVA_RELEASE is looked up from the series. The 5.4.2 fallback
(used only when STRIIM_HOME is unset) is kept in sync with the OP poms' own defaults.
"""
from __future__ import annotations

from pathlib import Path

# Java level per Striim series (the JDK the series ships/requires).
_SERIES_JAVA = {"5.4": "17", "5.0": "11", "5.2": "11"}
_DEFAULT_JAVA = "17"

# Fallback used ONLY when STRIIM_HOME is unset (e.g. a standalone `mvn package` that
# relies on the pom's own defaults). Kept in sync with the pom defaults.
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

    ``STRIIM_VERSION`` from ``${STRIIM_HOME}/lib/Platform-<ver>.jar``, ``STRIIM_SERIES``
    parsed from it, ``JAVA_RELEASE`` from the series, and the release-specific
    ``mssql-jdbc`` version from its jar (when present). Returns a dict of KEY=value
    strings -- the same shape threaded through the OP-jar build (and, in the live tier,
    the Docker provision)."""
    lib = Path(striim_home) / "lib"
    plats = sorted(p for p in lib.glob("Platform-*.jar")
                   if not p.name.endswith(("-sources.jar", "-javadoc.jar")))
    if not plats:
        raise ReleaseError(
            f"no Platform-*.jar under {lib} -- set STRIIM_HOME to a Striim install root")
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
    """Detect the release from ``STRIIM_HOME`` (the strict model: the installed version
    IS the version we build + test). Falls back to the 5.4.2 default only when
    ``STRIIM_HOME`` is unset."""
    home = env.get("STRIIM_HOME")
    if home:
        return detect_release(home)
    return _default_release()
