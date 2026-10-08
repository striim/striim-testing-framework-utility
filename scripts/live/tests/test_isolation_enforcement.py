"""Isolation-enforcement checker for the parallel-live-tests migration (spec §A.6).

HARD GATE (all 8 rules). The corpus is fully migrated (Phase 1, spec §E) and rule 8
(in-tql-load) is clean now that Recipe L stripped every in-TQL `LOAD OPEN PROCESSOR`
(runner-side registration). Any reintroduced violation fails the build.
Rule 8 is scoped to `.tql` -- the only thing shipped to the server AS TQL -- and within one
it skips WHOLE-line `--` comments, so prose about the statement does not count as the
statement, and a trailing comment on a real one does not either -- the gate masks
comments before calling the rule. That scoping needs every manifest's `tql:` to name a
`.tql`, or the app file goes unscanned -- asserted in the loop below, since this gate already
loads every manifest.
Run directly for a grouped summary: `pytest scripts/live/tests/test_isolation_enforcement.py -s`.
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from livetest.enforcement import (
    check_config_file_upload_tokenized,
    check_file_paths_tokenized,
    check_mssql_prefix,
    check_teradata_prefix,
    check_vertica_prefix,
    check_no_literal_fixed_names,
    check_no_load_open_processor,
    mask_comments,
    check_oracle_prefix,
    check_postgres_prefix,
    check_slug_length,
    check_spanner_prefix,
    check_tql_is_scannable,
    check_example_is_scannable,
    files_to_scan,
)
from livetest.manifest import load_manifest

REPO = Path(__file__).resolve().parents[3]
REGRESSION = REPO / "scripts" / "live" / "regression"

# Exemptions (spec §A.6): golden expected/ content, testkit fixture.json, README.md, docs/.
# The walker is livetest.enforcement.files_to_scan, beside the confinement checks that refuse a
# `tql:`/`example:`/local file landing in an exempt dir, so the check and the walker agree by
# construction -- two copies drifting apart is how the gate would stop being real while passing.


def _all_test_yamls() -> list[Path]:
    return sorted(REGRESSION.glob("**/test.yaml"))


def _slug(name: str) -> str:
    # mirrors plugin.py's _slug() EXACTLY (plugin.py:299-300) -- case-PRESERVED,
    # non-alnum runs -> single underscore, trimmed. ${TID} is defined in the spec as
    # exactly _slug(m.name); do not lowercase here.
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")


def test_every_referenced_server_file_exists():
    """A `server_files` entry names a file committed next to the manifest. If it is missing,
    nothing catches it until a live deploy fails minutes later — which is exactly how
    `framework-server-files` shipped referencing two CSVs that were never committed.

    `plugin.py` resolves the source as `manifest.source_dir / file`, so that is what is checked
    here. This is the cheap half of a check the live tier was paying for at deploy speed.
    """
    missing = []
    for ty in _all_test_yamls():
        man = load_manifest(ty)
        # A `disabled:` test does not run, and the two that exist declare their fixtures as
        # living outside git on purpose (`ggtrailfiles/` is gitignored, with its own README).
        # Failing them here would make the check unrunnable rather than useful.
        if getattr(man, "disabled", None):
            continue
        for entry in getattr(man, "server_files", []):
            src = entry[0] if isinstance(entry, (tuple, list)) else entry
            if not (man.source_dir / src).is_file():
                missing.append("%s: server_files names %r, absent from %s"
                               % (ty.relative_to(REGRESSION), src, man.source_dir.name))
    assert not missing, (
        "server_files entries name files that are not on disk:\n  " + "\n  ".join(missing))


def test_isolation_enforcement():
    violations = []
    exempted: list[str] = []
    for ty in _all_test_yamls():
        man = load_manifest(ty)
        requires = set(man.requires)
        slug = _slug(man.name)

        violations += check_tql_is_scannable(ty, man.tql)
        # `example:` REPLACES source_dir, so where a bad `tql:` hides one file a bad
        # `example:` hides the whole tree -- the walker would iterate, discard every file, and
        # the gate would pass having read nothing. Asserted HERE, in the loop that already loads
        # every manifest, because the three loader-side shapes all failed: a path check in
        # load_manifest breaks every tmp_path fixture, an absolute-path check breaks any clone
        # under a docs/ ancestor, and is_absolute() alone misses `../../x`.
        violations += check_example_is_scannable(ty, getattr(man, "example", None))

        violations += check_slug_length(slug)

        # The four DB-prefix rules exist to keep CONCURRENT tests off each other's objects:
        # ${TID}/${TID_ORACLE} give every test its own table names. A test that declares
        # `disabled_parallel` has opted out of concurrency entirely -- the runner skips it
        # under SLT_PARALLEL/xdist -- so prefixing buys it nothing, and some tests need
        # fixed names on purpose (a suite of such tests shares one SEED_* staging set
        # across the suite, which per-test prefixes would make impossible).
        #
        # Only the prefix rules are relaxed. Slug length, literal fixed names, in-TQL
        # LOAD OPEN PROCESSOR, config-upload tokenization and file-path tokenization still
        # apply to every test, parallel or not.
        # `disabled_parallel` => this test never runs concurrently, so the isolation
        # rules that exist purely to separate concurrent runs do not apply to it.
        prefix_exempt = bool(man.disabled_parallel)
        if prefix_exempt:
            exempted.append(man.name)

        files = files_to_scan(man, ty, REPO)
        for f in files:
            raw = f.read_text(errors="replace")
            # rule 8, three of the four DB-prefix rules and the literal-fixed-name
            # rule scan COMMENT-MASKED text, so prose in a comment cannot trip them --
            # the trap that reddened main once already. The
            # mask blanks comment characters only and preserves every offset, so reported
            # line numbers are unchanged. Rules that must see comments (config-upload
            # tokenization) keep the raw text.
            text = mask_comments(raw, f.suffix)
            if "oracle" in requires and not prefix_exempt:
                violations += check_oracle_prefix(f, text)
            if "mssql" in requires and not prefix_exempt:
                violations += check_mssql_prefix(f, text)
            if "teradata" in requires and not prefix_exempt:
                violations += check_teradata_prefix(f, text)
            if "vertica" in requires and not prefix_exempt:
                violations += check_vertica_prefix(f, text)
            if "spanner" in requires and not prefix_exempt:
                # RAW, not masked: check_spanner_prefix reads `-- isolation-exempt:`
                # markers OUT OF COMMENTS, which masking would blank -- silently
                # disabling a documented opt-out. It does not affect comment masking.
                violations += check_spanner_prefix(f, raw)
            if "postgres" in requires and not prefix_exempt:
                violations += check_postgres_prefix(f, text)
            violations += check_no_literal_fixed_names(f, text)
            violations += check_no_load_open_processor(f, text)
            if man.op_uploads and (f.suffix == ".tql" or f.name == "app.tql"):
                renamed_froms = {u["from"] for u in man.op_uploads if u["to"] is not None}
                violations += check_config_file_upload_tokenized(f, raw, renamed_froms)

        # rule 5 needs the raw test.yaml text (YAML-aware). Exempt for the same reason as
        # the DB-prefix rules: it keeps concurrent tests off each other's server-side files,
        # and a disabled_parallel test may share them deliberately -- such a suite
        # points every member at one /tmp/cdc-gg-trail copy of a 500MB trail, and at one
        # shared scm-striim-udfs jar (per-test copies of the same classes make Striim refuse
        # the second with "Above mentioned classes conflict with a previous Jar loaded").
        if not prefix_exempt:
            violations += check_file_paths_tokenized(ty, ty.read_text())

    by_rule = defaultdict(list)
    for v in violations:
        by_rule[v.rule].append(v)

    def _display(p: Path) -> str:
        try:
            return str(p.relative_to(REPO))
        except ValueError:
            return str(p)  # e.g. slug-length violations, whose "file" is a bare slug

    lines = [f"\n{len(violations)} isolation-enforcement violations (all 8 rules must be clean):"]
    for rule, vs in sorted(by_rule.items()):
        lines.append(f"  {rule}: {len(vs)}")
        for v in vs[:5]:
            lines.append(f"    {_display(v.file)}:{v.line}: {v.message}")
        if len(vs) > 5:
            lines.append(f"    ... and {len(vs) - 5} more")
    # Surface the exemptions so a growing list is visible rather than silent -- a test
    # added with `disabled_parallel` to dodge these rules should be an obvious choice.
    if exempted:
        lines.append(f"  ({len(exempted)} test(s) exempt from the DB-prefix rules via "
                     f"disabled_parallel: {', '.join(sorted(exempted))})")
    summary = "\n".join(lines)
    assert not violations, summary


# ---- comment masking -----------------------------------------------------
# Rule 8 skipped WHOLE-line comments only, so a trailing comment on a real statement line
# still flagged; and check_oracle_prefix / _mssql_ / _postgres_ / _no_literal_fixed_names
# had no comment handling at all while running over test.yaml, leaving the prose-in-a-comment
# trap that reddened main latent in four more rules.

def test_mask_preserves_every_offset_so_line_numbers_do_not_move():
    # The mask blanks comment characters rather than removing them. Rules report by offset,
    # so a mask that shortened the text would silently move every reported line number.
    for suffix, text in ((".tql", "a; -- c\nb;\n"), (".yaml", 'a: 1  # c\nb: 2\n')):
        assert len(mask_comments(text, suffix)) == len(text)
        assert mask_comments(text, suffix).count("\n") == text.count("\n")


def test_rule8_still_flags_a_real_statement():
    assert check_no_load_open_processor(
        Path("a.tql"), mask_comments("LOAD OPEN PROCESSOR Foo;", ".tql"))


def test_rule8_ignores_a_TRAILING_comment_on_a_real_line():
    # _line_is_comment alone could not do this -- it tests only whether the
    # line STARTS with `--`.
    assert not check_no_load_open_processor(
        Path("a.tql"), mask_comments("CREATE SOURCE s; -- LOAD OPEN PROCESSOR Foo;", ".tql"))


def test_masking_does_not_truncate_a_line_whose_QUOTED_SCALAR_contains_dashes():
    # Quotes are tracked so a `--` inside a string does not open a comment and truncate the
    # rest of the line. INSURANCE, not a fix: zero corpus lines have that shape today. (The comment masker's
    # row blamed "57 corpus lines cut short" for leaving rule 8 comment-blind; that figure
    # counted lines with a TRAILING `--` comment, which a naive blanker handles correctly.)
    masked = mask_comments("Tables: 'A--B', -- real comment", ".tql")
    assert "'A--B'" in masked, masked


def test_prefix_rules_ignore_a_reference_inside_a_YAML_COMMENT():
    assert not check_oracle_prefix(
        Path("t.yaml"), mask_comments("# ${ORACLE_SOURCE_SCHEMA}.ORDERS", ".yaml"))


def test_prefix_rules_STILL_SEE_a_reference_inside_a_QUOTED_SCALAR():
    # The failure mode to avoid: masking string CONTENT as well as comments would silently
    # disable the very rules this change hardens, since real references live in scalars.
    assert check_oracle_prefix(
        Path("t.yaml"), mask_comments('sql: "${ORACLE_SOURCE_SCHEMA}.ORDERS"', ".yaml"))


def test_a_hash_inside_a_yaml_scalar_is_not_a_comment():
    assert mask_comments("name: a#b", ".yaml") == "name: a#b"


def test_json_has_no_comment_syntax_and_is_returned_unchanged():
    text = '{"a": "-- not a comment", "b": "# nor this"}'
    assert mask_comments(text, ".json") == text


# ---- the five ways masking can silently disable a rule -------
# Each of these was a real regression found by review. A rule that stops catching a
# genuine violation is worse than a false positive and invisible to a green suite.

def test_yaml_masking_survives_an_APOSTROPHE_in_plain_prose():
    # Character-level quote parity is unsound on YAML: plain scalars contain apostrophes
    # ("object's", "can't"), and 6 of 246 corpus manifests desynced under it. After a
    # desync one stray apostrophe switches the rules off for the REST OF THE FILE.
    text = "purpose: the object's rows\nsql: \"${ORACLE_SOURCE_SCHEMA}.ORDERS\"\n"
    assert check_oracle_prefix(Path("t.yaml"), mask_comments(text, ".yaml"))


def test_yaml_masking_still_masks_a_comment_AFTER_an_apostrophe():
    # The other direction of the same desync: the comment stopped being masked.
    text = "purpose: the object's rows\n# ${ORACLE_SOURCE_SCHEMA}.ORDERS\n"
    assert not check_oracle_prefix(Path("t.yaml"), mask_comments(text, ".yaml"))


def test_a_hash_inside_a_YAML_BLOCK_SCALAR_is_literal_text():
    # `#` inside `|` or `>` is content, not a comment. Only 1 of 246 corpus manifests uses a
    # block scalar, so this is insurance. The content line must START with `#`
    # or the whole-line rule never applies and block detection is never consulted -- the
    # first version of this test used a line starting with `--`, so mutating the header
    # pattern to never match left the suite green.
    for marker in ("|", ">", ">-", "|+", "|2"):
        text = f"sql: {marker}\n  # ${{ORACLE_SOURCE_SCHEMA}}.ORDERS\n"
        assert check_oracle_prefix(Path("t.yaml"), mask_comments(text, ".yaml")), marker


def test_a_DOUBLE_quoted_string_in_tql_does_not_open_a_comment():
    # Only `'` was tracked, so `--` inside a double-quoted string truncated the line --
    # the very truncation the docstring claimed to have solved.
    text = 'Tables: "A--B ${ORACLE_SOURCE_SCHEMA}.ORDERS"'
    assert check_oracle_prefix(Path("a.tql"), mask_comments(text, ".tql"))


def test_spanner_prefix_keeps_its_isolation_exempt_opt_out():
    # check_spanner_prefix reads `-- isolation-exempt:` markers OUT OF COMMENTS, so masking
    # its input silently kills a documented escape hatch. It does not affect comment masking,
    # so the gate passes it RAW. This test pins both halves of that.
    text = "-- isolation-exempt: spanner-prefix -- fixture\nTables: 'NKM.ERCDBA.CM_CASES',\n"
    assert not check_spanner_prefix(Path("a.tql"), text), "raw: the marker must exempt"
    assert check_spanner_prefix(Path("a.tql"), mask_comments(text, ".tql")), \
        "masked: the marker is blanked, which is why the gate must not mask this rule"


def test_a_comment_ending_in_a_block_indicator_does_not_open_a_PHANTOM_block():
    # `# purpose: |` must not open a block that suppresses every more-indented line after it.
    # Two things prevent it and only one is load-bearing: the header pattern excludes `#`
    # from a key, so such a line never matches; the comment-check-first ordering is a
    # backstop. Reordering is a SURVIVING mutant -- this test would still pass -- so it pins
    # the OUTCOME, not the mechanism.
    text = "  # purpose: |\n    # ${ORACLE_SOURCE_SCHEMA}.ORDERS\n"
    assert not check_oracle_prefix(Path("t.yaml"), mask_comments(text, ".yaml"))


def test_a_BARE_SEQUENCE_ITEM_block_scalar_keeps_its_content():
    # `- |` has no key, so the block's parent is the sequence item and the threshold is the
    # DASH's column. Using the key's column blanked the block's own first line.
    text = "steps:\n  - |\n    # ${ORACLE_SOURCE_SCHEMA}.ORDERS\n"
    assert check_oracle_prefix(Path("t.yaml"), mask_comments(text, ".yaml"))


def test_a_SIBLING_KEY_ends_a_sequence_item_block_scalar():
    # With `- sql: |` the threshold is the KEY's column, so a sibling at that column exits
    # the block. Using the line's indent left every later line inside it forever.
    text = "steps:\n  - sql: |\n      SELECT 1\n    name: x\n    # ${ORACLE_SOURCE_SCHEMA}.ORDERS\n"
    assert not check_oracle_prefix(Path("t.yaml"), mask_comments(text, ".yaml"))


def test_a_block_header_with_a_TRAILING_COMMENT_is_still_detected():
    # `sql: | # why` is legal YAML; requiring end-of-line after the indicator missed it and
    # blanked the block's content.
    text = "sql: | # why\n  # ${ORACLE_SOURCE_SCHEMA}.ORDERS\n"
    assert check_oracle_prefix(Path("t.yaml"), mask_comments(text, ".yaml"))


def test_an_INDENTED_whole_line_yaml_comment_is_masked():
    # 107 of the corpus's whole-line YAML comments are indented. `line.startswith("#")`
    # instead of `stripped.startswith("#")` survived the whole suite until this.
    text = "key: 1\n    # ${ORACLE_SOURCE_SCHEMA}.ORDERS\n"
    assert not check_oracle_prefix(Path("t.yaml"), mask_comments(text, ".yaml"))


def test_an_ORACLE_OPTIMIZER_HINT_is_not_masked():
    # `/*+ ... */` is part of the statement, not commentary, and all 7 block-delimited spans
    # in the corpus are hints. Masking `/* */` would delete text the prefix rules must read.
    text = "SELECT /*+ INDEX(cc CM_CASES_IDX1) */ 1 FROM ${ORACLE_SOURCE_SCHEMA}.ORDERS"
    assert "/*+ INDEX(cc CM_CASES_IDX1) */" in mask_comments(text, ".sql")
    assert check_oracle_prefix(Path("a.sql"), mask_comments(text, ".sql"))


def test_json_is_untouched_even_when_it_contains_SQL_comment_syntax():
    # Pins the JSON branch against falling through to the SQL masker. Discriminating here is
    # harder than it looks: in VALID JSON every `--` sits inside a double-quoted string, and
    # the SQL masker tracks `"`, so on ordinary JSON the two branches produce identical text
    # and a mutant that deletes the branch survives. Two fixtures did survive it. What
    # separates them is the BACKSLASH ESCAPE, which the SQL masker does not model: `\"` reads
    # as a closing quote, so from there the masker believes it is outside a string and blanks
    # the rest of the line -- taking a real table reference with it.
    text = '{"len": "3\\" pipe -- ${ORACLE_SOURCE_SCHEMA}.ORDERS"}'
    assert mask_comments(text, ".json") == text
    assert "${ORACLE_SOURCE_SCHEMA}" not in mask_comments(text, ".sql"), \
        "fixture no longer discriminates: the SQL masker leaves it alone too"
    assert check_oracle_prefix(Path("t.json"), mask_comments(text, ".json"))
