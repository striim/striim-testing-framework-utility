from __future__ import annotations
import json


def coerce_cell(v):
    """Canonicalize a DB row value to the framework's golden-comparison text form. ONE
    definition shared by every admin (pgclient/oraadmin/spanneradmin/mssqladmin) so
    JSON / bool / bytes render identically no matter which database produced the row.

      - bool                          -> "true"/"false" (DB & JSON text convention;
                                         Python str(bool) gives "True"/"False", which
                                         silently traps golden authors)
      - bytes/bytearray/memoryview    -> hex string v.hex() (bytea/RAW/BLOB; str(memoryview)
                                         is a nondeterministic object address)
      - dict/list                     -> canonical JSON (sorted keys, compact) so a JSON
                                         column (Oracle 21c+ / Spanner JSON auto-parse to
                                         dict/list) compares key-order-independently
      - None                          -> None
      - everything else               -> str(v)

    IMPORTANT: bool is checked FIRST — bool is a subclass of int, so an int check ahead of
    it would swallow True/False.
    """
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (bytes, bytearray, memoryview)):
        return v.hex()
    if isinstance(v, (dict, list)):
        return json.dumps(v, sort_keys=True, separators=(",", ":"))
    if v is None:
        return None
    return str(v)


def split_sql_statements(sql: str) -> list[str]:
    """Split a plain-SQL script into individual statements on ``;`` — but NOT a ``;`` that
    sits inside a single-quoted string literal (Standard-SQL ``''`` escape honored). ``--`` line
    comments (to end-of-line) and ``/* ... */`` block comments are dropped, but ONLY outside a
    string literal. Empty/whitespace chunks are skipped.

    Shared by the Oracle / MSSQL / Spanner admins so a seed row containing a semicolon (e.g.
    ``'a;b'``) is not mis-split into two malformed statements. Each admin layers its own
    dialect quirks on top (Oracle strips a trailing ``/``; MSSQL drops ``GO`` batch separators).
    Framework DDL/seed files stay plain SQL (no PL/SQL blocks with internal ``;``).

    Comment stripping runs INSIDE the same character scan that tracks string state — a naive
    line-by-line ``--`` pre-pass silently deletes a multi-line string-literal continuation line
    that happens to start with ``--`` (e.g. a seed value spanning lines).
    """
    return _split(sql, quotes="'", hash_comments=False, dash_comment_needs_space=False,
                  backslash_escapes=False, keep_executable_blocks=False, mysql_blocks=False)


def split_mysql_statements(sql: str) -> list[str]:
    """``split_sql_statements`` with MySQL's lexing rules (used by the MySQL admin):

      - ``#`` starts a line comment.
      - ``--`` starts a line comment only when followed by whitespace or a control character
        (or end of input): MySQL treats ``--x`` as two minus signs, not a comment.
      - ``'…'``, ``"…"`` and `` `…` `` are quoted text; inside ``'`` and ``"`` a backslash escapes
        the next character and a doubled quote is a literal quote; a doubled backtick is a
        literal backtick.
      - ``/*! … */`` and ``/*+ … */`` are kept verbatim (the server executes them); any other
        ``/* … */`` becomes one space, as MySQL reads it (``1/*x*/FROM`` stays two tokens).
        An unterminated ``/*`` raises ``ValueError`` rather than dropping the rest of the file.

    Not supported: ``DELIMITER`` (no live-tier file uses it). Assumes the default ``sql_mode``:
    under ``NO_BACKSLASH_ESCAPES`` or ``ANSI_QUOTES`` the server lexes quotes differently.
    """
    return _split(sql, quotes="'\"`", hash_comments=True, dash_comment_needs_space=True,
                  backslash_escapes=True, keep_executable_blocks=True, mysql_blocks=True)


def _split(sql: str, *, quotes: str, hash_comments: bool, dash_comment_needs_space: bool,
           backslash_escapes: bool, keep_executable_blocks: bool,
           mysql_blocks: bool) -> list[str]:
    out: list[str] = []
    buf: list[str] = []
    in_str = ""   # the quote character while inside quoted text
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if in_str:
            if backslash_escapes and ch == "\\" and in_str != "`" and i + 1 < n:
                buf.append(sql[i:i + 2])
                i += 2
                continue
            if ch == in_str:
                if i + 1 < n and sql[i + 1] == in_str:   # doubled quote inside the text
                    buf.append(ch + ch)
                    i += 2
                    continue
                in_str = ""
            buf.append(ch)
            i += 1
            continue
        # outside quoted text:
        if ch in quotes:
            in_str = ch
            buf.append(ch)
        elif ch == "-" and sql.startswith("--", i) and (
                not dash_comment_needs_space or _is_space_or_control(sql[i + 2:i + 3])):
            nl = sql.find("\n", i)                                # -- line comment: skip to EOL
            i = n if nl == -1 else nl
            continue
        elif hash_comments and ch == "#":                         # # line comment: skip to EOL
            nl = sql.find("\n", i)
            i = n if nl == -1 else nl
            continue
        elif ch == "/" and sql.startswith("/*", i):               # /* ... */ block comment
            end = sql.find("*/", i + 2)
            if end == -1 and mysql_blocks:
                raise ValueError(f"unterminated /* comment at offset {i}")
            end = n if end == -1 else end + 2
            if keep_executable_blocks and sql[i + 2:i + 3] in ("!", "+"):
                buf.append(sql[i:end])
            elif mysql_blocks:
                buf.append(" ")
            i = end
            continue
        elif ch == ";":
            s = "".join(buf).strip()
            if s:
                out.append(s)
            buf = []
        else:
            buf.append(ch)
        i += 1
    s = "".join(buf).strip()
    if s:
        out.append(s)
    return out


def _is_space_or_control(ch: str) -> bool:
    # MySQL's rule for "-- ": the next byte is whitespace or a control character; at end of
    # input the lexer sees NUL, which counts as one.
    return ch == "" or ch.isspace() or ord(ch) < 32 or ord(ch) == 127
