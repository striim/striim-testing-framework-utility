#!/usr/bin/env bash
# Create an isolated contributor environment using the public platform lock.
set -eu
test_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
unset PYTHONPATH PYTHONHOME
unset UV_INDEX UV_DEFAULT_INDEX UV_INDEX_URL UV_EXTRA_INDEX_URL
unset PIP_INDEX_URL PIP_EXTRA_INDEX_URL
export PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 PIP_REQUIRE_VIRTUALENV=1
export UV_NO_CONFIG=1 UV_PYTHON_DOWNLOADS=never PIP_CONFIG_FILE=/dev/null
test_uv=$(command -v uv 2>/dev/null || true)
if [ -z "$test_uv" ] && [ -x "$HOME/.local/bin/uv" ]; then
  test_uv="$HOME/.local/bin/uv"
fi
test_python=${PYTHON:-}
if [ -z "$test_python" ] && [ -x "$test_root/.venv-test/bin/python" ]; then
  test_python="$test_root/.venv-test/bin/python"
fi
if [ -z "$test_python" ] && [ -n "$test_uv" ]; then
  test_python=$("$test_uv" python find --no-python-downloads 3.12 2>/dev/null || true)
fi
if [ -z "$test_python" ]; then
  test_python=$(command -v python3.12 2>/dev/null || command -v python3 || true)
fi
if [ -z "$test_python" ]; then
  echo 'Install Python 3.12 locally, then set PYTHON to its executable.' >&2
  exit 3
fi
"$test_python" -I - "$test_root" "$test_uv" <<'PY'
from pathlib import Path
import platform
import re
import subprocess
import sys


def refuse(message):
    print(message, file=sys.stderr)
    raise SystemExit(3)


if sys.version_info[:2] != (3, 12):
    refuse("The test locks require Python 3.12; select it with PYTHON.")
import tomllib

root = Path(sys.argv[1])
uv = sys.argv[2]
project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
minimum = re.fullmatch(r">=([0-9]+)\.([0-9]+)(?:\.([0-9]+))?", project["requires-python"])
if not minimum or sys.version_info[:3] < tuple(int(v or 0) for v in minimum.groups()):
    refuse("Selected Python does not satisfy the project's requires-python.")

venv = root / ".venv-test"
python = venv / "bin/python"
if venv.exists():
    cfg = venv / "pyvenv.cfg"
    if not python.exists() or not cfg.exists():
        refuse("Existing .venv-test is incomplete; create a fresh environment manually.")
    if re.search(r"include-system-site-packages\s*=\s*true", cfg.read_text(), re.I):
        refuse("Existing .venv-test includes system site-packages; create an isolated environment manually.")
    subprocess.run([str(python), "-I", "-c",
                    "import sys; assert sys.prefix != sys.base_prefix; assert sys.version_info[:2] == (3, 12)"],
                   check=True)

if platform.system() == "Darwin" and platform.machine() == "arm64":
    requirements = root / "requirements-test.macos-arm64.txt"
elif platform.system() == "Linux" and platform.machine() == "x86_64":
    requirements = root / "requirements-test.txt"
else:
    refuse("No test lock for this platform; compile a matching platform lock first.")
if not venv.exists():
    command = ([uv, "venv", "--no-python-downloads", "--python", sys.executable, str(venv)]
               if uv else [sys.executable, "-m", "venv", str(venv)])
    subprocess.run(command, check=True)

installer = ([uv, "pip"] if uv else [str(python), "-m", "pip", "--isolated"])
target = ["--python", str(python)] if uv else []
index = ["--index-url", "https://pypi.org/simple"]
subprocess.run(installer + (["sync"] if uv else ["install", "--no-deps"]) + target + index +
               ([] if uv else ["-r"]) + [str(requirements)], check=True)
subprocess.run(installer + ["install"] + target + index +
               ["--no-deps", "--no-build-isolation", "--editable", str(root)], check=True)
try:
    subprocess.run(installer + ["check"] + target, check=True)
except subprocess.CalledProcessError:
    refuse("Project dependencies do not match the test environment; "
           "recompile the locks with the command in their headers.")
subprocess.run([str(python), "-I", "-c", """
import sys
import importlib.util
import requests, yaml, fastavro, filelock, psutil, pytest
import psycopg2, oracledb, pymssql, pymysql, vertica_python, sqlalchemy, confluent_kafka
from google.cloud import spanner, storage
import livetest.plugin, inttest.plugin, striim_test.cli
assert sys.prefix != sys.base_prefix
assert importlib.util.find_spec('teradatasql') is None
"""], check=True)
print("Test environment ready: .venv-test/bin/python -m pytest")
PY
