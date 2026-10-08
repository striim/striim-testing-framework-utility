"""The contributor environment includes both engines and refuses shared packages.

These checks also run with the standard library before the test environment exists:
``python3 -I tests/test_test_environment.py``.
"""
from pathlib import Path
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest


REPO = Path(__file__).resolve().parents[1]


class TestEnvironment(unittest.TestCase):
    def test_input_covers_runtime_and_test_dependencies(self):
        requirements = {
            line.strip() for line in (REPO / "requirements-test.in").read_text().splitlines()
            if line.strip() and not line.startswith("#")
        }
        root_project = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]
        root_requirements = root_project["dependencies"] + root_project["optional-dependencies"]["dev"]
        for directory in (REPO, REPO / "scripts/live", REPO / "scripts/integration"):
            project = tomllib.loads((directory / "pyproject.toml").read_text())["project"]
            for dependency in project["dependencies"] + project["optional-dependencies"]["dev"]:
                # The root union can impose a stronger minimum than an engine does.
                if dependency not in requirements:
                    package = dependency.split(">=", 1)[0].lower()
                    stronger = [item for item in root_requirements
                                if item.split(">=", 1)[0].lower() == package]
                    self.assertTrue(stronger, dependency)
                    self.assertTrue(set(stronger) <= requirements, dependency)
        self.assertFalse(any("teradatasql" in item.lower() for item in requirements))
        self.assertIn("setuptools>=68", requirements)
        self.assertIn("wheel", requirements)

    def test_platform_locks_pin_every_direct_dependency(self):
        def name(line):
            return re.split(r"[<>=!~\s]", line, 1)[0].lower().replace("_", "-")

        direct = {
            name(line) for line in (REPO / "requirements-test.in").read_text().splitlines()
            if line and not line.startswith("#")
        }
        for filename in ("requirements-test.txt", "requirements-test.macos-arm64.txt"):
            content = (REPO / filename).read_text()
            pins = [line for line in content.splitlines() if line and not line.lstrip().startswith("#")]
            self.assertTrue(all(re.fullmatch(r"[\w.-]+==[\w.+!-]+", line) for line in pins))
            self.assertTrue(direct <= {name(line) for line in pins})
            self.assertNotIn("teradatasql", {name(line) for line in pins})
            self.assertIn("--default-index https://pypi.org/simple", content)
            self.assertIn("--python-version 3.12", content)

    @unittest.skipUnless(sys.version_info[:2] == (3, 12), "bootstrap locks require Python 3.12")
    def test_bootstrap_refuses_system_site_packages_without_changing_venv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tools").mkdir()
            shutil.copy2(REPO / "tools/make-test-venv.sh", root / "tools")
            shutil.copy2(REPO / "pyproject.toml", root)
            venv = root / ".venv-test"
            (venv / "bin").mkdir(parents=True)
            (venv / "bin/python").symlink_to(sys.executable)
            cfg = venv / "pyvenv.cfg"
            config = "include-system-site-packages = true\n"
            cfg.write_text(config)
            env = dict(os.environ, PYTHON=sys.executable, UV_OFFLINE="1")
            result = subprocess.run(["bash", str(root / "tools/make-test-venv.sh")],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
            self.assertIn("system site-packages", result.stderr)
            self.assertEqual(cfg.read_text(), config)

    @unittest.skipUnless(sys.version_info[:2] == (3, 12), "bootstrap locks require Python 3.12")
    def test_bootstrap_refuses_dependency_drift_with_recompile_guidance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tools").mkdir()
            shutil.copy2(REPO / "tools/make-test-venv.sh", root / "tools")
            (root / "pyproject.toml").write_text(
                '[project]\nrequires-python = ">=3.12"\n'
                'dependencies = ["missing-test-dependency>=1"]\n')
            for filename in ("requirements-test.txt", "requirements-test.macos-arm64.txt"):
                (root / filename).write_text("existing-test-dependency==1\n")
            # Keep installation offline; reproduce the installer's missing-dependency
            # check from the fixture project and its deliberately stale lock.
            uv = root / "uv"
            uv.write_text(f"#!{sys.executable}\n" + '''
from pathlib import Path
import sys
import tomllib
import venv

root = Path(__file__).parent
if sys.argv[1] == "venv":
    venv.EnvBuilder(with_pip=False).create(sys.argv[-1])
elif sys.argv[1:3] == ["pip", "check"]:
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    lock = (root / "requirements-test.txt").read_text()
    for dependency in project["dependencies"]:
        name = dependency.split(">=", 1)[0]
        if name + "==" not in lock:
            print(f"The project requires {name}, which is not installed", file=sys.stderr)
            sys.exit(1)
''')
            uv.chmod(0o755)
            env = dict(os.environ, PYTHON=sys.executable, UV_OFFLINE="1",
                       PATH=str(root) + os.pathsep + os.environ.get("PATH", ""))
            result = subprocess.run(["bash", str(root / "tools/make-test-venv.sh")],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
            self.assertIn("missing-test-dependency", result.stderr)
            self.assertIn("recompile the locks with the command in their headers", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertNotIn("Test environment ready", result.stdout)


if __name__ == "__main__":
    unittest.main()
