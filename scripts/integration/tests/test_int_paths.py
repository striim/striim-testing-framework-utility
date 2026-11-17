"""inttest.paths: twin of livetest.paths -- same core, integration defaults (spec section 2)."""
from pathlib import Path

import pytest

from inttest import paths

INT = Path(paths.__file__).resolve().parents[1]           # scripts/integration
SCRIPTS = INT.parent
REPO = SCRIPTS.parent
LIVE_PATHS = SCRIPTS / "live" / "livetest" / "paths.py"


def _core(text):
    start = text.index("# --- core")
    return text[start:text.index("# --- end core", start)]


def test_framework_dotenv_names_the_clone_env(tmp_path):
    """The integration engine's copy honours SLT_FRAMEWORK_DOTENV as livetest's does (text identity alone would not
    catch a behaviour change made in both copies)."""
    named = tmp_path / "named.env"
    assert paths.dotenv_path({"SLT_FRAMEWORK_DOTENV": str(named)}) == named
    assert paths.dotenv_path({}) == paths._default_project_root() / ".env"
    assert paths.dotenv_path({"SLT_PROJECT_ROOT": str(tmp_path), "SLT_FRAMEWORK_DOTENV": str(named)}) == tmp_path / ".env"


def test_core_block_identical_to_livetest():
    assert _core(Path(paths.__file__).read_text()) == _core(LIVE_PATHS.read_text())


def test_defaults_equal_todays_derivation():
    assert paths.project_root({}, {}) == REPO                               # opartifacts.py:35
    assert paths.int_dir({}, {}) == INT                                     # plugin.py:74 _ROOT
    assert paths.services_dir({}, {}) == INT / "services"                   # plugin.py:75, tokens.py:60
    assert paths.state_dir({}, {}) == INT                                   # plugin.py:341-352
    assert paths.perf_dir({}, {}) == INT / "perf"                           # plugin.py:76
    assert paths.perf_results_dir({}, {}) == INT / ".perf-results"         # plugin.py:77
    assert paths.shim_dir({}, {}) == INT / "java"                           # harness.py:30
    assert paths.int_cases({}, {}) == INT / "regression"


FN = {"SLT_PROJECT_ROOT": paths.project_root, "SLT_INT_CASES": paths.int_cases,
      "SLT_INT_SERVICES_DIR": paths.services_dir, "SLT_STATE_DIR": paths.state_dir}


@pytest.mark.parametrize("key", list(FN))
def test_precedence_and_missing(key, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    assert FN[key]({key: str(a)}, {key: str(b)}) == a.resolve()
    assert FN[key]({}, {key: str(b)}) == b.resolve()
    with pytest.raises(paths.PathConfigError, match=key):
        FN[key]({key: str(tmp_path / "missing")}, {})


def test_live_services_key_does_not_move_int_services(tmp_path):
    assert paths.services_dir({"SLT_SERVICES_DIR": str(tmp_path)}, {}) == INT / "services"


def test_perf_dir_follows_int_cases(tmp_path):
    (tmp_path / "regression").mkdir()
    assert paths.perf_dir({"SLT_INT_CASES": str(tmp_path / "regression")}, {}) == tmp_path.resolve() / "perf"


def test_framework_home_needs_inttest(tmp_path):
    (tmp_path / "live" / "livetest").mkdir(parents=True)
    with pytest.raises(paths.PathConfigError, match="SLT_FRAMEWORK_HOME.*environment"):
        paths.framework_home({"SLT_FRAMEWORK_HOME": str(tmp_path)}, {})


def test_hermetic_tests_see_no_dotenv(tmp_path, monkeypatch):
    # A real .env is planted so that this fails if the conftest guard is removed.
    (tmp_path / ".env").write_text("SLT_INT_CASES=/x\n")
    monkeypatch.setenv("SLT_PROJECT_ROOT", str(tmp_path))
    assert paths.read_dotenv(tmp_path / ".env") == {"SLT_INT_CASES": "/x"}
    assert paths.dotenv_values() == {}
