"""Isolation-enforcement rules for the parallel-live-tests migration (spec §A.6).

Pure rule-evaluation functions: each takes file text (and/or a slug) and returns
a list of `Violation`s. No filesystem walking here -- callers (tests/test_isolation_
enforcement.py) own corpus discovery and manifest resolution.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class Violation:
    file: Path
    line: int
    rule: str
    message: str


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


_ORACLE_RE = re.compile(
    r"(QASOURCE|QATARGET|\$\{ORACLE_SOURCE_SCHEMA\}|\$\{ORACLE_TARGET_SCHEMA\})"
    r"\.(?!\$\{TID\})[A-Za-z_]"
)


def check_oracle_prefix(path: Path, text: str) -> list[Violation]:
    out = []
    for m in _ORACLE_RE.finditer(text):
        out.append(Violation(
            path, _line_of(text, m.start()), "oracle-prefix",
            f"{m.group(0)}... is not ${{TID}}-prefixed (schema {m.group(1)})",
        ))
    return out


_TERADATA_RE = re.compile(
    r"(qasource|qatarget|\$\{TERADATA_(?:SOURCE|TARGET)_(?:SCHEMA|USER)\})"
    r"\.(?!\$\{TID\})[A-Za-z_]",
    re.IGNORECASE,
)


def check_teradata_prefix(path: Path, text: str) -> list[Violation]:
    out = []
    for m in _TERADATA_RE.finditer(text):
        out.append(Violation(
            path, _line_of(text, m.start()), "teradata-prefix",
            f"{m.group(0)}... is not ${{TID}}-prefixed (database {m.group(1)})",
        ))
    return out


_VERTICA_RE = re.compile(
    r"(qasource|qatarget|\$\{VERTICA_(?:SOURCE|TARGET)_SCHEMA\})"
    r"\.(?!\$\{TID\})[A-Za-z_]",
    re.IGNORECASE,
)


def check_vertica_prefix(path: Path, text: str) -> list[Violation]:
    out = []
    for m in _VERTICA_RE.finditer(text):
        out.append(Violation(
            path, _line_of(text, m.start()), "vertica-prefix",
            f"{m.group(0)}... is not ${{TID}}-prefixed (schema {m.group(1)})",
        ))
    return out


_MSSQL_RE = re.compile(
    r"((?:dbo|\$\{MSSQL_SOURCE_SCHEMA\}|\$\{MSSQL_TARGET_SCHEMA\})\.(?!\$\{TID\})[A-Za-z_]"
    r"|@source_name\s*=\s*N?'(?!\$\{TID\})[A-Za-z_]"
    r"|sys\.tables\s+WHERE\s+name\s*=\s*'(?!\$\{TID\})[A-Za-z_])",
    re.IGNORECASE,
)


def check_mssql_prefix(path: Path, text: str) -> list[Violation]:
    out = []
    for m in _MSSQL_RE.finditer(text):
        out.append(Violation(
            path, _line_of(text, m.start()), "mssql-prefix",
            f"untokenized dbo/CDC reference: {m.group(0)!r}",
        ))
    return out


_PG_DOTTED_RE = re.compile(
    r"(\$\{PG_SOURCE_SCHEMA\}|\$\{PG_TARGET_SCHEMA\})\.(?!\$\{TID\})[A-Za-z_]"
)
# Postgres ddl.sql/seed.sql write either a bare table name (no schema qualifier -- the
# harness sets search_path to the per-role schema at connect time) or an explicit
# literal `public.name` schema-qualified form; either way only the FINAL dot-separated
# segment is the table name that needs the ${TID} prefix (a literal schema-qualifier
# segment like `public` must never be mistaken for it). app.tql Tables: props and
# test.yaml assert targets use the ${PG_*_SCHEMA} dotted token form, caught by
# _PG_DOTTED_RE above, not by these.
_PG_BARE_CREATE_RE = re.compile(r"CREATE\s+TABLE\s+([A-Za-z_$][A-Za-z0-9_.${}]*)", re.IGNORECASE)
_PG_BARE_INSERT_RE = re.compile(r"INSERT\s+INTO\s+([A-Za-z_$][A-Za-z0-9_.${}]*)", re.IGNORECASE)

# Recognized per-test tokenization prefixes (spec §A.1/§A.1a). ${TID_ORACLE} (a short
# hashed id, distinct from ${TID}) is Oracle-only and legitimately appears in a test's
# .sql files alongside plain ${TID}-prefixed Postgres statements in the same file (a
# test can require both oracle and postgres) -- this bare-identifier scan must accept
# either as "tokenized", or every oracle+postgres test's Oracle-side statements
# false-positive as untokenized once they migrate off ${TID}.
_TID_PREFIXES = ("${TID}", "${TID_ORACLE}")


def _last_dotted_segment(ident: str) -> str:
    # Split only on dots that separate real identifier segments, not ones embedded
    # inside a ${...} token (none of our tokens contain a literal dot today, so a
    # plain rsplit is safe, but split defensively on ".": PG_SOURCE_SCHEMA-style
    # tokens never appear here -- this function only ever sees bare/public-qualified
    # names from .sql files).
    return ident.rsplit(".", 1)[-1]


def check_postgres_prefix(path: Path, text: str) -> list[Violation]:
    out = []
    for m in _PG_DOTTED_RE.finditer(text):
        out.append(Violation(
            path, _line_of(text, m.start()), "postgres-prefix",
            f"{m.group(0)}... is not ${{TID}}-prefixed (schema {m.group(1)})",
        ))
    # CREATE TABLE / INSERT INTO are real SQL DDL/DML only in .sql files -- in .tql,
    # `INSERT INTO <StreamName> SELECT ...` is Striim CQ stream-routing syntax with no
    # relation to a Postgres table, and would false-positive if scanned here.
    if path.suffix == ".sql":
        for m in _PG_BARE_CREATE_RE.finditer(text):
            if not _last_dotted_segment(m.group(1)).startswith(_TID_PREFIXES):
                out.append(Violation(path, _line_of(text, m.start()), "postgres-prefix",
                                      f"untokenized CREATE TABLE: {m.group(1)!r}"))
        for m in _PG_BARE_INSERT_RE.finditer(text):
            if not _last_dotted_segment(m.group(1)).startswith(_TID_PREFIXES):
                out.append(Violation(path, _line_of(text, m.start()), "postgres-prefix",
                                      f"untokenized INSERT INTO: {m.group(1)!r}"))
    return out


_SPANNER_TABLES_VALUE_RE = re.compile(r"Tables:\s*'([^']*)'")
_SPANNER_QUALIFIER_DOT_RE = re.compile(r"\.(?!\$\{TID\})[A-Za-z_]")
_PARENS_RE = re.compile(r"\([^()]*\)")
# GoogleSQL Tables: values are commonly bare (no db/schema qualifier at all, e.g.
# `Tables: 'src'`) -- the dotted regex above never matches those since it requires a
# literal "." before the table name.
_SPANNER_TABLES_BARE_RE = re.compile(r"Tables:\s*'(?!\$\{TID\})([A-Za-z_][A-Za-z0-9_]*)'")
_SPANNER_DDL_RE = re.compile(r"CREATE\s+TABLE\s+(?!\$\{TID\})([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)


# Inline, per-occurrence opt-out for a name that is provably NOT a resource this test
# creates, and so cannot be ${TID}-prefixed however much the rule would like it to be:
# a table name baked into a captured binary CDC fixture (a GG trail file records its
# own source table names), or a synthetic in-memory name an upstream OP stamps onto
# events. Neither can collide between parallel runs, because neither is ever created.
#
# Deliberately per-occurrence, not per-file: the file opting out of one Tables: value
# keeps every OTHER name in it -- including its real Spanner targets -- fully enforced.
# Write it on the line before, or at the end of the line that opens, the property:
#
#     -- isolation-exempt: spanner-prefix -- names live inside the trail fixture
#     Tables: 'NKM.ERCDBA.CM_CASES;...',
_EXEMPT_RE_CACHE: dict[str, re.Pattern] = {}


def _exempt_at(text: str, offset: int, rule: str) -> bool:
    pat = _EXEMPT_RE_CACHE.get(rule)
    if pat is None:
        pat = re.compile(r"isolation-exempt:\s*" + re.escape(rule) + r"\b")
        _EXEMPT_RE_CACHE[rule] = pat
    # The line the property opens on, plus the contiguous comment block directly above
    # it -- so the marker can head a multi-line justification instead of having to be
    # crammed onto the last line before the property.
    line_end = text.find("\n", offset)
    if pat.search(text[text.rfind("\n", 0, offset) + 1:
                       line_end if line_end != -1 else len(text)]):
        return True
    lines = text[:offset].split("\n")[:-1]
    for prev in reversed(lines):
        stripped = prev.strip()
        if not stripped.startswith("--"):
            break
        if pat.search(stripped):
            return True
    return False


def mask_comments(text: str, suffix: str) -> str:
    """Blank COMMENT TEXT ONLY, preserving every offset and newline (comment masking).

    Rules scan raw file text and report by offset, so comment characters are replaced with
    spaces rather than removed: `_line_of` keeps working and no reported line moves.

    It blanks the comment and NOTHING else. String contents are deliberately left intact,
    because the prefix rules look for table references INSIDE quoted scalars -- masking those
    would silently disable the rules this function exists to harden, which is worse than the
    false positives it removes and invisible to a green suite.

    The two formats are handled DIFFERENTLY, because measurement says they differ:

    SQL and TQL -- `--` comments, with `'` and `"` tracked so a `--` inside a string does not
    open one. That tracking is sound here: running this algorithm over the corpus leaves the
    quote state balanced in all 860 `.sql`/`.tql` files the gate masks (378 of those are the
    `scripts/live/regression` subtree; 143 of the 246 manifests point `source_dir` into
    `java/`). Without it a `--` inside a string truncates the rest of the line. MEASURED:
    zero corpus lines carry a `--` inside a quoted string, so this tracking changes nothing
    on the corpus and is insurance against the shape rather than a fix for a live defect.
    (The comment masker's row cited "57 corpus lines cut short"; that figure is the count of lines carrying
    a TRAILING `--` comment -- 6051 of them, in fact -- which a naive blanker truncates
    correctly, so it never supported the claim it was attached to.)

    `/* ... */` is NOT masked in either format, deliberately. All 7 occurrences in the
    corpus are Oracle optimizer hints (`/*+ LEADING(cc) ... */`), which are part of the
    statement rather than commentary: blanking them would delete text the prefix rules are
    meant to read. A genuine block comment carrying rule-tripping prose would still false-
    positive; no corpus file has one, and separating a hint from a comment needs more than
    the delimiter, so the cost is not paid until something needs it.

    YAML -- WHOLE-LINE `#` comments only, and NO quote tracking. Character-level quote parity
    is unsound on YAML: plain scalars contain apostrophes (`object's`, `can't`), and running
    the tracking version leaves 6 of 246 corpus manifests desynced, after which one stray
    apostrophe silently switches the rules off for the rest of the file -- both a real
    reference going unseen and a comment going unmasked were reproduced on real files. A
    trailing `# note` after a value is therefore NOT masked: deciding whether that `#` is a
    comment or scalar text needs a real YAML parse, and guessing it wrong disables a rule.
    Whole-line comments are the shape that actually reddened `main`, and they are decidable.
    Lines inside a block scalar (`|`, `>`) are skipped, because a `#` there is literal text.

    JSON and any other suffix are returned unchanged: they have no comment syntax this gate
    needs to mask.
    """
    if suffix in (".tql", ".sql"):
        return _mask_sql_comments(text)
    if suffix in (".yaml", ".yml"):
        return _mask_yaml_comments(text)
    return text                          # JSON and anything else: no comment syntax


def _mask_sql_comments(text: str) -> str:
    out = list(text)
    quote: str | None = None
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if quote is not None:
            if c == quote:
                quote = None
        elif c in ("'", '"'):
            quote = c
        elif text.startswith("--", i):
            j = text.find("\n", i)
            j = n if j == -1 else j
            for k in range(i, j):
                out[k] = " "
            i = j
            continue
        i += 1
    return "".join(out)


# A block-scalar header: `key: |`, `- key: >-`, or a bare sequence item `- |`. Captured in
# three parts -- indent, `- ` markers, optional key -- because the threshold block content
# must beat differs: the KEY's column when there is one (so a sequence item's sibling keys
# end the block) and the DASH's column when there is not (so the block's own first line is
# not blanked). A trailing comment after the indicator is legal YAML and must not defeat
# detection.
_BLOCK_SCALAR_RE = re.compile(r"^(\s*)((?:-\s+)*)([^:#\n]+:\s*)?[|>][-+0-9]*\s*(?:#.*)?$")


def _mask_yaml_comments(text: str) -> str:
    lines = text.split("\n")
    out = []
    block_indent: int | None = None
    for line in lines:
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if block_indent is not None:
            # Inside a block scalar while blank or indented past the KEY's column. A `#`
            # here is literal text, so the line is kept whole.
            if not stripped or indent > block_indent:
                out.append(line)
                continue
            block_indent = None
        # Comment check FIRST -- a BACKSTOP, and honestly labelled as one. The header
        # pattern excludes `#` from a key (`[^:#\n]+`), so a line whose first non-space
        # character is `#` cannot match it today and the order changes nothing: reordering
        # this is a surviving mutant, measured, not a killed one. The order is kept because
        # loosening that character class -- to allow a `#` inside a quoted key, say -- would
        # otherwise let `# purpose: |` open a phantom block that suppresses every
        # more-indented line after it, re-opening the trap this function exists to close.
        if stripped.startswith("#"):
            out.append(" " * len(line))
            continue
        m = _BLOCK_SCALAR_RE.match(line)
        if m:
            # Content must be indented past the block's PARENT node. With a key
            # (`- sql: |`) that is the key's own column, so sibling keys at the same
            # column correctly end the block. Without one (`- |`) the parent is the
            # sequence item itself, so the threshold is the dash's column -- using the
            # key's column there would blank the block's own first line.
            block_indent = len(m.group(1)) + (len(m.group(2)) if m.group(3) else 0)
            out.append(line)
            continue
        out.append(line)
    return "\n".join(out)


def _line_is_comment(text: str, offset: int) -> bool:
    # WHOLE-line comments only, deliberately: a trailing comment on a line of real DDL leaves
    # the DDL enforced (see test_spanner_prefix_still_flags_create_table_with_a_trailing_comment).
    return text[text.rfind("\n", 0, offset) + 1:offset].lstrip().startswith("--")


def _strip_parens(s: str) -> str:
    # ColumnMap(...) mapping expressions can reference nested JSON columns via dot
    # notation (e.g. `bug_json.items.subitems = JSON_ARRAY(@userdata(BUG_JSON))`) --
    # those dots are column/field references, not db.table qualifiers, so they must
    # not be scanned for ${TID} prefixing. Repeatedly strip innermost parens (handles
    # nesting like `JSON_ARRAY(@userdata(X))`) so only the qualifier portion remains.
    prev = None
    while prev != s:
        prev = s
        s = _PARENS_RE.sub("", s)
    return s


def check_spanner_prefix(path: Path, text: str) -> list[Violation]:
    out = []
    for tm in _SPANNER_TABLES_VALUE_RE.finditer(text):
        value = tm.group(1)
        if _exempt_at(text, tm.start(), "spanner-prefix"):
            continue
        stripped = _strip_parens(value)
        for m in _SPANNER_QUALIFIER_DOT_RE.finditer(stripped):
            out.append(Violation(path, _line_of(text, tm.start()), "spanner-prefix",
                                  f"untokenized Tables: value: {value!r}"))
            break   # one violation per Tables: value, not one per dot
    for m in _SPANNER_TABLES_BARE_RE.finditer(text):
        out.append(Violation(path, _line_of(text, m.start()), "spanner-prefix",
                              f"untokenized Tables: value: {m.group(0)!r}"))
    for m in _SPANNER_DDL_RE.finditer(text):
        # Skip a match sitting on a fully commented-out line: prose explaining why some
        # "CREATE TABLE fails" is not DDL. Only whole-line comments are skipped (a line
        # whose first non-space chars are --), never a trailing comment, so this cannot
        # hide a real statement -- and it deliberately does not strip -- inside quoted
        # values, where it is data rather than a comment.
        if _line_is_comment(text, m.start()):
            continue
        out.append(Violation(path, _line_of(text, m.start()), "spanner-prefix",
                              f"untokenized CREATE TABLE: {m.group(1)!r}"))
    return out


_FIXED_NAME_RE = re.compile(r"\b(slt[_-](?:src|tgt))(?![-_A-Za-z0-9])")


def check_no_literal_fixed_names(path: Path, text: str) -> list[Violation]:
    # BOTH kafka (slt_<tid>_src) and gcs (slt-<tid>-src) now interpose the tid BETWEEN
    # slt and src/tgt, so the literal fixed substring never occurs in a derived name.
    # The negative lookahead (which once earned its keep for gcs's old suffix form,
    # slt-src-<tid>) still excludes anything immediately followed by another identifier
    # char/hyphen/underscore -- only a truly bare, standalone
    # "slt_src"/"slt_tgt"/"slt-src"/"slt-tgt" token is flagged.
    out = []
    for m in _FIXED_NAME_RE.finditer(text):
        literal = m.group(1)
        out.append(Violation(path, _line_of(text, m.start()), "literal-fixed-name",
                              f"literal fixed shared-object name: {literal!r}"))
    return out


def check_slug_length(slug: str) -> list[Violation]:
    schema = f"slt_{slug}"
    if len(schema) > 63:
        return [Violation(Path(slug), 0, "slug-length",
                           f"postgres schema {schema!r} is {len(schema)} chars (limit 63)")]
    return []


_LOAD_OP_RE = re.compile(r"(?<!UN)LOAD OPEN PROCESSOR", re.IGNORECASE)


#: Directory names the isolation walker SKIPS. Defined here rather than in the gate so the
#: confinement checks below and the walker itself cannot drift apart -- two copies of this set is
#: how a gate stops being real.
WALKER_EXEMPT_DIR_PARTS = frozenset({"expected", "docs"})
WALKER_EXEMPT_NAMES = frozenset({"README.md", "fixture.json"})
WALKER_SUFFIXES = frozenset({".sql", ".tql", ".yaml", ".yml", ".json"})


def files_to_scan(man, manifest_path: Path, root: Path) -> list[Path]:
    """Every file the isolation gate reads for one manifest: `source_dir` walked, the manifest
    itself, and each `local: true` ddl/seed/upload file, which lives in the case dir and so is
    outside an `example:` case's `source_dir`. Exemptions are judged on the path relative to
    `root` (the repo), so a corpus directory named `docs` above the case does not count."""
    extra = [Path(manifest_path)] + [man.file_path(n) for n in sorted(man.local_files)]
    files = []
    for p in list(Path(man.source_dir).rglob("*")) + extra:
        if not p.is_file():
            continue
        if WALKER_EXEMPT_DIR_PARTS & set(p.relative_to(root).parts):
            continue
        if p.name in WALKER_EXEMPT_NAMES or p.suffix not in WALKER_SUFFIXES:
            continue
        files.append(p)
    return files


def _confinement_violations(path: Path, key: str, value: str, rule: str) -> list[Violation]:
    """The two ways a path can name a file the isolation walker never reads.

    Shared by `tql:` and `example:` because the hazard is one hazard: the walker rglobs
    `source_dir` and then SKIPS any file with an exempt directory in its path, so a value that
    escapes the first or lands inside the second is unscanned -- and an unscanned app file can
    carry a real `LOAD OPEN PROCESSOR` past a documented hard gate while the gate reports clean.
    """
    out = []
    parts = Path(value).parts
    if Path(value).is_absolute():
        out.append(Violation(path, 0, rule,
                             f"'{key}: {value}' is an ABSOLUTE path. It is resolved against the "
                             f"repo root, so an absolute value silently escapes it -- and nothing "
                             f"the isolation walker scans lives outside the repo."))
    if ".." in parts:
        out.append(Violation(path, 0, rule,
                             f"'{key}: {value}' reaches outside source_dir, which is the only "
                             f"directory the isolation walker walks"))
    hit = WALKER_EXEMPT_DIR_PARTS & set(parts)
    if hit:
        out.append(Violation(path, 0, rule,
                             f"'{key}: {value}' lies under {sorted(hit)!r}, which the isolation "
                             f"walker SKIPS -- so every file there is unscanned and the gate "
                             f"would report clean without having read the app at all."))
    return out


def check_tql_is_scannable(path: Path, tql: str) -> list[Violation]:
    """Rule 8's companion: the manifest's `tql:` must name a file rule 8 will actually scan.

    Scoping rule 8 to `.tql` means any other suffix has the app file skipped -- and before
    that scoping the walker read `.sql`/`.yaml`/`.json`, so `tql: app.sql` carrying a real
    statement WOULD have been caught. `..` is refused for a second reason: the walker rglobs
    `source_dir` only, so a `tql:` reaching outside it is never walked whatever its suffix.

    Two other routes can bypass the same gate, which the suffix and `..` checks did not
    cover: an ABSOLUTE path, and a path under a directory the walker EXEMPTS. `tql: docs/app.tql`
    ends in `.tql`, contains no `..`, is relative -- and is never read.

    Takes the raw `tql` string rather than a manifest so it can be exercised directly. It is
    called from the gate's own loop and from `new-live-test.py`'s authoring lint, so a test
    authored with a bad value is refused when it is written rather than in CI.
    """
    out = []
    if not tql.endswith(".tql"):
        out.append(Violation(path, 0, "in-tql-load",
                             f"'tql: {tql}' does not end in .tql, so rule in-tql-load "
                             f"would not scan the app file it names"))
    out += _confinement_violations(path, "tql", tql, "in-tql-load")
    return out


def check_example_is_scannable(path: Path, example: str | None) -> list[Violation]:
    """`example:` REPLACES source_dir, so an unscannable value costs more than `tql:` does.

    `manifest.py` resolves `source_dir = _REPO / example`, and the gate walks exactly that
    directory. So where a bad `tql:` hides ONE file, a bad `example:` hides the whole tree: an
    absolute value leaves the repo, a `..` climbs out of it, and a value under `docs/` or
    `expected/` is walked and then discarded file by file. In every case the gate iterates, finds
    nothing to check, and passes.

    This is a latent risk -- no corpus manifest does any of this. The point is
    that nothing stopped one, and the value of a hard gate is exactly that it cannot be stepped
    around by a config choice that looks ordinary.
    """
    if example is None:
        return []
    return _confinement_violations(path, "example", example, "in-tql-load")


def check_no_load_open_processor(path: Path, text: str) -> list[Violation]:
    """Rule 8: no in-TQL `LOAD OPEN PROCESSOR` (registration is runner-side post-§B.2).

    Scoped to `.tql`, which is the only thing shipped to the server AS TQL: `plugin.py` posts
    `m.tql` and nothing else, `.sql` goes to a DB admin over JDBC, and no manifest key carries
    inline TQL. A test.yaml therefore never carries an executable statement, and neither does
    a .sql, which is DDL. Scanning them could only ever produce false positives, and did:
    `statusreader-multi-node-cluster` went red over a YAML comment explaining why the case
    needs `on_agent`, while its app.tql was clean. Prose about the statement is not the
    statement, in a comment or in a `purpose:` value.

    Within a .tql, WHOLE-line `--` comments are skipped, matching the spanner rule and its
    helper. Two consequences, both deliberate:
      - A whole-line commented-out statement is skipped rather than reported as strippable.
        It registers nothing, so the rule's purpose holds.
      - What a TRAILING comment does depends on the CALLER, because `_line_is_comment` can
        only ever test whether a line STARTS with `--`. Raw text in, and `-- LOAD OPEN
        PROCESSOR` after a real statement still flags (pinned by
        test_no_load_open_processor_still_flags_a_statement_with_a_trailing_comment). Masked
        text in and it does not, because the mask blanked it first. Both production callers
        -- the gate and new-live-test.py's authoring lint -- mask comments, so in practice
        prose about the statement is ignored wherever it sits on the line.

    The scoping needs `tql:` to NAME a `.tql`, or the app file is skipped -- and before the
    scoping the walker did scan `.sql`/`.yaml`/`.json`, so `tql: app.sql` carrying a real
    statement would have been caught. That is asserted in the gate itself
    (`test_isolation_enforcement`, which already loads every manifest) rather than left as an
    observation about the corpus. It is deliberately NOT a `manifest.py` schema rule:
    `load_manifest` runs on fixtures outside the repo, and a path-shaped check there breaks
    them -- three attempts proved it.

    STILL NOT ENFORCED: `tql:` may name a subdirectory. It cannot escape the scan while
    `source_dir` is a corpus test dir, and `new-live-test.py`'s authoring lint covers
    confinement when a test is written; a loader-side guarantee is a follow-up.
    """
    if path.suffix != ".tql":
        return []
    out = []
    for m in _LOAD_OP_RE.finditer(text):
        # _line_is_comment is kept for callers that pass RAW text; when the caller has
        # already masked (the gate masks them), a comment cannot match here at all, and a
        # TRAILING comment on a real statement line is now excluded too -- which the
        # whole-line-only test below deliberately did not do.
        if _line_is_comment(text, m.start()):
            continue
        out.append(Violation(path, _line_of(text, m.start()), "in-tql-load",
                              "TQL contains LOAD OPEN PROCESSOR (should be stripped post-§B.2)"))
    return out


def check_file_paths_tokenized(path: Path, text: str) -> list[Violation]:
    doc = yaml.safe_load(text) or {}
    out = []
    for f in (doc.get("assert") or {}).get("file") or []:
        p = f.get("path", "")
        if "${NS}" not in p and "${TID}" not in p:
            out.append(Violation(path, 0, "file-path-untokenized",
                                  f"assert.file path lacks ${{NS}}/${{TID}}: {p!r}"))
    for sf in doc.get("server_files") or []:
        # A `load:` entry's dest is the uploaded jar's NAME, not a server path (see
        # manifest._normalize_server_files): the jar is a cluster-global registration,
        # like an op:/udf: module, so there is no per-test path to tokenize.
        if sf.get("load"):
            continue
        d = sf.get("dest", "")
        if "${NS}" not in d and "${TID}" not in d:
            out.append(Violation(path, 0, "file-path-untokenized",
                                  f"server_files dest lacks ${{NS}}/${{TID}}: {d!r}"))
    return out


_CONFIGFILE_RE = re.compile(r"ConfigFile:\s*'UploadedFiles/([^']+)'")


def check_config_file_upload_tokenized(path: Path, text: str, renamed_froms=frozenset()) -> list[Violation]:
    # renamed_froms: source paths covered by an op:/udf: upload: {from, to} entry.
    # upload_op_uploads keys its rewrite map by the source basename, even for a nested
    # local source. A nested server path is not rewritten and must not be exempted.
    # (manifest._normalize_uploads) -- the runner rewrites "UploadedFiles/<from>" to the real
    # per-test <to> name before deploy (plugin._rendered_tql), so a literal, untokenized
    # reference to one of these is fine (and expected -- it's what lets a shipped
    # example keep a clean, customer-realistic ConfigFile line).
    out = []
    renamed_basenames = {Path(source).name for source in renamed_froms}
    for m in _CONFIGFILE_RE.finditer(text):
        name = m.group(1)
        if name.startswith("${") or "${TID}" in name or name in renamed_basenames:
            continue
        out.append(Violation(path, _line_of(text, m.start()), "configfile-untokenized",
                              f"ConfigFile UploadedFiles/{name} lacks ${{TID}} prefix"))
    return out
