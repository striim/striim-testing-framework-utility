"""The Teradata driver (teradatasql) is an optional extra: without it every module still imports,
and a test that requires teradata skips, saying how to install it and what its licence asks."""
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from livetest import registry

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def no_driver(monkeypatch):
    # A None entry makes `import teradatasql` raise ImportError and find_spec answer None,
    # whether or not the driver is installed in this environment.
    monkeypatch.setitem(sys.modules, "teradatasql", None)


def test_engine_modules_import_without_the_driver():
    code = ("import sys; sys.modules['teradatasql'] = None\n"
            "import livetest.teradataadmin, livetest.registry, livetest.preflight, livetest.plugin\n"
            "print('ok')")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=ROOT / "scripts" / "live", env=os.environ.copy())
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


def test_teradata_is_unavailable_without_the_driver_even_with_a_host(no_driver):
    defn = registry.load_service("teradata")
    why = registry.unavailable(defn, {"SLT_TERADATA_HOST": "td.example"})
    assert why.startswith("Python module teradatasql is not installed")
    assert 'pip install -e ".[teradata]"' in why
    assert "licence" in why and "Teradata-authorized licence" in why
    assert "without Teradata's consent" in why


def test_a_teradata_test_skips_without_the_driver(no_driver):
    from livetest import plugin
    defn = registry.load_service("teradata")
    with pytest.raises(pytest.skip.Exception, match="teradatasql is not installed"):
        plugin._prepare_service(defn, {"SLT_TERADATA_HOST": "td.example"}, "docker")


def test_with_the_driver_the_host_setting_decides(monkeypatch):
    real = registry.importlib.util.find_spec
    monkeypatch.setattr(registry.importlib.util, "find_spec",
                        lambda name, *a: object() if name == "teradatasql" else real(name, *a))
    defn = registry.load_service("teradata")
    assert registry.unavailable(defn, {"SLT_TERADATA_HOST": "td.example"}) is None
    assert "SLT_TERADATA_HOST" in registry.unavailable(defn, {})


def test_missing_python_module_names_only_what_is_absent(no_driver):
    assert registry.missing_python_module(None) is None
    assert registry.missing_python_module("yaml") is None
    assert registry.missing_python_module("teradatasql") == "Python module teradatasql is not installed"
    assert registry.missing_python_module("teradatasql", "do X").endswith(": do X")


@pytest.mark.parametrize("pyproject", ["pyproject.toml", "scripts/live/pyproject.toml"])
def test_the_driver_is_an_extra_not_a_dependency(pyproject):
    project = tomllib.loads((ROOT / pyproject).read_text())["project"]
    assert not [d for d in project["dependencies"] if d.lower().startswith("teradatasql")]
    assert project["optional-dependencies"]["teradata"] == ["teradatasql>=20"]
