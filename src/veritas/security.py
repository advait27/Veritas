"""Security controls: untrusted-text neutralization and execution-policy checks.

Veritas runs untrusted *data* and model-written *code* next to a powerful
orchestrator. This module holds the deterministic gates that keep both contained
(see SECURITY.md):

- :func:`sanitize_text` neutralizes dataset- and execution-derived strings
  *structurally* before they re-enter model context — length-capped and stripped of
  the control characters that would let content break out of its data framing. It is
  not a phrase-matching "injection detector" (that would be unreliable theater);
  framing-breakout is what it prevents.
- :func:`validate_select` is the read-only gate for ``run_sql``: a single ``SELECT``
  statement, no settings/PRAGMA/CALL, and no filesystem/network table functions.
- :func:`check_python_source` is the static gate for ``run_python``: an AST import
  whitelist plus a denylist of escape-shaped builtins and dunder attributes, run in
  the parent before any subprocess is spawned.
"""

from __future__ import annotations

import ast
import re
import unicodedata
from typing import TYPE_CHECKING

import duckdb

if TYPE_CHECKING:
    from collections.abc import Iterable

PREVIEW_ROW_CAP = 50
"""Maximum rows in any preview returned to model context (SECURITY.md, threat 3)."""

PREVIEW_BYTE_CAP = 4096
"""Maximum size in bytes of any preview or captured-stdout string."""

CELL_TEXT_CAP = 200
"""Per-value character cap for dataset/execution-derived strings (mirrors D-012)."""


def sanitize_text(value: object, *, cap: int = CELL_TEXT_CAP) -> str:
    """Make an untrusted value safe to embed in tool output returned to the model.

    Coerces to ``str``, removes NUL and other C0/C1 control characters, renders
    newlines and tabs as visible escapes so the text cannot forge new logical lines,
    collapses the result, and hard-caps it at ``cap`` characters (appending ``…`` when
    truncation occurs, within the cap). This is a structural neutralizer, not a
    sanitizer of meaning: the text can still *say* "ignore instructions", but it can no
    longer *break framing* to be treated as one.

    Args:
        value: any value; non-strings are coerced with ``str()``.
        cap: maximum length of the returned string (must be positive).

    Returns:
        A single-line, control-free string of at most ``cap`` characters.

    Example:
        >>> sanitize_text("revenue   spike")
        'revenue spike'
    """
    if cap <= 0:
        msg = f"cap must be positive, got {cap}"
        raise ValueError(msg)
    text = str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\n", "\\n").replace("\t", " ")
    # Drop every Unicode control/format char (C0, C1, NUL, zero-width, bidi overrides);
    # the visible "\n" inserted above survives because backslash and 'n' are not in C.
    text = "".join(ch for ch in text if not unicodedata.category(ch).startswith("C"))
    text = re.sub(r" {2,}", " ", text).strip()
    if len(text) > cap:
        text = text[: max(cap - 1, 1)].rstrip() + "…"
    return text


# --- SQL: read-only SELECT gate ------------------------------------------------


class UnsafeSqlError(ValueError):
    """Raised when SQL submitted to ``run_sql`` is not a safe read-only query."""


_SQL_LEADING_KEYWORD_DENYLIST = frozenset(
    {
        "pragma",
        "set",
        "reset",
        "call",
        "attach",
        "detach",
        "use",
        "install",
        "load",
        "export",
        "import",
        "copy",
        "checkpoint",
        "force",
        "begin",
        "commit",
        "rollback",
        "update",
        "delete",
        "insert",
        "create",
        "drop",
        "alter",
        "truncate",
        "vacuum",
        "analyze",
    }
)
"""First-token denylist; catches PRAGMA/CALL, which DuckDB classifies as SELECT."""

_SQL_FORBIDDEN_FUNCTIONS = frozenset(
    {
        "read_csv",
        "read_csv_auto",
        "read_parquet",
        "parquet_scan",
        "read_json",
        "read_json_auto",
        "read_ndjson",
        "read_ndjson_auto",
        "read_json_objects",
        "read_text",
        "read_blob",
        "glob",
        "sniff_csv",
        "parquet_metadata",
        "parquet_schema",
        "parquet_file_metadata",
        "parquet_kv_metadata",
        "csv_sniff",
        "delta_scan",
        "iceberg_scan",
        "read_xlsx",
    }
)
"""Table/scalar functions that read the filesystem or network; denied in run_sql."""

_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_FUNCTION_CALL_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _strip_sql_comments(sql: str) -> str:
    """Remove ``--`` line comments and ``/* */`` block comments from SQL text."""
    return _COMMENT_RE.sub(" ", sql)


def validate_select(conn: duckdb.DuckDBPyConnection, sql: str) -> str:
    """Validate that ``sql`` is a single, read-only ``SELECT`` and return it stripped.

    Three layers, all deterministic: DuckDB's parser must see exactly one statement of
    type ``SELECT``; the first keyword must not be a settings/DDL/DML/transaction verb
    (DuckDB classifies ``PRAGMA``/``CALL`` as ``SELECT``, so a keyword guard is needed);
    and no filesystem/network table function (``read_csv``, ``read_parquet``, ``glob``,
    …) may appear. Comments are stripped before the keyword and function scans.

    Args:
        conn: a DuckDB connection used only to *parse* (never execute) the statement.
        sql: the candidate query.

    Returns:
        The original ``sql`` with surrounding whitespace stripped.

    Raises:
        UnsafeSqlError: if the statement is empty, multiple, non-SELECT, leads with a
            denied keyword, or references a denied function.

    Example:
        >>> import duckdb
        >>> validate_select(duckdb.connect(), "SELECT 1 AS n")
        'SELECT 1 AS n'
    """
    stripped = sql.strip()
    if not stripped:
        msg = "empty SQL statement"
        raise UnsafeSqlError(msg)
    try:
        statements = conn.extract_statements(stripped)
    except duckdb.Error as err:
        msg = f"could not parse SQL: {err}"
        raise UnsafeSqlError(msg) from err
    if len(statements) != 1:
        msg = f"exactly one statement is allowed, got {len(statements)}"
        raise UnsafeSqlError(msg)
    if statements[0].type != duckdb.StatementType.SELECT:
        msg = f"only SELECT queries are allowed, got {statements[0].type.name}"
        raise UnsafeSqlError(msg)

    decommented = _strip_sql_comments(stripped)
    first = _IDENTIFIER_RE.search(decommented)
    if first is not None and first.group(0).lower() in _SQL_LEADING_KEYWORD_DENYLIST:
        msg = f"statement keyword {first.group(0).upper()!r} is not allowed in run_sql"
        raise UnsafeSqlError(msg)
    for match in _FUNCTION_CALL_RE.finditer(decommented):
        name = match.group(1).lower()
        if name in _SQL_FORBIDDEN_FUNCTIONS:
            msg = f"function {name!r} reads the filesystem or network and is not allowed"
            raise UnsafeSqlError(msg)
    return stripped


# --- Python: static AST gate ---------------------------------------------------


class UnsafePythonError(ValueError):
    """Raised when Python submitted to ``run_python`` violates the static policy."""


PYTHON_IMPORT_WHITELIST = frozenset(
    {
        "pandas",
        "numpy",
        "scipy",
        "statsmodels",
        "matplotlib",
        "math",
        "statistics",
        "json",
        "datetime",
        "decimal",
        "fractions",
        "itertools",
        "functools",
        "collections",
        "re",
        "random",
        "typing",
        "dataclasses",
        "warnings",
        "string",
        "operator",
        "bisect",
        "heapq",
        "textwrap",
        "__future__",
    }
)
"""Root modules a sandboxed script may import; everything else is denied."""

_FORBIDDEN_NAMES = frozenset(
    {"eval", "exec", "compile", "__import__", "open", "input", "breakpoint", "exit", "quit"}
)
_FORBIDDEN_ATTRS = frozenset(
    {
        "__subclasses__",
        "__bases__",
        "__mro__",
        "__globals__",
        "__builtins__",
        "__code__",
        "__closure__",
        "__getattribute__",
        "__class__",
        "__dict__",
        "__reduce__",
        "__reduce_ex__",
        "__base__",
        "__subclasshook__",
        "__loader__",
        "__import__",
    }
)


def _check_import(node: ast.Import | ast.ImportFrom) -> None:
    """Reject relative imports and any import whose root is off the whitelist."""
    if isinstance(node, ast.ImportFrom):
        if node.level != 0:
            msg = "relative imports are not allowed"
            raise UnsafePythonError(msg)
        roots: Iterable[str] = [(node.module or "").split(".")[0]]
    else:
        roots = [alias.name.split(".")[0] for alias in node.names]
    for root in roots:
        if root not in PYTHON_IMPORT_WHITELIST:
            msg = f"import of {root!r} is not allowed in the sandbox"
            raise UnsafePythonError(msg)


def check_python_source(source: str) -> None:
    """Statically reject sandbox-escaping Python before it is ever executed.

    Parses ``source`` and walks the AST, enforcing the import whitelist
    (:data:`PYTHON_IMPORT_WHITELIST`, no relative imports) and denying the
    escape-shaped builtins (``eval``/``exec``/``open``/``__import__``/…) and dunder
    attributes (``__subclasses__``/``__globals__``/…). A :class:`SyntaxError` in the
    source is reported as a policy violation so the caller never spawns a subprocess
    for code that cannot run.

    Args:
        source: the Python source the model wants to run.

    Raises:
        UnsafePythonError: on a syntax error, a non-whitelisted import, or a
            forbidden name or attribute access.

    Example:
        >>> check_python_source("import os")
        Traceback (most recent call last):
        veritas.security.UnsafePythonError: import of 'os' is not allowed in the sandbox
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as err:
        msg = f"could not parse Python source: {err}"
        raise UnsafePythonError(msg) from err
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            _check_import(node)
        elif isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
            msg = f"use of {node.id!r} is not allowed in the sandbox"
            raise UnsafePythonError(msg)
        elif isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_ATTRS:
            msg = f"access to attribute {node.attr!r} is not allowed in the sandbox"
            raise UnsafePythonError(msg)
