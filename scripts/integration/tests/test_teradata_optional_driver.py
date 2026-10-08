"""Without the optional Teradata driver, an integration case that requires teradata skips (as the
live tier does), and the modules that use the driver still import."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from inttest import services


@pytest.fixture
def no_driver(monkeypatch):
    monkeypatch.setitem(sys.modules, "teradatasql", None)


def test_modules_import_without_the_driver():
    # A fresh interpreter: reloading here would replace classes other tests hold.
    code = ("import sys; sys.modules['teradatasql'] = None\n"
            "import inttest.dbroutes, inttest.services, inttest.plugin\n"
            "print('ok')")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=Path(__file__).resolve().parents[1], env=os.environ.copy())
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


def test_teradata_is_unavailable_without_the_driver_even_with_a_host(no_driver, monkeypatch):
    monkeypatch.setenv("INT_TERADATA_HOST", "td.example")
    why = services.unavailable("teradata")
    assert why.startswith("Python module teradatasql is not installed")
    assert 'pip install -e ".[teradata]"' in why and "Teradata-authorized licence" in why
