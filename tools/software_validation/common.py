"""Shared utilities for isolated EdgeER software validation."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import csv
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import random
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterable, Iterator, Sequence


ROOT = Path(__file__).resolve().parents[2]
SERVER_DIR = ROOT / "server"
EDGE_DIR = ROOT / "edge-node"
DEFAULT_OUTPUT = ROOT / "results" / "software_validation"

if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))


RESULT_SUBDIRECTORIES = (
    "automated_tests/failures",
    "static_checks",
    "packet_validation",
    "device_lifecycle",
    "workflow",
    "database",
    "scalability",
    "reconnection",
    "api_benchmarks",
    "dashboard/screenshots/1366x768",
    "dashboard/screenshots/1920x1080",
    "dashboard/screenshots/tablet",
    "dashboard/screenshots/narrow_laptop",
    "shared_ppg",
    "plots",
)


@dataclass
class GroupResult:
    """Outcome returned by one validation group."""

    name: str
    status: str
    summary: dict[str, Any] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    critical: bool = False
    duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ValidationContext:
    """Immutable configuration and paths shared by group runners."""

    output_dir: Path
    temp_dir: Path
    mode: str
    seed: int
    device_counts: list[int]
    duration_seconds: float
    keep_temp_data: bool
    skip_browser: bool
    skip_scalability: bool
    python: str = sys.executable


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def prepare_results_directory(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for relative in RESULT_SUBDIRECTORIES:
        (output_dir / relative).mkdir(parents=True, exist_ok=True)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def write_csv(path: Path, rows: Sequence[dict[str, Any]], fields: Sequence[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(fields or (rows[0].keys() if rows else ("note",)))
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        if rows:
            writer.writerows(rows)
        else:
            writer.writerow({fieldnames[0]: "No applicable rows were produced."})


def percentile(values: Sequence[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def latency_summary(values: Sequence[float]) -> dict[str, float]:
    if not values:
        return {key: 0.0 for key in ("min_ms", "mean_ms", "median_ms", "p95_ms", "p99_ms", "max_ms")}
    return {
        "min_ms": min(values),
        "mean_ms": statistics.fmean(values),
        "median_ms": statistics.median(values),
        "p95_ms": percentile(values, 95),
        "p99_ms": percentile(values, 99),
        "max_ms": max(values),
    }


def make_packet(
    patient_id: str,
    device_id: str,
    *,
    sequence: int = 1,
    now: datetime | None = None,
    updates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    timestamp = (now or datetime.now(timezone.utc)).isoformat()
    packet: dict[str, Any] = {
        "patient_id": patient_id,
        "device_id": device_id,
        "device_type": "SIMULATOR",
        "packet_sequence": sequence,
        "timestamp": timestamp,
        "device_send_time": timestamp,
        "heart_rate_bpm": 78.0,
        "spo2_percent": 98.0,
        "object_temp_c": 36.7,
        "ambient_temp_c": 24.0,
        "finger_detected": True,
        "signal_quality": "GOOD",
        "status": "GREEN",
        "reasons": [],
    }
    packet.update(updates or {})
    return packet


@contextmanager
def isolated_client(ctx: ValidationContext, name: str) -> Iterator[tuple[Any, Path]]:
    """Yield a TestClient backed by a group-specific temporary database."""

    import database
    import server
    from fastapi.testclient import TestClient

    database_path = ctx.temp_dir / f"{name}.db"
    old_path = database.DATABASE_PATH
    database.DATABASE_PATH = database_path
    try:
        with TestClient(server.app) as client:
            yield client, database_path
    finally:
        database.clear_in_memory_state()
        database.DATABASE_PATH = old_path


def run_command(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: float = 120.0,
    env: dict[str, str] | None = None,
) -> tuple[int, str, float]:
    started = time.perf_counter()
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    output = completed.stdout + completed.stderr
    return completed.returncode, output, time.perf_counter() - started


def git_output(*arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return (completed.stdout + completed.stderr).strip()


def database_snapshot(database_path: Path) -> dict[str, Any]:
    import sqlite3

    snapshot: dict[str, Any] = {
        "database_file": database_path.name,
        "database_bytes": database_path.stat().st_size if database_path.exists() else 0,
        "tables": {},
    }
    if not database_path.exists():
        return snapshot
    with sqlite3.connect(database_path) as connection:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        for table in tables:
            snapshot["tables"][table] = connection.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()[0]
        snapshot["integrity_check"] = connection.execute("PRAGMA integrity_check").fetchone()[0]
        snapshot["foreign_key_check"] = connection.execute("PRAGMA foreign_key_check").fetchall()
    return snapshot


def process_memory_bytes() -> int:
    """Return current process working-set bytes on Windows, or zero elsewhere."""

    if os.name != "nt":
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        get_current_process = ctypes.windll.kernel32.GetCurrentProcess
        get_current_process.restype = ctypes.c_void_p
        get_process_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
        get_process_memory_info.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), wintypes.DWORD]
        handle = get_current_process()
        if get_process_memory_info(handle, ctypes.byref(counters), counters.cb):
            return int(counters.WorkingSetSize)
    except (AttributeError, OSError):
        pass
    return 0


def package_versions(names: Iterable[str]) -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for name in names:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def deterministic_random(seed: int, namespace: str) -> random.Random:
    return random.Random(f"{seed}:{namespace}")
