"""The code samples under samples/code: each case resolves inside its own sample, and each
module builds its jar with a real `mvn` against a real Striim install.

The structure tests run everywhere. The build test is gated the same way as
scripts/integration/tests/test_opartifacts_build.py: skipped unless STRIIM_HOME names an
install with lib/Platform-*.jar and `mvn` is on PATH, because the poms compile against the
Striim jars in that lib/. It builds through livetest.opartifacts.build_jar, which is what a
live run of these cases does before it loads the jar.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from livetest import manifest, opartifacts, releases

_REPO = Path(__file__).resolve().parents[1]

# (sample dir, case dir, module kind, expected jar name without the "-<series>.jar" suffix)
SAMPLES = [
    ("samples/code/udf", "referenceudf-mark-processed", "udf", "ReferenceUdfV1"),
    ("samples/code/op", "referenceop-copy-adds-userdata", "op", "ReferenceOpV1"),
]
_IDS = [kind for _, _, kind, _ in SAMPLES]


def _inside(path: Path, root: Path) -> bool:
    return Path(path).resolve().is_relative_to(root.resolve())


def _load(sample, case):
    return manifest.load_manifest(_REPO / sample / case / "test.yaml")


@pytest.mark.parametrize("sample,case,kind,_jar", SAMPLES, ids=_IDS)
def test_case_reads_its_files_from_the_sample(sample, case, kind, _jar):
    m = _load(sample, case)
    root = _REPO / sample
    assert m.name == case
    assert _inside(m.source_dir, root), f"example: {m.source_dir} is outside {root}"
    assert (m.source_dir / m.tql).is_file()
    files = [f for _db, f in m.ddl_files] + [s[1] for s in m.seed_files]
    assert files
    for f in files:
        assert (m.source_dir / f).is_file(), f
    for check in m.assert_.get("data", []) + m.assert_.get("file", []):
        assert (_REPO / sample / case / check["match"]).is_file(), check


@pytest.mark.parametrize("sample,case,kind,_jar", SAMPLES, ids=_IDS)
def test_module_and_its_shared_sources_live_in_the_sample(sample, case, kind, _jar):
    m = _load(sample, case)
    root = _REPO / sample
    [mod] = m.modules
    assert mod["kind"] == kind
    module_dir = _REPO / opartifacts._module_dir(mod["jar"])
    assert _inside(module_dir, root)
    assert (module_dir / "pom.xml").is_file()
    # The poms add-source shared libraries by relative path (../../SampleCommon, and for the
    # OP ../OpenProcessorCommon). add_source_roots drops a root that does not exist, so
    # compare against the pom's own add-source count: every one must resolve, inside the sample.
    declared = [s for s in opartifacts._ADD_SOURCE_RE.findall((module_dir / "pom.xml").read_text())
                if s.startswith("${project.basedir}")]
    roots = opartifacts.add_source_roots(module_dir)
    assert len(roots) == len(declared) > 0, (declared, roots)
    assert all(_inside(r, root) for r in roots), roots


_STRIIM_HOME = os.environ.get("STRIIM_HOME")
_HAS_PLATFORM_JAR = bool(_STRIIM_HOME) and any(Path(_STRIIM_HOME, "lib").glob("Platform-*.jar"))


@pytest.mark.skipif(not _HAS_PLATFORM_JAR,
                    reason="STRIIM_HOME is not set (or has no lib/Platform-*.jar): the sample "
                           "poms compile against a real Striim install's lib/")
@pytest.mark.skipif(shutil.which("mvn") is None, reason="mvn is not on PATH")
@pytest.mark.parametrize("sample,case,kind,jar", SAMPLES, ids=_IDS)
def test_sample_builds_its_jar(sample, case, kind, jar):
    [mod] = _load(sample, case).modules
    release = releases.resolve_release(dict(os.environ))
    series = release["STRIIM_SERIES"]
    artifact = opartifacts.build_jar(mod["jar"], release)
    assert artifact.name == f"{jar}-{series}.jar"
    assert artifact.op_name == jar
    assert artifact.path.is_file()
    assert _inside(artifact.path, _REPO / sample)


@pytest.mark.parametrize("kind", ["op", "udf"])
def test_sample_java_packages_match_public_source_paths(kind):
    import re
    sources = list((_REPO / "samples/code" / kind / "java").rglob("*.java"))
    assert sources
    for source in sources:
        package = re.search(r"^package ([\w.]+);", source.read_text(), re.M)[1]
        assert package == "com.example" or package.startswith("com.example.")
        assert source.parent.as_posix().endswith("/" + package.replace(".", "/"))
