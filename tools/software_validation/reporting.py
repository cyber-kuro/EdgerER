"""Environment capture, plotting, and master-summary generation."""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
from typing import Any, Iterable

from common import (
    EDGE_DIR,
    ROOT,
    SERVER_DIR,
    GroupResult,
    ValidationContext,
    git_output,
    package_versions,
    utc_now,
    write_json,
    write_text,
)


def _total_memory_bytes() -> int | None:
    if os.name != "nt":
        return None
    try:
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.ullTotalPhys)
    except (AttributeError, OSError):
        pass
    return None


def capture_environment(ctx: ValidationContext) -> dict[str, Any]:
    packages = package_versions(
        ("fastapi", "starlette", "httpx", "uvicorn", "numpy", "scipy", "matplotlib", "pytest", "playwright", "selenium", "psutil", "pandas")
    )
    environment = {
        "captured_at_utc": utc_now(),
        "operating_system": platform.platform(),
        "python_version": platform.python_version(),
        "processor": platform.processor() or platform.machine(),
        "logical_cpu_count": os.cpu_count(),
        "total_memory_bytes": _total_memory_bytes(),
        "sqlite_version": sqlite3.sqlite_version,
        "git_commit": git_output("rev-parse", "HEAD"),
        "git_branch": git_output("branch", "--show-current"),
        "git_dirty": bool(git_output("status", "--porcelain")),
        "installed_package_versions": packages,
        "selected_test_mode": ctx.mode,
        "deterministic_seed": ctx.seed,
        "device_counts": ctx.device_counts,
        "duration_seconds_per_scalability_run": ctx.duration_seconds,
        "database_path_type": "isolated temporary SQLite files",
        "production_database_used": False,
    }
    write_json(ctx.output_dir / "test_environment.json", environment)
    git_information = "\n".join(
        (
            f"Commit: {environment['git_commit']}",
            f"Branch: {environment['git_branch']}",
            f"Dirty: {environment['git_dirty']}",
            "",
            "git status --short:",
            git_output("status", "--short"),
            "",
            "Tracked files:",
            git_output("ls-files"),
        )
    )
    write_text(ctx.output_dir / "git_information.txt", git_information + "\n")

    sys.path.insert(0, str(SERVER_DIR))
    import models
    import packet_validation

    configuration = {
        "offline_after_seconds": models.OFFLINE_AFTER_SECONDS,
        "recheck_after_seconds": models.RECHECK_AFTER_SECONDS,
        "maximum_packet_age_seconds": models.MAX_DEVICE_PACKET_AGE_SECONDS,
        "maximum_clock_lead_seconds": models.MAX_DEVICE_CLOCK_LEAD_SECONDS,
        "rr_confidence_gate": models.RR_CONFIDENCE_ELIGIBILITY_GATE,
        "numeric_ranges": packet_validation.NUMERIC_RANGES,
        "device_statuses": sorted(models.DEVICE_STATUSES),
        "simulator_scenarios": ["stable", "yellow_low_oxygen", "orange_trend", "crashing", "no_finger"],
        "benchmark_packet_interval_seconds": 0.0,
    }
    write_json(ctx.output_dir / "configuration_snapshot.json", configuration)
    return environment


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def generate_plots(ctx: ValidationContext) -> list[str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plot_dir = ctx.output_dir / "plots"
    generated: list[str] = []

    def save_line(filename: str, x: list[float], y: list[float], xlabel: str, ylabel: str, title: str) -> None:
        figure, axis = plt.subplots(figsize=(7.2, 4.4))
        axis.plot(x, y, marker="o", linewidth=1.8)
        axis.set(xlabel=xlabel, ylabel=ylabel, title=title)
        axis.grid(alpha=0.25)
        figure.tight_layout()
        figure.savefig(plot_dir / filename, dpi=160)
        plt.close(figure)
        generated.append(filename)

    scale = _read_csv(ctx.output_dir / "scalability" / "scalability_summary.csv")
    if scale:
        counts = [float(row["device_count"]) for row in scale]
        save_line("latency_vs_device_count.png", counts, [float(row["mean_latency_ms"]) for row in scale], "Simulated device count", "Mean latency (ms)", "Request latency versus simulated device count")
        save_line("throughput_vs_device_count.png", counts, [float(row["throughput_requests_per_second"]) for row in scale], "Simulated device count", "Throughput (requests/s)", "Throughput versus simulated device count")
        save_line("cpu_vs_device_count.png", counts, [float(row["cpu_average_percent"]) for row in scale], "Simulated device count", "Normalized process CPU (%)", "CPU use versus simulated device count")
        save_line("memory_vs_device_count.png", counts, [float(row["memory_peak_bytes"]) / 1048576 for row in scale], "Simulated device count", "Peak working set (MiB)", "Memory use versus simulated device count")

    growth = _read_csv(ctx.output_dir / "database" / "database_growth.csv")
    if growth:
        save_line("database_growth_vs_packets.png", [float(row["packets_accepted"]) for row in growth], [float(row["bytes_added"]) for row in growth], "Accepted packets", "Database growth (bytes)", "SQLite growth versus accepted packets")

    packet_summary_path = ctx.output_dir / "packet_validation" / "packet_validation_summary.json"
    if packet_summary_path.exists():
        summary = json.loads(packet_summary_path.read_text(encoding="utf-8"))
        figure, axis = plt.subplots(figsize=(7.2, 4.4))
        labels = ["Passed", "Failed", "False accept", "False reject"]
        values = [summary["passed"], summary["failed"], summary["false_acceptances"], summary["false_rejections"]]
        axis.bar(labels, values, color=["#2b7a68", "#a3424a", "#cf8a19", "#607784"])
        axis.set(ylabel="Case count", title="Packet-validation outcomes")
        figure.tight_layout()
        figure.savefig(plot_dir / "packet_validation_outcomes.png", dpi=160)
        plt.close(figure)
        generated.append("packet_validation_outcomes.png")

    reconnect = _read_csv(ctx.output_dir / "reconnection" / "reconnection_results.csv")
    if reconnect:
        cycles = [float(row["cycle"]) for row in reconnect]
        figure, axis = plt.subplots(figsize=(7.2, 4.4))
        axis.plot(cycles, [float(row["observed_detection_seconds"]) for row in reconnect], marker="o", label="Offline detection (s)")
        axis.plot(cycles, [float(row["recovery_time_ms"]) / 1000 for row in reconnect], marker="s", label="Recovery request (s)")
        axis.set(xlabel="Cycle", ylabel="Time (seconds)", title="Simulated offline detection and recovery")
        axis.legend()
        axis.grid(alpha=0.25)
        figure.tight_layout()
        figure.savefig(plot_dir / "reconnection_times.png", dpi=160)
        plt.close(figure)
        generated.append("reconnection_times.png")
    return generated


def generate_master_summary(
    ctx: ValidationContext,
    environment: dict[str, Any],
    results: Iterable[GroupResult],
    plots: list[str],
) -> dict[str, Any]:
    groups = [result.to_dict() for result in results]
    failures = [f"{group['name']}: {item}" for group in groups for item in group["failures"]]
    blocked = [f"{group['name']}: {item}" for group in groups for item in group["blocked"]]
    critical_failures = [group["name"] for group in groups if group["critical"] and group["status"] == "FAIL"]
    automated = next((group["summary"] for group in groups if group["name"] == "automated_tests"), {})
    packet = next((group["summary"] for group in groups if group["name"] == "packet_validation"), {})
    static = next((group["summary"] for group in groups if group["name"] == "static_checks"), {})
    ready_for_physical = not any(group["critical"] and group["status"] == "FAIL" for group in groups if group["name"] != "static_checks")
    ready_for_tag = ready_for_physical and not critical_failures and not static.get("tracked_databases") and not static.get("possible_secrets")
    summary = {
        "generated_at_utc": utc_now(),
        "mode": ctx.mode,
        "environment": environment,
        "groups": groups,
        "automated_test_totals": automated.get("totals", {}),
        "packet_validation": packet,
        "all_failures": failures,
        "all_blocked": blocked,
        "critical_failures": critical_failures,
        "plots": plots,
        "ready_for_physical_validation": ready_for_physical,
        "ready_for_v0_9_release_candidate": ready_for_tag,
    }
    write_json(ctx.output_dir / "software_validation_summary.json", summary)
    write_json(ctx.output_dir / f"{ctx.mode}_run_summary.json", summary)

    group_lines = [f"- **{group['name']}**: {group['status']} ({group['duration_seconds']:.3f} s)" for group in groups]
    failure_lines = [f"- {item}" for item in failures] or ["- None."]
    blocked_lines = [f"- {item}" for item in blocked] or ["- None."]
    reproduction_full = "python tools/software_validation/run_all_software_validation.py --full --duration-seconds 300 --device-counts 1,5,10,20"
    markdown = f"""# EdgeER software validation summary

## 1. Executive summary

This software-only engineering validation ran against isolated temporary SQLite databases. It did not access Raspberry Pi GPIO/I2C, physical sensors, or the normal development database. Overall critical status: **{'FAIL' if critical_failures else 'PASS'}**.

## 2. Test environment

- UTC: {environment['captured_at_utc']}
- OS: {environment['operating_system']}
- Python: {environment['python_version']}
- Logical CPUs: {environment['logical_cpu_count']}
- Mode: {ctx.mode}
- Seed: {ctx.seed}

## 3. Git commit tested

`{environment['git_commit']}` on branch `{environment['git_branch']}`; dirty working tree: `{environment['git_dirty']}`.

## 4. Test groups executed

{chr(10).join(group_lines)}

## 5. Automated test totals

```json
{json.dumps(automated.get('totals', {}), indent=2)}
```

## 6–8. Packet validation

- Total cases: {packet.get('total_cases', 0)}
- Passed: {packet.get('passed', 0)}
- Failed: {packet.get('failed', 0)}
- False acceptances: {packet.get('false_acceptances', 0)}
- False rejections: {packet.get('false_rejections', 0)}

## 9–17. Group details

Raw CSV, JSON, terminal output, environment information, database checks, scalability measurements, API latency, reconnection measurements, dashboard results, shared-PPG assertions, and repository findings are stored in their named subdirectories. Generated plots: {', '.join(plots) if plots else 'none'}.

## 18. Failed tests and checks

{chr(10).join(failure_lines)}

## 19. Blocked tests

{chr(10).join(blocked_lines)}

## 20. Limitations

- TestClient latency is not network or production deployment latency.
- Timestamp injection accelerates offline detection tests.
- Software architecture tests do not establish physiological accuracy.
- Physical sensors, GPIO LEDs, Raspberry Pi stability, manual RR, and reference-device comparisons were not tested.
- Full-mode runs shorter than 300 seconds per configuration are explicitly marked shortened.

## 21. Reproduction commands

```powershell
python tools/software_validation/run_all_software_validation.py --quick
python tools/software_validation/run_all_software_validation.py --full
{reproduction_full}
```

## 22. Ready for physical validation

**{ready_for_physical}**. This means software-only critical checks did not block progression; it is not a clinical-performance claim.

## 23. Ready for v0.9 release-candidate checkpoint

**{ready_for_tag}**. Tracked databases or possible secrets block release readiness even when functional tests pass.

## 24. Critical blockers before v1.0

{chr(10).join(failure_lines if critical_failures else ['- Physical hardware, reference-device, and long-duration Raspberry Pi validation remain outstanding.'])}
"""
    write_text(ctx.output_dir / "software_validation_summary.md", markdown)
    write_text(ctx.output_dir / "README.md", "# EdgeER software-validation results\n\nGenerated by `tools/software_validation/run_all_software_validation.py`. See `software_validation_summary.md` for the master report and the subdirectories for raw evidence. Generated results are not committed automatically.\n")
    return summary
