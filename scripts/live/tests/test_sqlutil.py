import pytest

from livetest.sqlutil import split_sql_statements, coerce_cell


def test_basic_split_and_comment_strip():
    sql = """
    -- a comment
    CREATE TABLE t (id int);
    INSERT INTO t VALUES (1);
    """
    assert split_sql_statements(sql) == ["CREATE TABLE t (id int)", "INSERT INTO t VALUES (1)"]


def test_semicolon_inside_string_literal_is_not_a_separator():
    # the whole point of the shared util: a ';' inside a quoted literal must NOT split
    sql = "INSERT INTO t (v) VALUES ('a;b'); INSERT INTO t (v) VALUES ('c');"
    assert split_sql_statements(sql) == [
        "INSERT INTO t (v) VALUES ('a;b')",
        "INSERT INTO t (v) VALUES ('c')",
    ]


def test_escaped_quote_inside_string():
    sql = "INSERT INTO t (v) VALUES ('O''Brien; Jr'); SELECT 1;"
    assert split_sql_statements(sql) == [
        "INSERT INTO t (v) VALUES ('O''Brien; Jr')",
        "SELECT 1",
    ]


def test_trailing_no_semicolon_and_blanks():
    assert split_sql_statements("SELECT 1") == ["SELECT 1"]
    assert split_sql_statements(";\n;  ;") == []
    assert split_sql_statements("") == []


def test_double_dash_inside_string_literal_is_preserved():
    # a '--' sequence inside a string literal is data, not a comment marker — must survive
    sql = "INSERT INTO t (msg) VALUES ('line one\n-- not a comment\nline two');"
    assert split_sql_statements(sql) == [
        "INSERT INTO t (msg) VALUES ('line one\n-- not a comment\nline two')",
    ]


def test_double_dash_line_comment_stripped_outside_string():
    sql = "SELECT 1; -- trailing comment\nSELECT 2;"
    assert split_sql_statements(sql) == ["SELECT 1", "SELECT 2"]


# ---- block comments ---------------------------------------------------------

def test_block_comment_with_semicolon_inside_does_not_split():
    # a ';' inside a /* ... */ block comment must NOT split the statement
    sql = "SELECT 1; /* has ; inside */ SELECT 2;"
    assert split_sql_statements(sql) == ["SELECT 1", "SELECT 2"]


def test_block_comment_open_inside_string_literal_is_data():
    # a '/*' sequence inside a string literal is data, not a comment marker
    sql = "INSERT INTO t (v) VALUES ('a /* not a comment */ b'); SELECT 2;"
    assert split_sql_statements(sql) == [
        "INSERT INTO t (v) VALUES ('a /* not a comment */ b')",
        "SELECT 2",
    ]


def test_unterminated_block_comment_swallows_to_end():
    sql = "SELECT 1; /* runs off the end SELECT 2;"
    assert split_sql_statements(sql) == ["SELECT 1"]


# ---- coerce_cell (shared row-value coercion) --------------------------------

def test_coerce_cell_dict_is_canonical_json():
    # sorted keys, compact separators -> key-order-independent golden comparison
    assert coerce_cell({"user_name": "C", "id": "1"}) == '{"id":"1","user_name":"C"}'
    assert coerce_cell([3, 1, 2]) == "[3,1,2]"


def test_coerce_cell_bool_is_lowercase():
    # bool must be checked before int (bool subclasses int); str(True) == "True" would trap
    assert coerce_cell(True) == "true"
    assert coerce_cell(False) == "false"


def test_coerce_cell_bytes_is_hex():
    assert coerce_cell(b"\x01\x02") == "0102"
    assert coerce_cell(bytearray(b"\x01\x02")) == "0102"
    assert coerce_cell(memoryview(b"\x01\x02")) == "0102"


def test_coerce_cell_none_stays_none_and_scalars_str():
    assert coerce_cell(None) is None
    assert coerce_cell(7) == "7"
    assert coerce_cell("x") == "x"


# ---- split_mysql_statements (MySQL lexing rules) ----------------------------

from livetest.sqlutil import split_mysql_statements


def test_mysql_semicolon_inside_dash_comment_does_not_split():
    # the shape that failed in the field: "...creates this; it quotes..." in a -- comment
    sql = (
        "CREATE TABLE a (id INT);\n"
        "-- The writer never creates this; it quotes this DDL into an error.\n"
        "CREATE TABLE b (id INT);\n"
    )
    assert split_mysql_statements(sql) == ["CREATE TABLE a (id INT)", "CREATE TABLE b (id INT)"]


def test_mysql_hash_comment_is_dropped_and_its_semicolon_ignored():
    sql = "SELECT 1; # first; done\n# a whole line; here\nSELECT 2;"
    assert split_mysql_statements(sql) == ["SELECT 1", "SELECT 2"]


def test_mysql_block_comment_semicolon_ignored():
    sql = "SELECT 1; /* a; b\n c */ SELECT 2;"
    assert split_mysql_statements(sql) == ["SELECT 1", "SELECT 2"]


def test_mysql_executable_and_hint_comments_are_kept_verbatim():
    sql = "/*!40101 SET NAMES utf8; */;\nSELECT /*+ MAX_EXECUTION_TIME(1000) */ 1;"
    assert split_mysql_statements(sql) == [
        "/*!40101 SET NAMES utf8; */",
        "SELECT /*+ MAX_EXECUTION_TIME(1000) */ 1",
    ]


def test_mysql_semicolon_inside_single_double_and_backtick_quotes():
    sql = (
        "INSERT INTO t VALUES ('a;b'); "
        'INSERT INTO t VALUES ("c;d"); '
        "SELECT `x;y` FROM t;"
    )
    assert split_mysql_statements(sql) == [
        "INSERT INTO t VALUES ('a;b')",
        'INSERT INTO t VALUES ("c;d")',
        "SELECT `x;y` FROM t",
    ]


def test_mysql_escaped_quotes_do_not_end_the_string():
    sql = (
        "INSERT INTO t VALUES ('it\\'s; here'); "
        "INSERT INTO t VALUES ('O''Brien; Jr'); "
        'INSERT INTO t VALUES ("say \\"hi\\"; now"); '
        'INSERT INTO t VALUES ("a""b; c"); '
        "SELECT `we``ird; name` FROM t; "
        "INSERT INTO t VALUES ('trailing backslash \\\\'); SELECT 9;"
    )
    assert split_mysql_statements(sql) == [
        "INSERT INTO t VALUES ('it\\'s; here')",
        "INSERT INTO t VALUES ('O''Brien; Jr')",
        'INSERT INTO t VALUES ("say \\"hi\\"; now")',
        'INSERT INTO t VALUES ("a""b; c")',
        "SELECT `we``ird; name` FROM t",
        "INSERT INTO t VALUES ('trailing backslash \\\\')",
        "SELECT 9",
    ]


def test_mysql_comment_markers_inside_quotes_are_data():
    sql = "INSERT INTO t VALUES ('-- not; a comment', \"# nor; this\", '/* nor; this */'); SELECT 2;"
    assert split_mysql_statements(sql) == [
        "INSERT INTO t VALUES ('-- not; a comment', \"# nor; this\", '/* nor; this */')",
        "SELECT 2",
    ]


def test_mysql_double_dash_needs_a_following_space():
    # MySQL: "--x" is two minus signs, "-- x", "--\t", "--\n" and "--" at end of input are comments
    assert split_mysql_statements("SELECT 1--1; SELECT 2;") == ["SELECT 1--1", "SELECT 2"]
    assert split_mysql_statements("SELECT 1 -- 1; x\n; SELECT 2;") == ["SELECT 1", "SELECT 2"]
    assert split_mysql_statements("SELECT 1 --\t1; x\n; SELECT 2;") == ["SELECT 1", "SELECT 2"]
    assert split_mysql_statements("SELECT 1 --\n; SELECT 2;") == ["SELECT 1", "SELECT 2"]
    assert split_mysql_statements("SELECT 1 --") == ["SELECT 1"]
    assert split_mysql_statements("SELECT 1 --;") == ["SELECT 1 --"]   # ';' is not whitespace


def test_mysql_trailing_comment_after_last_statement_is_not_a_statement():
    assert split_mysql_statements("SELECT 1;\n-- done; bye\n") == ["SELECT 1"]
    assert split_mysql_statements("SELECT 1;\n# done; bye") == ["SELECT 1"]
    assert split_mysql_statements("SELECT 1;\n/* done; bye */") == ["SELECT 1"]


def test_mysql_windows_line_endings():
    sql = "SELECT 1;\r\n-- a; b\r\n# c; d\r\nSELECT 2;\r\n"
    assert split_mysql_statements(sql) == ["SELECT 1", "SELECT 2"]


def test_mysql_empty_and_blank_input():
    assert split_mysql_statements("") == []
    assert split_mysql_statements("   \n\t") == []
    assert split_mysql_statements(";\n;  ;") == []
    assert split_mysql_statements("-- only; a comment") == []


def test_mysql_plain_sql_splits_as_before():
    # no comments, no quotes: same statements, same order, trimmed, empties dropped
    sql = "\n  CREATE TABLE t1 (id INT);\n\n  INSERT INTO t1 VALUES (1);;\n  INSERT INTO t1 VALUES (2)\n"
    assert split_mysql_statements(sql) == [
        "CREATE TABLE t1 (id INT)",
        "INSERT INTO t1 VALUES (1)",
        "INSERT INTO t1 VALUES (2)",
    ]


def test_standard_splitter_rules_are_unchanged():
    # the MySQL rules are a dialect, not a change to the shared splitter
    assert split_sql_statements("SELECT 1; --x; y\nSELECT 2;") == ["SELECT 1", "SELECT 2"]
    assert split_sql_statements("SELECT '#'; # x\n") == ["SELECT '#'", "# x"]
    assert split_sql_statements("SELECT 'a\\'; SELECT 2;") == ["SELECT 'a\\'", "SELECT 2"]
    assert split_sql_statements("SELECT /*+ hint */ 1;") == ["SELECT  1"]


def test_mysql_dropped_block_comment_separates_tokens_as_whitespace():
    assert split_mysql_statements("SELECT 1/*x*/FROM dual") == ["SELECT 1 FROM dual"]


def test_mysql_unterminated_block_comment_raises_instead_of_dropping_the_rest():
    with pytest.raises(ValueError, match="unterminated"):
        split_mysql_statements("SELECT 1; /* unterminated; DROP TABLE x;")


def test_shared_splitter_keeps_its_block_comment_behaviour():
    # Oracle/MSSQL/Spanner callers: unchanged by the MySQL rules.
    assert split_sql_statements("SELECT 1; /* open") == ["SELECT 1"]
