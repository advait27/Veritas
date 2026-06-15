"""Subprocess entry point that runs sandboxed, model-written Python (M2).

Launched as ``python -m veritas._sandbox_child <config.json>`` by
:func:`veritas.sandbox.run_python`. The source has already passed the static AST gate
(:func:`veritas.security.check_python_source`) in the parent; this process is the
*runtime* containment layer. It applies resource limits, neutralizes network egress,
loads the requested datasets as DataFrames, runs the code with stdout captured, and
writes a JSON manifest describing the result.

The security-relevant pieces are split into importable functions so they are
unit-tested in-process; only :func:`main` runs solely inside the subprocess.
"""

from __future__ import annotations

import _socket
import builtins
import contextlib
import io
import json
import resource
import socket
import sys
import traceback
from pathlib import Path
from typing import Any, NoReturn

import duckdb
import pandas as pd

from veritas.security import PYTHON_IMPORT_WHITELIST

NETWORK_DISABLED_MESSAGE = "network access is disabled in the Veritas sandbox"

# Patched on both the high-level `socket` module and the low-level `_socket` extension it
# is built on — patching only `socket` leaves `_socket.socket`/`fromfd` as live egress.
_SOCKET_ENTRY_POINTS = (
    "socket",
    "SocketType",
    "create_connection",
    "getaddrinfo",
    "socketpair",
    "fromfd",
    "dup",
)
_RAW_SOCKET_ENTRY_POINTS = ("socket", "fromfd", "dup")


def block_network() -> None:
    """Disable network egress by replacing the ``socket`` entry points with raisers.

    Defense in depth behind the import whitelist: a whitelisted library (e.g. pandas
    reading a URL) could still open a socket, so socket creation, outbound connection,
    and name resolution are neutralized on both the high-level ``socket`` module and the
    low-level ``_socket`` C extension it is built on.

    Example:
        >>> block_network()
        >>> import socket
        >>> socket.create_connection(("127.0.0.1", 80))
        Traceback (most recent call last):
        OSError: network access is disabled in the Veritas sandbox
    """

    def _blocked(*_args: object, **_kwargs: object) -> NoReturn:
        raise OSError(NETWORK_DISABLED_MESSAGE)

    for name in _SOCKET_ENTRY_POINTS:  # all present on the high-level socket module
        setattr(socket, name, _blocked)
    for name in _RAW_SOCKET_ENTRY_POINTS:  # _socket is leaner; only patch what it exposes
        if hasattr(_socket, name):
            setattr(_socket, name, _blocked)


_UNSAFE_BUILTINS = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "open",
        "input",
        "breakpoint",
        "exit",
        "quit",
        "help",
        "getattr",
        "setattr",
        "delattr",
        "globals",
        "vars",
        "memoryview",
    }
)


def safe_builtins() -> dict[str, Any]:
    """Return a restricted ``__builtins__`` mapping for the user namespace.

    The runtime backstop to the parent's static AST gate: even if a script reaches its
    own ``__builtins__`` (e.g. via a gadget the AST scan missed), the dangerous entries
    are simply absent. ``open``/``eval``/``exec``/``getattr``/… are removed, and
    ``__import__`` is replaced by a guard that enforces :data:`PYTHON_IMPORT_WHITELIST`
    at runtime — so a whitelisted ``import pandas`` works while ``__import__('os')`` does
    not. Already-imported libraries keep the real builtins via their own module globals,
    so this only constrains the user code, not the analysis stack.

    Example:
        >>> "open" in safe_builtins()
        False
    """
    safe = {
        name: getattr(builtins, name)
        for name in dir(builtins)
        if not name.startswith("_") and name not in _UNSAFE_BUILTINS
    }
    safe["__build_class__"] = builtins.__build_class__  # needed for `class` statements

    def _guarded_import(name: str, *args: Any, **kwargs: Any) -> Any:
        root = name.split(".", maxsplit=1)[0]
        if root not in PYTHON_IMPORT_WHITELIST:
            msg = f"import of {root!r} is not allowed in the sandbox"
            raise ImportError(msg)
        return builtins.__import__(name, *args, **kwargs)

    safe["__import__"] = _guarded_import
    return safe


def apply_limits(cpu_seconds: int | None, memory_bytes: int | None) -> None:
    """Best-effort CPU-time and address-space caps via ``setrlimit``.

    ``RLIMIT_CPU`` is reliable on Linux and macOS. ``RLIMIT_AS`` is not enforceable on
    macOS (``setrlimit`` raises), so address-space capping is best-effort and silently
    skipped where the OS refuses it; the parent's wall-clock timeout is the backstop.

    Args:
        cpu_seconds: hard CPU-time limit in seconds, or ``None`` to leave it unset.
        memory_bytes: hard address-space limit in bytes, or ``None`` to leave it unset.

    Example:
        >>> apply_limits(60, 2 * 1024**3)
    """
    if cpu_seconds is not None:
        with contextlib.suppress(ValueError, OSError):
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    if memory_bytes is not None:
        with contextlib.suppress(ValueError, OSError):
            resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))


def load_datasets(dataset_paths: dict[str, str]) -> dict[str, pd.DataFrame]:
    """Load each exported dataset Parquet into a pandas DataFrame keyed by dataset id.

    Args:
        dataset_paths: mapping of ``dataset_id`` to the Parquet path exported by the
            parent.

    Returns:
        A mapping of ``dataset_id`` to its DataFrame.

    Example:
        >>> load_datasets({})
        {}
    """
    conn = duckdb.connect()
    try:
        return {
            dataset_id: conn.execute("SELECT * FROM read_parquet(?)", [path]).df()
            for dataset_id, path in dataset_paths.items()
        }
    finally:
        conn.close()


def run_in_namespace(
    code: str, namespace: dict[str, Any], result_path: str, figure_dir: str
) -> dict[str, Any]:
    """Execute ``code`` in ``namespace``, capturing stdout, result, and figures.

    A ``result`` DataFrame/Series in the namespace is written to ``result_path`` as
    Parquet; any other ``result`` is captured as a repr. Open matplotlib figures are
    saved under ``figure_dir``. User exceptions are caught and reported in the manifest
    rather than raised — a failed run is still a recorded receipt.

    Args:
        code: the (already AST-vetted) Python source to execute.
        namespace: the globals to run against (datasets pre-loaded by the caller).
        result_path: where to write a tabular ``result`` as Parquet.
        figure_dir: directory to save any open matplotlib figures into.

    Returns:
        A manifest dict: ``status``, ``stdout``, ``error``, ``data`` (bool),
        ``figures`` (paths), and ``result_repr``.

    Example:
        >>> run_in_namespace("x = 1", {}, "r.parquet", ".")["status"]
        'ok'
    """
    manifest: dict[str, Any] = {
        "status": "ok",
        "stdout": "",
        "error": None,
        "data": False,
        "figures": [],
        "result_repr": None,
    }
    namespace["__builtins__"] = safe_builtins()  # runtime backstop to the static AST gate
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            exec(compile(code, "<veritas-sandbox>", "exec"), namespace)
    except Exception:
        manifest["status"] = "error"
        manifest["error"] = traceback.format_exc()
        manifest["stdout"] = buffer.getvalue()
        return manifest
    manifest["stdout"] = buffer.getvalue()
    _emit_result(namespace.get("result"), Path(result_path), manifest)
    manifest["figures"] = _save_figures(Path(figure_dir))
    return manifest


def _emit_result(result: object, result_path: Path, manifest: dict[str, Any]) -> None:
    """Persist a DataFrame/Series ``result`` to Parquet, else capture its repr."""
    if result is None:
        return
    if isinstance(result, pd.Series):
        result = result.to_frame()
    if isinstance(result, pd.DataFrame):
        _write_result_parquet(result, result_path)
        manifest["data"] = True
    else:
        manifest["result_repr"] = repr(result)[:2000]


def _write_result_parquet(frame: pd.DataFrame, path: Path) -> None:
    """Write a result frame to Parquet via DuckDB (no pyarrow), tz-normalized to UTC."""
    frame = frame.copy()
    for column in frame.columns:
        if isinstance(frame[column].dtype, pd.DatetimeTZDtype):
            frame[column] = frame[column].dt.tz_convert("UTC").dt.tz_localize(None)
    conn = duckdb.connect()
    try:
        conn.register("__veritas_result", frame)
        conn.sql("SELECT * FROM __veritas_result").write_parquet(str(path))
    finally:
        conn.close()


def _save_figures(figure_dir: Path) -> list[str]:
    """Save every open matplotlib figure as a PNG and return the paths."""
    pyplot: Any = sys.modules.get("matplotlib.pyplot")
    if pyplot is None:
        return []
    paths: list[str] = []
    for index, number in enumerate(pyplot.get_fignums()):
        path = figure_dir / f"fig_{index}.png"
        pyplot.figure(number).savefig(path)
        paths.append(str(path))
    pyplot.close("all")  # leave no global figure state behind (self-contained)
    return paths


def main(config_path: str) -> None:  # pragma: no cover - only runs in the subprocess
    """Read the config, run the sandboxed code, and write the manifest."""
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    apply_limits(config["cpu_seconds"], config["memory_bytes"])
    block_network()
    datasets = load_datasets(config["datasets"])
    namespace: dict[str, Any] = {"datasets": datasets}
    if len(datasets) == 1:
        namespace["df"] = next(iter(datasets.values()))
    manifest = run_in_namespace(
        config["code"], namespace, config["result_path"], config["figure_dir"]
    )
    Path(config["manifest_path"]).write_text(json.dumps(manifest), encoding="utf-8")


if __name__ == "__main__":  # pragma: no cover - only runs in the subprocess
    main(sys.argv[1])
