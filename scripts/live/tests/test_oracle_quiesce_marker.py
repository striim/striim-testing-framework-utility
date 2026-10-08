"""The Oracle image must bake QUIESCEMARKER into BOTH containers, root copy first.

Oracle does not fail a QUIESCE when C##STRIIM.QUIESCEMARKER is missing -- it DISAPPROVES it
and disables the feature, so the application keeps running and the arm times out. Nothing
else in the live tier touches that table, so its absence is invisible until a quiesce arm
runs, and then every Oracle quiesce arm fails together.

Why this is a test and not a comment: bake.sql is ONE sqlplus session, so
``ALTER SESSION SET CONTAINER=FREEPDB1`` persists for everything below it. c##striim is a
COMMON user, so its schema exists separately per container and the reader -- which connects
to the CDB ROOT, service.yaml's ORACLE_CDC_URL uses cdb_service FREE -- cannot see a table
created in the PDB. The first attempt at this fix put the CREATE below that switch: both
gated-quiesce arms failed again with the identical "Table {C##STRIIM.QUIESCEMARKER} not
found", and the mistake is invisible in review because the statement itself is correct and
the file still bakes cleanly. Moving it back down would silently restore the bug, and the
only signal would be two live arms failing on a tier that takes an hour to run.
"""

import re
from pathlib import Path

import pytest

BAKE = (Path(__file__).resolve().parents[3]
        / "scripts/live/services/oracle/images/oracle/bake.sql")

CREATE = "CREATE TABLE c##striim.QUIESCEMARKER"

# Matched as a REGEX, not a literal, and case-insensitively. A literal anchors on one exact
# spelling, and review showed four ways past it that all left the bug live and every test
# green: lower case, spaces around the `=`, a quoted container name, and `/` instead of `;`
# as the terminator. The whitespace class also covers a newline inside the statement.
#
# The earlier literal form had a worse problem: without the trailing semicolon it matched the
# PROSE above the root copy, so the test failed on a correct file. Its own negative control
# caught that.
SWITCH_RE = re.compile(r"alter\s+session\s+set\s+container\s*=\s*[\"']?(\w+)[\"']?\s*(?:;|/)",
                       re.IGNORECASE)
DOCKERFILE = BAKE.parent / "Dockerfile"


@pytest.fixture(scope="module")
def bake() -> str:
    assert BAKE.is_file(), f"bake.sql not found at {BAKE}"
    return BAKE.read_text()


def test_marker_is_created_in_both_containers(bake: str) -> None:
    assert bake.count(CREATE) == 2, (
        "expected exactly two QUIESCEMARKER creates, one per container; found "
        f"{bake.count(CREATE)}"
    )


def _switches(bake: str):
    """Every container switch in the file, as (position, container-name)."""
    return [(m.start(), m.group(1).upper()) for m in SWITCH_RE.finditer(bake)]


def test_there_is_exactly_one_container_switch_and_it_names_the_pdb(bake: str) -> None:
    """The ordering tests below are only meaningful if there is ONE switch to order against.

    A second switch anywhere above the root copy restores the bug while leaving a
    first-occurrence check green, which is how review got past the earlier version of this
    test four different ways.
    """
    switches = _switches(bake)
    assert len(switches) == 1, (
        f"expected exactly one ALTER SESSION SET CONTAINER; found {len(switches)}: {switches}. "
        "More than one means the container a statement runs in can no longer be read off its "
        "position relative to a single line, and these tests no longer establish it."
    )
    assert switches[0][1] == "FREEPDB1", f"the switch names {switches[0][1]}, expected FREEPDB1"


def test_the_first_create_is_above_the_pdb_switch(bake: str) -> None:
    """The root copy. Below the switch it lands in the PDB and the reader cannot see it."""
    first = bake.index(CREATE)
    switch = _switches(bake)[0][0]
    assert first < switch, (
        f"the CDB$ROOT copy of QUIESCEMARKER must be created BEFORE the container switch "
        f"(create at {first}, switch at {switch}). Below that line the session is in the PDB, "
        "c##striim's PDB schema is not the one the reader reads, and the quiesce arms fail with "
        "the table reported missing."
    )


def test_the_second_create_is_below_the_pdb_switch(bake: str) -> None:
    """The PDB copy. The fixtures require the table in both containers."""
    switch = _switches(bake)[0][0]
    second = bake.index(CREATE, bake.index(CREATE) + 1)
    assert second > switch, (
        f"the FREEPDB1 copy must be created AFTER the container switch (second create at "
        f"{second}, switch at {switch})"
    )


def test_the_qasource_grant_is_in_the_pdb(bake: str) -> None:
    """qasource is a PDB-local user, so the grant only parses there.

    In CDB$ROOT it fails, and bake.sql runs under WHENEVER SQLERROR EXIT, so a misplaced
    grant does not merely warn -- it fails the image build.
    """
    grant = "ON c##striim.QUIESCEMARKER TO qasource"
    assert grant in bake, "the qasource grant is missing"
    assert bake.index(grant) > _switches(bake)[0][0], (
        "the qasource grant must be inside the FREEPDB1 section; qasource does not exist "
        "in CDB$ROOT and the failure would break the image build, not just the tier"
    )


def test_the_bake_runs_as_sysdba_against_the_root() -> None:
    """The other half of the invariant, and unasserted until review pointed it out.

    Everything above reasons about position relative to the one switch, which establishes the
    container only if the session STARTS in CDB$ROOT. Pointing the Dockerfile's sqlplus at a
    PDB service would put both copies in the PDB and leave every test above green.
    """
    text = DOCKERFILE.read_text()
    assert "sqlplus" in text, f"no sqlplus invocation in {DOCKERFILE}"
    line = next(l for l in text.splitlines() if "sqlplus" in l and "bake.sql" in l)
    assert "/ as sysdba" in line, (
        "bake.sql must be run as a bequeath / as sysdba connection, which lands in CDB$ROOT; "
        f"found: {line.strip()}"
    )
    assert "@" not in line.split("as sysdba")[0], (
        "the sqlplus connect string must name no service -- a service would resolve to a PDB "
        f"and silently move the root copy into it; found: {line.strip()}"
    )
