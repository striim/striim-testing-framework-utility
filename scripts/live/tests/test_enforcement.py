from __future__ import annotations
from pathlib import Path

from livetest.enforcement import (
    check_config_file_upload_tokenized,
    check_file_paths_tokenized,
    check_mssql_prefix,
    check_teradata_prefix,
    check_vertica_prefix,
    check_no_literal_fixed_names,
    check_no_load_open_processor,
    check_tql_is_scannable,
    check_example_is_scannable,
    WALKER_EXEMPT_DIR_PARTS,
    check_oracle_prefix,
    check_postgres_prefix,
    check_slug_length,
    check_spanner_prefix,
)


def test_oracle_prefix_flags_untokenized_table():
    text = "CREATE TABLE QASOURCE.SRC (id NUMBER);\n"
    v = check_oracle_prefix(Path("ddl.sql"), text)
    assert len(v) == 1
    assert v[0].line == 1
    assert v[0].rule == "oracle-prefix"


def test_oracle_prefix_allows_tokenized_table():
    text = "CREATE TABLE QASOURCE.${TID}SRC (id NUMBER);\n"
    assert check_oracle_prefix(Path("ddl.sql"), text) == []


def test_oracle_prefix_flags_qatarget_too():
    text = "target: QATARGET.TGT\n"
    v = check_oracle_prefix(Path("test.yaml"), text)
    assert len(v) == 1


def test_oracle_prefix_flags_udf_string_arg():
    text = "WAReplaceBeforeFromData(s, 'QASOURCE.WAMUT_D2B')\n"
    v = check_oracle_prefix(Path("app.tql"), text)
    assert len(v) == 1


def test_oracle_prefix_flags_untokenized_schema_token_form():
    # Post credential-normalization, the corpus uses ${ORACLE_SOURCE_SCHEMA}/
    # ${ORACLE_TARGET_SCHEMA} tokens instead of literal QASOURCE/QATARGET.
    text = "CREATE TABLE ${ORACLE_SOURCE_SCHEMA}.SRC (id NUMBER);\n"
    v = check_oracle_prefix(Path("ddl.sql"), text)
    assert len(v) == 1


def test_oracle_prefix_allows_tokenized_schema_token_form():
    text = "CREATE TABLE ${ORACLE_TARGET_SCHEMA}.${TID}SRC (id NUMBER);\n"
    assert check_oracle_prefix(Path("ddl.sql"), text) == []


def test_mssql_prefix_flags_untokenized_dbo():
    text = "CREATE TABLE dbo.SRC (id INT);\n"
    v = check_mssql_prefix(Path("ddl.sql"), text)
    assert len(v) == 1


def test_mssql_prefix_allows_tokenized_dbo():
    text = "CREATE TABLE dbo.${TID}SRC (id INT);\n"
    assert check_mssql_prefix(Path("ddl.sql"), text) == []


def test_mssql_prefix_flags_cdc_source_name():
    text = "EXEC sys.sp_cdc_enable_table @source_name = N'SRC';\n"
    v = check_mssql_prefix(Path("ddl.sql"), text)
    assert len(v) == 1


def test_mssql_prefix_flags_untokenized_schema_token_form():
    # Post credential-normalization, the corpus uses ${MSSQL_SOURCE_SCHEMA}/
    # ${MSSQL_TARGET_SCHEMA} tokens instead of literal dbo.
    text = "CREATE TABLE ${MSSQL_SOURCE_SCHEMA}.SRC (id INT);\n"
    v = check_mssql_prefix(Path("ddl.sql"), text)
    assert len(v) == 1


def test_mssql_prefix_allows_tokenized_schema_token_form():
    text = "CREATE TABLE ${MSSQL_TARGET_SCHEMA}.${TID}SRC (id INT);\n"
    assert check_mssql_prefix(Path("ddl.sql"), text) == []


def test_spanner_prefix_flags_untokenized_tables_prop():
    text = "Tables: 'gsql.emp'\n"
    v = check_spanner_prefix(Path("app.tql"), text)
    assert len(v) == 1


def test_spanner_prefix_flags_create_table():
    text = "CREATE TABLE bug_table (id INT64) PRIMARY KEY (id);\n"
    v = check_spanner_prefix(Path("spanner_ddl.sql"), text)
    assert len(v) == 1


def test_spanner_prefix_allows_tokenized():
    text = "Tables: 'gsql.${TID}emp'\nCREATE TABLE ${TID}bug_table (id INT64);\n"
    assert check_spanner_prefix(Path("app.tql"), text) == []


def test_spanner_prefix_flags_untokenized_bare_tables_prop():
    # GoogleSQL Tables: values are commonly bare (no db/schema qualifier), e.g. 'src' --
    # the dotted-only regex never matched these.
    text = "Tables: 'src'\n"
    v = check_spanner_prefix(Path("app.tql"), text)
    assert len(v) == 1


def test_spanner_prefix_allows_tokenized_bare_tables_prop():
    text = "Tables: '${TID}src'\n"
    assert check_spanner_prefix(Path("app.tql"), text) == []


def test_spanner_prefix_does_not_double_count_dotted_value():
    # A dotted, untokenized value must be flagged exactly once (by the dotted regex),
    # not also matched by the bare-name regex.
    text = "Tables: 'gsql.emp'\n"
    v = check_spanner_prefix(Path("app.tql"), text)
    assert len(v) == 1


def test_spanner_prefix_ignores_columnmap_json_path_dots():
    # ColumnMap(...) mapping expressions reference nested JSON columns via dot notation
    # (e.g. bug_json.items) -- those are column/field references, not db.table
    # qualifiers, and must not be flagged even though the qualifier segment before
    # ColumnMap( is already correctly tokenized.
    text = (
        "Tables: 'FREEPDB1.${ORACLE_SOURCE_SCHEMA}.${TID_ORACLE}BUG_TABLE,"
        "${SPANNER_GSQL_DB}.${TID}bug_table ColumnMap(\n"
        "    bug_id = BUG_ID,\n"
        "    bug_json.items = JSON_ARRAY(@userdata(BUG_JSON))\n"
        "  )'\n"
    )
    assert check_spanner_prefix(Path("app.tql"), text) == []


def test_spanner_prefix_still_flags_untokenized_qualifier_alongside_columnmap():
    # A genuinely untokenized qualifier outside ColumnMap(...) must still be caught,
    # even when the same Tables: value also contains JSON-path dot notation inside
    # ColumnMap(...).
    text = (
        "Tables: 'FREEPDB1.${ORACLE_SOURCE_SCHEMA}.BUG_TABLE,"
        "${SPANNER_GSQL_DB}.${TID}bug_table ColumnMap(\n"
        "    bug_json.items = JSON_ARRAY(@userdata(BUG_JSON))\n"
        "  )'\n"
    )
    v = check_spanner_prefix(Path("app.tql"), text)
    assert len(v) == 1


def test_spanner_prefix_honors_inline_exemption_marker():
    # A name that is never CREATED -- here, table names recorded inside a captured CDC
    # fixture -- cannot be ${TID}-prefixed, so an inline marker opts that one value out.
    text = (
        "  -- isolation-exempt: spanner-prefix -- names live inside the trail fixture\n"
        "  Tables: 'NKM.ERCDBA.CM_CASES;NKM.ERCDBA.CM_BRM_CASE_DATA',\n"
    )
    assert check_spanner_prefix(Path("app.tql"), text) == []


def test_spanner_prefix_exemption_marker_may_head_a_comment_block():
    # The marker heads a multi-line justification rather than being crammed onto the
    # last line before the property.
    text = (
        "  -- isolation-exempt: spanner-prefix -- synthetic names stamped by an OP;\n"
        "  -- no such table is ever created, so only the tokenized target is real.\n"
        "  Tables: 'CLEAN.CM_CASES_MAPPED,${SPANNER_GSQL_DB}.${TID}Cases',\n"
    )
    assert check_spanner_prefix(Path("app.tql"), text) == []


def test_spanner_prefix_exemption_is_per_occurrence_not_per_file():
    # The exempted value is skipped; a DIFFERENT untokenized value in the same file is
    # still caught -- the opt-out must not disarm the rule for the whole file.
    text = (
        "  -- isolation-exempt: spanner-prefix -- fixture-recorded names\n"
        "  Tables: 'NKM.ERCDBA.CM_CASES',\n"
        "  Tables: '${TID}src,${SPANNER_GSQL_DB}.Cases',\n"
    )
    v = check_spanner_prefix(Path("app.tql"), text)
    assert len(v) == 1
    assert "${SPANNER_GSQL_DB}.Cases" in v[0].message


def test_spanner_prefix_ignores_create_table_inside_a_comment():
    # Prose explaining that some "CREATE TABLE fails" is not DDL and must not be flagged.
    text = "-- MAX_STRING_SIZE=STANDARD -- CREATE TABLE fails ORA-00910 at 12000\n"
    assert check_spanner_prefix(Path("oracle_ddl.sql"), text) == []


def test_spanner_prefix_still_flags_create_table_with_a_trailing_comment():
    # Only a WHOLE-line comment is skipped; real DDL carrying a trailing comment is not.
    text = "CREATE TABLE bug_table (id INT64);  -- see note\n"
    assert len(check_spanner_prefix(Path("spanner_ddl.sql"), text)) == 1


def test_postgres_prefix_flags_untokenized_dotted():
    text = "target: ${PG_TARGET_SCHEMA}.out\n"
    v = check_postgres_prefix(Path("test.yaml"), text)
    assert len(v) == 1


def test_postgres_prefix_allows_tokenized_dotted():
    text = "target: ${PG_TARGET_SCHEMA}.${TID}out\n"
    assert check_postgres_prefix(Path("test.yaml"), text) == []


def test_postgres_prefix_flags_bare_create_table():
    text = "CREATE TABLE src (id INT);\n"
    v = check_postgres_prefix(Path("ddl.sql"), text)
    assert len(v) == 1


def test_postgres_prefix_flags_bare_insert_into():
    text = "INSERT INTO src VALUES (1);\n"
    v = check_postgres_prefix(Path("seed.sql"), text)
    assert len(v) == 1


def test_postgres_prefix_allows_tokenized_bare_forms():
    text = "CREATE TABLE ${TID}src (id INT);\nINSERT INTO ${TID}src VALUES (1);\n"
    assert check_postgres_prefix(Path("ddl.sql"), text) == []


def test_postgres_prefix_ignores_tql_insert_into_stream_routing():
    # `INSERT INTO StreamName SELECT ...` is Striim CQ stream-routing syntax in .tql
    # files, unrelated to Postgres tables -- must not be flagged as a bare table name.
    text = "INSERT INTO FilteredStream SELECT * FROM SourceStream;\n"
    assert check_postgres_prefix(Path("app.tql"), text) == []


def test_postgres_prefix_allows_explicit_public_schema_tokenized():
    # A literal `public.name` schema-qualified CREATE TABLE (as opposed to relying on
    # search_path) must check only the final segment for the ${TID} prefix -- `public`
    # itself must never be mistaken for an untokenized table name.
    text = "CREATE TABLE public.${TID}customers (id BIGINT);\n"
    assert check_postgres_prefix(Path("ddl.sql"), text) == []


def test_postgres_prefix_flags_explicit_public_schema_untokenized():
    text = "CREATE TABLE public.customers (id BIGINT);\n"
    v = check_postgres_prefix(Path("ddl.sql"), text)
    assert len(v) == 1


def test_postgres_prefix_allows_tokenized_bare_forms_with_tid_oracle():
    # ${TID_ORACLE} (spec §A.1a) is a distinct, Oracle-only per-test token -- a test
    # requiring both oracle and postgres can have Oracle-side bare CREATE/INSERT
    # statements in the same .sql file using ${TID_ORACLE}, which must not
    # false-positive as "untokenized" just because it isn't literally ${TID}.
    text = "CREATE TABLE ${TID_ORACLE}src (id INT);\nINSERT INTO ${TID_ORACLE}src VALUES (1);\n"
    assert check_postgres_prefix(Path("ddl.sql"), text) == []


def test_no_literal_fixed_names_flags_each_variant():
    for literal in ("slt_src", "slt_tgt", "slt-src", "slt-tgt"):
        v = check_no_literal_fixed_names(Path("app.tql"), f"topic: {literal}\n")
        assert len(v) == 1, literal


def test_no_literal_fixed_names_allows_kafka_derived_name():
    # kafka derives slt_<tid>_src -- tid sits BETWEEN slt and src, so the literal
    # substring "slt_src" never appears.
    text = "topic: slt_abc123_src\n"
    assert check_no_literal_fixed_names(Path("app.tql"), text) == []


def test_no_literal_fixed_names_allows_gcs_derived_name():
    # gcs now derives slt-<tid>-src (tid BETWEEN slt and src/tgt, like kafka) -- the
    # bare "slt-src" head never occurs in a derived name at all.
    text = "bucket: slt-t4f2a9c1b0-src\n"
    assert check_no_literal_fixed_names(Path("app.tql"), text) == []
    # The regex is UNCHANGED: its lookahead still exempts any non-bare occurrence
    # (e.g. a legacy suffix-form name) -- only a truly bare slt-src/slt-tgt/slt_src/
    # slt_tgt token is flagged.
    assert check_no_literal_fixed_names(Path("app.tql"), "bucket: slt-src-abc123\n") == []


def test_slug_length_flags_over_63():
    assert check_slug_length("x" * 60) != []  # "slt_" + 60 chars = 64 > 63


def test_slug_length_allows_46():
    assert check_slug_length("x" * 46) == []  # "slt_" + 46 = 50 <= 63


def test_no_load_open_processor_flags_load_line():
    text = 'LOAD OPEN PROCESSOR "UploadedFiles/${OP_JAR}";\n'
    v = check_no_load_open_processor(Path("app.tql"), text)
    assert len(v) == 1


def test_no_load_open_processor_ignores_unload():
    text = 'UNLOAD OPEN PROCESSOR "UploadedFiles/${OP_JAR}";\n'
    assert check_no_load_open_processor(Path("app.tql"), text) == []


def test_no_load_open_processor_is_scoped_to_tql():
    # A manifest's `tql:` is always a FILENAME, so a test.yaml never carries an executable
    # statement, and a .sql is DDL. Scanning them can only produce false positives -- which is
    # what sent statusreader-multi-node-cluster red over a YAML comment.
    text = 'LOAD OPEN PROCESSOR "x";\n'
    assert check_no_load_open_processor(Path("test.yaml"), text) == []
    assert check_no_load_open_processor(Path("ddl.sql"), text) == []
    assert len(check_no_load_open_processor(Path("app.tql"), text)) == 1


def test_no_load_open_processor_ignores_the_statusreader_yaml_comment():
    # The exact shape that turned the gate red.
    text = ("op:\n"
            "  jar: java/OpenProcessors/StatusReader\n"
            "  # cannot deploy without this. `LOAD OPEN PROCESSOR` is a server-side op.\n"
            "  on_agent: true\n")
    assert check_no_load_open_processor(Path("test.yaml"), text) == []


def test_no_load_open_processor_ignores_the_phrase_in_a_yaml_value():
    # Not just comments: a `purpose:` or `disabled:` value naming the statement is prose too.
    # An earlier revision of this fix still flagged these, which is the same false-positive
    # class one syntax over.
    text = ('purpose: "proves the app deploys without an in-TQL LOAD OPEN PROCESSOR"\n'
            'disabled: "SLT-123 -- blocked until LOAD OPEN PROCESSOR moves runner-side"\n')
    assert check_no_load_open_processor(Path("test.yaml"), text) == []


def test_no_load_open_processor_ignores_a_whole_line_comment_in_tql():
    text = "-- LOAD OPEN PROCESSOR is stripped post-B.2; registration is runner-side.\n"
    assert check_no_load_open_processor(Path("app.tql"), text) == []


def test_no_load_open_processor_still_flags_a_statement_with_a_trailing_comment():
    # Only a WHOLE-line comment is skipped -- the same choice the spanner rule makes and
    # pins. Real registration carrying a trailing comment stays enforced.
    text = 'LOAD OPEN PROCESSOR "x";  -- registers the module\n'
    v = check_no_load_open_processor(Path("app.tql"), text)
    assert len(v) == 1 and v[0].line == 1


def test_no_load_open_processor_reports_the_line_of_the_statement_not_the_comment():
    # A skipped comment must not shift the reported line of the statement below it.
    text = '-- LOAD OPEN PROCESSOR in a comment\n\n\nLOAD OPEN PROCESSOR "x";\n'
    v = check_no_load_open_processor(Path("app.tql"), text)
    assert len(v) == 1 and v[0].line == 4


def test_no_load_open_processor_does_not_treat_hash_as_a_comment_in_tql():
    # `#` is not TQL comment syntax; Striim ships the line verbatim, so it must stay flagged.
    text = '# LOAD OPEN PROCESSOR "x";\n'
    assert len(check_no_load_open_processor(Path("app.tql"), text)) == 1


def test_tql_is_scannable_accepts_a_plain_tql_name():
    assert check_tql_is_scannable(Path("test.yaml"), "app.tql") == []
    assert check_tql_is_scannable(Path("test.yaml"), "app_8_jdbc.tql") == []


def test_tql_is_scannable_flags_a_non_tql_suffix():
    # Rule 8 scans .tql only, so `tql: app.sql` would have its app file skipped -- and the
    # PRE-scoping rule did scan .sql, so this shape is a regression the scoping would
    # otherwise introduce, not a pre-existing gap.
    v = check_tql_is_scannable(Path("test.yaml"), "app.sql")
    assert len(v) == 1 and v[0].rule == "in-tql-load"


def test_tql_is_scannable_flags_a_parent_escape():
    # The walker rglobs source_dir only, so `../b/app.tql` is never walked whatever its
    # suffix -- the suffix check alone would pass it.
    v = check_tql_is_scannable(Path("test.yaml"), "../b/app.tql")
    assert len(v) == 1 and v[0].rule == "in-tql-load"


def test_tql_is_scannable_flags_both_reasons_independently():
    # A value that is wrong on both counts reports both, so fixing one does not hide the other.
    assert len(check_tql_is_scannable(Path("test.yaml"), "../b/app.sql")) == 2


# ---- the two routes past the gate that the suffix and `..` checks did not cover ------

def test_tql_is_scannable_flags_an_ABSOLUTE_path():
    # `/x/app.tql` ends in .tql and contains no `..`, so both suffix and parent-path checks pass it -- and it
    # names a file outside anything the walker reads. is_absolute() alone is not enough either,
    # which is why it sits beside the `..` check rather than replacing it.
    v = check_tql_is_scannable(Path("test.yaml"), "/elsewhere/app.tql")
    assert len(v) == 1 and v[0].rule == "in-tql-load"


def test_tql_is_scannable_flags_a_WALKER_EXEMPT_directory():
    # The sharpest of the three, because it looks completely ordinary: `docs/app.tql` is
    # relative, has the right suffix, has no `..` -- and the walker skips every file whose path
    # carries an exempt part, so rule 8 never reads it.
    for part in sorted(WALKER_EXEMPT_DIR_PARTS):
        v = check_tql_is_scannable(Path("test.yaml"), f"{part}/app.tql")
        assert len(v) == 1 and v[0].rule == "in-tql-load", part
        assert part in v[0].message


def test_example_is_scannable_accepts_an_ordinary_example_dir():
    # The control that matters most: `example:` is a SHIPPED feature and the corpus uses it.
    # A guard that refused a normal value would take the live tier out.
    assert check_example_is_scannable(Path("test.yaml"), None) == []
    assert check_example_is_scannable(
        Path("test.yaml"), "java/OpenProcessors/LookupOp/examples/write-through") == []


def test_example_is_scannable_flags_the_three_escapes():
    # `example:` REPLACES source_dir, so where a bad `tql:` hides ONE file this hides the whole
    # tree -- the walker iterates, discards every file, and the gate passes having read nothing.
    for value in ("/elsewhere/x", "../x", "java/OpenProcessors/X/docs"):
        v = check_example_is_scannable(Path("test.yaml"), value)
        assert len(v) == 1 and v[0].rule == "in-tql-load", value


def test_the_exempt_set_is_SHARED_with_the_walker_not_restated():
    # Two copies of this set drifting apart is how the gate would stop being real while still
    # passing. The gate imports it; this pins that it is importable and
    # non-empty, so a future edit cannot quietly reduce it to nothing.
    assert WALKER_EXEMPT_DIR_PARTS and "docs" in WALKER_EXEMPT_DIR_PARTS


def test_file_paths_tokenized_flags_missing_tokens():
    text = """
name: t
tql: app.tql
requires: [oracle]
assert:
  smoke: true
  file:
    - path: /tmp/plainpath
      match: expected/x.csv
"""
    v = check_file_paths_tokenized(Path("test.yaml"), text)
    assert len(v) == 1
    assert v[0].rule == "file-path-untokenized"


def test_file_paths_tokenized_allows_ns_or_tid():
    text = """
name: t
tql: app.tql
requires: [oracle]
assert:
  smoke: true
  file:
    - path: /tmp/${NS}-out
      match: expected/x.csv
server_files:
  - file: cfg.json
    dest: /tmp/${TID}-cfg.json
"""
    assert check_file_paths_tokenized(Path("test.yaml"), text) == []


def test_file_paths_tokenized_skips_load_entries():
    # `load:` dests are uploaded jar names, never server paths: nothing to tokenize.
    text = """
name: t
tql: app.tql
requires: [mssql]
assert:
  smoke: true
server_files:
  - file: externaljars/Dup.scm
    dest: Dup.scm
    load: open_processor
  - file: externaljars/udf.jar
    dest: udf.jar
    load: true
"""
    assert check_file_paths_tokenized(Path("test.yaml"), text) == []


def test_config_file_upload_flags_untokenized_configfile():
    text = "ConfigFile: 'UploadedFiles/merge.json',\n"
    v = check_config_file_upload_tokenized(Path("app.tql"), text)
    assert len(v) == 1


def test_config_file_upload_allows_tid_tokenized():
    text = "ConfigFile: 'UploadedFiles/${TID}merge.json',\n"
    assert check_config_file_upload_tokenized(Path("app.tql"), text) == []


def test_config_file_upload_allows_op_token_style():
    text = "ConfigFile: 'UploadedFiles/${OP_JAR}',\n"
    assert check_config_file_upload_tokenized(Path("app.tql"), text) == []


def test_config_file_upload_nested_local_source_uses_flat_server_basename():
    text = "ConfigFile: 'UploadedFiles/changeop-001.json',\n"
    assert check_config_file_upload_tokenized(Path("app.tql"), text,
                                            {"configs/changeop-001.json"}) == []


def test_config_file_upload_rejects_nested_server_path_that_runner_will_not_rewrite():
    text = "ConfigFile: 'UploadedFiles/configs/changeop-001.json',\n"
    violations = check_config_file_upload_tokenized(Path("app.tql"), text,
                                                   {"configs/changeop-001.json"})
    assert len(violations) == 1
    assert violations[0].rule == "configfile-untokenized"


def test_teradata_prefix_flags_untokenized_tables():
    text = ("CREATE TABLE ${TERADATA_SOURCE_SCHEMA}.ORDERS (id INT);\n"
            "INSERT INTO qatarget.items VALUES (1);\n"
            "SELECT * FROM ${TERADATA_TARGET_USER}.items;\n")
    v = check_teradata_prefix(Path("ddl.sql"), text)
    assert [x.line for x in v] == [1, 2, 3] and all(x.rule == "teradata-prefix" for x in v)


def test_teradata_prefix_accepts_tid_prefixed_tables():
    text = ("CREATE TABLE ${TERADATA_SOURCE_SCHEMA}.${TID}ORDERS (id INT);\n"
            "SELECT * FROM QATARGET.${TID}items;\n")
    assert check_teradata_prefix(Path("ddl.sql"), text) == []


def test_vertica_prefix_flags_untokenized_tables():
    text = ("CREATE TABLE ${VERTICA_SOURCE_SCHEMA}.ORDERS (id INT);\n"
            "INSERT INTO qatarget.items VALUES (1);\n"
            "SELECT * FROM QASOURCE.items;\n")
    v = check_vertica_prefix(Path("ddl.sql"), text)
    assert [x.line for x in v] == [1, 2, 3] and all(x.rule == "vertica-prefix" for x in v)


def test_vertica_prefix_accepts_tid_prefixed_tables():
    text = ("CREATE TABLE ${VERTICA_SOURCE_SCHEMA}.${TID}ORDERS (id INT);\n"
            "SELECT * FROM QATARGET.${TID}items;\n")
    assert check_vertica_prefix(Path("ddl.sql"), text) == []
