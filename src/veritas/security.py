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
    # Every character that can forge a new logical line becomes a visible "\n": ASCII
    # CR/LF, vertical tab and form feed, and the Unicode line/paragraph separators
    # (U+2028/U+2029, categories Zl/Zp — *not* dropped by the C-category filter below).
    for code in (0x0B, 0x0C, 0x2028, 0x2029):  # VT, FF, line/para separators
        text = text.replace(chr(code), "\n")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\n", "\\n").replace("\t", " ")
    # Drop every Unicode control/format char (C0, C1, NUL, zero-width, bidi overrides);
    # the visible "\n" inserted above survives because backslash and 'n' are not in C.
    text = "".join(ch for ch in text if not unicodedata.category(ch).startswith("C"))
    text = re.sub(r" {2,}", " ", text).strip()
    if len(text) > cap:
        text = text[: cap - 1].rstrip() + "…"
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
        "read_json_objects",
        "read_json_objects_auto",
        "read_ndjson",
        "read_ndjson_auto",
        "read_ndjson_objects",
        "read_text",
        "read_blob",
        "glob",
        "sniff_csv",
        "parquet_metadata",
        "parquet_schema",
        "parquet_file_metadata",
        "parquet_full_metadata",
        "parquet_kv_metadata",
        "parquet_bloom_probe",
        "csv_sniff",
        "delta_scan",
        "iceberg_scan",
        "iceberg_metadata",
        "iceberg_snapshots",
        "arrow_scan",
        "read_arrow",
        "shapefile_meta",
        "st_read",
        "st_read_meta",
        "read_xlsx",
    }
)
"""Table/scalar functions that read the filesystem or network; denied in run_sql."""

_FUNCTION_CALL_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_STRING_SENTINEL = "\x00"  # marks where a single-quoted string literal stood
_TABLE_SOURCE_STRING_RE = re.compile(rf"(?i)\b(?:from|join)\s+{_STRING_SENTINEL}")
# A string literal that looks like a file path/URL — i.e. would trigger a DuckDB
# replacement-scan read. Matched either by a path-like start (``/``, ``./``, ``~``,
# ``C:\``, ``\\``) or by a data-file extension at the end.
_PATH_LITERAL_RE = re.compile(
    r"(?i)^\s*(?:~|\.{0,2}/|[a-z]:[\\/]|\\\\)"
    r"|\.(?:csv|tsv|psv|parquet|pq|json|jsonl|ndjson|xlsx|xls|arrow"
    r"|feather|orc|avro|db|duckdb|sqlite|wal)\s*$"
)


def _read_quoted(sql: str, start: int, quote: str) -> tuple[str, int]:
    """Read a quoted token from its opening quote; a doubled quote ('' or "") escapes it."""
    index, length = start + 1, len(sql)
    buffer: list[str] = []
    while index < length:
        if sql[index] == quote:
            if index + 1 < length and sql[index + 1] == quote:
                buffer.append(quote)
                index += 2
                continue
            return "".join(buffer), index + 1
        buffer.append(sql[index])
        index += 1
    return "".join(buffer), index  # unterminated quote: treat the remainder as content


def _normalize_sql(sql: str) -> tuple[str, list[str]]:
    """Strip comments and blank string literals, *ignoring* markers that sit inside strings.

    A naive regex strip treats a ``--`` or ``/*`` that appears inside a string literal as a
    real comment and deletes the call hidden after it, defeating the keyword/function scans
    (e.g. ``SELECT '/*' AS m, * FROM read_csv('x')``). This char-level pass instead tracks
    string and identifier quoting, so it returns the SQL with comments removed, every
    single-quoted string replaced by a NUL sentinel, and double-quoted identifiers reduced
    to bare (space-padded) text — plus the list of string-literal contents for path checks.
    """
    out: list[str] = []
    literals: list[str] = []
    index, length = 0, len(sql)
    while index < length:
        char = sql[index]
        if char == "'":
            content, index = _read_quoted(sql, index, "'")
            literals.append(content)
            out.append(_STRING_SENTINEL)
        elif char == '"':
            content, index = _read_quoted(sql, index, '"')
            out.append(f" {content} ")
        elif char == "-" and index + 1 < length and sql[index + 1] == "-":
            end = sql.find("\n", index)
            index = length if end == -1 else end
        elif char == "/" and index + 1 < length and sql[index + 1] == "*":
            end = sql.find("*/", index + 2)
            index = length if end == -1 else end + 2
        else:
            out.append(char)
            index += 1
    return "".join(out), literals


def validate_select(conn: duckdb.DuckDBPyConnection, sql: str) -> str:
    """Validate that ``sql`` is a single, read-only ``SELECT`` and return it stripped.

    Layers, all deterministic, over a string/comment-aware normalization of the SQL
    (:func:`_normalize_sql`, so a call cannot be smuggled inside a string or comment):
    DuckDB's parser must see exactly one statement of type ``SELECT``; the first keyword
    must not be a settings/DDL/DML/transaction verb (DuckDB classifies ``PRAGMA``/``CALL``
    as ``SELECT``); no filesystem/network table function may appear; and no string literal
    may sit in table-source position or look like a file path — both of which trigger
    DuckDB's *replacement scan*, a file read with no ``read_csv`` token (DECISIONS.md,
    D-019/D-024). This is a denylist hardened against grammar tricks, not an
    engine-enforced jail; see D-024 for the residual risk and the deferred engine fix.

    Args:
        conn: a DuckDB connection used only to *parse* (never execute) the statement.
        sql: the candidate query.

    Returns:
        The original ``sql`` with surrounding whitespace stripped.

    Raises:
        UnsafeSqlError: if the statement is empty, multiple, non-SELECT, leads with a
            denied keyword, references a denied function, or uses a file-path string.

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

    normalized, literals = _normalize_sql(stripped)
    first = _IDENTIFIER_RE.search(normalized)
    if first is not None and first.group(0).lower() in _SQL_LEADING_KEYWORD_DENYLIST:
        msg = f"statement keyword {first.group(0).upper()!r} is not allowed in run_sql"
        raise UnsafeSqlError(msg)
    for match in _FUNCTION_CALL_RE.finditer(normalized):
        name = match.group(1).lower()
        if name in _SQL_FORBIDDEN_FUNCTIONS:
            msg = f"function {name!r} reads the filesystem or network and is not allowed"
            raise UnsafeSqlError(msg)
    if _TABLE_SOURCE_STRING_RE.search(normalized):
        msg = "a string literal cannot be a table source (file read via replacement scan)"
        raise UnsafeSqlError(msg)
    for literal in literals:
        if _PATH_LITERAL_RE.search(literal):
            msg = f"string literal {literal[:60]!r} looks like a file path and is not allowed"
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
    {
        "eval",
        "exec",
        "compile",
        "__import__",
        "__builtins__",
        "open",
        "input",
        "breakpoint",
        "exit",
        "quit",
        # introspection/escape enablers: getattr with a runtime-built dunder string defeats
        # the _FORBIDDEN_ATTRS check, and globals()/vars() reach the builtins/object graph.
        "getattr",
        "setattr",
        "delattr",
        "globals",
        "locals",
        "vars",
    }
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
