"""Engine contracts against the REAL services/ tree and Striim recipe, not fixtures.

Restored from a test repo's scripts/live/tests/test_registry_contract.py: the framework had
removed the whole file because part of it loads that repo's own runner. The parts that test
framework files are here; the runner agreements stay in that repo.

Everything else that touches the registry does so through a monkeypatched seam, so each of
these functions could be gutted entirely and the rest of the suite would stay green. What
they encode is a cross-file agreement, and an agreement is only worth anything if something
checks it. The rename ledger compares against this repo's own `main` (the framework's history).
"""
import re
import subprocess
import tempfile
from pathlib import Path

import pytest

from livetest import preflight, services
from livetest import striim_provision as _sp
from livetest.registry import all_services, load_service

_REPO = Path(__file__).resolve().parents[3]


# --- registry.all_services against the real directory --------------------------------------

def test_all_services_lists_every_service_yaml_on_disk():
    on_disk = sorted(p.name for p in (_REPO / "scripts/live/services").iterdir()
                     if (p / "service.yaml").is_file())
    assert all_services() == on_disk
    assert on_disk, "the live tier registers no services -- the scan is broken"


def test_striim_is_not_a_registry_service():
    # It has compose files but no service.yaml: striim_provision owns its lifecycle, and
    # `stop live <svc>` must never route the cluster through compose_down.
    assert (_REPO / "scripts/live/services/striim/compose.yaml").is_file()
    assert "striim" not in all_services()


def test_every_listed_service_actually_loads():
    # Membership is "has a service.yaml"; load_service additionally requires name/isolation.
    # If those two rules disagree, `stop live` raises partway through a teardown.
    for name in all_services():
        assert load_service(name).name == name


# --- compose project names are tier-scoped --------------------------------------------------

def test_no_compose_project_name_is_shared_between_tiers():
    # Both tiers register gcs/oracle/postgres/spanner. An unprefixed project name makes them
    # ONE docker compose project, which owns the networks and volumes -- so a future
    # `--remove-orphans` would take out the other tier's database.
    def projects(suite):
        return {re.search(r'^name:.*?([\w.-]+)\s*$', p.read_text(), re.M).group(1)
                for p in (_REPO / suite / "services").glob("*/compose.yaml")}

    live, integration = projects("scripts/live"), projects("scripts/integration")
    assert not (live & integration), f"tiers share compose project(s): {sorted(live & integration)}"


# --- preflight.known_test_ids against the real regression tree -------------------------------

def test_known_test_ids_finds_the_real_manifests():
    ids = preflight.known_test_ids()
    assert ids, "empty means every live test id becomes an 'unknown target' SystemExit"
    assert "gcs-diff" in ids
    on_disk = list((_REPO / "scripts/live/regression").rglob("test.yaml"))
    assert len(ids) > len(on_disk) * 0.9   # a few may share a name; most must resolve


# --- a rename must carry its ledger entry --------------------------------------------------
#
# `_RENAMED_CONTAINERS` is a hand-maintained ledger, and a ledger only stays correct if
# something checks it. A container renamed without an entry is invisible twice over: to
# `docker compose down` (the old container belongs to no project today's file names) and to
# `orphaned_containers` (which reads today's compose file) -- which is exactly how the
# slt-gcs-token -> slt-token rename slipped through and cost a `start live gcs` a
# "port is already allocated" the teardown never warned about.

def _git_show(ref: str, path: str):
    """`path` at `ref`, or None when git/the ref/the file isn't there."""
    r = subprocess.run(["git", "show", f"{ref}:{path}"],
                       cwd=_REPO, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def _baseline_ref():
    for ref in ("main", "origin/main"):
        if subprocess.run(["git", "rev-parse", "--verify", "-q", ref],
                          cwd=_REPO, capture_output=True).returncode == 0:
            return ref
    return None


def _declared_at(ref: str, compose_rel_paths) -> set:
    """The container names `ref` declares, read with today's parser.

    Written to a temp file rather than parsed in-memory because `declared_containers` takes
    paths -- the point is to run the SAME parser over both revisions, so a parser change can
    never make this test disagree with the code it guards."""
    names = set()
    with tempfile.TemporaryDirectory() as tmp:
        for i, rel in enumerate(compose_rel_paths):
            text = _git_show(ref, rel)
            if text is None:
                continue                      # new file on this branch: nothing to rename FROM
            p = Path(tmp) / f"{i}.yaml"
            p.write_text(text, encoding="utf-8")
            names |= set(services.declared_containers([p]))
    return names


def _live_compose_rel_paths() -> list:
    # Only services that HAVE a compose file. A live_only service (no container anywhere --
    # teradata) declares none, and asking Docker about containers it never declares is the dead
    # question this whole ledger exists to avoid.
    rels = [f"scripts/live/services/{s}/compose.yaml" for s in all_services()
            if (_REPO / "scripts/live/services" / s / "compose.yaml").is_file()]
    rels += [f"scripts/live/services/striim/{f.name}"
             for f in _sp.compose_files(_REPO / "scripts/live/services/striim")]
    return rels


def test_every_renamed_container_is_in_the_ledger():
    ref = _baseline_ref()
    if ref is None:
        pytest.skip("no main/origin/main to compare against")
    rels = _live_compose_rel_paths()
    was = _declared_at(ref, rels)
    if not was:
        pytest.skip(f"{ref} declares no containers -- nothing to compare")
    # Today's names INCLUDING the ledger's old spellings: that is the whole point -- a name
    # the baseline declared may disappear from the compose files as long as the ledger still
    # carries it, so `orphaned_containers` keeps finding leftovers under it.
    now = set()
    for rel in rels:
        now |= set(services.declared_containers([_REPO / rel]))

    dropped = sorted(was - now)
    assert not dropped, (
        f"{ref} declares container(s) {dropped} that this tree no longer names, and no "
        f"_RENAMED_CONTAINERS entry carries them. A `docker compose down` cannot see a "
        f"container under its old name, so a leftover would be stranded silently. Add "
        f"{{'<new-name>': {dropped}}} to livetest.services._RENAMED_CONTAINERS.")


def test_the_ledger_does_not_carry_names_that_are_still_declared():
    # A ledger entry whose "old" name is still in a compose file is either a typo or a rename
    # that never happened; either way it makes orphaned_containers ask Docker a dead question.
    declared = set()
    for rel in _live_compose_rel_paths():
        text = (_REPO / rel).read_text(encoding="utf-8")
        for line in text.splitlines():
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


# --- the image's embedded inputs must mirror the checkout's LAYOUT ---------------------------

def test_the_dockerfile_copies_exactly_the_inputs_the_comparison_reads():
    """The image's embedded inputs must mirror the checkout's LAYOUT and its SET.

    `COPY ./files /dest/` copies the CONTENTS of files/, flattening it -- and a stubbed docker
    cannot catch that, because the fake reproduces whatever layout the test author assumed. The
    unit tests were green while every real image read as stale (a rebuild on every run); only an
    end-to-end rebuild caught it.

    Checked against `striim_provision._HASH_INPUTS`-equivalent source of truth rather than a
    literal list, so adding an input on one side only fails here instead of silently making
    every image stale."""
    dockerfile_path = _REPO / "scripts/live/services/striim/images/striim/Dockerfile"
    dockerfile = dockerfile_path.read_text()

    dests = {}
    for line in dockerfile.splitlines():
        line = line.strip()
        if not line.startswith("COPY") or "/slt-build-inputs" not in line:
            continue
        # Drop the verb and any --flags (e.g. --chown=...); COPY <src>... <dest>
        words = [w for w in line.split()[1:] if not w.startswith("--")]
        assert len(words) == 2, (
            f"{line!r}: copy build inputs ONE source at a time -- a directory among multiple "
            f"sources is flattened into the destination directory")
        src, dest = words
        dests[src.lstrip("./").rstrip("/")] = dest

    # The set the comparison actually reads, straight from the module under test.
    wanted = set(_sp._BUILD_INPUT_NAMES)
    assert set(dests) == wanted, (
        f"Dockerfile copies {sorted(dests)} into /slt-build-inputs but the comparison reads "
        f"{sorted(wanted)} -- they must be the same set or images read as stale forever")
    for name, dest in dests.items():
        assert dest.rstrip("/") == f"/slt-build-inputs/{name}", (
            f"{name!r} is copied to {dest!r}; it must land at /slt-build-inputs/{name} so the "
            f"in-image layout matches the checkout")
