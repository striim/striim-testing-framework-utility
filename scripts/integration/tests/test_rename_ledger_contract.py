"""The integration tier's half of the container-rename contract.

Mirrors scripts/live/tests/test_registry_contract.py's ledger tests, against this tier's
compose files. `_RENAMED_CONTAINERS` is hand-maintained, and a ledger only stays correct if
something checks it: a container renamed without an entry is invisible twice over -- to
`docker compose down` (the old container belongs to no project today's file names) and to
`orphaned_containers` (which reads today's compose file) -- so a leftover is stranded with
no diagnostic, which is exactly how the live tier's slt-gcs-token rename slipped through.

This tier's ledger is empty today: it renamed compose PROJECTS on this branch, not
containers. That is precisely when a guard is worth adding -- the first rename is the one
that gets forgotten.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

from inttest import services

_REPO = Path(__file__).resolve().parents[3]   # scripts/integration/tests -> repo root
_SERVICES = _REPO / "scripts/integration/services"


def _compose_rel_paths() -> list:
    return [f"scripts/integration/services/{p.name}/compose.yaml"
            for p in sorted(_SERVICES.iterdir()) if (p / "compose.yaml").is_file()]


def _baseline_ref():
    for ref in ("main", "origin/main"):
        if subprocess.run(["git", "rev-parse", "--verify", "-q", ref],
                          cwd=_REPO, capture_output=True).returncode == 0:
            return ref
    return None


def _declared_at(ref: str, rels) -> set:
    """The names `ref` declares, read with TODAY's parser -- so a parser change can never make
    this test disagree with the code it guards."""
    names = set()
    with tempfile.TemporaryDirectory() as tmp:
        for i, rel in enumerate(rels):
            r = subprocess.run(["git", "show", f"{ref}:{rel}"],
                               cwd=_REPO, capture_output=True, text=True)
            if r.returncode != 0:
                continue                      # new file on this branch: nothing to rename FROM
            p = Path(tmp) / f"{i}.yaml"
            p.write_text(r.stdout, encoding="utf-8")
            names |= set(services.declared_containers([p]))
    return names


def test_every_renamed_container_is_in_the_ledger():
    ref = _baseline_ref()
    if ref is None:
        pytest.skip("no main/origin/main to compare against")
    rels = _compose_rel_paths()
    was = _declared_at(ref, rels)
    if not was:
        pytest.skip(f"{ref} declares no containers -- nothing to compare")
    now = set()
    for rel in rels:
        now |= set(services.declared_containers([_REPO / rel]))

    dropped = sorted(was - now)
    assert not dropped, (
        f"{ref} declares container(s) {dropped} that this tree no longer names, and no "
        f"_RENAMED_CONTAINERS entry carries them. A `docker compose down` cannot see a "
        f"container under its old name, so a leftover would be stranded silently. Add "
        f"{{'<new-name>': {dropped}}} to inttest.services._RENAMED_CONTAINERS.")


def test_the_ledger_does_not_carry_names_that_are_still_declared():
    # An entry whose "old" name is still in a compose file is a typo or a rename that never
    # happened; either way orphaned_containers ends up asking Docker a dead question.
    declared = set()
    for rel in _compose_rel_paths():
        for line in (_REPO / rel).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("container_name:"):
                value = services._INTERPOLATION.sub("", line.split(":", 1)[1])
                declared.add(value.split("#", 1)[0].strip().strip("'\"").lstrip("-"))
    for current, olds in services._RENAMED_CONTAINERS.items():
        assert current in declared, f"_RENAMED_CONTAINERS key {current!r} is not declared anywhere"
        for old in olds:
            assert old not in declared, (
                f"_RENAMED_CONTAINERS says {old!r} was renamed to {current!r}, but {old!r} is "
                f"still declared in a compose file")


def test_every_compose_scopes_its_project_and_containers_to_INT_STACK_PREFIX():
    """Two stacks are distinct only if BOTH the project name and every container_name carry
    ``INT_STACK_PREFIX``. services.py states it as an invariant -- "compose.yaml prefixes the
    project name and every container_name, so ``ec`` and ``alt`` own entirely separate
    containers" -- and nothing checked it.

    ⚠ teradata/compose.yaml broke it in two ways at once: its project name interpolated
    ``SLT_STACK_PREFIX`` (the LIVE tier's variable, unset here, so the prefix silently did
    nothing) and its ``container_name`` was the bare literal ``int-teradata``. The ``ec``
    stack's Teradata therefore came up AS ``int-teradata``, on the same published port as the
    unprefixed stack's, and the two fought over one container -- with no error, because from
    Docker's side it is simply a container that already exists.

    A per-service copy/paste invariant needs a per-service check, or the one service that
    deviates is the one nobody looks at.
    """
    offenders = []
    for rel in _compose_rel_paths():
        text = (_REPO / rel).read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if stripped.startswith("name:") or stripped.startswith("container_name:"):
                key, value = stripped.split(":", 1)
                value = value.split("#", 1)[0].strip()
                if "${INT_STACK_PREFIX" not in value:
                    offenders.append(f"{rel}: {key}: {value}")
    assert not offenders, (
        "every compose project name and container_name must interpolate ${INT_STACK_PREFIX} "
        "so two stacks own separate containers:\n  " + "\n  ".join(offenders))
