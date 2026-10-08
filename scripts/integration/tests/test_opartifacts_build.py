"""End-to-end test of inttest.opartifacts.build_jar against a REAL Striim install and a
REAL `mvn` build of java/OpenProcessors/ReferenceOp (docs/INTEGRATION-TESTS.md).

Gated: skipped unless STRIIM_HOME is set AND ${STRIIM_HOME}/lib/Platform-*.jar exists,
so the fast suite (test_opartifacts.py) still runs everywhere with no Striim install
and no maven invocation. This module is the only place that actually shells out to
`mvn` for opartifacts -- everything else is covered with fakes.

Proves, against the real ReferenceOp + OpenProcessorCommon modules:
  1. an initial build_jar() call builds (or reuses, if a prior run already left a
     fresh jar) the release-matched <artifactId>-<series>.jar (ReferenceOp's own pom names it);
  2. an immediate second call REUSES it -- the `run` callback is not invoked again;
  3. touching a file under OpenProcessorCommon/src (the SPEC §8 "build-staleness
     gotcha" fold) makes the NEXT call rebuild -- the `run` callback IS invoked, and
     the jar's mtime advances.

The OpenProcessorCommon source file's mtime is restored in a `finally` so the repo is
left as found; the rebuilt jar under ReferenceOp/target/ is left in place (target/ is
gitignored, and it *is* a validly built artifact for the detected release).
"""
from __future__ import annotations

import os
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from inttest import opartifacts, releases

# Where build_jar resolves `jar:` modules: the project root (SLT_PROJECT_ROOT, an active
# project, else this clone). ReferenceOp and OpenProcessorCommon are a test repo's Java tree.
_REPO = opartifacts._root()

_STRIIM_HOME = os.environ.get("STRIIM_HOME")
_HAS_PLATFORM_JAR = bool(_STRIIM_HOME) and any(Path(_STRIIM_HOME, "lib").glob("Platform-*.jar"))

_REFERENCE_OP = "java/OpenProcessors/ReferenceOp"
_MISSING = [str(_REPO / d) for d in (_REFERENCE_OP, "java/OpenProcessors/OpenProcessorCommon")
            if not (_REPO / d).is_dir()]


def _artifact_id(module: str) -> str:
    """The module's own <artifactId> (a direct child of <project>, never the parent's): the jar is
    <artifactId>-<series>.jar and the OP name its artifactId, so a version renumbering in the test
    repo (ReferenceOpV1 -> ReferenceOpV1_0) does not break this test."""
    root = ET.parse(_REPO / module / "pom.xml").getroot()
    return next(c.text.strip() for c in root if c.tag.rsplit("}", 1)[-1] == "artifactId")

pytestmark = [
    pytest.mark.skipif(
        not _HAS_PLATFORM_JAR,
        reason="STRIIM_HOME is not set (or has no lib/Platform-*.jar) -- building an OP jar "
               "requires a real Striim install; set STRIIM_HOME to run this test",
    ),
    # Content only a test repo has: skip, not fail, from the framework's own checkout.
    pytest.mark.skipif(bool(_MISSING), reason=f"OP modules not in this project: {', '.join(_MISSING)}"),
]
# The common library's Logger, wherever the project's package puts it (com/example/common in the
# consumer template, a project's own package elsewhere).
_COMMON_SRC_FILE = next(
    iter(sorted((_REPO / "java/OpenProcessors/OpenProcessorCommon/src/main/java").glob("**/common/Logger.java"))),
    _REPO / "java/OpenProcessors/OpenProcessorCommon/src/main/java/com/example/common/Logger.java")

# Clearly larger than any plausible mtime-comparison granularity (some filesystems only
# offer ~1s resolution) so the staleness check is unambiguous even under a slow/loaded CI
# runner.
_FUTURE_DELTA_SECONDS = 120


def _counting_run():
    calls = []

    def run(argv, cwd, env):
        calls.append(argv)
        return opartifacts._default_run(argv, cwd, env)

    return calls, run


def test_build_reuse_then_rebuild_on_common_change():
    assert _COMMON_SRC_FILE.is_file(), f"fixture assumption broken: {_COMMON_SRC_FILE} not found"

    release = releases.resolve_release(dict(os.environ))
    assert release.get("STRIIM_SERIES"), f"could not resolve a release from the environment: {release!r}"

    calls, run = _counting_run()

    # 1. Initial build (or reuse of a jar left by a prior local run) succeeds and names
    #    the release-matched jar.
    artifact1 = opartifacts.build_jar(_REFERENCE_OP, release, run=run)
    op_name = _artifact_id(_REFERENCE_OP)
    assert artifact1.name == f"{op_name}-{release['STRIIM_SERIES']}.jar"
    assert artifact1.op_name == op_name
    assert artifact1.path.is_file()
    mtime1 = artifact1.path.stat().st_mtime

    # 2. An immediate second call REUSES the jar: no rebuild, same mtime, run not invoked.
    calls.clear()
    artifact2 = opartifacts.build_jar(_REFERENCE_OP, release, run=run)
    assert artifact2.path == artifact1.path
    assert artifact2.path.stat().st_mtime == mtime1
    assert calls == [], f"expected NO mvn invocation on reuse, got: {calls}"

    # 3. Touching a file under OpenProcessorCommon/src (outside ReferenceOp's own src/
    #    tree entirely) must still trigger a rebuild -- the SPEC §8 fold.
    original_mtime = _COMMON_SRC_FILE.stat().st_mtime
    future = time.time() + _FUTURE_DELTA_SECONDS
    try:
        os.utime(_COMMON_SRC_FILE, (future, future))

        calls.clear()
        artifact3 = opartifacts.build_jar(_REFERENCE_OP, release, run=run)

        assert calls, "expected a rebuild (mvn invocation) after touching OpenProcessorCommon/src"
        assert artifact3.path == artifact1.path   # same jar path, rebuilt in place
        assert artifact3.path.stat().st_mtime > mtime1
    finally:
        os.utime(_COMMON_SRC_FILE, (original_mtime, original_mtime))
