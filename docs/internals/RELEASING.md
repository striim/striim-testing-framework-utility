# Releasing the framework

For maintainers. Users pin a release by its tag (`vX.Y.Z`) or by the commit it names; this page is
how a tag is made.

## Creating the public repository

Follow this checklist once, in order, when publishing a fresh repository:

1. Create the new repository as **private**, from a squashed snapshot of `main`: one baseline
   commit, without the private history or its tags. Put that baseline on the new repository's
   `origin/main`, then work from a clean checkout of its `main`.
2. Run `python tools/release.py --bootstrap-pin --push`. This repairs the starter and worked
   consumer pins that still name the old history, commits them and pushes `main`. See
   "The first release of a fresh repository" below. Until this runs, the template and example pins
   name commits the new repository does not have. Then make the repository public.
3. Enable **private vulnerability reporting** in **Settings → Code security**. Verify that the
   Security tab offers **Report a vulnerability**: [SECURITY.md](../../SECURITY.md) depends on it.
4. Protect `main`: require maintainer review for contributions, and prevent
   force pushes and deletion. Allow the designated release maintainer to push the release commit
   through the protection; the release tool commits directly on `main`.
5. Write the first release's notes by hand. Set `export RELEASE_DENY_PATTERNS=<deny-patterns-file>`
   to the file of names that must never be published (see "The notes" below), then run
   `python tools/release.py <version> --notes-file <notes>` as a dry run and read the entry. Run
   `python tools/release.py <version> --apply --notes-file <notes>` with the chosen `X.Y.Z` version
   and notes file. Use the contributor test environment from [CONTRIBUTING.md](../../CONTRIBUTING.md).
   Review the local commit and annotated tag, then run the two publishing commands the tool prints
   (the atomic push and `gh release create`). `--apply` alone does not publish.
6. Confirm the tag and GitHub release are public, then announce the repository and release.

## The command

```bash
export RELEASE_DENY_PATTERNS=~/release-deny.txt   # your names that must never be published (below)
python tools/release.py --bump minor           # dry run: checks, then prints every step
python tools/release.py --bump minor --apply   # the same steps, locally: commit and tag, no push
python tools/release.py --bump minor --push    # also push main and the tag, create the GitHub release
```

Give the version instead of `--bump patch|minor|major` to choose it yourself: `tools/release.py 0.3.0`.
`--bump` counts from the version in the root `pyproject.toml`.

Without `--apply` or `--push` nothing changes: the checks run and each step is printed, the
release-notes entry included. Run that first and read the entry.

## What it checks first

It refuses (exit 2), changing nothing, when:

- the checkout is not on `main`;
- a tracked file has uncommitted changes, or there is an untracked file that is not ignored (the
  `--notes-file`, if it is inside the checkout, is the one exception; it is never committed);
- `main` is not equal to `origin/main` after a `git fetch` (`--no-fetch` compares with the last fetch;
  `--remote` names another remote);
- the version is not `X.Y.Z`, its tag exists, or it is not above the previous `vX.Y.Z` tag;
- `templates/consumer-repo/framework.pin` names no commit or tag of this repository (see "The first
  release of a fresh repository");
- the release notes match a deny pattern (see below; the matches are listed), or there is no previous
  tag and no `--notes-file`, or the notes are generated and no deny-pattern file is given.

## What it does

1. Runs the hermetic suites with the current interpreter (`--python` names another), and stops at
   the first failure before any file changes (exit 1):
   - `tests/` at the root;
   - `scripts/live`: `pytest -m "not live" tests`;
   - `scripts/integration`: `pytest -m "not docker" tests`.

   `--skip-tests` skips them and prints a warning. Use it only when the same commit has just passed
   them.
2. Sets the version everywhere a package states it: `version` in the `[project]` table of
   `pyproject.toml`, `scripts/live/pyproject.toml` and `scripts/integration/pyproject.toml`, and
   `__version__` in `striim_test`, `livetest` and `inttest` (what `striim-test` and the perf reports
   print). `tests/test_release.py` fails if another `__version__` literal appears without being added.
   It also writes the tag into `templates/consumer-repo/framework.pin` and every shipped
   `examples/*/framework.pin`, so the starter and worked consumer repos use the release
   containing their guides and apps; `tests/test_consumer_template.py` checks the pin resolves in this repository.
3. Adds an entry at the top of `RELEASE-NOTES.md` (creating the file the first time): the version, the
   date, and the notes (below).
4. Commits those files as `Release vX.Y.Z` and makes the annotated tag `vX.Y.Z`, whose message is the
   notes.
5. With `--push` only: `git push --atomic origin main vX.Y.Z`, then
   `gh release create vX.Y.Z --verify-tag --title vX.Y.Z --notes-from-tag`. If either fails, the commit
   and tag stay local; run the remaining printed steps from the checkout.

`--apply` stops after step 4 and prints the two publishing steps. They need no other file, so they
work as printed after the tool exits. Review with `git show vX.Y.Z`.

## The first release of a fresh repository

A repository made from another one's tree (for example a squashed public copy) starts with the
consumer pins naming commits of the old history. That commit is not in the new one, so the
template test fails and the release refuses before its suites. Once, after the baseline commit is on
`origin/main` and before the first release:

```bash
python tools/release.py --bootstrap-pin          # dry run: prints the old pin and the new one
python tools/release.py --bootstrap-pin --push   # pin unresolved template and example pins to the current commit, commit, push main
```

It runs the same checks as a release (on `main`, clean, equal to `origin/main`), refuses once any
`vX.Y.Z` tag exists, and does nothing when all consumer pins already resolve. `--apply` commits without
pushing; push `main` yourself before the release. Then run the release as usual (the first one with
`--notes-file`): its suites pass, and it moves all consumer pins to the new tag.

## The notes

Either written by hand, or generated:

- **By hand:** `--notes-file notes.md`, Markdown, used as written. Keep it outside the checkout, or
  leave it untracked; it is not committed.
- **Generated:** one line per pull request merged on `main` since the previous tag, its title only:
  squash-merge subjects (`Title (#N)`) and, for merge commits (`Merge pull request #N from ...`), the
  first line of the commit body, which GitHub fills with the title. A pull request's description is
  never copied. Commits pushed straight to `main` are not listed.

Pull request titles can name things that were removed from the public tree. Generated notes are
therefore refused without a deny-pattern file: `--deny-patterns <file>` or `RELEASE_DENY_PATTERNS`.
It holds one `<kind> <regex>` per line, `#` comments allowed, for example:

```
customer (?i)\bcustomer-name\b
repo (?i)private-repo-name
host (?i)\bbuildhost\d*\b
```

Keep the file out of this repository; it holds the very names it guards. Personal home paths and
private IP addresses are always checked. Hand-written notes are checked against the same patterns. A
match refuses the release and lists each hit (`#N: title [kind: match]`); copy the entry from the dry
run into a notes file, edit it, and pass `--notes-file`.

## The first release

The first public release is cut from a fresh repository and its notes are written by hand. With no
previous tag the tool refuses generated notes: history from before the first release may carry
another repository's pull request numbers and titles.

```bash
python tools/release.py 0.1.0 --notes-file ../first-release-notes.md     # dry run, version unchanged
```

Later releases count from the previous tag and may use generated notes.

## After a release

Consumers move by changing the commit their lock file names to the one the tag points at
(`git rev-parse vX.Y.Z^{commit}`), in a change of their own.

## Keeping the docs current

Run `python tools/doc_catalog.py` after adding, removing or editing guides; use
`python tools/doc_catalog.py --check` for the CI-style check. The catalog uses titles and opening
prose, so later section changes may leave it unchanged.

Keep the offline [reference page](../reference.html) linked to the current guides.
`tests/test_reference_page.py` checks its checkout-relative links and its own anchors; it runs in
the root hermetic suite described in [CONTRIBUTING.md](../../CONTRIBUTING.md).

Keep the worked consumer example [examples/acme-retail/](../../examples/acme-retail/README.md)
consistent with the guides. Its `framework.pin` moves with releases, alongside the consumer
template's pin; bootstrap repairs it when creating the public repository.
