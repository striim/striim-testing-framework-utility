"""`jar:` and `example:` resolve against SLT_PROJECT_ROOT.

A customer's own OP/UDF source lives in their repo, so a module ref must not resolve against the
framework clone. Unset, the project root is the clone, so today's layout is unchanged. Each check
runs in a fresh interpreter because the root is read when the module is imported, with the
.env layer stubbed out.
"""
import os
import subprocess
import sys
from pathlib import Path

LIVE = Path(__file__).resolve().parents[1]           # scripts/live
REPO = LIVE.parents[1]
INT = LIVE.parent / "integration"


def _run(tier: Path, expr: str, env_extra: dict, cwd: Path) -> Path:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SLT_", "STRIIM_", "INT_", "GOLD_"))}
    env.update(PYTHONPATH=str(tier), **env_extra)
    # The clone's own .env may set SLT_PROJECT_ROOT (docs/SET-UP-YOUR-OWN-REPO.md shows it there): stub
    # the .env layer before the import, so only the env given here decides the root.
    paths_mod = "livetest.paths" if tier == LIVE else "inttest.paths"
    stub = f"import importlib\nimportlib.import_module({paths_mod!r}).dotenv_values = lambda env=None: {{}}\n"
    out = subprocess.run([sys.executable, "-c", f"{stub}print({expr})"], cwd=cwd, env=env,
                         capture_output=True, text=True, check=True)
    return Path(out.stdout.strip().splitlines()[-1])


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"                         # outside the clone
    (proj / "java" / "UserDefinedFunctions" / "Foo").mkdir(parents=True)
    (proj / "java" / "UserDefinedFunctions" / "Foo" / "pom.xml").write_text("<project/>\n")
    (proj / "java" / "Foo").mkdir(parents=True)
    (proj / "java" / "Foo" / "app.tql").write_text("CREATE APPLICATION x;\nEND APPLICATION x;\n")
    return proj


def test_live_jar_module_resolves_under_project_root(tmp_path):
    proj = _project(tmp_path)
    expr = "importlib.import_module('livetest.opartifacts')._root() / 'java/UserDefinedFunctions/Foo'"
    assert _run(LIVE, expr, {"SLT_PROJECT_ROOT": str(proj)}, tmp_path) == (
        proj / "java" / "UserDefinedFunctions" / "Foo").resolve()
    assert _run(LIVE, expr, {}, tmp_path) == REPO / "java" / "UserDefinedFunctions" / "Foo"


def test_live_example_resolves_under_project_root(tmp_path):
    proj = _project(tmp_path)
    case = proj / "cases" / "c1"
    case.mkdir(parents=True)
    (case / "test.yaml").write_text("name: c1\nexample: java/Foo\ntql: app.tql\nassert:\n  smoke: true\n")
    expr = (f"importlib.import_module('livetest.manifest').load_manifest("
            f"__import__('pathlib').Path(r'{case / 'test.yaml'}')).source_dir")
    assert _run(LIVE, expr, {"SLT_PROJECT_ROOT": str(proj)}, tmp_path) == (proj / "java" / "Foo").resolve()


def test_int_jar_module_resolves_under_project_root(tmp_path):
    proj = _project(tmp_path)
    expr = "importlib.import_module('inttest.opartifacts')._root() / 'java/UserDefinedFunctions/Foo'"
    assert _run(INT, expr, {"SLT_PROJECT_ROOT": str(proj)}, tmp_path) == (
        proj / "java" / "UserDefinedFunctions" / "Foo").resolve()
    assert _run(INT, expr, {}, tmp_path) == REPO / "java" / "UserDefinedFunctions" / "Foo"
