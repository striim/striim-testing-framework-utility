#!/usr/bin/env python3
"""Build the audience index from Markdown titles and opening prose."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
CATALOG = Path("docs/CATALOG.md")
GROUPS = (
    "Start here", "Writing tests", "Services", "Reference",
    "Running and troubleshooting", "Contributing and internals",
)
# Release notes and this index are generated. Fixture data and dependency/build
# trees are not guides, even when a tracked file in them has a Markdown suffix.
GENERATED = {CATALOG.as_posix(), "RELEASE-NOTES.md"}
EXCLUDED_PARTS = {
    ".git", ".venv", ".venv-test", "venv", "node_modules", "vendor", "vendored",
    "third_party", "build", "dist", "target", "__pycache__", "fixtures", "expected",
}


def documents(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root, check=True, capture_output=True,
    )
    paths = {Path(os.fsdecode(p)) for p in result.stdout.split(b"\0") if p}
    return sorted(
        (p for p in paths if p.suffix.lower() == ".md"
         and p.as_posix() not in GENERATED
         and not EXCLUDED_PARTS.intersection(p.parts)
         and p.parts[:2] != ("tests", "controls")
         and (root / p).is_file() and not (root / p).is_symlink()),
        key=lambda p: p.as_posix(),
    )


def plain(text: str) -> str:
    text = re.sub(r"!?\[([^]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[`*]", "", text)
    return " ".join(text.split()).replace("|", "\\|")


def metadata(text: str) -> tuple[str, str]:
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    title = re.search(r"^#\s+(.+?)\s*#*\s*$", text, re.M)
    if not title:
        raise ValueError("missing level-one title")
    # First prose paragraph after the title, skipping code, tables and headings.
    prose: list[str] = []
    fence = None
    for line in text[title.end():].splitlines():
        line = line.strip()
        marker = re.match(r"(`{3,}|~{3,})", line)
        if marker:
            if prose:
                break
            if fence is None:
                fence = marker[1][0]
            elif marker[1][0] == fence:
                fence = None
            continue
        if fence:
            continue
        if not line or line.startswith(("#", "|", "---")):
            if prose:
                break
            continue
        prose.append(re.sub(r"^(?:>\s*|[-+]\s+|\d+\.\s+)", "", line))
    if not prose:
        raise ValueError("missing opening prose")
    description = plain(" ".join(prose))
    # Keep a short opening label with the sentence that explains it.
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z])", description)
    description = sentences.pop(0)
    while len(description) < 40 and sentences:
        description += " " + sentences.pop(0)
    if len(description) > 280:
        description = description[:277].rsplit(" ", 1)[0] + "…"
    return plain(title[1]), description


def audience(path: Path) -> str:
    name = path.name
    parts = path.parts
    if path.as_posix() == "README.md" or name in {"START-HERE.md", "RUN-YOUR-FIRST-TEST.md"}:
        return GROUPS[0]
    if ("services" in parts and "regression" not in parts) or name in {
        "SERVICES.md", "YOUR-OWN-SERVICES.md", "SERVICENOW.md",
    }:
        return GROUPS[2]
    if name in {"TEST-YAML.md", "WHAT-IS-SUPPORTED.md", "SETTINGS.md"} or parts[:3] == ("docs", "tql", "REFERENCE.md"):
        return GROUPS[3]
    if parts[:2] == ("docs", "tql") or parts[0] == "skills":
        return GROUPS[1]
    if name in {"TROUBLESHOOTING.md", "SET-UP-YOUR-OWN-REPO.md", "RUN-A-DOWNLOADED-EXAMPLE.md"} or parts[0] == "templates":
        return GROUPS[4]
    if parts[0] in {"samples", "examples"} or name in {
        "WRITING-TESTS.md", "TESTING-YOUR-JAVA.md", "INTEGRATION-TESTS.md", "USING-AI.md",
    }:
        return GROUPS[1]
    return GROUPS[5]


def render(root: Path) -> str:
    groups: dict[str, list[str]] = {group: [] for group in GROUPS}
    for path in documents(root):
        try:
            title, description = metadata((root / path).read_text(encoding="utf-8"))
        except ValueError as error:
            raise ValueError(f"{path.as_posix()}: {error}") from error
        target = quote(os.path.relpath(path, CATALOG.parent).replace(os.sep, "/"), safe="/.-_")
        groups[audience(path)].append(f"| [{title}]({target}) | `{path.as_posix()}` | {description} |")
    lines = [
        "# Documentation catalog", "",
        "Guides and reference material, grouped by audience.", "",
        "Generated from each file's title and opening prose. Run `python tools/doc_catalog.py`",
        "after adding, removing or editing a guide; `python tools/doc_catalog.py --check`",
        "checks the committed index without changing it.", "",
        "Includes tracked and unignored Markdown. Excludes generated release notes and this",
        "catalog, vendored dependencies, build output and test fixtures (including expected data).",
        "Runnable regression guides are included under Contributing and internals.", "",
    ]
    for group, rows in groups.items():
        lines += [f"## {group}", "", "| Title | File | Description |", "|---|---|---|", *rows, ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the catalog is missing or stale")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository to index")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    expected = render(root)
    target = root / CATALOG
    if args.check:
        if not target.is_file() or target.read_text(encoding="utf-8") != expected:
            print("Documentation catalog is stale. Run: python tools/doc_catalog.py")
            return 1
        print("Documentation catalog is current.")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(expected, encoding="utf-8")
        print(f"Wrote {CATALOG.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
