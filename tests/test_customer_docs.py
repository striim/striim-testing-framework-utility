"""The customer docs agree with the code: .env.example parses to the settings the
README relies on and names only keys the framework reads, the README's run line selects a real
sample, its links resolve, and each moved doc leaves a stub pointing at its new place."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from livetest import paths

REAL_DOTENV_VALUES = paths.dotenv_values

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts" / "cli"))

from striim_test import doctor  # noqa: E402

README = (REPO / "README.md").read_text()
FIRST = (REPO / "docs" / "RUN-YOUR-FIRST-TEST.md").read_text()
EXAMPLE = REPO / ".env.example"


def test_env_example_sets_the_live_cases_to_the_samples():
    assert paths.read_dotenv(EXAMPLE) == {"SLT_LIVE_CASES": "samples", "SLT_INFRA_OWNERSHIP": "shared",
                                          "SLT_KEEP_SERVICES": "1"}
    got = paths.live_cases({"SLT_PROJECT_ROOT": str(REPO)}, paths.read_dotenv(EXAMPLE))
    assert got == (REPO / "samples").resolve()


def test_env_example_names_only_keys_env_supplies():
    """Every key in .env.example, commented out or not, is one .env is read for."""
    keys = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", EXAMPLE.read_text(), re.M))
    assert "SLT_POSTGRES_MEM_LIMIT" in doctor.declared_service_keys({})
    known = set(paths.KEYS) | set(paths.SERVICE_KEYS) | doctor.declared_service_keys({})
    assert keys and keys <= known, keys - known
    assert "SLT_PG_HOST" in keys                        # commented out, so nothing is set
    assert {"STRIIM_URL", "STRIIM_USER", "STRIIM_PASS", "SLT_PROJECT_ROOT",
            "SLT_LIVE_CASES"} <= keys


def test_env_example_passes_doctors_env_checks(tmp_path, monkeypatch):
    # Read this test's .env instead of the root fixture's empty stub; keep machine settings out.
    monkeypatch.setattr(paths, "dotenv_values", REAL_DOTENV_VALUES)
    monkeypatch.setattr(paths, "machine_values", lambda *a, **k: {})
    (tmp_path / ".env").write_text(EXAMPLE.read_text())
    (tmp_path / "samples").mkdir()
    checks, values = doctor.check_env({"SLT_PROJECT_ROOT": str(tmp_path)}, REPO)
    assert [c.status for c in checks] == ["ok"], [c.line() for c in checks]
    assert values.get("SLT_INFRA_OWNERSHIP") == "shared", values
    assert values.get("SLT_KEEP_SERVICES") == "1", values
    # The shipped .env declares shared ownership with kept services, so a fresh clone's
    # run is neither refused nor able to tear down a cluster it did not start.
    owner, = doctor.check_ownership({"SLT_PROJECT_ROOT": str(tmp_path)}, values)
    assert owner.status == "ok" and owner.message.startswith("shared "), owner.line()


def test_doctor_rejects_an_undeclared_doc_setting(tmp_path):
    (tmp_path / ".env").write_text("SLT_POSTGRES_MEM_LIMT=4g\n")
    checks, _ = doctor.check_env({"SLT_PROJECT_ROOT": str(tmp_path)}, REPO)
    failures = [c for c in checks if c.status == "FAIL"]
    assert len(failures) == 1
    assert "SLT_POSTGRES_MEM_LIMT" in failures[0].message
    assert "is not a key" in failures[0].message


def test_password_is_documented_as_the_name_the_engine_reads():
    """STRIIM_PASS is the documented name; the first-test guide names STRIIM_PASSWORD once, as its alias."""
    assert "STRIIM_PASSWORD" not in EXAMPLE.read_text() and "STRIIM_PASSWORD" not in README
    assert FIRST.count("STRIIM_PASSWORD") == 1
    assert "`STRIIM_PASSWORD` is accepted as another name for `STRIIM_PASS`" in " ".join(FIRST.split())
    for readme in (REPO / "samples").rglob("README.md"):
        assert "STRIIM_PASSWORD" not in readme.read_text(), readme


def _child_env(monkeypatch, base, clean=()):
    from types import SimpleNamespace

    from striim_test.dispatch import child_env
    for k in ("STRIIM_URL", "STRIIM_USER", "STRIIM_PASS", "STRIIM_PASSWORD", "SLT_INFRA_OWNERSHIP",
              "SLT_KEEP_SERVICES", "SLT_PG_HOST", *clean):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(paths, "dotenv_values", lambda env=None: paths.read_dotenv(paths.dotenv_path(env)))
    return child_env(SimpleNamespace(mode="clone", home=REPO), base=base)


def test_striim_settings_in_env_reach_the_run(tmp_path, monkeypatch):
    """striim-test hands STRIIM_URL/USER/PASS from .env to the tier child,
    with the ownership and service keys, so `.env` is enough and the docs say nothing about
    exporting them."""
    (tmp_path / ".env").write_text("STRIIM_URL=http://localhost:49080\nSTRIIM_USER=admin\nSTRIIM_PASS=x\n"
                                   "SLT_INFRA_OWNERSHIP=shared\nSLT_KEEP_SERVICES=1\nSLT_PG_HOST=db.example\n")
    env = _child_env(monkeypatch, {"SLT_PROJECT_ROOT": str(tmp_path)})
    assert (env["STRIIM_URL"], env["STRIIM_USER"], env["STRIIM_PASS"]) == ("http://localhost:49080", "admin", "x")
    assert env["SLT_INFRA_OWNERSHIP"] == "shared" and env["SLT_PG_HOST"] == "db.example"
    assert "export all three" not in README and "ignores them and uses Docker mode" not in EXAMPLE.read_text()


def test_striim_settings_shell_wins_and_the_alias_is_read(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("STRIIM_URL=http://from-dotenv:9080\nSTRIIM_PASSWORD=from-dotenv\n")
    env = _child_env(monkeypatch, {"SLT_PROJECT_ROOT": str(tmp_path), "STRIIM_URL": "http://shell:9080"})
    assert env["STRIIM_URL"] == "http://shell:9080"         # the shell wins
    assert env["STRIIM_PASS"] == "from-dotenv"              # STRIIM_PASSWORD in .env is STRIIM_PASS
    env = _child_env(monkeypatch, {"SLT_PROJECT_ROOT": str(tmp_path), "STRIIM_PASSWORD": "shell"})
    assert "STRIIM_PASS" not in env                         # either name in the shell beats .env


def test_cpu_cap_settings_are_documented_and_wired():
    """A Docker cluster on a big host exceeds the license's CPU cap unless both nodes are
    capped; the first-test guide and .env.example name both settings, and the compose file passes them on."""
    import yaml
    for doc in (FIRST, EXAMPLE.read_text()):
        assert "export SLT_STRIIM_PRIMARY_CPUS=12 SLT_STRIIM_NODE_CPUS=12" in doc
        assert "24" in doc and "license" in doc
    services = yaml.safe_load((REPO / "scripts/live/services/striim/compose.yaml").read_text())["services"]
    assert services["slt-striim"]["cpus"] == "${SLT_STRIIM_PRIMARY_CPUS:-0}"
    assert services["slt-node"]["cpus"] == "${SLT_STRIIM_NODE_CPUS:-0}"


def test_readme_run_line_names_a_sample():
    runs = re.findall(r"^\s*striim-test run (samples/live/\S+)\s*$", README, re.M)
    assert runs == ["samples/live/01-plain-replication"]
    assert (REPO / runs[0] / "test.yaml").is_file()
    for sample in (REPO / "samples" / "live").iterdir():
        assert f"`{sample.name}`" in README, sample.name


def test_readme_steps_in_order():
    steps = ["python3 -m venv .venv && .venv/bin/pip install -e .", "cp .env.example .env", "striim-test doctor",
             "striim-test run samples/live/01-plain-replication"]
    at = [README.index(s) for s in steps]
    assert at == sorted(at)


def test_temporary_notes_are_marked():
    """Each temporary statement sits in one marked block, so it is easy to find and delete."""
    marks = {}
    for doc in REPO.glob("**/*.md"):
        if ".venv" in doc.parts:
            continue
        for m in re.findall(r"<!-- TEMPORARY \(([^)]+)\)", doc.read_text()):
            marks.setdefault(m, []).append(doc.relative_to(REPO).as_posix())
    assert marks == {}


def test_every_sample_readme_run_line_uses_the_path_form():
    for readme in (REPO / "samples").rglob("README.md"):
        if "java" in readme.relative_to(REPO / "samples").parts:
            continue
        text = readme.read_text()
        assert "--suite" not in text and "SLT_LIVE_CASES=samples " not in text, readme
        for path in re.findall(r"striim-test run (samples/\S+)", text):
            assert (REPO / path.rstrip("`,")).exists(), (readme, path)


def test_doc_links_resolve():
    for doc in [REPO / "README.md", *(REPO / "docs").rglob("*.md"),
                *(REPO / name for name in ("AGENTS.md", "CONTRIBUTING.md") if (REPO / name).is_file()),
                *(REPO / "templates").rglob("*.md")]:
        for target in re.findall(r"\]\(([^)#:]+)(?:#[^)]*)?\)", doc.read_text()):
            assert (doc.parent / target).exists(), (doc.name, target)


def test_deps_manifest_route_is_documented(tmp_path):
    """A customer who already has the Striim installers points SLT_STRIIM_DEPS_MANIFEST at
    them instead of the 6.3 GB download. The first-test guide and .env.example show it as a shell export
    (.env does not supply it), the guide lists every file the manifest must name, and a manifest written
    the way the guide describes loads."""
    from livetest import striim_provision
    for doc in (FIRST, EXAMPLE.read_text()):
        assert "export SLT_STRIIM_DEPS_MANIFEST=" in doc
    assert "SLT_STRIIM_DEPS_MANIFEST" not in paths.KEYS
    section = FIRST[FIRST.index("export SLT_STRIIM_DEPS_MANIFEST="):]
    for name in striim_provision.REQUIRED_DEPS:
        assert f"`{name}`" in section, name
    manifest = tmp_path / "deps-manifest.json"
    import json
    manifest.write_text(json.dumps({"schemaVersion": 1, "directory": ".",
                                    "sha256": {n: "0" * 64 for n in striim_provision.REQUIRED_DEPS}}))
    directory, digests = striim_provision.load_striim_deps_manifest(manifest)
    assert set(digests) == set(striim_provision.REQUIRED_DEPS)


# Words that must not appear in the customer docs. Private names (modules, hosts, tools) are not
# listed here: they come from the maintainer's deny-pattern file, the one the release tool reads
# (RELEASE_DENY_PATTERNS, kept outside this repository), when it is set.
_PRIVATE = re.compile(r"\bfleet\b|SLT_(SPANNER|GCS|KAFKA)=1|INT_(SPANNER|GCS)=1", re.I)


def _deny_patterns():
    import importlib.util
    spec = importlib.util.spec_from_file_location("release", REPO / "tools" / "release.py")
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    return release.load_patterns(os.environ.get("RELEASE_DENY_PATTERNS") or None)


def test_customer_docs_name_nothing_private():
    docs = [REPO / "README.md", REPO / "AGENTS.md", REPO / "CONTRIBUTING.md", *(REPO / "docs").rglob("*.md"),
            *(REPO / "samples").rglob("*.md"), *(REPO / "templates").rglob("*.md"),
            *(REPO / "skills").rglob("*.md")]
    found = [(d.relative_to(REPO).as_posix(), m.group(0)) for d in docs for m in _PRIVATE.finditer(d.read_text())]
    patterns = _deny_patterns()
    found += [(d.relative_to(REPO).as_posix(), m.group(0)) for d in docs
              for _, rx in patterns for m in rx.finditer(d.read_text())]
    assert not found, found


def test_the_jmx_reference_tells_existing_cases_how_to_upgrade():
    """`bean.domain` became required: a case written before it must be told what to set."""
    text = (REPO / "docs" / "TEST-YAML.md").read_text()
    section = text[text.index("### `jmx`"):]
    section = section[:section.index("\n## ")]
    note = next((p for p in section.split("\n\n") if p.startswith("**Upgrading")), "")
    assert "domain:" in note and "no implicit domain" in note and "example" in note, note
