"""Tests for the deterministic security gates: sanitization and policy checks."""

from __future__ import annotations

import duckdb
import pytest

from veritas.security import (
    CELL_TEXT_CAP,
    UnsafePythonError,
    UnsafeSqlError,
    _normalize_sql,
    _read_quoted,
    check_python_source,
    sanitize_text,
    validate_select,
)

# --- sanitize_text -------------------------------------------------------------


def test_sanitize_passes_plain_text() -> None:
    assert sanitize_text("revenue") == "revenue"


def test_sanitize_coerces_non_strings() -> None:
    assert sanitize_text(123) == "123"
    assert sanitize_text(None) == "None"


def test_sanitize_escapes_newlines_and_drops_controls() -> None:
    # A NUL and a bell vanish; the newline becomes a visible escape, not a real break.
    assert sanitize_text("a\nb\x00c\x07") == "a\\nbc"


def test_sanitize_escapes_carriage_returns() -> None:
    assert sanitize_text("a\r\nb\rc") == "a\\nb\\nc"


def test_sanitize_collapses_tabs_and_spaces() -> None:
    assert sanitize_text("a\t\tb   c") == "a b c"


def test_sanitize_drops_zero_width_and_bidi() -> None:
    # zero-width space (U+200B) and right-to-left override (U+202E) are format chars.
    dangerous = "a" + chr(0x200B) + "b" + chr(0x202E) + "c"
    assert sanitize_text(dangerous) == "abc"


def test_sanitize_escapes_unicode_line_separators_and_vt_ff() -> None:
    # U+2028/U+2029 (Zl/Zp) survive the C-category filter, so they must be escaped
    # explicitly; vertical tab and form feed likewise become a visible "\n".
    dangerous = "a" + chr(0x2028) + "b" + chr(0x2029) + "c" + chr(0x0B) + "d" + chr(0x0C) + "e"
    assert sanitize_text(dangerous) == "a\\nb\\nc\\nd\\ne"


def test_sanitize_truncates_with_ellipsis_within_cap() -> None:
    out = sanitize_text("abcdefgh", cap=4)
    assert out == "abc…"
    assert len(out) == 4


def test_sanitize_does_not_truncate_at_exact_cap() -> None:
    assert sanitize_text("abcd", cap=4) == "abcd"


def test_sanitize_never_exceeds_cap_even_at_one() -> None:
    # the old "head + ellipsis" form overflowed by one char at cap==1
    assert len(sanitize_text("abcdef", cap=1)) == 1


def test_sanitize_default_cap_is_cell_text_cap() -> None:
    assert len(sanitize_text("x" * 1000)) == CELL_TEXT_CAP


def test_sanitize_rejects_nonpositive_cap() -> None:
    with pytest.raises(ValueError, match="cap must be positive"):
        sanitize_text("x", cap=0)


# --- validate_select -----------------------------------------------------------


@pytest.fixture
def conn() -> duckdb.DuckDBPyConnection:
    return duckdb.connect()


def test_validate_accepts_select(conn: duckdb.DuckDBPyConnection) -> None:
    assert validate_select(conn, "  SELECT 1 AS n  ") == "SELECT 1 AS n"


def test_validate_accepts_cte(conn: duckdb.DuckDBPyConnection) -> None:
    sql = "WITH c AS (SELECT 1 AS n) SELECT * FROM c"
    assert validate_select(conn, sql) == sql


def test_validate_accepts_allowed_aggregate(conn: duckdb.DuckDBPyConnection) -> None:
    # count(...) is a function call but not on the filesystem denylist; the query is
    # returned unchanged (a benign function call is preserved, not stripped).
    sql = "SELECT count(*) AS n FROM (SELECT 1)"
    assert validate_select(conn, sql) == sql


def test_validate_accepts_string_with_quote_and_double_quoted_identifier(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    # an escaped quote inside a string and a "" inside a quoted identifier must not trip
    # the tokenizer or the path/keyword scans
    assert validate_select(conn, "SELECT 'O''Brien' AS \"a\"\"b\"") is not None


def test_validate_rejects_empty(conn: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(UnsafeSqlError, match="empty"):
        validate_select(conn, "   ")


def test_validate_rejects_unparseable(conn: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(UnsafeSqlError, match="could not parse"):
        validate_select(conn, "SELECT 1 +")


def test_validate_rejects_multiple_statements(conn: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(UnsafeSqlError, match="exactly one"):
        validate_select(conn, "SELECT 1; SELECT 2")


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE t (a INTEGER)",
        "INSERT INTO t VALUES (1)",
        "COPY (SELECT 1) TO 'out.csv'",
        "ATTACH 'other.db'",
        "DELETE FROM t",
    ],
)
def test_validate_rejects_non_select(conn: duckdb.DuckDBPyConnection, sql: str) -> None:
    with pytest.raises(UnsafeSqlError):
        validate_select(conn, sql)


def test_validate_rejects_pragma_classified_as_select(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    # DuckDB types PRAGMA as SELECT, so only the keyword guard stops it.
    with pytest.raises(UnsafeSqlError, match="PRAGMA"):
        validate_select(conn, "PRAGMA database_list")


def test_validate_rejects_call(conn: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(UnsafeSqlError):
        validate_select(conn, "CALL pragma_version()")


def test_validate_rejects_leading_keyword_behind_comment(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    with pytest.raises(UnsafeSqlError, match="PRAGMA"):
        validate_select(conn, "-- harmless?\nPRAGMA version")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM read_parquet('s3://b/k')",
        "SELECT * FROM glob('/**')",
        "/* sneaky */ SELECT * FROM read_json('x')",
    ],
)
def test_validate_rejects_filesystem_functions(conn: duckdb.DuckDBPyConnection, sql: str) -> None:
    with pytest.raises(UnsafeSqlError, match="filesystem or network"):
        validate_select(conn, sql)


@pytest.mark.parametrize(
    "sql",
    [
        # a denied call hidden behind a comment marker that lives *inside* a string —
        # a naive comment strip would delete the read_csv after it (the critical bypass)
        "SELECT '/*' AS m, * FROM read_csv('/tmp/secret.csv') x /* t */",
        "SELECT '--' AS m, * FROM read_csv('/tmp/secret.csv')",
        # functions that read the filesystem but were missing from the original denylist
        "SELECT * FROM read_json_objects_auto('/tmp/x.json')",
        "SELECT * FROM read_ndjson_objects('/tmp/x.json')",
        # a quoted function name must still be caught
        "SELECT * FROM \"read_csv\"('/etc/passwd')",
    ],
)
def test_validate_rejects_hidden_or_aliased_file_functions(
    conn: duckdb.DuckDBPyConnection, sql: str
) -> None:
    with pytest.raises(UnsafeSqlError):
        validate_select(conn, sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM '/etc/passwd'",
        "SELECT * FROM 'data.parquet'",
        "SELECT * FROM t1 JOIN '/tmp/x.parquet' ON true",
    ],
)
def test_validate_rejects_string_literal_as_table_source(
    conn: duckdb.DuckDBPyConnection, sql: str
) -> None:
    # DuckDB's replacement scan reads a file named by a bare string in table position
    with pytest.raises(UnsafeSqlError, match="table source"):
        validate_select(conn, sql)


@pytest.mark.parametrize("sql", ["SELECT '/etc/passwd' AS v", "SELECT 'report.csv' AS v"])
def test_validate_rejects_path_like_string_literal(
    conn: duckdb.DuckDBPyConnection, sql: str
) -> None:
    with pytest.raises(UnsafeSqlError, match="file path"):
        validate_select(conn, sql)


# --- _normalize_sql / _read_quoted (tokenizer internals) -----------------------


def test_normalize_sql_blanks_strings_and_strips_comments() -> None:
    normalized, literals = _normalize_sql("SELECT 'a''b' /* c */ FROM t -- tail\n")
    assert "a'b" in literals  # the doubled quote is one literal apostrophe
    assert "/* c */" not in normalized and "-- tail" not in normalized
    assert "'" not in normalized  # the string literal is gone, replaced by a sentinel


def test_normalize_sql_dequotes_identifiers() -> None:
    normalized, _ = _normalize_sql('SELECT "we""ird" FROM t')
    assert 'we"ird' in normalized  # quoted identifier kept (de-quoted) for the scans


def test_normalize_sql_handles_unterminated_quote_and_comment() -> None:
    # both are unterminated; the tokenizer must consume to end of input without looping
    _, literals = _normalize_sql("SELECT 'abc")
    assert literals == ["abc"]
    normalized, _ = _normalize_sql("SELECT 1 /* unterminated")
    assert "unterminated" not in normalized


def test_read_quoted_unescapes_doubled_quote() -> None:
    content, end = _read_quoted("'a''b'rest", 0, "'")
    assert content == "a'b"
    assert end == len("'a''b'")


# --- check_python_source -------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "import pandas as pd\nimport numpy as np\nresult = pd.DataFrame()",
        "from pandas import DataFrame\nfrom __future__ import annotations",
        "import scipy.stats\nx = scipy.stats.norm",
        "print('hello')",
    ],
)
def test_check_python_allows_whitelisted(source: str) -> None:
    check_python_source(source)  # must not raise


def test_check_python_rejects_syntax_error() -> None:
    with pytest.raises(UnsafePythonError, match="could not parse"):
        check_python_source("def (:")


@pytest.mark.parametrize("source", ["import os", "import sys, math", "import socket"])
def test_check_python_rejects_unlisted_import(source: str) -> None:
    with pytest.raises(UnsafePythonError, match="is not allowed"):
        check_python_source(source)


def test_check_python_rejects_from_import_of_unlisted() -> None:
    with pytest.raises(UnsafePythonError, match="'subprocess'"):
        check_python_source("from subprocess import run")


def test_check_python_rejects_relative_import() -> None:
    with pytest.raises(UnsafePythonError, match="relative"):
        check_python_source("from . import secrets")


@pytest.mark.parametrize(
    "source",
    ["eval('1+1')", "exec('x=1')", "open('/etc/passwd')", "x = __import__"],
)
def test_check_python_rejects_forbidden_names(source: str) -> None:
    with pytest.raises(UnsafePythonError):
        check_python_source(source)


@pytest.mark.parametrize(
    "source",
    [
        "__builtins__['__import__']('os')",  # the known builtins-dict escape
        "getattr(x, 'y')",  # getattr + a runtime dunder string defeats the attr denylist
        "setattr(x, 'y', 1)",
        "delattr(x, 'y')",
        "globals()",
        "locals()",
        "vars(x)",
    ],
)
def test_check_python_rejects_introspection_escape_enablers(source: str) -> None:
    with pytest.raises(UnsafePythonError, match="not allowed"):
        check_python_source(source)


@pytest.mark.parametrize(
    "source",
    [
        "().__class__.__bases__[0].__subclasses__()",
        "x = (1).__class__",
        "f.__globals__",
    ],
)
def test_check_python_rejects_dunder_attributes(source: str) -> None:
    with pytest.raises(UnsafePythonError):
        check_python_source(source)
