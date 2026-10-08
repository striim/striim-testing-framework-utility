"""Verify: every pytest plugin the CLI loads into a tier child ships with a plain ``pip install``."""
import tomllib

from _clikit import REPO

# -p plugins dispatch.py passes to tier children, and the distribution that provides each.
CHILD_PLUGINS = {"pytest": "pytest", "xdist.plugin": "pytest-xdist"}


def _dependency_names() -> set:
    deps = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["dependencies"]
    return {d.split(">")[0].split("=")[0].split("<")[0].split("[")[0].strip().lower() for d in deps}


def test_child_plugins_are_runtime_dependencies():
    # `striim-test run --parallel` adds -p xdist.plugin; a dev-only xdist breaks it after pip install -e .
    missing = {plugin: dist for plugin, dist in CHILD_PLUGINS.items() if dist not in _dependency_names()}
    assert not missing, missing


def test_dispatch_names_only_the_known_child_plugins():
    text = (REPO / "scripts/cli/striim_test/dispatch.py").read_text()
    assert '"-p", "xdist.plugin"' in text
