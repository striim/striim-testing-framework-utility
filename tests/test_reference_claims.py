"""Claims in the guides and in code messages that the code can check: settings layers, service
lists, cited sections, commands and their arguments, links. Each test reads the doc and the code it
describes, so a change to either that makes the doc wrong fails here."""
from __future__ import annotations

import ast
import re
import sys
import urllib.parse
from pathlib import Path

import yaml
from livetest import paths

REAL_DOTENV_VALUES = paths.dotenv_values        # the root conftest stubs it per test
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts" / "cli"))
CUSTOMER = [REPO / n for n in ("README.md", "AGENTS.md", "CONTRIBUTING.md")] + sorted((REPO / "docs").rglob("*.md")) \
    + sorted((REPO / "templates").rglob("*.md"))
SKIP_PARTS = {".git", ".venv", ".venv-test", "venv", "node_modules", "target", "__pycache__"}


def _doc(rel: str) -> str:
    return (REPO / rel).read_text()


def _flat(rel: str) -> str:
    return " ".join(_doc(rel).split())


def _settings_row(name: str) -> list:
    row = next(line for line in _doc("docs/SETTINGS.md").splitlines() if line.startswith(f"| `{name}` |"))
    return [c.strip() for c in row.strip("|").split("|")]


# --- settings layers ------------------------------------------------------------------------------

def test_the_clone_dotenv_is_a_documented_layer_under_a_project_root(tmp_path, monkeypatch):
    """The clone's `.env` (or SLT_FRAMEWORK_DOTENV) is read below the project's even with
    SLT_PROJECT_ROOT set; the settings page and ENGINE.md say so."""
    monkeypatch.setattr(paths, "dotenv_values", REAL_DOTENV_VALUES)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text("SLT_PG_PORT=7000\n")
    clone = tmp_path / "clone.env"
    clone.write_text("SLT_PG_HOST=from-the-clone\nSLT_PG_PORT=1\n")
    values = paths.dotenv_values({"SLT_PROJECT_ROOT": str(project), "SLT_FRAMEWORK_DOTENV": str(clone),
                                  "SLT_MACHINE_ENV": str(tmp_path / "none.env"), "HOME": str(tmp_path)})
    assert values.get("SLT_PG_HOST") == "from-the-clone" and values.get("SLT_PG_PORT") == "7000"
    page = _flat("docs/SETTINGS.md")
    where = page[page.index("## Where settings come from"):page.index("## Striim")]
    order = [where.index(s) for s in ("your shell's environment", "your project root",
                                      "the framework clone's own `.env`", "the machine file")]
    assert order == sorted(order)
    assert "when `SLT_PROJECT_ROOT` is not set" not in _settings_row("SLT_FRAMEWORK_DOTENV")[3]
    engine = _flat("docs/internals/ENGINE.md")
    assert "then `<project root>/.env`, then the clone's `.env`, then the machine file" in engine
    assert "four layers" in _flat("docs/SET-UP-YOUR-OWN-REPO.md")


def test_the_integration_case_root_row_names_the_real_default():
    from inttest import paths as int_paths
    default = int_paths.int_cases({"SLT_FRAMEWORK_DOTENV": "/dev/null", "SLT_MACHINE_ENV": "/dev/null"}, {})
    rel = default.relative_to(REPO).as_posix()
    cell = _settings_row("SLT_INT_CASES")[2]
    assert f"`{rel}`" in cell
    assert ("does not ship" in cell) == (not default.exists())


def test_service_rows_mark_required_settings_and_connection_only_hosts():
    for tier in ("live", "integration"):
        for file in sorted((REPO / "scripts" / tier / "services").glob("*/service.yaml")):
            raw = yaml.safe_load(file.read_text()) or {}
            for name in raw.get("required_env") or ():
                assert _settings_row(name)[3].startswith("**Required.**"), name
            host = (raw.get("live_env") or {}).get("host")
            if host and not raw.get("compose"):
                what = _settings_row(host)[3]
                assert "instead of the container" not in what and "ships no container" in what, host
    assert "admin account" in _settings_row("SLT_TERADATA_USER")[3]
    teradata = _doc("docs/SETTINGS.md").split("### Teradata (`teradata`)")[1]
    first_row = next(line for line in teradata.splitlines() if line.startswith("| `"))
    assert first_row.startswith("| `SLT_TERADATA_HOST` |")          # "set its host (the first row)"


# --- service lists ---------------------------------------------------------------------------------

def _services(tier: str) -> set:
    return {p.parent.name for p in (REPO / "scripts" / tier / "services").glob("*/service.yaml")}


def _ticked(text: str) -> set:
    return set(re.findall(r"`([a-z]+(?:-[a-z]+)*)`", text))


def test_requires_lists_name_every_service_directory():
    guide = _flat("docs/INTEGRATION-TESTS.md")
    start = guide.index("`requires:` (services:")
    listed = _ticked(guide[start:guide.index("see [SERVICES.md]", start)]) - {"requires"}
    assert listed == _services("integration")
    start_here = _flat("docs/START-HERE.md")
    begin = start_here.index("under `requires:` (")
    listed = _ticked(start_here[begin:start_here.index(")", begin + len("under `requires:` ("))]) - {"requires"}
    assert "Teradata and ServiceNow are always your own instance" in start_here
    assert listed | {"teradata", "servicenow"} == _services("live")


# --- sections that code messages cite ---------------------------------------------------------------

def _headings(path: Path) -> set:
    return {m.group(1).strip() for m in re.finditer(r"^#{1,6} (.+)$", path.read_text(), re.M)}


def _strings(tree) -> list:
    """Every string literal, an f-string's literal parts joined around a placeholder."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            out.append("".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
    return out


def test_sections_named_in_code_messages_exist():
    cited = []
    for file in sorted((REPO / "scripts").rglob("*.py")):
        if "tests" in file.parts or SKIP_PARTS & set(file.parts):
            continue
        for text in _strings(ast.parse(file.read_text())):
            text = " ".join(text.split())
            assert "When a run cannot start" not in text and "README's Docker mode" not in text, file
            for doc, section in re.findall(r"((?:docs/)?[A-Z][A-Z-]+\.md), [\"']([^\"']+)[\"']", text):
                cited.append((file, doc, section))
    assert cited
    for file, doc, section in cited:
        target = REPO / doc if (REPO / doc).is_file() else REPO / "docs" / doc
        assert target.is_file(), (file, doc)
        heads = _headings(target)                 # a heading, or its opening words before " — "
        assert any(h == section or h.startswith(section + " — ") for h in heads), (file, doc, section)


# --- commands ---------------------------------------------------------------------------------------

def test_every_striim_test_option_has_help_and_case_is_documented_per_command():
    from striim_test import cli
    sub = next(a for a in cli.build_parser()._actions if a.dest == "command").choices
    bare = sorted(f"{name} {o}" for name, p in sub.items() for a in p._actions
                  for o in a.option_strings if a.help is None)
    assert bare == []
    assert "fetch" in cli.__doc__
    docs = "".join(p.read_text() for p in CUSTOMER)
    assert re.search(r"striim-test run[^\n`|]*--case", docs)
    first = _flat("docs/RUN-YOUR-FIRST-TEST.md")
    assert "Here `--case` is a **path**" in first and "by **id**" in first


def test_replay_commands_name_their_ledger():
    from livetest import ownership
    import pytest
    with pytest.raises(SystemExit) as e:
        ownership.main(["replay"])
    assert e.value.code == 2                                     # the ledger argument is required
    for doc in CUSTOMER:
        for tail in re.findall(r"livetest\.ownership replay([^`\n]*)`", doc.read_text()):
            assert tail.strip().startswith("<ledger"), (doc.name, tail)


def test_docs_name_no_option_or_tool_the_repo_lacks():
    defined = set()
    for file in (REPO / "scripts").rglob("*.py"):
        if "tests" not in file.parts:
            defined |= set(re.findall(r"""add(?:option|_argument)\(\s*["'](--[a-z-]+)""", file.read_text()))
    assert "--parallel" in defined and "--depth" not in defined
    assert "`--depth`" not in _doc("docs/internals/ENGINE.md")
    assert "gold-cli" not in "".join(p.read_text() for p in CUSTOMER)


def test_live_loader_refuses_seed_when_and_the_guide_says_so(tmp_path):
    from livetest.manifest import ManifestError, load_manifest
    import pytest
    d = tmp_path / "c"
    d.mkdir()
    (d / "app.tql").write_text("-- x\n")
    (d / "test.yaml").write_text("name: c\npurpose: p\ntql: app.tql\nseed_when: post_start\nassert: {smoke: true}\n")
    with pytest.raises(ManifestError, match="seed_when"):
        load_manifest(d / "test.yaml")
    guide = _flat("docs/INTEGRATION-TESTS.md")
    assert "The **live** tier has a similarly-named `seed_when`" not in guide
    assert "the live loader now refuses `seed_when`" in guide


# --- TEST-YAML rows the loader decides --------------------------------------------------------------

def test_test_yaml_timeout_times_and_seed_after_match_the_loader(tmp_path):
    from livetest.manifest import load_manifest
    d = tmp_path / "c"
    d.mkdir()
    (d / "app.tql").write_text("-- x\n")
    (d / "test.yaml").write_text("name: c\npurpose: p\ntql: app.tql\ntimeout: 2.9\n"
                                 "recover: {mode: stop, times: 3, after: 1}\n"
                                 "assert: {data: [{target: x, min_rows: 1}]}\n")
    m = load_manifest(d / "test.yaml")
    assert m.timeout == 2 and m.recover["times"] == 3 and "trigger" not in m.recover
    doc = _doc("docs/TEST-YAML.md")
    timeout = next(line for line in doc.splitlines() if line.startswith("| `timeout` |"))
    assert "| int |" not in timeout and "`2.9` is `2`" in timeout and "`90s` is refused" in timeout
    times = next(line for line in doc.splitlines() if line.startswith("| `times` |"))
    assert "and a repeatable trigger" not in times and "With a `trigger`" in times
    seed = " ".join(doc[doc.index("- **`after`** — optional delay"):].split())[:400]
    assert "reports that it is reading" in seed
    assert "`workload` is relative to the test dir" not in doc and "one outside the test dir," not in doc


# --- getting started --------------------------------------------------------------------------------

def test_downloaded_example_creates_the_venv_before_using_it():
    doc = _doc("docs/RUN-A-DOWNLOADED-EXAMPLE.md")
    assert doc.index('-m venv "$FW/.venv"') < doc.index('"$FW/.venv/bin/')
    assert "Python 3.12 or later" in doc and "- Python 3.12.\n" not in doc


def test_set_up_guide_matches_the_template():
    guide = _doc("docs/SET-UP-YOUR-OWN-REPO.md")
    assert "ships pinned to the version it came from" not in guide
    pin = (REPO / "templates/consumer-repo/framework.pin").read_text().strip()
    version = re.search(r'^version = "([^"]+)"', _doc("pyproject.toml"), re.M).group(1)
    assert pin == f"v{version}" and "latest release tag" in guide
    table = guide[guide.index("| File | What it is |"):guide.index("## 2.")]
    for top in (REPO / "templates/consumer-repo").iterdir():
        if top.is_file():
            assert f"`{top.name}`" in table, top.name
    script = (REPO / "templates/consumer-repo/scripts/sync-framework.py").read_text()
    options = set(re.findall(r"""add_argument\(\s*["'](--[a-z-]+)""", script))
    assert options and all(f"`{o}" in guide for o in options), options


def test_readme_lists_every_top_level_folder():
    readme = _doc("README.md")
    listing = readme[readme.index("## What is in this repo"):readme.index("## License")]
    for top in REPO.iterdir():
        if (top.is_dir() and not top.name.startswith(".") and top.name not in SKIP_PARTS | {"tests"}
                and not top.name.endswith(".egg-info")):
            assert re.search(rf"`{re.escape(top.name)}[/`]", listing), top.name


def test_installer_names_follow_the_release():
    first = _flat("docs/RUN-YOUR-FIRST-TEST.md")
    assert "the four `striim-*` packages carry its version in place of `5.4.2`" in first


def test_release_fallback_comment_does_not_claim_the_pom_default():
    from livetest import releases
    for rel in ("scripts/live/livetest/releases.py", "scripts/integration/inttest/releases.py"):
        assert "in sync with the pom defaults" not in _doc(rel).lower()
        assert "in sync with the op poms" not in _doc(rel).lower()
    pom = _doc("samples/code/udf/java/UserDefinedFunctions/ReferenceUdf/pom.xml")
    default = re.search(r"<STRIIM_VERSION>([0-9.]+)</STRIIM_VERSION>", pom).group(1)
    assert default != releases._DEFAULT_VERSION and f"({default})" in _doc("scripts/live/livetest/releases.py")


def test_oracle_service_is_named_for_its_release_line():
    tag = re.search(r"gvenzl/oracle-free:(\d+)\.(\d+)", _doc("scripts/live/services/oracle/images/oracle/Dockerfile"))
    services = _doc("docs/SERVICES.md")
    if tag.group(1) == "23" and int(tag.group(2)) >= 26:
        assert "23ai" not in services and "26ai" in services


# --- links ------------------------------------------------------------------------------------------

def _slugs(path: Path) -> set:
    out, seen, fence = set(), {}, False
    for line in path.read_text(errors="replace").splitlines():
        if re.match(r"\s*```", line):
            fence = not fence
            continue
        m = None if fence else re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if not m:
            continue
        h = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", m.group(2)).replace("`", "")
        s = re.sub(r"[^\w\- ]", "", h.strip().lower()).replace(" ", "-")
        n = seen.get(s, 0)
        seen[s] = n + 1
        out.add(s if n == 0 else f"{s}-{n}")
    return out


def test_every_markdown_link_and_anchor_in_the_tree_resolves():
    """Every .md in the tree, READMEs under scripts/ and samples/ included, with GitHub's heading
    slugs: wider than test_customer_docs.test_doc_links_resolve."""
    bad = set()
    for doc in REPO.rglob("*.md"):
        if SKIP_PARTS & set(doc.parts) or "egg-info" in str(doc):
            continue
        body = re.sub(r"```.*?```", "", doc.read_text(errors="replace"), flags=re.S)
        for target in re.findall(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", body):
            if re.match(r"[a-z]+:", target):
                continue
            path, _, frag = target.partition("#")
            dest = (doc.parent / urllib.parse.unquote(path)).resolve() if path else doc
            if not dest.exists():
                bad.add((doc.relative_to(REPO).as_posix(), target))
            elif frag and dest.suffix == ".md" and urllib.parse.unquote(frag) not in _slugs(dest):
                bad.add((doc.relative_to(REPO).as_posix(), target))
    assert bad == set()
