"""Test.yaml parsing/normalization (docs/INTEGRATION-TESTS.md), adapted from scripts/live/livetest/manifest.py.

Pure Python: load a test.yaml, validate every top-level key SPEC §3 defines, and return
a typed, normalized in-memory `TestManifest`. No Docker, no Striim, no jar building, no
`${...}` token substitution (that is a later slice — tokens.py) and no pytest wiring
(plugin.py stays on its own inline parser until a later slice calls into this module).

File-reference policy (ddl/seed/assert.data input+match): each is stored BOTH as the
literal test.yaml string (`file`/`input`/`match` — what token substitution will act on
later) and as an absolute path resolved against the test directory (`path`/`input_path`/
`match_path` — convenient for anything that wants to read the file right now). Existence
on disk is deliberately NOT checked here — this slice's job is parsing/normalization, and
a missing file is a runtime concern for the slice that actually opens it.
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from zoneinfo import available_timezones

from . import dbroutes, waevent


def _only_variant() -> str:
    """The single variant to run, from INT_VARIANT, or "" for all of them.

    ⚠ NARROWING, not selection: it is for iterating on ONE engine without bringing up six
    databases, which is the difference between a two-minute loop and a twenty-minute one. It is
    OFF unless set, and a name no case defines yields NO runs rather than all of them -- a filter
    that silently fell back to everything would read as "the filter worked" while running the
    whole matrix.

    The live tier does this with pytest markers (`-m "live and teradata"`); this tier composes
    variants INSIDE one test, so a marker cannot reach them.
    """
    import os
    return os.environ.get("INT_VARIANT", "").strip()

DEFAULT_DB_ROUTE = "postgres-source"

#: `assert.target:` reads the database a TARGET wrote, so it defaults to the target route --
#: unlike `ddl:`/`seed:`, which seed the SOURCE side and default to `postgres-source`.
DEFAULT_TARGET_DB_ROUTE = "postgres-target"
DEFAULT_TIMEOUT = 120

#: `seed:` runs before the reader is driven (the default) or after its source has started.
SEED_WHEN_VALUES = ("pre_start", "post_start")
DEFAULT_SEED_WHEN = "pre_start"

UDF_KINDS = {"waevent", "jsonnode"}
DEFAULT_UDF_KIND = "waevent"
# data[N]/before[N]/userdata.KEY -- the jsonnode envelope slot grammar, kept
# byte-equivalent with UdfCore's own Java-side SLOT_RE: digits bounded to 9 (so a
# match can never overflow a Java int), `\Z` (not `$`, which Python -- like Java's
# default, non-MULTILINE `$` -- tolerates before a single trailing newline) so a
# quoted-string author can't sneak one past collection only to fail inside the JVM,
# and `re.ASCII` so `\S` matches the same (non-Unicode) byte class Java's does.
_SLOT_RE = re.compile(r"^(?:(?:data|before)\[\d{1,9}\]|userdata\.\S+)\Z", re.ASCII)
# A UDF class name must be fully qualified (contain a package) -- a bare "MyUdf"
# is an author error (there is no default package to resolve it against).
_UDF_CLASS_RE = re.compile(r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+$")
_UDF_METHOD_RE = re.compile(r"^[A-Za-z_$][\w$]*$")
_UDF_BIND_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ManifestError(Exception):
    """Raised when a test.yaml fails to parse or normalize per docs/INTEGRATION-TESTS.md"""


@dataclass(frozen=True)
class OpRef:
    """Normalized `op:` block. Single-module only (SPEC §3): `op: {jar: <module ref>}`.
    `jar` is a repo-relative MODULE reference (a directory or a pom.xml), never a
    versioned jar path — resolving/building it is opartifacts.py's job, not this one's."""
    jar: str


# key -> the OTHER family's directory name, i.e. what must NOT appear in this key's jar.
_WRONG_MODULE_FAMILY = {"op": "UserDefinedFunctions", "udf": "OpenProcessors"}


def _normalize_module_ref(raw, key: str, path) -> str:
    """The single implementation of the `op.jar`/`udf.jar` rule: a non-empty
    repo-relative MODULE reference (a directory or a pom.xml). A
    cross-family guard (NOT a positive membership requirement -- plenty of test
    fixtures use placeholder refs like `some/module` that name neither real family)
    rejects an `op:` ref under `java/UserDefinedFunctions/` and a `udf:` ref under
    `java/OpenProcessors/`. This is what makes the no-compatibility-shim migration
    self-verifying -- a UDF fixture that kept the old `op:` block name for its own
    artifact fails loudly here rather than loading cleanly and misbehaving downstream.
    NOTE: `scripts/live/`'s equivalent guard is POSITIVE (a `java/OpenProcessors/`
    prefix is required, not just a `java/UserDefinedFunctions/` absence) -- deliberate,
    not a drift to reconcile. This tier's placeholder-ref fixtures forced the weaker
    negative check; don't tighten one to match the other without re-checking that."""
    if not isinstance(raw, str) or not raw.strip():
        raise ManifestError(
            f"{path}: '{key}.jar' is required and must be a non-empty repo-relative "
            f"MODULE reference (its dir, or pom.xml), got {raw!r}")
    wrong_family = _WRONG_MODULE_FAMILY[key]
    if f"/{wrong_family}/" in raw:
        raise ManifestError(
            f"{path}: '{key}.jar' ({raw!r}) looks like a {wrong_family} module reference "
            f"but is declared under '{key}:' -- move it to the other block")
    return raw


@dataclass(frozen=True)
class FileSpec:
    """One `ddl:`/`seed:` entry, normalized. `file` is the literal test.yaml string
    (test-dir-relative); `path` is that string resolved to an absolute path against the
    test directory. `db` defaults to "postgres-source" per SPEC §3."""
    file: str
    db: str
    path: Path
    #: True when the entry NAMED a route; False when `db` is the default.
    #:
    #: ⚠ §84. A variant forces its own route onto every DDL file it names, so that an author
    #: cannot create a PostgreSQL table through the Oracle connection. That rule silently
    #: DISCARDED an explicit `db:` -- the §98.1 shape again, a route resolved at parse time and
    #: then overwritten. A case whose fixture spans two schemas of ONE engine (see
    #: jdbcsink-refuses-an-ambiguous-table-name) needs both routes, so an explicit `db:`
    #: now wins, restricted to a route of the SAME SERVICE as the variant's.
    db_explicit: bool = False


@dataclass(frozen=True)
class DataAssertion:
    """One `assert.data:` entry: an input WAEvent-JSON file and the expected-output
    WAEvent-JSON file it must match. Both the literal (test-dir-relative) string and the
    resolved absolute path are exposed, same policy as FileSpec.

    `input`/`input_path` are None for a `source:` (reader) case and only for one: a reader
    PULLS from its source, so there is no input fixture to name and `load_manifest` rejects one.

    `ignore_fields`/`project` narrow the comparison for an operator that stamps something
    non-deterministic (a wall clock, a generated id) into its output: `ignore_fields` drops
    those fields, `project` compares only the listed ones. Mutually exclusive. Each entry is
    a path in `waevent.parse_field_path`'s grammar, validated HERE at load so a typo fails
    before any container starts -- and validated again for reachability at compare time, so
    a path that has silently stopped matching fails instead of quietly weakening the case.

    `sort_by` orders BOTH sides by the named paths before comparing, for an operator whose
    output is a SET rather than a sequence. It is orthogonal to the other two: those say which
    FIELDS are compared, this says how the EVENTS are lined up.

    It exists because a change-data reader's initial-load snapshot reads with `SELECT *` and no
    `ORDER BY`, so its row order is unspecified -- and `compare` is positional, which made a
    multi-row snapshot unassertable. The alternative considered and rejected was a bare
    `unordered: true` multiset compare: it needs no key, but it cannot say WHICH event differs
    (only that the bags differ), and it lets a case stay green while the operator's output
    order becomes genuinely arbitrary. Naming the key is the honest form -- the case states what
    makes its output a set, and the diff stays positional and readable."""
    input: str | None
    match: str
    input_path: Path | None
    match_path: Path
    ignore_fields: tuple = ()
    project: tuple = ()
    sort_by: tuple = ()


@dataclass(frozen=True)
class JmxSpec:
    """Normalized `assert.jmx:` block: the op's MBean attributes after its events are processed.

    `attributes` maps an attribute name to an exact value (int/float/bool/str) or a
    `{"min": n, "max": n}` range (either bound optional). `bean` optionally names the MBean
    class; absent, the runner discovers it (JmxSnapshot.java)."""
    attributes: dict
    bean: str | None = None


@dataclass(frozen=True)
class AssertSpec:
    """Normalized `assert:` block. `data` is the list of DataAssertion pairs (empty if
    the block only declares `smoke: true`); `smoke` defaults to False. `gcs_objects` is
    an optional list of object KEYS (bucket-relative, never `gs://` URIs) that must
    exist in `${GCS_BUCKET}` after the operator runs -- a side-effect assertion that
    supplements `data`, for a writer op whose emitted WAEvent only reports where it
    claims to have written. GCS-specific by design: `load_manifest` rejects it unless
    `requires:` names `gcs`, and another object store would add a sibling key rather
    than overload this one."""
    data: list = field(default_factory=list)
    smoke: bool = False
    gcs_objects: list = field(default_factory=list)   # list[str]
    target: list = field(default_factory=list)        # list[TargetAssertion]
    expect_error: str | None = None                   # substring the drive's failure must contain
    expect_log: list = field(default_factory=list)    # list[str]; substrings the writer must LOG
    monitor: dict = field(default_factory=dict)       # MON metric name -> expected value
    acked: int | None = None                          # expected acknowledged event count
    restarts: int | None = None                       # expected restart count
    replayed: int | None = None                       # expected replayed-event count
    # What the writer handed to the exception store: a list of lists of input ordinals,
    # one inner list per notification, in order. `[]` asserts NOTHING reached the store.
    exception_store: list | None = None
    jmx: JmxSpec | None = None                        # MBean attributes after the drive


@dataclass(frozen=True)
class UdfStep:
    """One `udf.pipeline:` step: a static-method call plus where its result goes.
    `args` is ALREADY in wire form (a list of single-key dicts, e.g. `{"reg": True}`/
    `{"ref": name}`/`{"json": value}`/`{"val": value}`) -- normalization happens once,
    at load, so nothing converts twice."""
    function: str
    args: list = field(default_factory=list)
    as_: str | None = None


@dataclass(frozen=True)
class UdfSpec:
    """Normalized `udf:` block. Fully self-sufficient: `jar` names the module to
    build/load, exactly like `op.jar` does for an OP -- there is no
    `op:` block at all for a UDF case. Drives a bare UDF's `public static` functions
    directly via `UdfCore`, with no `Processor`/constructor/manifest involved. `kind`
    governs the `$` register's runtime type; `source`/`target` (jsonnode only) name the
    WAEvent-JSON envelope slot the register is read from / written back to."""
    jar: str
    class_name: str
    kind: str = DEFAULT_UDF_KIND
    pipeline: list = field(default_factory=list)  # list[UdfStep], non-empty
    source: str | None = None
    target: str | None = None


@dataclass(frozen=True)
class SourceSpec:
    """Normalized `source:` block: drive the op as a READER -- ticked rather than fed.

    `max_ticks` is the tick BUDGET, and there is deliberately no wall-clock sibling: the
    caller decides how many ticks happen, so a slow machine takes longer to run the case but
    cannot change its outcome. `expect_events`, when set, stops ticking as soon as that many
    events have been collected and turns an exhausted budget into a loud shortfall; when
    unset, exactly `max_ticks` ticks are driven, which is what a case proving a source stays
    QUIET needs.

    What the source emits is not declared here -- it reaches the core through the op's own
    `IntegrationSeams` class or through a `requires:` service, because a scripted source is
    code, not YAML."""
    max_ticks: int
    expect_events: int | None = None

    #: When `seed:` runs. `pre_start` (default) commits it before the reader is driven at all --
    #: right for a snapshot, which reads what is already there. `post_start` commits it AFTER the
    #: source has taken its first tick, which is the only shape a CHANGE STREAM can be tested in:
    #: a stream captures commits made after its start timestamp, so data seeded beforehand is
    #: invisible to it by design.
    #:
    #: ⚠ The LIVE tier's `seed_when:` no longer exists -- it was reworked into a per-file `when:`
    #: with an `after:` delay, and the top-level key is now REJECTED there. The two tiers solve the
    #: same problem differently and are not interchangeable: this one is a case-level flag with a
    #: blocking handshake, because a subprocess driver has no "app is RUNNING" moment to hang a
    #: delay on. See docs/INTEGRATION-TESTS.md.
    seed_when: str = DEFAULT_SEED_WHEN


@dataclass(frozen=True)
class TargetSpec:
    """Normalized `target:` block: drive the module as a Striim TARGET -- a `RetriableWriter`
    that emits nothing and writes to a real database.

    A target's observable output is the target database, the checkpoint row, and what it
    acknowledged, so this block says only how to DRIVE it; `assert.target:`/`assert.acked:`
    say what to check.

    `input` is here rather than on `assert.data[]` because a target has no emitted events to
    match against -- `assert.data` is rejected outright for a target case, so there is no
    entry to hang an input on.

    `restart_after` is the tier's reason for existing. `close()` does not flush, so the events
    accumulated at that point were never applied and never acked; the driver then resumes from
    whatever the writer reports as durable and feeds the rest again. A writer that double-writes
    on replay shows it in the target table -- the one place a mocked driver could never reveal it.

    A LIST restarts more than once. `restart_after: [2, 4]` stops after the 2nd event and again
    after the 4th, which is not the same test as restarting once: the second restart resumes from
    a checkpoint the FIRST recovery wrote, so it is the only shape that exercises a position the
    writer itself persisted after a replay rather than during a clean first run. A bare int is
    normalised to a one-element tuple, so every consumer sees the same shape.
    """
    input: str
    input_path: Path
    restart_after: tuple[int, ...] | None = None   # ordinals to restart AFTER, ascending
    mid_run: tuple[MidRunStep, ...] = ()          # SQL to run BETWEEN events
    positions: bool = True
    distribution_id: str | None = None
    timezone: str | None = None


@dataclass(frozen=True)
class MidRunStep:
    """One `target.mid_run:` entry -- SQL run against a live target, BETWEEN two events.

    The tier had no way to change anything while the writer was running. `seed_when: post_start`
    is a `source:` feature and fires once, before the reader's first tick; a `target:` case feeds
    events itself and needs to act at a chosen point in that stream instead.

    Two diagnostics could not be reached end to end without it, and both are implemented and
    unit-tested with no integration case:

      - `verifyUnchanged` -- the target ALTERed underneath a running writer. Metadata is resolved
        once at start-up, so every cached statement is wrong afterwards, and the writer's job is to
        say so rather than let the driver report a column-not-found buried in a batch failure.
      - a restart after a FAILURE rather than a clean stop, where the checkpoint is behind and the
        replay is therefore larger than the one clean-restart cases produce.

    `after` is an event ORDINAL, 1-based, matching `restart_after:`. The driver feeds that many
    events, then blocks until this side reports the SQL committed. Steps sharing an `after` run at
    that one gate, in list order, each on its own route.
    """
    after: int
    file: str
    db: str
    path: Path
    #: True when the case NAMED a route; False when `db` is this field's default.
    #:
    #: ⚠ §84. A variant runs the same case against another engine, and its `db:` redirects the
    #: ddl and the assertions -- but `mid_run` resolved its route at PARSE time, so it kept
    #: running against PostgreSQL while the writer wrote to Oracle. Nothing failed loudly: the SQL
    #: succeeded, against the wrong database, and the case then failed on an assertion that looked
    #: like a writer bug. An explicit `db:` still wins, so a case that deliberately touches a
    #: second database keeps working.
    db_explicit: bool = False


@dataclass(frozen=True)
class Variant:
    """One named entry of a `variants:` block — the CONNECTION axis.

    A variant supplies token VALUES, its own `ddl:`, and the route its assertions read through.
    It supplies nothing else, and that limit is the construct: the `properties:` block, the query
    shape and the expected fixture are authored ONCE and are identical for every variant, so the
    case states "every engine agrees" rather than enumerating what each engine happens to do.

    ⚠ IT MAY NOT CARRY `COLUMNMAP` OR `KEYCOLUMNS`. Those change the MAPPING, and a different
    mapping writes different rows -- which needs its own golden and therefore its own case. They
    are §6.1's STRUCTURAL axis, not this one. A variant that could introduce one would silently
    break the single-shared-expectation property the whole construct rests on.
    """
    name: str
    db: str
    ddl: list = field(default_factory=list)     # list[FileSpec]
    tokens: dict = field(default_factory=dict)  # token name -> value, rendered per variant


@dataclass(frozen=True)
class TargetAssertion:
    """One `assert.target:` entry: a SQL query against a `db:` route, and the rows it must
    return.

    `match` names a JSON file holding a list of rows, each row a list of column values -- the
    same shape `dbroutes.query_rows` returns. Rows are compared IN ORDER, so the query must
    carry its own `ORDER BY`; an unordered query would make the case pass or fail on the
    database's scan order, which is exactly the flake the reader tier's `sort_by:` exists to
    avoid and which a query can simply state instead.
    """
    query: str
    db: str
    match: str
    match_path: Path
    #: True when the entry NAMED a route; False when `db` is the default.
    #:
    #: ⚠ §100.4. A variant case reads every assertion through the VARIANT's route, so an
    #: explicit `db:` here was accepted and then silently overridden. `load_manifest` refuses the
    #: combination outright, and this is what lets it tell a named route from the default.
    db_explicit: bool = False


@dataclass(frozen=True)
class TestManifest:
    """A fully parsed and normalized test.yaml (docs/INTEGRATION-TESTS.md)."""
    name: str
    op: OpRef | None
    properties: dict
    assert_: AssertSpec
    dir: Path                                   # the directory containing test.yaml
    purpose: str | None = None
    requires: list = field(default_factory=list)
    ddl: list = field(default_factory=list)     # list[FileSpec]
    seed: list = field(default_factory=list)    # list[FileSpec]
    timeout: int = DEFAULT_TIMEOUT
    disabled: object = None                     # str reason, bool, or None; truthy => skip
    types: dict = field(default_factory=dict)   # dict[str, list[str]]; source schemas by TableName
    password_properties: list = field(default_factory=list)  # list[str]; `properties:` keys to wrap in Password
    udf: object = None                          # UdfSpec | None
    source: object = None                       # SourceSpec | None; set => drive `op` as a reader
    target: object = None                       # TargetSpec | None; set => drive `op` as a target
    matrix: dict = field(default_factory=dict)  # property name -> the values to cross-product
    variants: list = field(default_factory=list) # list[Variant]; the CONNECTION axis

    def runs(self) -> list:
        """Every (variant, permutation) this case executes -- the two axes composed.

        A case with neither yields one run with no variant and no overrides: the existing
        behaviour, expressed rather than special-cased. Four variants and eight permutations
        yield thirty-two runs of ONE authored case, which is the arithmetic §6.1 exists to
        defend.
        """
        variants = self.variants or [None]
        only = _only_variant()
        if only and self.variants:
            variants = [v for v in variants if getattr(v, "name", None) == only]
            if not variants:
                # Named a variant this case does not have: yield nothing rather than silently
                # running every variant, which would look like the filter worked.
                return []
        return [(v, overrides) for v in variants for overrides in self.permutations()]

    def permutations(self) -> list:
        """Every combination of `matrix:` values, as property-override dicts.

        Absent `matrix:` yields ONE empty overlay, so a case without the block runs exactly once
        with exactly its own `properties:` -- the existing behaviour, expressed rather than
        special-cased at the call site.

        Order is the declaration order of the keys and of each key's values, so a failure names a
        permutation an author can find by reading their own file top to bottom.
        """
        if not self.matrix:
            return [{}]
        names = list(self.matrix)
        out = []
        for values in itertools.product(*(self.matrix[n] for n in names)):
            out.append(dict(zip(names, values)))
        return out

    @property
    def module_ref(self) -> str:
        """The one buildable module reference for this test, regardless of whether it
        came from `op:` or `udf:` -- `load_manifest` already
        guarantees exactly one of `self.op`/`self.udf` is set."""
        return self.op.jar if self.op is not None else self.udf.jar


def _normalize_op(raw, path) -> OpRef | None:
    """`op:` is now OPTIONAL at this function's level -- `load_manifest`'s
    exactly-one-of-`op`/`udf` guard is what makes it required overall. Absent
    (`None`) is the entire UDF path."""
    if raw is None:
        return None
    if isinstance(raw, list):
        raise ManifestError(
            f"{path}: 'op' must be a single mapping, not a list -- the integration "
            f"harness drives exactly one module per subprocess, so 'op:'/'udf:' take a "
            f"single mapping here (the list form is scripts/live/-only)")
    if not isinstance(raw, dict) or not raw:
        raise ManifestError(f"{path}: 'op' must be a mapping with a 'jar' key")
    unknown = sorted(set(raw.keys()) - {"jar"})
    if unknown:
        raise ManifestError(f"{path}: 'op' has unknown key(s) {unknown} (allowed: ['jar'])")
    return OpRef(jar=_normalize_module_ref(raw.get("jar"), "op", path))


def _require_exactly_one_of_op_udf(op, udf, path) -> None:
    """Shared by `manifest.load_manifest` and `perfmanifest.load_perf_manifest` so the
    two loaders' error messages cannot drift."""
    if op is None and udf is None:
        raise ManifestError(
            f"{path}: 'name' plus exactly one of 'op'/'udf' is required, but neither is present")
    if op is not None and udf is not None:
        raise ManifestError(
            f"{path}: exactly one of 'op'/'udf' is required, but both are present")


def _normalize_properties(raw, path) -> dict:
    """`properties:` is OPTIONAL -- an operator that takes no configuration at
    all (no properties of its own) has nothing to put here. Absent (`None`) and
    an explicit empty mapping (`properties: {}`) both normalize to `{}`,
    matching how every other optional mapping/list key in this loader treats
    "nothing here" (e.g. `requires: []`/absent both normalize to `[]`)."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ManifestError(f"{path}: 'properties' must be a mapping, got {type(raw).__name__}")
    out: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key:
            raise ManifestError(f"{path}: 'properties' keys must be non-empty strings, got {key!r}")
        if isinstance(value, (dict, list)):
            raise ManifestError(
                f"{path}: 'properties.{key}' must be a scalar (string/bool/number), "
                f"got {type(value).__name__}")
        if value is None:
            raise ManifestError(f"{path}: 'properties.{key}' has no value")
        if isinstance(value, bool):
            # YAML lets an author write `EnableRegex: true` unquoted; the operator receives
            # every property as a string, so normalize to the Java-boolean-parse spelling.
            out[key] = "true" if value else "false"
        else:
            # ${...} tokens are left untouched here -- substitution is a later slice.
            out[key] = str(value)
    return out


def _normalize_requires(raw, path) -> list:
    if raw is None:
        return []
    if isinstance(raw, str):
        if not raw:
            raise ManifestError(f"{path}: 'requires' string entry must be non-empty")
        return [raw]
    if isinstance(raw, list):
        out = []
        for item in raw:
            if not isinstance(item, str) or not item:
                raise ManifestError(
                    f"{path}: 'requires' list items must be non-empty service-name "
                    f"strings, got {item!r}")
            out.append(item)
        return out
    raise ManifestError(
        f"{path}: 'requires' must be a service name string or a list of them, got {raw!r}")


def _normalize_file_specs(raw, field_name: str, test_dir: Path, path) -> list:
    """Normalize a `ddl:`/`seed:` block per SPEC §3: a bare string, a bare list (of
    strings and/or {file, db} mappings), each routing to `db` (default
    "postgres-source")."""
    if raw is None:
        return []
    if isinstance(raw, str):
        if not raw:
            raise ManifestError(f"{path}: '{field_name}' string value must be non-empty")
        return [FileSpec(file=raw, db=DEFAULT_DB_ROUTE, path=(test_dir / raw).resolve())]
    if isinstance(raw, list):
        if not raw:
            raise ManifestError(f"{path}: '{field_name}' list must not be empty")
        out = []
        for i, item in enumerate(raw):
            if isinstance(item, str):
                if not item:
                    raise ManifestError(
                        f"{path}: '{field_name}[{i}]' string entry must be non-empty")
                out.append(FileSpec(file=item, db=DEFAULT_DB_ROUTE,
                                     path=(test_dir / item).resolve()))
                continue
            if isinstance(item, dict):
                file_ = item.get("file")
                if not isinstance(file_, str) or not file_:
                    raise ManifestError(
                        f"{path}: '{field_name}[{i}]' needs a non-empty 'file' (plus an "
                        f"optional 'db' route): {item!r}")
                db = item.get("db", DEFAULT_DB_ROUTE)
                if not isinstance(db, str) or not db:
                    raise ManifestError(
                        f"{path}: '{field_name}[{i}].db' must be a non-empty string "
                        f"route, got {db!r}")
                out.append(FileSpec(file=file_, db=db, path=(test_dir / file_).resolve(),
                                     db_explicit="db" in item))
                continue
            raise ManifestError(
                f"{path}: '{field_name}[{i}]' must be a filename string or a "
                f"{{file, db}} mapping, got {item!r}")
        return out
    raise ManifestError(
        f"{path}: '{field_name}' must be a string or a list of {{file, db}}, got {raw!r}")


_DATA_ASSERTION_KEYS = {"input", "match", "ignore_fields", "project", "sort_by"}

_SOURCE_KEYS = {"max_ticks", "expect_events", "seed_when"}

_TARGET_KEYS = {"input", "restart_after", "positions", "distribution_id", "timezone",
                "mid_run"}

_TARGET_ASSERTION_KEYS = {"query", "db", "match"}


def _normalize_field_paths(item: dict, key: str, idx: int, path) -> tuple:
    """Validate `assert.data[idx].<key>` as a list of comparison field paths.

    Grammar is checked at load rather than at compare time so a malformed path fails before
    any service container starts -- the message names the legal forms rather than leaving an
    author to guess from a regex.
    """
    raw = item.get(key)
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [raw]                     # scalar courtesy, same as ddl:/requires:
    if not isinstance(raw, list) or not raw:
        raise ManifestError(
            f"{path}: 'assert.data[{idx}].{key}' must be a non-empty field-path string or "
            f"list of them")
    out = []
    for entry in raw:
        try:
            waevent.parse_field_path(entry)
        except ValueError as e:
            raise ManifestError(f"{path}: 'assert.data[{idx}].{key}': {e}") from None
        out.append(entry)
    return tuple(out)


def _normalize_assert(raw, test_dir: Path, path, *, is_source: bool = False,
                      is_target: bool = False) -> AssertSpec:
    if not isinstance(raw, dict) or not raw:
        raise ManifestError(f"{path}: 'assert' is required and must be a non-empty mapping")

    smoke = raw.get("smoke", False)
    if not isinstance(smoke, bool):
        raise ManifestError(f"{path}: 'assert.smoke' must be true/false, got {smoke!r}")

    data_raw = raw.get("data")
    data: list[DataAssertion] = []
    if data_raw is not None and is_target:
        # Rejected rather than ignored: a target emits NOTHING, so a WAEvent comparison here
        # would assert against an empty list and pass or fail for reasons unconnected to the
        # writer. What a target case checks is `assert.target:` (the database it wrote) and
        # `assert.acked:` (what it acknowledged).
        raise ManifestError(
            f"{path}: 'assert.data' has no meaning for a 'target:' case -- a target emits no "
            f"events. Assert the database it wrote with 'assert.target:', and what it "
            f"acknowledged with 'assert.acked:'. The input events go on 'target.input:'.")
    if data_raw is not None:
        if not isinstance(data_raw, list) or not data_raw:
            raise ManifestError(
                f"{path}: 'assert.data' must be a non-empty list of {{input, match}} entries")
        for i, item in enumerate(data_raw):
            if not isinstance(item, dict):
                raise ManifestError(f"{path}: 'assert.data[{i}]' must be a mapping, got {item!r}")
            # Unknown-key rejection, same policy as the `op:`/`udf:` entries above. It matters
            # more here than it looks: a singular `ignore_field:` typo would otherwise be
            # dropped in silence, leaving the comparison at exact-match and the author
            # believing a field was excluded -- the same silent-weakening this slice's
            # reachability check exists to prevent.
            unknown = set(item) - _DATA_ASSERTION_KEYS
            if unknown:
                raise ManifestError(
                    f"{path}: 'assert.data[{i}]' has unknown key(s) {sorted(unknown)} -- "
                    f"allowed: {sorted(_DATA_ASSERTION_KEYS)}")
            inp = item.get("input")
            match = item.get("match")
            if is_source:
                # Rejected rather than ignored: a reader PULLS from its source, so an input
                # fixture here would never be read, and an author who wrote one would be
                # asserting against events they believe they supplied.
                if inp is not None:
                    raise ManifestError(
                        f"{path}: 'assert.data[{i}].input' has no meaning for a 'source:' case "
                        f"-- a reader pulls from its source rather than being fed, so the "
                        f"events come from 'source:' ticks; remove it")
            elif not isinstance(inp, str) or not inp:
                raise ManifestError(
                    f"{path}: 'assert.data[{i}]' needs a non-empty 'input' WAEvent-JSON "
                    f"file path")
            if not isinstance(match, str) or not match:
                raise ManifestError(
                    f"{path}: 'assert.data[{i}]' needs a non-empty 'match' WAEvent-JSON "
                    f"file path")
            ignore_fields = _normalize_field_paths(item, "ignore_fields", i, path)
            project = _normalize_field_paths(item, "project", i, path)
            sort_by = _normalize_field_paths(item, "sort_by", i, path)
            # Sorting on a field the case has just declared volatile is a contradiction, and a
            # silent one: the sort would be stable in the run that authored the golden and
            # arbitrary afterwards.
            overlap = sorted(set(sort_by) & set(ignore_fields))
            if overlap:
                raise ManifestError(
                    f"{path}: 'assert.data[{i}]' sorts by {overlap} but also declares "
                    f"them in 'ignore_fields' -- a field cannot be both the stable key that "
                    f"orders events and too volatile to compare")
            if sort_by and project:
                missing = sorted(set(sort_by) - set(project))
                if missing:
                    raise ManifestError(
                        f"{path}: 'assert.data[{i}]' sorts by {missing}, which 'project' "
                        f"excludes from the comparison -- project onto the sort key too, or "
                        f"sort by a projected field")
            if ignore_fields and project:
                raise ManifestError(
                    f"{path}: 'assert.data[{i}]' declares both 'ignore_fields' and 'project' "
                    f"-- they are inverses, so declaring both is ambiguous. Pick the one that "
                    f"says what the case means: 'project' when only a few fields are "
                    f"deterministic, 'ignore_fields' when only a few are not.")
            data.append(DataAssertion(
                input=inp, match=match,
                input_path=None if inp is None else (test_dir / inp).resolve(),
                match_path=(test_dir / match).resolve(),
                ignore_fields=ignore_fields, project=project, sort_by=sort_by,
            ))

    objects_raw = raw.get("gcs_objects")
    gcs_objects: list[str] = []
    if objects_raw is not None:
        if isinstance(objects_raw, str):
            objects_raw = [objects_raw]     # scalar form, same courtesy as ddl:/requires:
        if not isinstance(objects_raw, list) or not objects_raw:
            raise ManifestError(
                f"{path}: 'assert.gcs_objects' must be a non-empty object-key string or "
                f"list of them")
        for i, item in enumerate(objects_raw):
            if not isinstance(item, str) or not item:
                raise ManifestError(
                    f"{path}: 'assert.gcs_objects[{i}]' must be a non-empty object-key "
                    f"string, got {item!r}")
            if item.startswith("gs://"):
                raise ManifestError(
                    f"{path}: 'assert.gcs_objects[{i}]' must be a bucket-relative object "
                    f"key, not a gs:// URI -- the bucket comes from ${{GCS_BUCKET}}: {item!r}")
            gcs_objects.append(item)

    target_raw = raw.get("target")
    target: list[TargetAssertion] = []
    if target_raw is not None:
        if not is_target:
            raise ManifestError(
                f"{path}: 'assert.target' needs a 'target:' block -- it reads the database a "
                f"TARGET wrote, and nothing else in this tier writes one")
        if not isinstance(target_raw, list) or not target_raw:
            raise ManifestError(
                f"{path}: 'assert.target' must be a non-empty list of {{query, db, match}} "
                f"entries")
        for i, item in enumerate(target_raw):
            if not isinstance(item, dict):
                raise ManifestError(
                    f"{path}: 'assert.target[{i}]' must be a mapping, got {item!r}")
            unknown = sorted(set(item) - _TARGET_ASSERTION_KEYS)
            if unknown:
                raise ManifestError(
                    f"{path}: 'assert.target[{i}]' has unknown key(s) {unknown} -- allowed: "
                    f"{sorted(_TARGET_ASSERTION_KEYS)}")
            query = item.get("query")
            if not isinstance(query, str) or not query.strip():
                raise ManifestError(
                    f"{path}: 'assert.target[{i}]' needs a non-empty 'query'")
            match = item.get("match")
            if not isinstance(match, str) or not match:
                raise ManifestError(
                    f"{path}: 'assert.target[{i}]' needs a non-empty 'match' file path (a "
                    f"JSON list of rows, each row a list of column values)")
            db = item.get("db", DEFAULT_TARGET_DB_ROUTE)
            if not isinstance(db, str) or not db:
                raise ManifestError(
                    f"{path}: 'assert.target[{i}].db' must be a non-empty route name, got "
                    f"{db!r}")
            # Validated at load, not after a container has started -- and re-raised as a
            # ManifestError so a bad manifest is one exception type to the caller, whatever
            # part of it is wrong. (`ddl:`/`seed:` do not do this yet; they fail later, at
            # the point of use.)
            try:
                dbroutes.parse_route(db)
            except dbroutes.RouteError as e:
                raise ManifestError(f"{path}: 'assert.target[{i}].db': {e}") from None
            # Rows are compared in order, so the query has to impose one. Checked here rather
            # than left to a reviewer: an unordered query is green on the machine that wrote
            # the golden and arbitrary everywhere else, which is the worst kind of assertion.
            if "order by" not in query.lower():
                raise ManifestError(
                    f"{path}: 'assert.target[{i}].query' has no ORDER BY. Rows are compared in "
                    f"order, and a database is free to return them in any order it likes -- "
                    f"without one the case passes on the machine that recorded the golden and "
                    f"fails elsewhere. Add an ORDER BY over the key.")
            target.append(TargetAssertion(
                query=query, db=db, match=match,
                match_path=(test_dir / match).resolve(),
                db_explicit="db" in item))

    expect_error = raw.get("expect_error")
    if expect_error is not None:
        if not isinstance(expect_error, str) or not expect_error.strip():
            raise ManifestError(
                f"{path}: 'assert.expect_error' must be a non-empty substring of the failure the "
                f"drive is expected to produce, got {expect_error!r}")
        # A bare "Exception"/"Error" would pass against ANY failure, including the harness failing
        # to start or the database being unreachable -- a case that cannot tell the refusal it is
        # testing from an environment problem is worse than no case.
        if expect_error.strip().lower() in {"error", "exception", "failed", "failure"}:
            raise ManifestError(
                f"{path}: 'assert.expect_error' is too broad: {expect_error!r} matches almost any "
                f"failure, including the harness failing to start. Quote enough of the message to "
                f"name the refusal this case is about.")

    # ⚠ WHAT THE OPERATOR SAID, on a run that SUCCEEDS. `expect_error` reads the failure text and
    # `target:` reads the database; neither can see a WARNING on an otherwise-clean run, which is
    # the only observable behaviour three capabilities have -- VendorConfiguration (§142.3),
    # useBulkCopyForBatchInsert on a non-SQL-Server engine (§133), and the set-based back-out
    # warning (§122.5), which a dev tool already scrapes out of a subprocess by hand because the
    # tier could not.
    expect_log_raw = raw.get("expect_log")
    expect_log: list = []
    if expect_log_raw is not None:
        items = expect_log_raw if isinstance(expect_log_raw, list) else [expect_log_raw]
        if not items:
            raise ManifestError(
                f"{path}: 'assert.expect_log' is empty. Omit the key, or name the line the "
                f"operator must log.")
        for item in items:
            if not isinstance(item, str) or not item.strip():
                raise ManifestError(
                    f"{path}: every 'assert.expect_log' entry must be a non-empty substring of a "
                    f"line the operator logs, got {item!r}")
            # The same guard expect_error carries, for the same reason: a substring this broad
            # matches almost any run, so the case could not tell the behaviour it is about from
            # the operator logging anything at all.
            if item.strip().lower() in {"warn", "warning", "error", "info", "log", "the"}:
                raise ManifestError(
                    f"{path}: 'assert.expect_log' entry {item!r} is too broad -- it would match "
                    f"almost any run. Quote enough of the line to name the behaviour this case "
                    f"is about.")
            expect_log.append(item)

    monitor_raw = raw.get("monitor")
    monitor: dict = {}
    if monitor_raw is not None:
        if not is_target:
            raise ManifestError(
                f"{path}: 'assert.monitor' needs a 'target:' block -- it reads what the writer "
                f"published to the platform's monitor")
        if not isinstance(monitor_raw, dict) or not monitor_raw:
            raise ManifestError(
                f"{path}: 'assert.monitor' must be a non-empty mapping of metric name -> "
                f"expected value")
        for name, value in monitor_raw.items():
            if name in _CLOCK_METRICS:
                raise ManifestError(
                    f"{path}: 'assert.monitor.{name}' is a CLOCK, and a case that asserts one is "
                    f"measuring the machine it runs on -- a flake authored on purpose. It is "
                    f"still reported for a human to read. Assertable metrics are "
                    f"{sorted(_ASSERTABLE_METRICS)}.")
            if name in _SHAPE_METRICS:
                # The expected value names the KEYS: a list of table names (tokens allowed).
                if not isinstance(value, list) or not all(isinstance(k, str) and k for k in value):
                    raise ManifestError(
                        f"{path}: 'assert.monitor.{name}' is a SHAPE metric: give the list of "
                        f"table names it must report a lag for, e.g. ['${{V_TABLE}}']. Its values "
                        f"are clocks and are not compared, only required to be non-negative.")
                monitor[name] = list(value)
                continue
            if name not in _ASSERTABLE_METRICS:
                raise ManifestError(
                    f"{path}: 'assert.monitor.{name}' is not a metric this writer publishes. "
                    f"Assertable: {sorted(_ASSERTABLE_METRICS)}; shape only: "
                    f"{sorted(_SHAPE_METRICS)}; published but not assertable (clocks): "
                    f"{sorted(_CLOCK_METRICS)}.")
            monitor[name] = "" if value is None else str(value)

    jmx = _normalize_jmx(raw.get("jmx"), path, is_source=is_source, is_target=is_target)
    if jmx is not None and not data:
        # The bean is read after an `assert.data` drive, the only OP drive there is -- a
        # smoke-only case never drives the operator. Counters without the emitted events would
        # also prove the op counted, not that it did the right thing.
        raise ManifestError(
            f"{path}: 'assert.jmx' supplements 'assert.data' -- the MBean is read after the "
            f"data drive, and a case without one never drives the operator")

    acked = raw.get("acked")
    if acked is not None:
        if not is_target:
            raise ManifestError(
                f"{path}: 'assert.acked' needs a 'target:' block -- only a target acknowledges")
        if isinstance(acked, bool) or not isinstance(acked, int) or acked < 0:
            raise ManifestError(
                f"{path}: 'assert.acked' must be a non-negative integer event count, got "
                f"{acked!r}")

    # `restarts`/`replayed` are what make a recovery case actually about recovery. Without them a
    # `restart_after:` case asserts the same ack count and the same rows as the case with no
    # restart at all -- so it would pass unchanged if the harness ignored the restart entirely.
    restarts = _normalize_target_count(raw, "restarts", is_target, path)
    replayed = _normalize_target_count(raw, "replayed", is_target, path)
    exception_store = _normalize_exception_store(raw, is_target, path)

    if (not data and not smoke and not target and acked is None
            and restarts is None and replayed is None and expect_error is None and not monitor):
        # ⚠ `expect_log` is DELIBERATELY not enough on its own, and this branch is where that is
        # decided. A log-only case proves the operator SAID something without proving it DID
        # anything -- and a warning is emitted on a run that otherwise succeeds, so "warned" only
        # means "warned while working" if the working part is asserted too. Pair it with `target:`
        # or `acked:`.
        raise ManifestError(
            f"{path}: 'assert' must declare at least one assertion (e.g. 'data' or 'smoke: true'"
            f"; for a target: 'target' or 'acked'). "
            + ("⚠ 'expect_log' alone is not enough: it proves what the operator SAID, not what it "
               "DID. Pair it with 'target:' or 'acked:' so the warning is asserted on a run that "
               "is also shown to work." if expect_log else ""))

    if gcs_objects and not data:
        raise ManifestError(
            f"{path}: 'assert.gcs_objects' supplements 'assert.data' -- the object check "
            f"runs after the WAEvent comparison, so a case with only 'smoke: true' has "
            f"nothing to attach it to")

    if expect_error is not None and (data or acked is not None or monitor
                                     or restarts is not None or replayed is not None
                                     or exception_store is not None or jmx is not None):
        # A failed drive produces NO RUN REPORT -- driveTarget throws before writing one -- so
        # `acked`, `monitor`, `restarts` and `replayed` have nothing to read, and `data` is an
        # OpenProcessor's emitted events, which likewise do not exist.
        #
        # ⚠ `target:` IS DELIBERATELY STILL ALLOWED, and this rule forbade it by mistake until
        # 2026-08-29. The DATABASE survives the failure, and its state is exactly what proves a
        # ROLLBACK: that a failing window landed NOTHING, and that the checkpoint did not advance.
        # Forbidding it made the most important assertion about a failure unwritable.
        raise ManifestError(
            f"{path}: 'assert.expect_error' cannot be combined with "
            f"'data'/'acked'/'monitor'/'restarts'/'replayed'/'exception_store'/'jmx' -- a failed drive writes no run "
            f"report, so there is nothing for those to read. 'assert.target:' IS allowed, and is "
            f"how a rollback is proved: query the table and the checkpoint row after the failure.")

    return AssertSpec(data=data, smoke=smoke, gcs_objects=gcs_objects, target=target, acked=acked,
                      restarts=restarts, replayed=replayed, expect_error=expect_error,
                      expect_log=expect_log, monitor=monitor, exception_store=exception_store,
                      jmx=jmx)


_JMX_KEYS = {"attributes", "bean"}

#: A JMX attribute name that reads a clock or a duration. Refused for the reason `monitor` refuses
#: its clocks: asserting one measures the machine. `(?![a-z])` keeps a COUNT like
#: `GateTimeoutsPassThrough` assertable while refusing `...Time`, `...Millis`, `...Latency`.
_JMX_CLOCK_ATTRIBUTE = re.compile(
    r"(Millis|Micros|Nanos|Seconds|Secs|Ms|Latency|Time|Timestamp|Uptime|Age|Duration|Elapsed)(?![a-z])")


def _normalize_jmx(raw, path, *, is_source: bool, is_target: bool) -> JmxSpec | None:
    """`assert.jmx:` -- see JmxSpec. OP cases only: a reader, a writer and a UDF are driven by
    other cores, which the runner does not snapshot."""
    if raw is None:
        return None
    if is_source or is_target:
        raise ManifestError(
            f"{path}: 'assert.jmx' is supported for 'op:' cases only, not for a "
            f"{'source:' if is_source else 'target:'} case")
    if not isinstance(raw, dict):
        raise ManifestError(f"{path}: 'assert.jmx' must be a mapping with 'attributes:'")
    unknown = sorted(set(raw) - _JMX_KEYS)
    if unknown:
        raise ManifestError(
            f"{path}: 'assert.jmx' has unknown key(s) {unknown} -- allowed: {sorted(_JMX_KEYS)}")
    bean = raw.get("bean")
    if bean is not None and (not isinstance(bean, str) or not bean.strip()):
        raise ManifestError(
            f"{path}: 'assert.jmx.bean' must be a class name (simple, in the core's package, or "
            f"fully qualified), got {bean!r}")
    attrs_raw = raw.get("attributes")
    if not isinstance(attrs_raw, dict) or not attrs_raw:
        raise ManifestError(
            f"{path}: 'assert.jmx.attributes' must be a non-empty mapping of attribute name -> "
            f"expected value or {{min, max}}")
    attributes: dict = {}
    for name, want in attrs_raw.items():
        if not isinstance(name, str) or not name:
            raise ManifestError(f"{path}: 'assert.jmx.attributes' key {name!r} is not a name")
        if _JMX_CLOCK_ATTRIBUTE.search(name):
            raise ManifestError(
                f"{path}: 'assert.jmx.attributes.{name}' reads a CLOCK or a duration, and a case "
                f"that asserts one is measuring the machine it runs on. Assert counts and states.")
        if isinstance(want, dict):
            extra = sorted(set(want) - {"min", "max"})
            bounds = {k: want[k] for k in ("min", "max") if k in want}
            if extra or not bounds:
                raise ManifestError(
                    f"{path}: 'assert.jmx.attributes.{name}' range takes 'min' and/or 'max' only, "
                    f"got {want!r}")
            for k, v in bounds.items():
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    raise ManifestError(
                        f"{path}: 'assert.jmx.attributes.{name}.{k}' must be a number, got {v!r}")
            if "min" in bounds and "max" in bounds and bounds["min"] > bounds["max"]:
                raise ManifestError(
                    f"{path}: 'assert.jmx.attributes.{name}' has min > max: {want!r}")
            attributes[name] = bounds
        elif isinstance(want, (bool, int, float, str)):
            attributes[name] = want
        else:
            raise ManifestError(
                f"{path}: 'assert.jmx.attributes.{name}' must be a number, true/false, a string, "
                f"or {{min, max}}, got {want!r}")
    return JmxSpec(attributes=attributes, bean=bean.strip() if bean else None)


def jmx_to_wire(spec: JmxSpec | None) -> dict | None:
    """The request-JSON form of `assert.jmx:` (Java `JmxSpec`), minus `outputFile`, which the
    harness adds because only it knows the temp directory. `None` in, `None` out."""
    if spec is None:
        return None
    return {"bean": spec.bean}


def _normalize_exception_store(raw, is_target: bool, path) -> list | None:
    """`assert.exception_store:` -- the notifications the writer made, as lists of input ordinals.

    A skipped row carries every source event that folded into it, so one notification is a
    LIST of ordinals (1-based, the position in `target.input`), and the assertion is a list of
    those in the order the writer made them. `[]` is a real assertion: nothing reached the store.
    """
    value = raw.get("exception_store")
    if value is None:
        return None
    if not is_target:
        raise ManifestError(
            f"{path}: 'assert.exception_store' needs a 'target:' block -- only a target skips rows")
    if not isinstance(value, list) or any(
            not isinstance(n, list) or any(isinstance(o, bool) or not isinstance(o, int) or o < 1
                                           for o in n) for n in value):
        raise ManifestError(
            f"{path}: 'assert.exception_store' must be a list of lists of 1-based input ordinals "
            f"-- one inner list per notification, e.g. [[2, 3], [5]] -- got {value!r}")
    return [list(n) for n in value]


def _normalize_target_count(raw, key: str, is_target: bool, path) -> int | None:
    """One of the target run-report counts (`assert.restarts:`/`assert.replayed:`)."""
    value = raw.get(key)
    if value is None:
        return None
    if not is_target:
        raise ManifestError(
            f"{path}: 'assert.{key}' needs a 'target:' block -- it reads the writer's run report")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ManifestError(
            f"{path}: 'assert.{key}' must be a non-negative integer, got {value!r}")
    return value


def _normalize_timeout(raw, path) -> int:
    if raw is None:
        return DEFAULT_TIMEOUT
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ManifestError(f"{path}: 'timeout' must be an integer number of seconds, got {raw!r}")
    if raw <= 0:
        raise ManifestError(f"{path}: 'timeout' must be a positive integer, got {raw!r}")
    return raw


def _normalize_disabled(raw, path):
    if raw is not None and not isinstance(raw, (str, bool)):
        raise ManifestError(f"{path}: 'disabled' must be a string reason or a boolean, got {raw!r}")
    if isinstance(raw, str) and not raw.strip():
        raise ManifestError(f"{path}: 'disabled' string must be non-empty (give a reason / ticket ref)")
    return raw


def _normalize_types(raw, path) -> dict:
    """Normalize the optional `types:` block (docs/INTEGRATION-TESTS.md): source schemas for
    type-consuming operators, keyed by the source event's `metadata.TableName`, valued
    by the ordered list of source column names positional to the input WAEvent
    `data[]`/`before[]`. Absent -> `{}` (no source schema; unchanged behavior for
    operators that only resolve userdata-based mappings)."""
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not raw:
        raise ManifestError(f"{path}: 'types' must be a non-empty mapping of table name -> column list")
    out: dict[str, object] = {}
    for table, spec in raw.items():
        if not isinstance(table, str) or not table:
            raise ManifestError(f"{path}: 'types' keys must be non-empty table-name strings, got {table!r}")

        # Two forms. The bare list is the original and stays exact; the mapping form adds the
        # metadata a source type carries beyond its column order -- key columns and display
        # aliases -- which an operator that builds DDL or propagates keys genuinely reads. The
        # list form is shorthand for {columns: [...]} with no keys and no aliases.
        keys: list[str] = []
        aliases: dict[str, str] = {}
        if isinstance(spec, dict):
            unknown = set(spec) - {"columns", "keys", "aliases"}
            if unknown:
                raise ManifestError(
                    f"{path}: 'types.{table}' has unknown key(s) {sorted(unknown)} -- "
                    f"allowed: ['aliases', 'columns', 'keys']")
            columns = spec.get("columns")
            keys = _normalize_type_keys(spec.get("keys"), table, path)
            aliases = _normalize_type_aliases(spec.get("aliases"), table, path)
        else:
            columns = spec

        if not isinstance(columns, list) or not columns:
            raise ManifestError(
                f"{path}: 'types.{table}' must be a non-empty list of column-name strings "
                f"(or a mapping with a 'columns' list), got {columns!r}")
        cols: list[str] = []
        for i, column in enumerate(columns):
            if not isinstance(column, str) or not column:
                raise ManifestError(
                    f"{path}: 'types.{table}[{i}]' must be a non-empty column-name string, got {column!r}")
            cols.append(column)

        # A key or alias naming a column the table does not declare is a typo, and a silent one:
        # it would simply never match, leaving the case asserting less than its author believes.
        for k in keys:
            if k not in cols:
                raise ManifestError(
                    f"{path}: 'types.{table}.keys' names {k!r}, which is not one of its columns "
                    f"{cols}")
        for a in aliases:
            if a not in cols:
                raise ManifestError(
                    f"{path}: 'types.{table}.aliases' names {a!r}, which is not one of its columns "
                    f"{cols}")

        out[table] = cols if not (keys or aliases) else {
            "columns": cols, "keys": keys, "aliases": aliases}
    return out


def _normalize_type_keys(raw, table: str, path) -> list:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        raise ManifestError(
            f"{path}: 'types.{table}.keys' must be a non-empty column-name string or list of them")
    for k in raw:
        if not isinstance(k, str) or not k:
            raise ManifestError(
                f"{path}: 'types.{table}.keys' entries must be non-empty strings, got {k!r}")
    return list(raw)


def _normalize_type_aliases(raw, table: str, path) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not raw:
        raise ManifestError(
            f"{path}: 'types.{table}.aliases' must be a non-empty mapping of column -> alias")
    for k, v in raw.items():
        if not isinstance(k, str) or not k or not isinstance(v, str) or not v:
            raise ManifestError(
                f"{path}: 'types.{table}.aliases' entries must be non-empty string pairs, "
                f"got {k!r}: {v!r}")
    return dict(raw)


def _normalize_password_properties(raw, path) -> list:
    """Normalize the optional `password_properties:` block: the subset of `properties:`
    keys whose value must be wrapped in the harness's mock `Password` (the
    IntegrationProcessor capability added alongside this normalizer) before an
    operator's core is constructed, so an `instanceof Password` check on a connection
    property (e.g. a JDBC pool's `Password`/`BootstrapPassword`) succeeds the same way
    it would against the real Striim platform type. Absent (or an empty list) -> `[]`
    (no properties require Password wrapping). Shape mirrors `requires:`, a scalar
    string or list of them; `load_manifest` separately checks every named key is
    actually declared under `properties:`."""
    if raw is None:
        return []
    if isinstance(raw, str):
        if not raw:
            raise ManifestError(f"{path}: 'password_properties' string entry must be non-empty")
        return [raw]
    if isinstance(raw, list):
        out = []
        for item in raw:
            if not isinstance(item, str) or not item:
                raise ManifestError(
                    f"{path}: 'password_properties' list items must be non-empty "
                    f"property-name strings, got {item!r}")
            out.append(item)
        return out
    raise ManifestError(
        f"{path}: 'password_properties' must be a property-name string or a list of them, got {raw!r}")


def _normalize_udf_value(raw, path, step_index: int, arg_index: int):
    """Normalizes one arg-or-list-element NODE to its wire form. Used both for a
    top-level `args[]` entry and, recursively, for each element of a YAML list
    arg -- the same four forms are legal in both places:

    - the literal string `'$'` -> the tag dict `{"reg": True}`
    - `{ref: name}` -> the tag dict `{"ref": name}` (a prior step's `as:` binding --
      forward/self references are rejected by `_normalize_udf`'s cross-check, not here)
    - `{json: ...}` -> the tag dict `{"json": value}` (a `str` value must itself parse
      as JSON, checked here so a typo fails at collection, not inside the JVM)
    - `{str: text}` -> the plain string `text`, UNWRAPPED (the escape hatch for a
      literal `"$"` or a number-shaped string that must not be re-interpreted)
    - any other scalar (str/int/float/bool/None) -> itself, UNWRAPPED
    - a YAML list -> a plain Python list of recursively normalized elements, UNWRAPPED

    The caller (`_normalize_udf_arg` for a top-level arg; this function itself for a
    nested list element) decides whether an unwrapped scalar/list result needs a `{"val":
    ...}` wrapper -- a tag dict (`reg`/`ref`/`json`) is always returned as-is."""
    if raw == "$":
        return {"reg": True}
    if isinstance(raw, dict):
        if len(raw) != 1:
            raise ManifestError(
                f"{path}: 'udf.pipeline[{step_index}].args[{arg_index}]' mapping must have exactly one "
                f"key (one of ref/json/str), got {raw!r}")
        (key, value), = raw.items()
        if key == "ref":
            if not isinstance(value, str) or not value or not _UDF_BIND_RE.match(value):
                raise ManifestError(
                    f"{path}: 'udf.pipeline[{step_index}].args[{arg_index}].ref' must be a non-empty "
                    f"identifier naming an earlier step's 'as:', got {value!r}")
            return {"ref": value}
        if key == "json":
            import json as _json
            if isinstance(value, str):
                try:
                    _json.loads(value)
                except ValueError as e:
                    raise ManifestError(
                        f"{path}: 'udf.pipeline[{step_index}].args[{arg_index}].json' is not valid JSON: {e}") from e
            else:
                # A non-str YAML value (a mapping/list/number/bool/null) is passed through
                # structurally rather than re-parsed -- but it still has to survive the
                # SAME json.dumps() round-trip `udf_to_wire`/harness.drive will put it
                # through later, or a YAML-only type (e.g. PyYAML's `!!timestamp` ->
                # datetime.date for an unquoted `2020-01-01`) reaches the request JSON
                # writer as an opaque, uncaught TypeError at RUN time instead of a
                # ManifestError naming the file and key at COLLECTION time.
                try:
                    _json.dumps(value)
                except TypeError as e:
                    raise ManifestError(
                        f"{path}: 'udf.pipeline[{step_index}].args[{arg_index}].json' must be a JSON string or a "
                        f"JSON-representable value (mapping/list/number/bool/null), got {type(value).__name__}: {e}") from e
            return {"json": value}
        if key == "str":
            if not isinstance(value, str):
                raise ManifestError(
                    f"{path}: 'udf.pipeline[{step_index}].args[{arg_index}].str' must be a string, got {value!r}")
            return value
        raise ManifestError(
            f"{path}: 'udf.pipeline[{step_index}].args[{arg_index}]' has unknown key {key!r} "
            f"(expected one of ref, json, str)")
    if isinstance(raw, list):
        return [_normalize_udf_value(item, path, step_index, arg_index) for item in raw]
    if raw is None or isinstance(raw, (str, int, float, bool)):
        return raw
    raise ManifestError(
        f"{path}: 'udf.pipeline[{step_index}].args[{arg_index}]' must be a scalar, a "
        f"{{ref|json|str: ...}} mapping, or a list of them, got {raw!r}")


def _normalize_udf_arg(raw, path, step_index: int, arg_index: int) -> dict:
    """Normalizes one top-level `udf.pipeline[].args[]` entry to its wire form: a tag
    dict (`{"reg"|"ref"|"json": ...}`) from `_normalize_udf_value` is returned as-is; a
    bare scalar or list is wrapped as `{"val": ...}` (the form `UdfCore.decodeArg`'s
    top-level dispatch requires for every arg)."""
    node = _normalize_udf_value(raw, path, step_index, arg_index)
    return node if isinstance(node, dict) else {"val": node}


def _collect_udf_refs(node) -> list[str]:
    """Recursively collects every `{"ref": name}` tag's `name` inside one already-wire-
    normalized arg node (a nested `{"val": [...]}` list can itself hold ref/json tags --
    the worked `udf:` example in docs/INTEGRATION-TESTS.md, e.g.
    `JSONBuildArrayFromList` over a list of `{ref: ...}` entries)."""
    if isinstance(node, dict):
        if "ref" in node:
            return [node["ref"]]
        if "val" in node:
            return _collect_udf_refs(node["val"])
        return []
    if isinstance(node, list):
        refs = []
        for item in node:
            refs.extend(_collect_udf_refs(item))
        return refs
    return []


def _normalize_udf(raw, path) -> UdfSpec | None:
    """Normalizes the optional `udf:` block. Absent (`None`) is the entire OP
    path -- no other line of this function executes for an existing
    `test.yaml`."""
    if raw is None:
        return None
    if isinstance(raw, list):
        raise ManifestError(
            f"{path}: 'udf' must be a single mapping, not a list -- the integration "
            f"harness drives exactly one module per subprocess, so 'op:'/'udf:' take a "
            f"single mapping here (the list form is scripts/live/-only)")
    if not isinstance(raw, dict) or not raw:
        raise ManifestError(f"{path}: 'udf' must be a non-empty mapping")
    allowed = {"jar", "class", "kind", "source", "target", "pipeline"}
    unknown = sorted(set(raw.keys()) - allowed)
    if unknown:
        raise ManifestError(f"{path}: 'udf' has unknown key(s) {unknown} (allowed: {sorted(allowed)})")

    jar = _normalize_module_ref(raw.get("jar"), "udf", path)

    class_name = raw.get("class")
    if not isinstance(class_name, str) or not class_name or not _UDF_CLASS_RE.match(class_name):
        raise ManifestError(
            f"{path}: 'udf.class' is required and must be a fully-qualified class name "
            f"(e.g. 'com.example.MyUdf'), got {class_name!r}")

    kind = raw.get("kind", DEFAULT_UDF_KIND)
    if kind not in UDF_KINDS:
        raise ManifestError(f"{path}: 'udf.kind' must be one of {sorted(UDF_KINDS)}, got {kind!r}")

    source = raw.get("source")
    target = raw.get("target")
    if kind == "waevent":
        if source is not None or target is not None:
            raise ManifestError(f"{path}: 'udf.source'/'udf.target' only apply when 'udf.kind' is 'jsonnode'")
    else:
        for field_name, value in (("source", source), ("target", target)):
            if value is not None and (not isinstance(value, str) or not _SLOT_RE.match(value)):
                raise ManifestError(
                    f"{path}: 'udf.{field_name}' must match data[N]/before[N]/userdata.KEY, got {value!r}")

    pipeline_raw = raw.get("pipeline")
    if not isinstance(pipeline_raw, list) or not pipeline_raw:
        raise ManifestError(f"{path}: 'udf.pipeline' is required and must be a non-empty list")

    steps: list[UdfStep] = []
    bound_names: set[str] = set()
    any_updates_register = False
    for i, item in enumerate(pipeline_raw):
        if not isinstance(item, dict):
            raise ManifestError(f"{path}: 'udf.pipeline[{i}]' must be a mapping, got {item!r}")
        step_unknown = sorted(set(item.keys()) - {"function", "args", "as"})
        if step_unknown:
            raise ManifestError(f"{path}: 'udf.pipeline[{i}]' has unknown key(s) {step_unknown}")

        function = item.get("function")
        if not isinstance(function, str) or not function or not _UDF_METHOD_RE.match(function):
            raise ManifestError(
                f"{path}: 'udf.pipeline[{i}].function' must be a simple (unqualified) method name, got {function!r}")

        args_raw = item.get("args", [])
        if not isinstance(args_raw, list):
            raise ManifestError(f"{path}: 'udf.pipeline[{i}].args' must be a list, got {args_raw!r}")
        args = [_normalize_udf_arg(a, path, i, j) for j, a in enumerate(args_raw)]

        # Forward/self references: a {ref: name} (however deeply nested inside a `val`
        # list) may only name an EARLIER step's `as:`.
        for j, a in enumerate(args):
            for name in _collect_udf_refs(a):
                if name not in bound_names:
                    raise ManifestError(
                        f"{path}: 'udf.pipeline[{i}].args[{j}]' refers to {name!r}, which is not "
                        f"bound by any earlier step's 'as:'")

        as_ = item.get("as")
        if as_ is not None:
            if not isinstance(as_, str) or not as_ or not _UDF_BIND_RE.match(as_) or as_ == "$":
                raise ManifestError(f"{path}: 'udf.pipeline[{i}].as' must be a non-empty identifier, got {as_!r}")
            if as_ in bound_names:
                raise ManifestError(f"{path}: 'udf.pipeline[{i}].as' name {as_!r} is already bound by an earlier step")
            bound_names.add(as_)
        else:
            any_updates_register = True

        steps.append(UdfStep(function=function, args=args, as_=as_))

    if not any_updates_register:
        raise ManifestError(
            f"{path}: 'udf.pipeline' has no step without 'as:' -- the '$' register would never be "
            f"updated, making the whole pipeline a no-op")

    return UdfSpec(jar=jar, class_name=class_name, kind=kind, pipeline=steps, source=source, target=target)


def udf_to_wire(spec: UdfSpec | None) -> dict | None:
    """The single dataclass -> request-JSON converter for a `udf:` block, shared by
    `harness.py` and `perf.py` so the two cannot drift. `None` in, `None` out -- for
    every existing OP `test.yaml` this adds exactly `"udf": null` to the request JSON,
    which `UdfCore`-branching Java code treats identically to the field's absence."""
    if spec is None:
        return None
    return {
        "className": spec.class_name,
        "kind": spec.kind,
        "source": spec.source,
        "target": spec.target,
        "pipeline": [{"function": s.function, "args": s.args, "as": s.as_} for s in spec.pipeline],
    }


def _normalize_source(raw, path) -> SourceSpec | None:
    """Normalizes the optional `source:` block. Absent (`None`) is the entire in-stream path."""
    if raw is None:
        return None
    if isinstance(raw, list):
        raise ManifestError(
            f"{path}: 'source' must be a single mapping, not a list -- the integration "
            f"harness drives exactly one module per subprocess")
    if not isinstance(raw, dict) or not raw:
        raise ManifestError(f"{path}: 'source' must be a mapping with a 'max_ticks' key")
    unknown = sorted(set(raw.keys()) - _SOURCE_KEYS)
    if unknown:
        raise ManifestError(
            f"{path}: 'source' has unknown key(s) {unknown} (allowed: {sorted(_SOURCE_KEYS)}). "
            f"There is deliberately no timeout/wait key: the budget is a tick COUNT, so a slow "
            f"machine takes longer without changing the result")

    max_ticks = raw.get("max_ticks")
    # bool is an int in Python, and `max_ticks: true` is a mistake worth naming.
    if isinstance(max_ticks, bool) or not isinstance(max_ticks, int) or max_ticks < 1:
        raise ManifestError(
            f"{path}: 'source.max_ticks' is required and must be an integer >= 1 (the tick "
            f"budget), got {max_ticks!r}")

    expect_events = raw.get("expect_events")
    if expect_events is not None:
        if isinstance(expect_events, bool) or not isinstance(expect_events, int) or expect_events < 0:
            raise ManifestError(
                f"{path}: 'source.expect_events' must be an integer >= 0 when present, got "
                f"{expect_events!r}")
        if expect_events == 0:
            # `expect_events: 0` is met before the first tick, so the reader would never run --
            # and a case proving a source stays quiet is exactly the one that must tick.
            raise ManifestError(
                f"{path}: 'source.expect_events: 0' would stop before the first tick. Omit it "
                f"instead: with no expectation the reader is ticked the full 'max_ticks', which "
                f"is how a case proves a source stays quiet")

    seed_when = raw.get("seed_when", DEFAULT_SEED_WHEN)
    if seed_when not in SEED_WHEN_VALUES:
        raise ManifestError(
            f"{path}: 'source.seed_when' must be one of {list(SEED_WHEN_VALUES)}, got "
            f"{seed_when!r}")

    return SourceSpec(max_ticks=max_ticks, expect_events=expect_events, seed_when=seed_when)


#: Clauses that change the MAPPING rather than the connection. A variant may not carry one: a
#: different mapping writes different rows, which needs its own golden and therefore its own case.
#:
#: COLUMNMAP and KEYCOLUMNS are JdbcSink's TQL property clauses, and for years they were
#: the whole list -- which made this guard REAL for that module and merely decorative for every
#: module whose mapping is expressed some other way. The issue was found from a config-driven
#: OP: that operator's mapping lives in its config.json as `keyColumnNames`, `valColumnNames`,
#: `sourceColumnNames` and `tables[].columns[].columnNames`, none of which contains either string,
#: so a token feeding a different key column into a config passed silently.
#:
#: COLUMNNAMES closes all four at once, because it is a substring of every one of them. That is
#: why it is a single entry rather than a list: the vocabulary is one word with four prefixes, and
#: spelling them out separately would invite the next one to be forgotten.
#:
#: The check is a substring test on the token's VALUE, so the cost of a false positive is a
#: refused variant -- and a connection-axis token (a URL, a user, a schema, a table name) has no
#: reason to contain the text "columnnames" at all. Verified against every variant token in the
#: corpus at the time of writing: zero matches.
_MAPPING_CLAUSES = ("COLUMNMAP", "KEYCOLUMNS", "COLUMNNAMES", "APPENDONLY")

#: MON metrics a case may assert: counts and a rendered position, all exact.
_ASSERTABLE_METRICS = {
    "PROCESSED", "TOTAL_EVENTS_IN_LAST_COMMIT", "TOTAL_EVENTS_IN_LAST_IO",
    "TARGET_COMMIT_POSITION",
    # §156. The §132.3 fields that are COUNTS or shapes, not clocks.
    "NUM_OF_EXCEPTIONS_IGNORED", "OPERATION_METRICS", "TABLE_INFO",
    "NO_OP_OPERATIONS",   # §157
}

#: MON metrics a case may NOT assert: they are CLOCKS. A case asserting a latency or a timestamp
#: is measuring the machine it runs on, which is the same mistake §6.1a refuses for performance --
#: and it would be a flake authored on purpose. They are still reported, for a human to read.
_CLOCK_METRICS = {
    "LAST_COMMIT_TIME", "LAST_IO_TIME", "COMMIT_LATENCY", "EXTERNAL_IO_LATENCY",
    # §156. An AGE is a clock measured from a clock -- asserting "0.003 seconds since the last
    # write" is asserting how fast this machine ran. Published for a human, never assertable.
    "LAST_WRITE_AGE",
}

#: MON metrics whose SHAPE is assertable but whose values are clocks: `COMMIT_LAG` is
#: `{"<target table>": <millis behind the source's commit>}`. A case asserts WHICH tables carry
#: a lag -- that the writer measured one where the events carried a source time and none where
#: they did not -- and every value must be a non-negative integer; the millis themselves are the
#: machine's clock against the fixture's and are not compared.
_SHAPE_METRICS = {"COMMIT_LAG"}

_VARIANT_KEYS = {"db", "ddl", "tokens"}


def _normalize_variants(raw, test_dir: Path, path) -> list:
    """Normalizes the optional `variants:` block (§48.6): the CONNECTION axis.

    One case, every database type, ONE expectation. A variant supplies token values, its own
    `ddl:`, and the route its assertions read through -- and nothing else. The `properties:`
    block, the query shape and the expected fixture are authored once and shared, which is what
    makes the case state "every engine agrees" instead of enumerating what each engine does.

    Unlike `matrix:`, this is not a cross-product: an engine's URL, user, provider type, DDL and
    routes are CORRELATED, and cross-producting them would pair a PostgreSQL URL with an Oracle
    password.
    """
    if raw is None:
        return []
    if not isinstance(raw, dict) or not raw:
        raise ManifestError(
            f"{path}: 'variants' must be a non-empty mapping of name -> {{db, ddl, tokens}}")
    if len(raw) < 2:
        raise ManifestError(
            f"{path}: 'variants' needs at least two entries to be worth the construct, got "
            f"{sorted(raw)}. One engine is an ordinary case.")
    out = []
    for name, spec in raw.items():
        if not isinstance(name, str) or not name:
            raise ManifestError(f"{path}: 'variants' keys must be names, got {name!r}")
        if not isinstance(spec, dict) or not spec:
            raise ManifestError(
                f"{path}: 'variants.{name}' must be a mapping with a 'db' key, got {spec!r}")
        unknown = sorted(set(spec) - _VARIANT_KEYS)
        if unknown:
            raise ManifestError(
                f"{path}: 'variants.{name}' has unknown key(s) {unknown} (allowed: "
                f"{sorted(_VARIANT_KEYS)}). A variant supplies a route, its DDL and token values "
                f"-- it does not restructure the case, which is what keeps one expectation shared "
                f"across every engine.")
        db = spec.get("db")
        if not isinstance(db, str) or not db:
            raise ManifestError(
                f"{path}: 'variants.{name}' needs a non-empty 'db' route -- it is where this "
                f"variant's `assert.target:` queries read from")
        try:
            dbroutes.parse_route(db)
        except dbroutes.RouteError as e:
            raise ManifestError(f"{path}: 'variants.{name}.db': {e}") from None

        tokens = spec.get("tokens") or {}
        if not isinstance(tokens, dict):
            raise ManifestError(
                f"{path}: 'variants.{name}.tokens' must be a mapping of name -> value, got "
                f"{tokens!r}")
        rendered = {}
        for key, value in tokens.items():
            if not isinstance(key, str) or not key:
                raise ManifestError(
                    f"{path}: 'variants.{name}.tokens' keys must be token names, got {key!r}")
            text = "" if value is None else str(value)
            # ⚠ THE GUARD THAT KEEPS ONE EXPECTATION HONEST. A variant may name a different TABLE;
            # it may not change how that table's columns are MAPPED. COLUMNMAP/KEYCOLUMNS write
            # different rows, so a variant carrying one would need its own golden -- and the whole
            # construct is that every variant shares one.
            upper = text.upper()
            for clause in _MAPPING_CLAUSES:
                if clause in upper:
                    raise ManifestError(
                        f"{path}: 'variants.{name}.tokens.{key}' contains {clause}. A variant "
                        f"varies the CONNECTION, not the mapping -- a different mapping writes "
                        f"different rows, so it needs its own expected fixture and therefore its "
                        f"own case. That is §6.1's structural axis, not this one.")
            rendered[key] = text

        ddl = _normalize_file_specs(spec.get("ddl"), f"variants.{name}.ddl", test_dir, path)
        # A variant's DDL runs through the variant's OWN route, so an author cannot accidentally
        # create a PostgreSQL table through the Oracle connection. ⚠ An explicit `db:` used to be
        # discarded here rather than honoured or refused -- the §98.1 shape. It now wins, but only
        # for another route of the SAME SERVICE: a fixture may span source and target schemas of
        # one engine, and may not reach a different engine, which is the property this line was
        # protecting all along.
        ddl = [_variant_file_spec(f, db, name, path) for f in ddl]
        out.append(Variant(name=name, db=db, ddl=ddl, tokens=rendered))
    return out


def _variant_file_spec(spec, variant_db: str, variant_name: str, path):
    """Route one of a variant's DDL files: the variant's own route unless the entry named one.

    An explicit route must belong to the SAME SERVICE. A case needs both `postgres-source` and
    `postgres-target` when its fixture creates the same table name in two schemas -- the shape
    `jdbcsink-refuses-an-ambiguous-table-name` is built on -- while `oracle-target`
    under a `postgres` variant is the accident the unconditional override existed to stop.
    """
    if not spec.db_explicit:
        return FileSpec(file=spec.file, db=variant_db, path=spec.path)
    # ⚠ parse_route raises RouteError, which IntYamlFile.collect does NOT catch -- a typo'd route
    # would reach the author as a traceback instead of a message. The variant's own `db:` is
    # already guarded this way a few lines up; this path has to be too.
    try:
        same_service = dbroutes.parse_route(spec.db)[0] == dbroutes.parse_route(variant_db)[0]
    except dbroutes.RouteError as e:
        raise ManifestError(
            f"{path}: 'variants.{variant_name}.ddl' names route {spec.db!r} for "
            f"{spec.file!r}: {e}") from None
    if not same_service:
        raise ManifestError(
            f"{path}: 'variants.{variant_name}.ddl' names route {spec.db!r} for {spec.file!r}, "
            f"but this variant connects through {variant_db!r}. A variant's DDL may name another "
            f"route of the SAME service (source vs target schema), never another engine -- that "
            f"would create one engine's tables through another engine's connection.")
    return FileSpec(file=spec.file, db=spec.db, path=spec.path, db_explicit=True)


def _normalize_matrix(raw, properties: dict, path) -> dict:
    """Normalizes the optional `matrix:` block (§6.1a): property name -> values to cross-product.

    The case then runs once per combination, ALL against its single `assert:` block. That one
    shared expectation is the point rather than an economy: it states the invariant directly --
    *these knobs are not observable in the output* -- where N transcribed expectations could each
    drift independently and still look green.

    What may legitimately differ between permutations is PERFORMANCE, which this tier does not
    measure and must not start to: `CompactEvents: false` is slower by design, `NormalizeColumnSet:
    true` costs a full row per update. That is T3's.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not raw:
        raise ManifestError(
            f"{path}: 'matrix' must be a non-empty mapping of property name -> list of values")
    out: dict[str, list] = {}
    for name, values in raw.items():
        if not isinstance(name, str) or not name:
            raise ManifestError(f"{path}: 'matrix' keys must be property names, got {name!r}")
        if isinstance(values, (str, bool, int)):
            raise ManifestError(
                f"{path}: 'matrix.{name}' must be a LIST of values to cross-product, got "
                f"{values!r}. A single value is not a matrix -- put it in 'properties:' instead.")
        if not isinstance(values, list) or len(values) < 2:
            raise ManifestError(
                f"{path}: 'matrix.{name}' needs at least two values to be worth cross-producting, "
                f"got {values!r}")
        seen = [str(v) for v in values]
        duplicates = sorted({v for v in seen if seen.count(v) > 1})
        if duplicates:
            raise ManifestError(
                f"{path}: 'matrix.{name}' repeats {duplicates} -- the same permutation would run "
                f"twice and prove nothing the first run did not")
        # Rejected rather than merged: a key in both places reads as "this is the value" in one
        # spot and "these are the values" in another, and which wins is not something an author
        # should have to know.
        if name in properties:
            raise ManifestError(
                f"{path}: '{name}' is in both 'properties:' and 'matrix:'. The matrix supplies it "
                f"per permutation, so the 'properties:' entry would be silently overridden -- "
                f"remove it from 'properties:'.")
        out[name] = seen
    return out


def _normalize_target(raw, test_dir: Path, path) -> TargetSpec | None:
    """Normalizes the optional `target:` block. Absent (`None`) is every other case in the tier."""
    if raw is None:
        return None
    if isinstance(raw, list):
        raise ManifestError(
            f"{path}: 'target' must be a single mapping, not a list -- the integration "
            f"harness drives exactly one module per subprocess")
    if not isinstance(raw, dict) or not raw:
        raise ManifestError(f"{path}: 'target' must be a mapping with an 'input' key")
    unknown = sorted(set(raw.keys()) - _TARGET_KEYS)
    if unknown:
        raise ManifestError(
            f"{path}: 'target' has unknown key(s) {unknown} (allowed: {sorted(_TARGET_KEYS)})")

    input_file = raw.get("input")
    if not isinstance(input_file, str) or not input_file:
        raise ManifestError(
            f"{path}: 'target.input' is required and must be a non-empty WAEvent-JSON file "
            f"path -- a target is FED events, unlike a reader, and it emits none, so the "
            f"input cannot live on an 'assert.data' entry the way an OpenProcessor's does")

    restart_after = raw.get("restart_after")
    if restart_after is not None:
        # int or list-of-int. A bare int normalises to a one-element tuple so nothing downstream
        # has to branch on the two spellings.
        raw_points = restart_after if isinstance(restart_after, list) else [restart_after]
        if isinstance(restart_after, list) and not raw_points:
            raise ManifestError(
                f"{path}: 'target.restart_after' is an empty list. Omit the key to run without a "
                f"restart; an empty list asks for a recovery case that never recovers.")
        points: list[int] = []
        for point in raw_points:
            if isinstance(point, bool) or not isinstance(point, int):
                raise ManifestError(
                    f"{path}: 'target.restart_after' must be an integer event count, or a list of "
                    f"them, got {point!r}")
            if point < 1:
                raise ManifestError(
                    f"{path}: 'target.restart_after' must be at least 1, got {point}. A restart "
                    f"before the first event proves nothing: the writer would come back up having "
                    f"written nothing and having nothing to resume from.")
            points.append(point)
        if len(set(points)) != len(points):
            raise ManifestError(
                f"{path}: 'target.restart_after' repeats an ordinal: {points}. The driver restarts "
                f"AT a point, so naming one twice cannot restart twice there -- the second is "
                f"silently unreachable, which reads as a two-restart case that only restarts once.")
        if points != sorted(points):
            raise ManifestError(
                f"{path}: 'target.restart_after' must be ascending, got {points}. The driver feeds "
                f"events in order and restarts as it passes each point, so an out-of-order entry "
                f"never fires.")
        restart_after = tuple(points)

    mid_run = _normalize_mid_run(raw.get("mid_run"), test_dir, path)

    positions = raw.get("positions", True)
    if not isinstance(positions, bool):
        raise ManifestError(
            f"{path}: 'target.positions' must be true/false, got {positions!r}")
    if restart_after is not None and not positions:
        raise ManifestError(
            f"{path}: 'target.restart_after' needs 'positions: true'. With no positions there "
            f"is nothing to checkpoint and nothing to resume from, so the restart would replay "
            f"the whole input every time and the case would assert the writer's idempotence "
            f"rather than its recovery. Those are different properties; write them as "
            f"different cases.")

    distribution_id = raw.get("distribution_id")
    if distribution_id is not None and (not isinstance(distribution_id, str)
                                        or not distribution_id):
        raise ManifestError(
            f"{path}: 'target.distribution_id' must be a non-empty string when present, got "
            f"{distribution_id!r}")

    timezone = raw.get("timezone")
    if timezone is not None:
        if not isinstance(timezone, str) or not timezone.strip():
            raise ManifestError(
                f"{path}: 'target.timezone' must be a non-empty IANA zone id, got {timezone!r}")
        # Validated here rather than left to the JVM: an unknown -Duser.timezone does not
        # fail, it silently falls back to GMT -- so a typo would turn a non-UTC case into a
        # second UTC one that still passes and certifies nothing.
        if timezone not in available_timezones():
            raise ManifestError(
                f"{path}: 'target.timezone' is not a known zone id: {timezone!r}. The JVM does "
                f"not reject an unknown -Duser.timezone -- it silently falls back to GMT, which "
                f"would turn this into a second UTC case that passes and proves nothing.")

    return TargetSpec(
        timezone=timezone,
        input=input_file,
        input_path=(test_dir / input_file).resolve(),
        restart_after=restart_after,
        positions=positions,
        distribution_id=distribution_id,
        mid_run=mid_run,
    )


def _normalize_mid_run(raw, test_dir: Path, path: Path) -> tuple:
    """`target.mid_run:` -> a tuple of MidRunStep, non-descending by `after`.

    Several steps may share an `after`: the driver gates once there and they run in list order,
    each on its own route, sharing that gate's timeout.

    Shape mirrors `ddl:`/`seed:` -- a `file:` plus a `db:` route -- with an `after:` ordinal saying
    WHEN. The route defaults to the target's, not the source's: this SQL acts on the table the
    writer is writing, which is the whole reason the hook exists.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw:
        raise ManifestError(
            f"{path}: 'target.mid_run' must be a non-empty list of "
            f"{{after, file, db}} entries, got {raw!r}")
    steps: list[MidRunStep] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ManifestError(
                f"{path}: each 'target.mid_run' entry must be a mapping with 'after' and 'file', "
                f"got {entry!r}")
        unknown = set(entry) - {"after", "file", "db"}
        if unknown:
            raise ManifestError(
                f"{path}: unknown key(s) {sorted(unknown)} in a 'target.mid_run' entry; "
                f"allowed: after, file, db")
        after = entry.get("after")
        if isinstance(after, bool) or not isinstance(after, int) or after < 1:
            raise ManifestError(
                f"{path}: 'target.mid_run[].after' must be an event ordinal of at least 1, got "
                f"{after!r}. It counts events the way 'restart_after' does; 0 would mean 'before "
                f"the run', which is what 'ddl:' already is.")
        file = entry.get("file")
        if not isinstance(file, str) or not file.strip():
            raise ManifestError(
                f"{path}: 'target.mid_run[].file' is required and must be a .sql path relative to "
                f"the test directory, got {file!r}")
        db_explicit = "db" in entry
        db = entry.get("db", DEFAULT_TARGET_DB_ROUTE)
        if not isinstance(db, str) or not db.strip():
            raise ManifestError(
                f"{path}: 'target.mid_run[].db' must be a route name, got {db!r}")
        steps.append(MidRunStep(after=after, file=file, db=db.strip(),
                                path=(test_dir / file).resolve(),
                                db_explicit=db_explicit))
    ordinals = [step.after for step in steps]
    if ordinals != sorted(ordinals):
        raise ManifestError(
            f"{path}: 'target.mid_run' must be non-descending by 'after', got {ordinals}. The "
            f"driver feeds events in order and gates as it passes each point, so an out-of-order "
            f"entry never fires.")
    return tuple(steps)


def target_to_wire(spec: TargetSpec | None) -> dict | None:
    """The single dataclass -> request-JSON converter for a `target:` block, shared by
    every caller so the wire shape cannot drift. `None` stays `None`, which for every
    existing `test.yaml` adds exactly `"target": null` to the request JSON.

    `input` is NOT on the wire: it becomes the request's own `inputFile`, which the driver
    already reads for every case.
    """
    if spec is None:
        return None
    return {
        "restartAfter": list(spec.restart_after) if spec.restart_after else None,
        # Only the ORDINALS cross the wire: the driver gates, this side runs the SQL,
        # so the file and route are none of the driver's business.
        "midRunAfter": [step.after for step in spec.mid_run] or None,
        "positions": spec.positions,
        "distributionId": spec.distribution_id,
    }


def source_to_wire(spec: SourceSpec | None) -> dict | None:
    """The single dataclass -> request-JSON converter for a `source:` block, shared by
    `harness.py` and any later perf caller so the two cannot drift. `None` in, `None` out --
    for every existing `test.yaml` this adds exactly `"source": null` to the request JSON,
    which the `SourceCore`-branching Java code treats identically to the field's absence."""
    if spec is None:
        return None
    return {"maxTicks": spec.max_ticks, "expectEvents": spec.expect_events,
            "seedWhen": spec.seed_when}


def _normalize_purpose(raw, path):
    # SPEC §3 marks `purpose` required for shipped tests, but that is a gold-hermetic-check
    # concern (a later slice/CI gate), not the loader's -- mirroring live/livetest/manifest.py,
    # the loader itself treats it as optional and only validates shape when present.
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ManifestError(f"{path}: 'purpose' must be a non-empty string when present")
    return raw


def load_manifest(path) -> TestManifest:
    """Load and normalize a test.yaml per docs/INTEGRATION-TESTS.md. Raises ManifestError naming `path`
    and the offending key on any parse/validation failure."""
    path = Path(path)
    try:
        text = path.read_text()
    except OSError as e:
        raise ManifestError(f"{path}: cannot read manifest: {e}") from e
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ManifestError(f"{path}: invalid YAML: {e}") from e

    if raw is None:
        raise ManifestError(f"{path}: manifest is empty")
    if not isinstance(raw, dict):
        raise ManifestError(f"{path}: top level must be a mapping, got {type(raw).__name__}")

    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ManifestError(f"{path}: 'name' is required and must be a non-empty string")

    test_dir = path.parent

    properties = _normalize_properties(raw.get("properties"), path)
    password_properties = _normalize_password_properties(raw.get("password_properties"), path)
    unknown = [key for key in password_properties if key not in properties]
    if unknown:
        raise ManifestError(
            f"{path}: 'password_properties' names key(s) not present in 'properties': {unknown}")

    udf = _normalize_udf(raw.get("udf"), path)
    # `properties:`/`password_properties:` are read by nothing at all for a bare UDF
    # pipeline (there is no Processor/constructor to configure) -- reject rather than
    # silently ignore, so a fixture author who copies an OP test.yaml learns their
    # config is dead instead of wondering why it has no effect. (A single combined
    # check: `password_properties` non-empty already implies `properties` non-empty,
    # per the unknown-key cross-check above, so a separate password_properties-only
    # branch would be unreachable.)
    if udf is not None and (properties or password_properties):
        raise ManifestError(
            f"{path}: 'properties'/'password_properties' have no meaning for a 'udf:' case "
            f"(a UDF is a bare static function, not a configured Processor); remove them")

    op = _normalize_op(raw.get("op"), path)
    _require_exactly_one_of_op_udf(op, udf, path)

    source = _normalize_source(raw.get("source"), path)
    # A UDF is a bare static function: there is no tick to drive and no channel to emit
    # through, so the two blocks cannot describe the same case. Rejected rather than
    # silently preferring one, same policy as the op/udf guard above.
    if source is not None and udf is not None:
        raise ManifestError(
            f"{path}: 'source' and 'udf' cannot both be present -- a reader is ticked and a "
            f"UDF is a bare static function")

    target = _normalize_target(raw.get("target"), test_dir, path)
    # A target is FED and emits nothing; a reader is TICKED and emits everything; a UDF is a bare
    # static function that is neither. No case is two of those, and rejecting the combination is
    # what keeps a copied test.yaml from half-working -- same policy as the op/udf guard above.
    if target is not None and source is not None:
        raise ManifestError(
            f"{path}: 'target' and 'source' cannot both be present -- a target is fed events "
            f"and a reader is ticked for them")
    if target is not None and udf is not None:
        raise ManifestError(
            f"{path}: 'target' and 'udf' cannot both be present -- a target is a configured "
            f"writer and a UDF is a bare static function")

    matrix = _normalize_matrix(raw.get("matrix"), properties, path)
    variants = _normalize_variants(raw.get("variants"), test_dir, path)
    requires = _normalize_requires(raw.get("requires"), path)
    assert_ = _normalize_assert(raw.get("assert"), test_dir, path, is_source=source is not None,
                                is_target=target is not None)
    if assert_.jmx is not None and udf is not None:
        raise ManifestError(
            f"{path}: 'assert.jmx' is supported for 'op:' cases only -- a UDF has no MBean")
    # A target writes to a real database, so a case without one is asserting against nothing.
    # `requires:` is what provisions it, and an unprovisioned route fails deep inside a JDBC
    # connect with a message about a refused port rather than about a missing service.
    if target is not None and not requires:
        raise ManifestError(
            f"{path}: a 'target:' case needs 'requires:' to name the database it writes to "
            f"(e.g. 'requires: [postgres]') -- the whole point of this tier for a target is "
            f"that the database is real")
    # `matrix:` works for every case kind -- target, OpenProcessor, UDF and reader. What it
    # overrides is `properties:`, which a UDF case does not have, so that one combination is
    # refused rather than left to run N identical passes and look like it proved something.
    if variants and udf is not None:
        raise ManifestError(
            f"{path}: 'variants:' has no meaning for a 'udf:' case -- a UDF has no connection to "
            f"vary.")
    if variants and _normalize_file_specs(raw.get("ddl"), "ddl", test_dir, path):
        raise ManifestError(
            f"{path}: a case with 'variants:' puts its DDL on each VARIANT, not at case level. "
            f"Each engine needs its own SQL and its own route, and a shared 'ddl:' block would "
            f"run one engine's DDL through every variant's connection.")
    # ⚠ Third instance of the same shape, in the ASSERTION path. `assert.target[].db:` is
    # accepted by the schema and then unconditionally overridden by the variant's route
    # (plugin.py `_assert_target_rows`), so an author who writes one is reading a database the
    # harness silently chose for them. Reading through the variant IS the design -- it is what
    # makes one authored query reach five engines -- so the key is refused here rather than
    # honoured: nothing needs a cross-database assertion under variants, and "accepted and
    # ignored" is the trap §98.1 and §100.4 were both made of.
    if variants:
        for i, spec in enumerate(assert_.target):
            if spec.db_explicit:
                raise ManifestError(
                    f"{path}: 'assert.target[{i}]' names a 'db:' route, but a case with "
                    f"'variants:' reads every assertion through the VARIANT's route -- that is "
                    f"what lets one authored query reach every engine. The key would be silently "
                    f"ignored, so it is refused: remove it.")
    # ⚠ The same argument, for the same reason. A case-level `seed:` is rendered with the
    # VARIANT's tokens but runs through its OWN route, which defaults to postgres-source -- so it
    # would seed PostgreSQL with Oracle's table names while the writer wrote to Oracle, and
    # nothing would fail loudly. That is §98.1 exactly, and it is refused rather than fixed
    # because no case needs it: a seed that varies per engine is DDL, and belongs on the variant.
    if variants and _normalize_file_specs(raw.get("seed"), "seed", test_dir, path):
        raise ManifestError(
            f"{path}: a case with 'variants:' cannot carry a case-level 'seed:'. It would be "
            f"rendered with each variant's tokens but run through its own route, seeding one "
            f"engine with another's table names. Put the rows in that variant's 'ddl:' instead.")

    # ⚠ VARIANTS ARE HONOURED ON TWO CASE SHAPES ONLY, and a case that declares them on a third
    # is refused rather than run once and silently.
    #
    # `plugin.py` loops over `runs()` in exactly two
    # branches -- `assert.expect_error` and `target:` -- and the `assert.data` branch, which every
    # OP case uses, loops over `permutations()` instead and renders every token from the CASE-level
    # map. So a data case could declare three variants, parse cleanly, have `runs()` return three,
    # and then execute ONCE against whatever the case-level tokens happened to name. Declared and
    # inert: §126's shape, in the harness that refuses it everywhere else.
    #
    # It is refused rather than implemented because implementing it is not a small change -- the
    # data branch renders properties, the ConfigFile CONTENTS, the gcs keys and the input fixture
    # from one token map, and every one would have to become per-variant. That is shared plumbing
    # under every module's data cases, so it wants its own slice and its own evidence.
    #
    # MEASURED before adding: of the 74 cases in the corpus declaring `variants:`, 73 are `target:`
    # cases and none is a data case, so this refuses nothing that exists today. It would have
    # caught an unsupported first attempt at load instead of after a Docker run reported one pass.
    if variants and target is None and (assert_ is None or assert_.expect_error is None):
        raise ManifestError(
            f"{path}: 'variants:' is not honoured for this case shape. The runner expands "
            f"variants for a 'target:' case and for an 'assert.expect_error' case; an "
            f"'assert.data' case is driven once, with the CASE-level tokens, so a variants block "
            f"here would be silently ignored and the case would test one engine while appearing "
            f"to declare several. Until the data path supports it, give each engine its own case "
            f"with its own 'requires:'.")

    if matrix and udf is not None:
        raise ManifestError(
            f"{path}: 'matrix:' has no meaning for a 'udf:' case. It overrides 'properties:', and "
            f"a UDF is a bare static function with none -- every permutation would be identical.")

    if (target is not None and not assert_.target and assert_.acked is None
            and not assert_.smoke and assert_.expect_error is None and not assert_.monitor):
        # `restarts`/`replayed` deliberately do NOT satisfy this. They describe what the HARNESS
        # did -- how many times it restarted the writer and how many events it fed again -- not
        # what the writer wrote or released. A case asserting only those would drive the writer
        # and check nothing about it, which is the silent weakening every other cross-check here
        # exists to prevent. They supplement an assertion; they are not one.
        raise ManifestError(
            f"{path}: a 'target:' case needs 'assert.target:', 'assert.acked:' or "
            f"'assert.smoke: true' or 'assert.expect_error:'. A target emits no events, so "
            f"drives the writer and checks nothing it did. ('assert.restarts'/'assert.replayed' "
            f"describe what the harness did, not what the writer did, and 'assert.exception_store' "
            f"what the writer did NOT write, so they supplement an assertion and do not count.)")
    # A smoke-only case never drives the operator at all (plugin.py treats steps 2-5 completing
    # AS the assertion), so a `source:` beside it would be ticked zero times and sit dead --
    # the same silent-weakening the gcs_objects/password_properties cross-checks exist to stop.
    # ⚠ A post_start case can carry exactly ONE data assertion, and this rejects the rest rather
    # than letting them fail obscurely at run time. Each `assert.data` entry drives the operator
    # again, in a fresh temp directory, and the seed has already been committed -- so the second
    # reader blocks on the handshake, gets released by a callback that returns immediately, and
    # dies with "expected at least N events". A first attempt at this latched the callback, which
    # only changed the diagnostic from ALREADY_EXISTS (which named the cause) to an event-count
    # error (which does not). The constraint is real, so it belongs here, at load, in words.
    if (source is not None and source.seed_when == "post_start"
            and len(assert_.data) > 1):
        raise ManifestError(
            f"{path}: 'source.seed_when: post_start' allows exactly one 'assert.data' entry, and "
            f"this case has {len(assert_.data)}. Each entry drives the reader again, and the seed "
            f"is committed only for the first -- later drives would see an empty source and fail "
            f"on the event count. Split them into separate cases.")
    if source is not None and not assert_.data:
        raise ManifestError(
            f"{path}: 'source' needs 'assert.data' -- a smoke-only case never drives the "
            f"operator, so the reader would be ticked zero times and the block would sit "
            f"unused. Add an 'assert.data' entry (for a reader: a 'match' and no 'input'), "
            f"or remove 'source'")
    # A `gcs_objects` check reads ${GCS_BUCKET}, which only a `requires: [gcs]` test
    # provisions (plugin.py step 4b's `_ensure_gcs_bucket`) -- reject rather than let
    # the key sit silently dead, same policy as the password_properties cross-check
    # above.
    if assert_.gcs_objects and "gcs" not in requires:
        raise ManifestError(
            f"{path}: 'assert.gcs_objects' needs 'requires: [gcs]' -- the bucket it checks "
            f"is only provisioned for a test that requires the gcs service")

    return TestManifest(
        name=name,
        op=op,
        properties=properties,
        assert_=assert_,
        dir=test_dir,
        purpose=_normalize_purpose(raw.get("purpose"), path),
        requires=requires,
        ddl=_normalize_file_specs(raw.get("ddl"), "ddl", test_dir, path),
        seed=_normalize_file_specs(raw.get("seed"), "seed", test_dir, path),
        timeout=_normalize_timeout(raw.get("timeout"), path),
        disabled=_normalize_disabled(raw.get("disabled"), path),
        types=_normalize_types(raw.get("types"), path),
        password_properties=password_properties,
        udf=udf,
        source=source,
        target=target,
        matrix=matrix,
        variants=variants,
    )
