"""Run the complete bounded EdgeER software-only validation suite."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import sys
import tempfile
import traceback

from common import DEFAULT_OUTPUT, GroupResult, ValidationContext, prepare_results_directory, write_json, write_text
from groups import (
    run_api_benchmarks,
    run_automated_tests,
    run_dashboard_checks,
    run_database_persistence,
    run_device_lifecycle,
    run_packet_validation,
    run_reconnection,
    run_scalability_and_growth,
    run_shared_ppg,
    run_static_checks,
    run_workflow,
)
from reporting import capture_environment, generate_master_summary, generate_plots


def parse_device_counts(value: str) -> list[int]:
    counts = sorted({int(item.strip()) for item in value.split(",") if item.strip()})
    if not counts or any(count <= 0 or count > 100 for count in counts):
        raise argparse.ArgumentTypeError("device counts must be integers from 1 to 100")
    return counts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run isolated EdgeER software validation.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--quick", action="store_true", help="Run the short bounded suite (default).")
    mode.add_argument("--full", action="store_true", help="Run all counts with three benchmark repetitions.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seed", type=int, default=20260720)
    parser.add_argument("--keep-temp-data", action="store_true")
    parser.add_argument("--skip-browser", action="store_true")
    parser.add_argument("--skip-scalability", action="store_true")
    parser.add_argument("--device-counts", type=parse_device_counts)
    parser.add_argument("--duration-seconds", type=float)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    mode = "full" if args.full else "quick"
    device_counts = args.device_counts or ([1, 5, 10, 20] if mode == "full" else [1, 5, 10])
    duration = args.duration_seconds if args.duration_seconds is not None else (300.0 if mode == "full" else 1.0)
    if duration <= 0 or duration > 3600:
        raise SystemExit("--duration-seconds must be greater than zero and no more than 3600")
    output_dir = args.output_dir.resolve()
    prepare_results_directory(output_dir)
    temporary = Path(tempfile.mkdtemp(prefix="edgeer-software-validation-"))
    matplotlib_config = temporary / "matplotlib"
    matplotlib_config.mkdir(parents=True, exist_ok=True)
    os.environ["MPLCONFIGDIR"] = str(matplotlib_config)
    ctx = ValidationContext(
        output_dir=output_dir,
        temp_dir=temporary,
        mode=mode,
        seed=args.seed,
        device_counts=device_counts,
        duration_seconds=duration,
        keep_temp_data=args.keep_temp_data,
        skip_browser=args.skip_browser,
        skip_scalability=args.skip_scalability,
    )
    results: list[GroupResult] = []
    try:
        environment = capture_environment(ctx)
        for runner in (
            run_automated_tests,
            run_packet_validation,
            run_device_lifecycle,
            run_workflow,
            run_database_persistence,
        ):
            result = runner(ctx)
            results.append(result)
            print(f"[{result.status}] {result.name} ({result.duration_seconds:.2f}s)", flush=True)
        scalability, growth = run_scalability_and_growth(ctx)
        results.extend((scalability, growth))
        print(f"[{scalability.status}] scalability ({scalability.duration_seconds:.2f}s)", flush=True)
        for runner in (run_reconnection, run_api_benchmarks, run_dashboard_checks, run_shared_ppg, run_static_checks):
            result = runner(ctx)
            results.append(result)
            print(f"[{result.status}] {result.name} ({result.duration_seconds:.2f}s)", flush=True)
        plots = generate_plots(ctx)
        summary = generate_master_summary(ctx, environment, results, plots)
        write_json(output_dir / "group_results.json", [result.to_dict() for result in results])
        if args.keep_temp_data:
            write_text(output_dir / "temporary_data_location.txt", str(temporary) + "\n")
        return 1 if summary["critical_failures"] else 0
    except KeyboardInterrupt:
        write_text(output_dir / "interrupted.txt", "Validation interrupted by user. Temporary subprocesses use bounded calls; no production database was opened.\n")
        return 130
    except Exception:
        write_text(output_dir / "runner_failure.txt", traceback.format_exc())
        return 2
    finally:
        if not args.keep_temp_data:
            shutil.rmtree(temporary, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
