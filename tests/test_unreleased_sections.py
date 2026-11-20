"""RELEASE-NOTES.md and docs/UPGRADING.md each keep a standing `## Unreleased` section that the
release tool can read, and every release with breaking changes has an UPGRADING section.

Consumers pin commits of `main`, not only tags, so these sections are how they learn what a commit
changes before a release names it.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("release", REPO / "tools" / "release.py")
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)

NOTES = (REPO / release.NOTES).read_text(encoding="utf-8")
UPGRADING = (REPO / release.UPGRADING).read_text(encoding="utf-8")


def _sections(text):
    """{heading: body} for each `## ` section."""
    parts = re.split(r"^(## .*)$", text, flags=re.M)
    return {parts[i].strip(): parts[i + 1] for i in range(1, len(parts), 2)}


def test_both_files_open_with_an_unreleased_section_the_release_tool_reads():
    for name, text in ((release.NOTES, NOTES), (release.UPGRADING, UPGRADING)):
        first = re.search(r"^## .*$", text, re.M).group(0).strip()
        assert first == release.UNRELEASED, f"{name}: first section is {first!r}"
        assert release.unreleased(text) is not None
        assert text.count(release.UNRELEASED + "\n") == 1, name


def test_every_release_with_breaking_changes_has_an_upgrading_section():
    upgrading = set(_sections(UPGRADING))
    for heading, body in _sections(NOTES).items():
        m = re.fullmatch(r"## (v\d+\.\d+\.\d+) \(\d{4}-\d{2}-\d{2}\)", heading)
        if m and "**Breaking changes" in body:
            assert f"## {m.group(1)}" in upgrading, f"{m.group(1)} has breaking changes and no UPGRADING section"


def test_unreleased_breaking_changes_have_upgrading_headings():
    if "**Breaking changes" in (release.unreleased(NOTES) or ""):
        assert release.unreleased(UPGRADING), "Unreleased breaking changes need UPGRADING entries"


def test_the_consumer_template_ignores_the_case_records():
    for rel in ("templates/consumer-repo/.gitignore", "examples/acme-retail/.gitignore"):
        assert ".local-evidence/" in (REPO / rel).read_text().splitlines(), rel
