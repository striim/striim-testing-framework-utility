"""Every YAML snippet in the customer docs is valid, checked by the code a run uses.

Each fenced ```yaml block in DOCS is preceded by an HTML comment (invisible on GitHub) saying what
it is, and the block is checked as that:

    <!-- snippet: manifest <case>/<file> -->   a whole test.yaml: load_manifest, and doctor's case check
    <!-- snippet: fragment -->                 part of a test.yaml: merged into a minimal one, then loaded
    <!-- snippet: file <case>/<file> -->       any file of a case the doc builds (SQL, TQL, CSV, Dockerfile)
    <!-- snippet: service <name> -->           a service.yaml: the registry loads it
    <!-- snippet: compose <name> -->           that service's compose.yaml: the preflight accepts it
    <!-- snippet: project -->                  a gold-targets.yaml: the project loader accepts it
    <!-- snippet: shape <why> -->              YAML that parses but is not a whole document of any
                                               kind above (a key list, an annotated outline)

A yaml block without a comment fails, so a new snippet cannot go unchecked. Blocks naming the same
<case> are assembled into one case folder; a manifest's case is seeded from the sample the doc says
to copy (``cp -r samples/... <dir>/<case>``). Services and the project are assembled into one
scratch project, and the doc's test cases are loaded against it.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts" / "cli"))

from striim_test import doctor  # noqa: E402

DOCS = ["docs/WRITING-TESTS.md", "docs/TEST-YAML.md", "docs/WHAT-IS-SUPPORTED.md",
        "docs/YOUR-OWN-SERVICES.md", "docs/SERVICENOW.md", "docs/START-HERE.md",
        "docs/RUN-YOUR-FIRST-TEST.md", "docs/SET-UP-YOUR-OWN-REPO.md", "docs/SERVICES.md",
        "docs/TESTING-YOUR-JAVA.md", "docs/INTEGRATION-TESTS.md", "docs/TROUBLESHOOTING.md",
        "docs/RUN-A-DOWNLOADED-EXAMPLE.md", "docs/USING-AI.md", "docs/CATALOG.md", "SECURITY.md"]
KINDS = {"manifest", "fragment", "file", "service", "compose", "project", "shape"}
_NOTE = re.compile(r"<!--\s*snippet:\s*(\w+)\s*(.*?)\s*-->")
_FENCE = re.compile(r"^(\s*)```(\S*)")


def blocks(doc: str) -> list[dict]:
    """[{doc, line, lang, kind, arg, body}] for every fenced block; kind None when unannotated."""
    out, lines, i = [], (REPO / doc).read_text().splitlines(), 0
    while i < len(lines):
        m = _FENCE.match(lines[i])
        if not m:
            i += 1
            continue
        indent, lang, start = m.group(1), m.group(2), i
        j = i + 1
        while not lines[j].startswith(f"{indent}```"):
            j += 1
        body = "\n".join(line[len(indent):] for line in lines[i + 1:j]) + "\n"
        k = start - 1
        while k >= 0 and not lines[k].strip():
            k -= 1
        note = _NOTE.search(lines[k]) if k >= 0 else None
        out.append({"doc": doc, "line": start + 1, "lang": lang, "body": body,
                    "kind": note.group(1) if note else None, "arg": note.group(2) if note else ""})
        i = j + 1
    return out


ALL = [b for d in DOCS for b in blocks(d)]


def _id(b):
    return f"{Path(b['doc']).name}:{b['line']}"


def test_every_doc_exists_and_has_snippets():
    for d in DOCS:
        assert (REPO / d).is_file(), d
    assert [b for b in ALL if b["kind"] == "manifest"]


@pytest.mark.parametrize("b", [b for b in ALL if b["lang"] in ("yaml", "yml")], ids=_id)
def test_every_yaml_snippet_is_annotated(b):
    assert b["kind"] in KINDS, (f"{_id(b)}: precede the block with <!-- snippet: KIND ... --> "
                                f"(one of {sorted(KINDS)})")
    yaml.safe_load(b["body"])


def test_annotations_name_a_known_kind():
    bad = [_id(b) for b in ALL if b["kind"] and b["kind"] not in KINDS]
    assert not bad, f"unknown snippet kinds at {bad}"


# --- test.yaml -----------------------------------------------------------------------------------

def _load(path: Path):
    from livetest.manifest import load_manifest
    return load_manifest(path)


def _case_dir(tmp: Path, raw: dict) -> Path:
    d = tmp / "case"
    d.mkdir(parents=True, exist_ok=True)
    (d / str(raw.get("tql") or "app.tql")).write_text("-- snippet\n")
    return d


@pytest.mark.parametrize("b", [b for b in ALL if b["kind"] == "fragment"], ids=_id)
def test_fragment_loads_inside_a_minimal_manifest(b, tmp_path):
    frag = yaml.safe_load(b["body"])
    assert isinstance(frag, dict), _id(b)
    raw = {"name": "snippet", "purpose": "a documentation snippet", "tql": "app.tql",
           "assert": {"smoke": True}}
    for key, value in frag.items():
        if key == "assert":
            raw["assert"] = {**raw["assert"], **value}
        else:
            raw[key] = value
    d = _case_dir(tmp_path, raw)
    (d / "test.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))
    _load(d / "test.yaml")


def _cases(doc: str, tmp: Path, kinds=("manifest", "file")) -> dict:
    """{case: folder} assembled from the doc's manifest and file blocks, each case first seeded
    from the sample the doc copies into it."""
    text = (REPO / doc).read_text()
    cases = {}
    for b in blocks(doc):
        if b["kind"] not in kinds:
            continue
        rel = Path(b["arg"])
        case = rel.parts[0]
        if case not in cases:
            cases[case] = tmp / case
            seed = re.search(rf"cp -r (samples/\S+) \S*/{re.escape(case)}\b", text)
            if seed:
                shutil.copytree(REPO / seed.group(1), cases[case])
                (cases[case] / "README.md").unlink(missing_ok=True)
            else:
                cases[case].mkdir(parents=True)
        target = tmp / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(b["body"])
    return cases


def _referenced_files(m) -> list[str]:
    names = [f for _db, f in m.ddl_files] + [f for _db, f, *_ in m.seed_files] + [m.tql]
    for specs in m.assert_.values():
        if isinstance(specs, list):
            names += [s["match"] for s in specs if isinstance(s, dict) and "match" in s]
    if m.lifecycle is not None and m.lifecycle.sentinel:
        names += [m.lifecycle.sentinel["insert"], m.lifecycle.sentinel["delete"]]
    return names


@pytest.mark.parametrize("doc", [d for d in DOCS if any(b["kind"] == "manifest" for b in blocks(d))])
def test_manifest_snippets_load_as_a_run_loads_them(doc, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    services = _services_root(doc, tmp_path / "project")
    if services is not None:
        from livetest import layout
        monkeypatch.setattr(layout, "services_roots", lambda: [services, REPO / "scripts/live/services"])
    for case, d in _cases(doc, tmp_path / "cases").items():
        m = _load(d / "test.yaml")
        missing = [f for f in _referenced_files(m) if not (d / f).is_file()]
        assert not missing, f"{doc}: {case} names files the doc does not provide: {missing}"
        check, = doctor.check_manifests([d / "test.yaml"])
        assert check.status == doctor.OK_, check.line()
        if services is not None:
            from livetest import registry
            for svc in m.requires:
                registry.load_service(svc)


# --- services and the project ----------------------------------------------------------------------

def _services_root(doc: str, root: Path) -> Path | None:
    """The doc's services, written as <root>/services/<name>/, with their files; None if it has none."""
    bs = blocks(doc)
    svc = [b for b in bs if b["kind"] in ("service", "compose")]
    if not svc:
        return None
    base = root / "services"
    for b in svc:
        name = b["arg"].split()[0]
        (base / name).mkdir(parents=True, exist_ok=True)
        (base / name / ("service.yaml" if b["kind"] == "service" else "compose.yaml")).write_text(b["body"])
    for b in bs:
        if b["kind"] == "file" and b["arg"].startswith("services/"):
            (root / b["arg"]).parent.mkdir(parents=True, exist_ok=True)
            (root / b["arg"]).write_text(b["body"])
    return base


SERVICE_DOCS = [d for d in DOCS if any(b["kind"] == "service" for b in blocks(d))]


@pytest.mark.parametrize("doc", SERVICE_DOCS)
def test_service_snippets_load_and_pass_preflight(doc, tmp_path):
    from livetest import registry, resource_profiles
    base = _services_root(doc, tmp_path)
    for d in sorted(p for p in base.iterdir() if p.is_dir()):
        defn = registry.load_service(d.name, services_dir=base)
        assert defn.compose and (d / defn.compose).is_file(), d.name
        prof = resource_profiles.select_profile("live", d.name, services_dir=base)
        resource_profiles.check_profile(prof)


@pytest.mark.parametrize("doc", SERVICE_DOCS)
def test_a_doc_service_named_like_a_shipped_one_overrides_it(doc, tmp_path, monkeypatch):
    from livetest import layout, registry
    base = _services_root(doc, tmp_path)
    shipped = REPO / "scripts" / "live" / "services"
    monkeypatch.setattr(layout, "services_roots", lambda: [base, shipped])
    for d in sorted(p for p in base.iterdir() if p.is_dir()):
        assert registry._find(d.name) == d
        if (shipped / d.name / "service.yaml").is_file():
            assert registry._hits(d.name)[1:] == [shipped / d.name], d.name


@pytest.mark.parametrize("doc", SERVICE_DOCS)
def test_compose_snippets_pass_docker_compose_config(doc, tmp_path):
    if shutil.which("docker") is None or subprocess.run(["docker", "compose", "version"],
                                                        capture_output=True).returncode:
        pytest.skip("docker compose is not installed")
    base = _services_root(doc, tmp_path)
    for compose in sorted(base.glob("*/compose.yaml")):
        r = subprocess.run(["docker", "compose", "-f", str(compose), "config", "--quiet"],
                           capture_output=True, text=True)
        assert r.returncode == 0, f"{compose.parent.name}: {r.stderr}"


@pytest.mark.parametrize("b", [b for b in ALL if b["kind"] == "project"], ids=_id)
def test_project_snippet_loads(b, tmp_path):
    from livetest import project as live_project
    (tmp_path / "gold-targets.yaml").write_text(b["body"])
    raw = yaml.safe_load(b["body"])
    for rel in (raw.get("suites") or {}).values():
        (tmp_path / rel).mkdir(parents=True, exist_ok=True)
    for entry in raw.get("servicesRoots") or []:
        if "$" not in entry:
            (tmp_path / entry).mkdir(parents=True, exist_ok=True)
    live_project.load_project(tmp_path / "gold-targets.yaml")


# --- links -----------------------------------------------------------------------------------------

def _slug(heading: str) -> str:
    s = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return s.replace(" ", "-")


@pytest.mark.parametrize("doc", DOCS + ["README.md"])
def test_anchor_links_resolve(doc):
    text = (REPO / doc).read_text()
    for target, anchor in re.findall(r"\]\(([^)#:\s]*)#([^)\s]+)\)", text):
        path = (REPO / doc).parent / target if target else REPO / doc
        heads = [_slug(h) for h in re.findall(r"^#+ (.+)$", path.read_text(), re.M)]
        assert anchor in heads, f"{doc}: #{anchor} is not a heading of {path.name}"
