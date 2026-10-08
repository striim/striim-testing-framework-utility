"""tools/release.py: the maintainer's release command.

Each test builds a throwaway repository with the three pyproject files and a bare `origin`, so
nothing here touches this checkout, its remote or GitHub. `gh` is always a stub that records
its arguments.
"""
from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("release", REPO / "tools" / "release.py")
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)

PYPROJECTS = ("pyproject.toml", "scripts/live/pyproject.toml", "scripts/integration/pyproject.toml")
RUNTIME = ("scripts/cli/striim_test/__init__.py", "scripts/live/livetest/__init__.py",
           "scripts/integration/inttest/__init__.py")
TEMPLATE_PIN = "templates/consumer-repo/framework.pin"


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def commit(repo, subject, body=""):
    (repo / "change.txt").write_text(subject + "\n")
    git(repo, "add", "change.txt")
    git(repo, "commit", "-q", "-m", subject + ("\n\n" + body if body else ""))


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for key, value in {"GIT_AUTHOR_NAME": "Maintainer", "GIT_AUTHOR_EMAIL": "maintainer@example.com",
                       "GIT_COMMITTER_NAME": "Maintainer", "GIT_COMMITTER_EMAIL": "maintainer@example.com",
                       "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}.items():
        monkeypatch.setenv(key, value)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    for rel in PYPROJECTS:
        path = work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('[build-system]\nrequires = ["setuptools>=68"]\n\n[project]\nname = "x"\n'
                        'version = "0.1.0"\ndescription = "x"\n\n[tool.other]\nversion = "9.9.9"\n')
    for rel in RUNTIME:
        path = work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('"""pkg"""\n__version__ = "0.1.0"\n\ntry:\n    __version__ = PROVENANCE["VERSION"]\n'
                        'except NameError:\n    pass\n')
    (work / TEMPLATE_PIN).parent.mkdir(parents=True)
    (work / TEMPLATE_PIN).write_text("0" * 40 + "\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "Initial layout")
    (work / TEMPLATE_PIN).write_text(git(work, "rev-parse", "HEAD") + "\n")   # a commit of this history
    git(work, "commit", "-q", "-am", "Pin the template")
    commit(work, "Carried over from an older repository (#900)")
    git(work, "tag", "-a", "v0.1.0", "-m", "v0.1.0")
    commit(work, "Live harness: first fix (#1)")
    commit(work, "Merge pull request #2 from org/branch-two", "Integration: second change")
    commit(work, "A direct commit with no pull request")
    git(work, "remote", "add", "origin", str(origin))
    git(work, "push", "-q", "origin", "main")
    gh_log = tmp_path / "gh.log"
    gh = tmp_path / "gh"
    gh.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" >> "{gh_log}"\n')
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("RELEASE_GH", str(gh))
    deny = tmp_path / "deny.txt"
    deny.write_text("# names that must never be published\ncustomer (?i)\\bacme\\b\nrepo (?i)private-field\n")
    monkeypatch.setenv("RELEASE_DENY_PATTERNS", str(deny))
    return work


def run(repo, *args):
    return release.main(["--repo", str(repo), *args])


def versions(repo):
    return [release.read_version((repo / rel).read_text()) for rel in PYPROJECTS]


def runtime_versions(repo):
    return [release.read_runtime_version((repo / rel).read_text()) for rel in RUNTIME]


# --- version arithmetic -------------------------------------------------------------------

@pytest.mark.parametrize("part,want", [("patch", "0.1.1"), ("minor", "0.2.0"), ("major", "1.0.0")])
def test_bump(part, want):
    assert release.bump("0.1.0", part) == want


def test_set_version_changes_only_the_project_table():
    text = '[project]\nname = "x"\nversion = "0.1.0"\n\n[tool.other]\nversion = "9.9.9"\n'
    out = release.set_version(text, "0.2.0")
    assert 'version = "0.2.0"' in out and 'version = "9.9.9"' in out


# --- refusals -----------------------------------------------------------------------------

def test_refuses_off_main(repo, capsys):
    git(repo, "switch", "-q", "-c", "feature")
    assert run(repo, "--bump", "minor") == 2
    assert "not on main" in capsys.readouterr().err


def test_refuses_uncommitted_tracked_changes(repo, capsys):
    (repo / "pyproject.toml").write_text((repo / "pyproject.toml").read_text() + "\n")
    assert run(repo, "--bump", "minor") == 2
    assert "uncommitted" in capsys.readouterr().err


def test_refuses_main_ahead_of_origin(repo, capsys):
    commit(repo, "Local only (#3)")
    assert run(repo, "--bump", "minor") == 2
    assert "origin/main" in capsys.readouterr().err


def test_refuses_main_behind_origin(repo, tmp_path, capsys):
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "origin.git"), str(other)], check=True)
    commit(other, "Landed elsewhere (#3)")
    git(other, "push", "-q", "origin", "main")
    assert run(repo, "--bump", "minor") == 2           # the fetch sees origin/main move on
    assert "origin/main" in capsys.readouterr().err


def test_refuses_an_existing_tag_or_a_version_not_above_the_last_release(repo, capsys):
    git(repo, "tag", "-a", "v0.2.0", "-m", "v0.2.0")
    git(repo, "push", "-q", "origin", "v0.2.0")
    assert run(repo, "0.2.0") == 2
    assert "already exists" in capsys.readouterr().err
    assert run(repo, "0.1.5") == 2
    assert "not above" in capsys.readouterr().err


def test_refuses_a_malformed_version(repo, capsys):
    assert run(repo, "1.2") == 2
    assert "X.Y.Z" in capsys.readouterr().err


# --- release notes ------------------------------------------------------------------------

def test_notes_list_merged_pull_requests_since_the_previous_tag(repo):
    git(repo, "tag", "-a", "v0.1.1", "-m", "v0.1.1")
    commit(repo, "Services: third change (#3)")
    entries = release.pull_requests(repo, release.previous_tag(repo))
    assert entries == [(3, "Services: third change")]


def test_first_release_needs_a_hand_written_notes_file(repo, capsys, tmp_path):
    """History carried over from another repository has that repository's PR numbers in it."""
    git(repo, "tag", "-d", "v0.1.0")
    assert release.previous_tag(repo) is None
    assert run(repo, "0.1.0") == 2
    assert "--notes-file" in capsys.readouterr().err
    notes = tmp_path / "first.md"
    notes.write_text("First public release.\n")
    assert run(repo, "0.1.0", "--notes-file", str(notes)) == 0
    out = capsys.readouterr().out
    assert "First public release." in out and "#900" not in out and "(#1)" not in out


def test_generated_notes_with_a_denied_title_are_refused_with_the_hits(repo, capsys):
    commit(repo, "Drop references to private-field docs (#3)")
    commit(repo, "Merge pull request #4 from org/x", "Acme fixture fix\n\nThe ACME body text is never read")
    git(repo, "push", "-q", "origin", "main")
    assert run(repo, "--bump", "minor") == 2
    err = capsys.readouterr().err
    assert "#3" in err and "private-field" in err and "#4" in err and "Acme" in err
    assert "body text" not in err
    assert git(repo, "tag") == "v0.1.0"


def test_generated_notes_without_a_deny_pattern_file_are_refused(repo, monkeypatch, capsys):
    monkeypatch.delenv("RELEASE_DENY_PATTERNS")
    assert run(repo, "--bump", "minor") == 2
    err = capsys.readouterr().err
    assert "--deny-patterns" in err and "--notes-file" in err


def test_deny_patterns_flag_and_hand_notes_are_checked_too(repo, monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("RELEASE_DENY_PATTERNS")
    deny = tmp_path / "other.txt"
    deny.write_text("host (?i)buildhost\\d+\n")
    notes = tmp_path / "notes.md"
    notes.write_text("Fixed on buildhost7.\n")
    assert run(repo, "--bump", "minor", "--notes-file", str(notes), "--deny-patterns", str(deny)) == 2
    assert "buildhost7" in capsys.readouterr().err
    notes.write_text("Fixed a leak in /home/someone/work.\n")       # built-in personal-path check
    assert run(repo, "--bump", "minor", "--notes-file", str(notes)) == 2
    assert "/home/someone/" in capsys.readouterr().err


def test_merge_commit_bodies_are_never_copied(repo):
    commit(repo, "Merge pull request #4 from org/x", "Title line\n\nA description that stays private")
    titles = [t for _, t in release.pull_requests(repo, "v0.1.0")]
    assert "Title line" in titles and not any("description" in t for t in titles)


def test_notes_entry_and_file():
    entry = release.notes_entry("0.2.0", [(1, "First"), (2, "Second")], "2026-10-05")
    assert entry == "## v0.2.0 (2026-10-05)\n\n- First (#1)\n- Second (#2)\n"
    text = release.add_entry(None, entry)
    assert text.startswith("# Release notes\n") and entry in text
    newer = release.notes_entry("0.3.0", [(3, "Third")], "2026-11-01")
    both = release.add_entry(text, newer)
    assert both.index("v0.3.0") < both.index("v0.2.0")


# --- dry run, apply, push -----------------------------------------------------------------

def test_dry_run_is_the_default_and_changes_nothing(repo, capsys, tmp_path):
    head = git(repo, "rev-parse", "HEAD")
    pin = (repo / TEMPLATE_PIN).read_text()
    assert run(repo, "--bump", "minor") == 0
    out = capsys.readouterr().out
    for step in ("pytest", f"{TEMPLATE_PIN}: pin {pin.strip()} -> v0.2.0", "Release v0.2.0", "git tag -a v0.2.0", "git push --atomic origin main v0.2.0",
                 "release create v0.2.0", "- Live harness: first fix (#1)", "DRY RUN"):
        assert step in out, step
    assert git(repo, "rev-parse", "HEAD") == head and git(repo, "status", "--porcelain") == ""
    assert git(repo, "tag") == "v0.1.0" and versions(repo) == ["0.1.0"] * 3
    assert runtime_versions(repo) == ["0.1.0"] * 3
    assert (repo / TEMPLATE_PIN).read_text() == pin
    assert "#900" not in out
    assert not (tmp_path / "gh.log").exists()


def test_apply_commits_and_tags_locally_without_pushing(repo, capsys, tmp_path):
    origin_head = git(tmp_path / "origin.git", "rev-parse", "main")
    assert run(repo, "--bump", "minor", "--apply", "--skip-tests") == 0
    assert "WARNING" in capsys.readouterr().err                  # --skip-tests is loud
    assert versions(repo) == ["0.2.0"] * 3
    assert runtime_versions(repo) == ["0.2.0"] * 3                  # what the packages report at run time
    for rel in RUNTIME:
        assert 'PROVENANCE["VERSION"]' in (repo / rel).read_text()  # only the literal moved
    assert git(repo, "show", "--name-only", "--format=", "HEAD").split() == sorted([*PYPROJECTS, *RUNTIME, TEMPLATE_PIN, "RELEASE-NOTES.md"])
    assert git(repo, "log", "-1", "--format=%s") == "Release v0.2.0"
    assert git(repo, "rev-parse", "v0.2.0^{commit}") == git(repo, "rev-parse", "HEAD")
    assert git(repo, "cat-file", "-t", "v0.2.0") == "tag"        # annotated
    assert git(repo, "show", f"v0.2.0:{TEMPLATE_PIN}") == "v0.2.0"   # a new consumer starts at this release
    notes = (repo / "RELEASE-NOTES.md").read_text()
    assert "## v0.2.0 (" in notes and "- Integration: second change (#2)" in notes
    assert "direct commit" not in notes
    assert git(repo, "status", "--porcelain") == ""
    assert git(tmp_path / "origin.git", "rev-parse", "main") == origin_head
    assert not (tmp_path / "gh.log").exists()


def test_printed_publish_commands_work_after_the_tool_exits(repo, capsys, tmp_path):
    """The notes travel in the annotated tag, so the printed gh command needs no file."""
    assert run(repo, "--bump", "minor", "--apply", "--skip-tests") == 0
    out = capsys.readouterr().out
    lines = [l.strip().removeprefix("==> ") for l in out.splitlines()]
    push = next(l for l in lines if l.startswith("git push"))
    create = next(l for l in lines if "release create" in l)
    assert "--notes-file" not in create and "--notes-from-tag" in create
    assert "- Integration: second change (#2)" in git(repo, "tag", "-l", "--format=%(contents)", "v0.2.0")
    subprocess.run(push, shell=True, cwd=repo, check=True, capture_output=True)
    subprocess.run(create, shell=True, cwd=repo, check=True)
    assert git(tmp_path / "origin.git", "rev-parse", "v0.2.0^{commit}") == git(repo, "rev-parse", "HEAD")
    assert (tmp_path / "gh.log").read_text().split("\n")[:3] == ["release", "create", "v0.2.0"]


def test_untracked_files_are_refused_and_notes_are_never_read_from_the_tree(repo, tmp_path, capsys):
    (repo / "RELEASE-NOTES.md").write_text("scratch draft, not for release\n")
    assert run(repo, "--bump", "minor", "--apply", "--skip-tests") == 2
    assert "untracked" in capsys.readouterr().err and git(repo, "tag") == "v0.1.0"
    notes = repo / "draft.md"                                      # the named notes file is the one exception
    (repo / "RELEASE-NOTES.md").unlink()
    notes.write_text("Hand-written notes.\n")
    assert run(repo, "--bump", "minor", "--apply", "--skip-tests", "--notes-file", str(notes)) == 0
    assert "Hand-written notes." in git(repo, "show", "HEAD:RELEASE-NOTES.md")
    assert "draft.md" not in git(repo, "show", "--name-only", "--format=", "HEAD")


def test_every_runtime_version_literal_in_this_repo_is_one_the_tool_moves():
    """A new `__version__ = "..."` anywhere in the engines must be added to RUNTIME_VERSIONS."""
    tracked = subprocess.run(["git", "-C", str(REPO), "ls-files", "*.py"], capture_output=True, text=True).stdout.split()
    found = {rel for rel in tracked if not rel.startswith(("tests/", "tools/")) and "/tests/" not in rel
             and release._RUNTIME_LINE.search((REPO / rel).read_text(errors="replace"))}
    assert found == set(release.RUNTIME_VERSIONS)
    root = release.read_version((REPO / "pyproject.toml").read_text())
    assert {release.read_runtime_version((REPO / rel).read_text()) for rel in found} == {root}


def test_push_pushes_commit_and_tag_then_creates_the_release(repo, tmp_path):
    assert run(repo, "0.2.0", "--push", "--skip-tests") == 0
    origin = tmp_path / "origin.git"
    assert git(origin, "rev-parse", "main") == git(repo, "rev-parse", "HEAD")
    assert git(origin, "rev-parse", "v0.2.0^{commit}") == git(repo, "rev-parse", "HEAD")
    args = (tmp_path / "gh.log").read_text().split("\n")
    assert args[:4] == ["release", "create", "v0.2.0", "--verify-tag"]
    assert "--title" in args and "--notes-from-tag" in args
    assert "- Live harness: first fix (#1)" in git(origin, "tag", "-l", "--format=%(contents)", "v0.2.0")


def test_failing_suite_stops_before_anything_changes(repo, monkeypatch, capsys):
    monkeypatch.setattr(release, "SUITES", [(".", ["-c", "import sys; sys.exit(1)"])])
    head = git(repo, "rev-parse", "HEAD")
    assert run(repo, "--bump", "patch", "--apply") == 1
    assert "suite failed" in capsys.readouterr().err
    assert git(repo, "rev-parse", "HEAD") == head and versions(repo) == ["0.1.0"] * 3
    assert git(repo, "tag") == "v0.1.0"


def test_suites_run_with_the_chosen_python(repo, monkeypatch, tmp_path):
    marker = tmp_path / "ran"
    monkeypatch.setattr(release, "SUITES", [("scripts/live", ["-c", f"open({str(marker)!r}, 'w').write(__import__('os').getcwd())"])])
    assert run(repo, "--bump", "patch", "--apply") == 0
    assert marker.read_text() == str(repo / "scripts" / "live")


# --- the first release of a fresh repository ----------------------------------------------

# What tests/test_consumer_template.py checks, run as the release's suite: the pin resolves here.
_PIN_SUITE = ("import subprocess, sys; pin = open(sys.argv[1]).read().split()[0]; "
              "ref = pin if len(pin) == 40 else 'refs/tags/' + pin; "
              "sys.exit(subprocess.run(['git', 'rev-parse', '--verify', '--quiet', ref + '^{commit}']).returncode)")


def test_a_fresh_repository_bootstraps_the_pin_before_its_first_release(repo, monkeypatch, tmp_path, capsys):
    # A squashed public history: no release tags, and the template still pins a commit of the old one.
    git(repo, "tag", "-d", "v0.1.0")
    (repo / TEMPLATE_PIN).write_text("fd06e5d910d1ed8fa3764a195887a0ce011aca10\n")
    git(repo, "commit", "-q", "-am", "Public baseline")
    git(repo, "push", "-q", "origin", "main")
    baseline = git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(release, "SUITES", [(".", ["-c", _PIN_SUITE, TEMPLATE_PIN])])
    notes = tmp_path / "notes.md"
    notes.write_text("First public release.\n")

    assert run(repo, "0.2.0", "--apply", "--notes-file", str(notes)) == 2
    assert "--bootstrap-pin" in capsys.readouterr().err
    assert git(repo, "rev-parse", "HEAD") == baseline and git(repo, "tag") == ""

    assert run(repo, "--bootstrap-pin") == 0                        # dry run by default
    assert baseline in capsys.readouterr().out and git(repo, "rev-parse", "HEAD") == baseline
    assert run(repo, "--bootstrap-pin", "--push") == 0
    assert git(repo, "show", f"HEAD:{TEMPLATE_PIN}") == baseline
    assert git(repo, "show", "--name-only", "--format=", "HEAD").split() == [TEMPLATE_PIN]
    assert git(tmp_path / "origin.git", "rev-parse", "main") == git(repo, "rev-parse", "HEAD")

    assert run(repo, "0.2.0", "--apply", "--notes-file", str(notes)) == 0   # the suite passes now
    assert git(repo, "show", f"v0.2.0:{TEMPLATE_PIN}") == "v0.2.0"


def test_bootstrap_pin_is_only_for_a_repository_without_releases(repo, capsys):
    assert run(repo, "--bootstrap-pin", "--apply") == 2
    assert "v0.1.0" in capsys.readouterr().err


def test_release_updates_worked_consumer_pins(repo):
    example_pin = repo / 'examples/retail/framework.pin'
    example_pin.parent.mkdir(parents=True)
    example_pin.write_text(git(repo, 'rev-parse', 'HEAD') + '\n')
    git(repo, 'add', str(example_pin.relative_to(repo)))
    git(repo, 'commit', '-q', '-m', 'Add a worked consumer')
    git(repo, 'push', '-q', 'origin', 'main')
    assert run(repo, '--bump', 'patch', '--apply', '--skip-tests') == 0
    assert example_pin.read_text().strip() == 'v0.1.1'
    assert git(repo, 'show', 'v0.1.1:examples/retail/framework.pin').strip() == 'v0.1.1'


def test_bootstrap_repairs_a_worked_consumer_pin(repo):
    for tag in git(repo, 'tag').splitlines():
        git(repo, 'tag', '-d', tag)
    example_pin = repo / 'examples/retail/framework.pin'
    example_pin.parent.mkdir(parents=True)
    example_pin.write_text('0' * 40 + '\n')
    git(repo, 'add', str(example_pin.relative_to(repo)))
    git(repo, 'commit', '-q', '-m', 'Carry a worked consumer pin from another history')
    git(repo, 'push', '-q', 'origin', 'main')
    baseline = git(repo, 'rev-parse', 'HEAD')
    assert run(repo, '--bootstrap-pin', '--apply') == 0
    assert example_pin.read_text().strip() == baseline
