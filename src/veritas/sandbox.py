"""Run model-written Python in an isolated subprocess and persist results (M2).

``run_python`` is the Python half of the execution layer. The source passes the static
AST gate (:func:`veritas.security.check_python_source`), then runs in a separate
process with CPU/memory limits, no network egress, an ephemeral working directory, and
only the requested datasets exposed as DataFrames (``df`` when exactly one is loaded,
plus a ``datasets`` map). The full result frame and any figures persist as artifacts;
only a bounded, sanitized preview and capped stdout re-enter model context.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from veritas.execute import tabular_preview
from veritas.security import PREVIEW_BYTE_CAP, check_python_source, sanitize_text
from veritas.session import ArtifactRecord, new_id, quote_identifier

if TYPE_CHECKING:
    from collections.abc import Sequence

    from veritas.session import InvestigationSession

DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_MEMORY_BYTES = 2 * 1024**3
_CHILD_MODULE = "veritas._sandbox_child"
_CPU_GRACE_SECONDS = 5


def run_python(
    session: InvestigationSession,
    code: str,
    dataset_ids: Sequence[str] | None = None,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> ArtifactRecord:
    """Run sandboxed Python over the session's datasets and persist it as an artifact.

    The code is statically vetted, then executed in an isolated subprocess. A
    ``result`` DataFrame becomes a Parquet artifact with a bounded preview; figures are
    saved as PNG artifacts; stdout is captured (bounded). Failures — user exceptions,
    timeouts, or a crashed sandbox — are recorded as ``error`` artifacts, never raised
    (except a policy violation, which is raised before anything runs).

    Args:
        session: the investigation session providing datasets and the artifact store.
        code: the Python source to execute.
        dataset_ids: dataset ids to expose; defaults to every registered dataset.
        timeout_seconds: wall-clock limit for the subprocess.

    Returns:
        An :class:`~veritas.session.ArtifactRecord` describing the run.

    Raises:
        UnsafePythonError: if ``code`` fails the static AST policy (before execution).
        UnknownDatasetError: if a requested ``dataset_id`` is not registered.

    Example:
        ``record = run_python(session, "result = df.describe()")``
    """
    check_python_source(code)
    ids = (
        list(dataset_ids)
        if dataset_ids is not None
        else [record.dataset_id for record in session.list_datasets()]
    )
    artifact_id = new_id("art")
    with tempfile.TemporaryDirectory(prefix="veritas_sandbox_") as tmp_name:
        tmp = Path(tmp_name)
        outcome = _run_child(session, code, ids, tmp, timeout_seconds)
        record = _build_record(session, artifact_id, code, tmp, outcome)
    session.register_artifact(record)
    return record


def _child_env(tmp: Path) -> dict[str, str]:
    """Build a minimal, network-free environment for the sandbox subprocess."""
    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL") if key in os.environ}
    env.update(
        {
            "MPLBACKEND": "Agg",
            "MPLCONFIGDIR": str(tmp),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        }
    )
    return env


def _export_datasets(
    session: InvestigationSession, dataset_ids: list[str], tmp: Path
) -> dict[str, str]:
    """Export each requested dataset table to a Parquet file in the sandbox temp dir."""
    paths: dict[str, str] = {}
    for dataset_id in dataset_ids:
        record = session.get_dataset(dataset_id)
        path = tmp / f"{dataset_id}.parquet"
        table = quote_identifier(record.table_name)
        session.conn.sql(f"SELECT * FROM {table}").write_parquet(str(path))
        paths[dataset_id] = str(path)
    return paths


def _run_child(
    session: InvestigationSession,
    code: str,
    dataset_ids: list[str],
    tmp: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Export datasets, run the child subprocess, and interpret what it produced."""
    figure_dir = tmp / "figures"
    figure_dir.mkdir()
    manifest_path = tmp / "manifest.json"
    config = {
        "code": code,
        "datasets": _export_datasets(session, dataset_ids, tmp),
        "result_path": str(tmp / "result.parquet"),
        "figure_dir": str(figure_dir),
        "manifest_path": str(manifest_path),
        "cpu_seconds": timeout_seconds + _CPU_GRACE_SECONDS,
        "memory_bytes": DEFAULT_MEMORY_BYTES,
    }
    config_path = tmp / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    returncode, stderr, timed_out = _spawn_child(config_path, tmp, timeout_seconds)
    if timed_out:
        return {
            "status": "error",
            "manifest": None,
            "error": f"execution exceeded the {timeout_seconds}s time limit",
        }
    return _read_outcome(manifest_path, returncode, stderr)


def _spawn_child(config_path: Path, tmp: Path, timeout_seconds: int) -> tuple[int, str, bool]:
    """Run the child in its own process group; on timeout kill the whole group.

    ``start_new_session=True`` makes the child a session/group leader, so a script that
    forked or daemonized cannot outlive the wall-clock timeout — the real backstop where
    ``RLIMIT_AS``/``RLIMIT_CPU`` are unenforced (e.g. macOS).
    """
    proc = subprocess.Popen(
        [sys.executable, "-m", _CHILD_MODULE, str(config_path)],
        cwd=str(tmp),
        env=_child_env(tmp),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        _, stderr = proc.communicate(timeout=timeout_seconds)
        return proc.returncode, stderr, False
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.kill()
        _, stderr = proc.communicate()
        return proc.returncode, stderr or "", True


def _read_outcome(manifest_path: Path, returncode: int, stderr: str) -> dict[str, Any]:
    """Turn the child's exit state and manifest (if any) into an outcome dict."""
    if not manifest_path.exists():
        detail = sanitize_text(stderr or "no output", cap=PREVIEW_BYTE_CAP)
        return {
            "status": "error",
            "manifest": None,
            "error": (
                f"sandbox process produced no result (exit {returncode}); "
                f"likely a CPU, memory, or time limit. stderr: {detail}"
            ),
        }
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {
        "status": manifest.get("status", "error"),
        "manifest": manifest,
        "error": manifest.get("error"),
    }


def _build_record(
    session: InvestigationSession,
    artifact_id: str,
    code: str,
    tmp: Path,
    outcome: dict[str, Any],
) -> ArtifactRecord:
    """Assemble the ArtifactRecord from the sandbox outcome, persisting result/figures."""
    created_at = datetime.now(UTC)
    manifest = outcome["manifest"]
    if manifest is None:
        return ArtifactRecord(
            artifact_id=artifact_id,
            kind="python",
            created_at=created_at,
            source=code,
            status="error",
            error=sanitize_text(outcome["error"], cap=PREVIEW_BYTE_CAP),
        )
    raw_stdout = manifest.get("stdout")
    stdout = sanitize_text(raw_stdout, cap=PREVIEW_BYTE_CAP) if raw_stdout else None
    figure_paths = _persist_figures(session, artifact_id, manifest.get("figures") or [])
    if manifest.get("status") != "ok":
        return ArtifactRecord(
            artifact_id=artifact_id,
            kind="python",
            created_at=created_at,
            source=code,
            status="error",
            stdout=stdout,
            error=sanitize_text(manifest.get("error") or "sandbox error", cap=PREVIEW_BYTE_CAP),
            figure_paths=figure_paths,
        )
    fields = _result_fields(session, artifact_id, tmp, manifest)
    return ArtifactRecord(
        artifact_id=artifact_id,
        kind="python",
        created_at=created_at,
        source=code,
        status="ok",
        stdout=stdout,
        figure_paths=figure_paths,
        **fields,
    )


def _result_fields(
    session: InvestigationSession, artifact_id: str, tmp: Path, manifest: dict[str, Any]
) -> dict[str, Any]:
    """Persist a tabular result (if any) and return the artifact's result-shaped fields."""
    source_result = tmp / "result.parquet"
    if manifest.get("data") and source_result.exists():
        dest = session.artifacts_dir / f"{artifact_id}.parquet"
        shutil.move(str(source_result), str(dest))
        columns, types, row_count, preview = tabular_preview(session.conn, dest)
        return {
            "row_count": row_count,
            "columns": columns,
            "column_types": types,
            "data_path": str(dest.relative_to(session.session_dir)),
            "preview": preview,
        }
    if manifest.get("result_repr") is not None:
        return {"preview": sanitize_text(manifest["result_repr"], cap=PREVIEW_BYTE_CAP)}
    return {}


def _persist_figures(
    session: InvestigationSession, artifact_id: str, figures: list[str]
) -> list[str]:
    """Move each saved figure into the artifact store and return relative paths."""
    saved: list[str] = []
    for index, source in enumerate(figures):
        source_path = Path(source)
        if not source_path.exists():
            continue
        dest = session.artifacts_dir / f"{artifact_id}_fig{index}.png"
        shutil.move(str(source_path), str(dest))
        saved.append(str(dest.relative_to(session.session_dir)))
    return saved
