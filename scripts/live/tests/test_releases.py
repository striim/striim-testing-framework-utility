from pathlib import Path
import pytest

from livetest import releases
from livetest.releases import detect_release, resolve_release, ReleaseError


def _mk_install(root: Path, version: str, *, mssql: str | None = None, extra_platform: str | None = None):
    lib = root / "lib"; lib.mkdir(parents=True, exist_ok=True)
    (lib / f"Platform-{version}.jar").write_text("x")
    if mssql:
        (lib / f"mssql-jdbc-{mssql}.jar").write_text("x")
    if extra_platform:
        (lib / f"Platform-{extra_platform}.jar").write_text("x")
    return root


# ---- detect_release: the strict model derives everything from the install --------------------

def test_detect_release_from_install(tmp_path):
    _mk_install(tmp_path, "5.4.0.6", mssql="12.8.1.jre8")
    assert detect_release(tmp_path) == {
        "STRIIM_RELEASE": "5.4.0.6", "STRIIM_VERSION": "5.4.0.6",
        "STRIIM_SERIES": "5.4", "JAVA_RELEASE": "17", "MSSQL_JDBC_VERSION": "12.8.1.jre8"}

def test_detect_release_maps_java_11_for_5_0(tmp_path):
    _mk_install(tmp_path, "5.0.6.2F", mssql="7.2.2.jre8")
    rel = detect_release(tmp_path)
    assert rel["STRIIM_SERIES"] == "5.0" and rel["JAVA_RELEASE"] == "11"
    assert rel["MSSQL_JDBC_VERSION"] == "7.2.2.jre8"

def test_detect_release_lettered_patch_keeps_series(tmp_path):
    _mk_install(tmp_path, "5.4.0.6C")
    rel = detect_release(tmp_path)
    assert rel["STRIIM_VERSION"] == "5.4.0.6C" and rel["STRIIM_SERIES"] == "5.4" and rel["JAVA_RELEASE"] == "17"

def test_detect_release_no_platform_jar_raises(tmp_path):
    (tmp_path / "lib").mkdir()
    with pytest.raises(ReleaseError, match="no Platform"):
        detect_release(tmp_path)

def test_detect_release_ambiguous_install_raises(tmp_path):
    _mk_install(tmp_path, "5.4.0.6", extra_platform="5.0.6.2F")
    with pytest.raises(ReleaseError, match="ambiguous"):
        detect_release(tmp_path)

def test_detect_release_ignores_sources_and_javadoc_jars(tmp_path):
    lib = tmp_path / "lib"; lib.mkdir()
    (lib / "Platform-5.4.0.6.jar").write_text("x")
    (lib / "Platform-5.4.0.6-sources.jar").write_text("x")
    (lib / "Platform-5.4.0.6-javadoc.jar").write_text("x")
    assert detect_release(tmp_path)["STRIIM_VERSION"] == "5.4.0.6"

# ---- resolve_release: detect from STRIIM_HOME, else default ----------------------------------

def test_resolve_release_detects_from_striim_home(tmp_path):
    _mk_install(tmp_path, "5.2.0.6A")
    rel = resolve_release({"STRIIM_HOME": str(tmp_path)})
    assert rel["STRIIM_VERSION"] == "5.2.0.6A" and rel["JAVA_RELEASE"] == "11"

def test_resolve_release_defaults_when_striim_home_unset():
    rel = resolve_release({})
    assert rel["STRIIM_VERSION"] == "5.4.2" and rel["STRIIM_SERIES"] == "5.4" and rel["JAVA_RELEASE"] == "17"


# ---- release_matches: xfail.releases entries -------------------------------------------------
# Striim ships parallel patch lines (5.4.0.2A-G, 5.4.0.6A-G), so a fix in one line says nothing
# about another. Entries are exact releases or a letter range inside ONE line; never an ordering
# across lines.

@pytest.mark.parametrize("version,entries,want", [
    ("5.4.0", ["5.4.0"], True),
    ("5.4.0.2", ["5.4.0"], False),              # exact means exact, not a prefix
    ("5.4.0.6C", ["5.4.0.6C"], True),
    ("5.4.0.6", ["5.4.0.6-5.4.0.6F"], True),    # the unlettered base opens its line
    ("5.4.0.6C", ["5.4.0.6A-5.4.0.6F"], True),
    ("5.4.0.6G", ["5.4.0.6A-5.4.0.6F"], False),
    ("5.4.0.6", ["5.4.0.6A-5.4.0.6F"], False),
    ("5.4.0.2C", ["5.4.0.6A-5.4.0.6F"], False), # same letter, other line
    ("5.4.2", ["5.4.0", "5.4.2"], True),
])
def test_release_matches(version, entries, want):
    assert releases.release_matches(version, entries) is want


@pytest.mark.parametrize("entry", ["5.4.0", "5.4.0.6G", "5.4.0.6-5.4.0.6F", "5.4.0.6A-5.4.0.6A"])
def test_check_release_entry_accepts(entry):
    releases.check_release_entry(entry)


@pytest.mark.parametrize("entry,why", [
    ("5.4.0.2-5.4.0.6G", "one patch line"),     # a range across lines is the ordering we refuse
    ("5.4.0.6F-5.4.0.6A", "reversed"),
    ("5.4.0.6g", "not a Striim release"),
    ("<5.4.0.6G", "not a Striim release"),
    ("5.4.0.6A-", "not a Striim release"),
    ("", "not a Striim release"),
])
def test_check_release_entry_rejects(entry, why):
    with pytest.raises(ReleaseError, match=why):
        releases.check_release_entry(entry)


def test_release_matches_rejects_an_unparseable_running_version():
    with pytest.raises(ReleaseError, match="not a Striim release"):
        releases.release_matches("5.4.2-SNAPSHOT", ["5.4.2"])
