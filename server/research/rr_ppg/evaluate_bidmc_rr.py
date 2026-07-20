"""Starter evaluator for the public BIDMC PPG and Respiration Dataset.

The script reads local CSV files only. It never downloads data.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
from typing import Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

try:
    from .rr_algorithm import estimate_respiratory_rate
except ImportError:
    from rr_algorithm import estimate_respiratory_rate


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = SCRIPT_DIR / "data"
DEFAULT_RESULTS_FILE = SCRIPT_DIR / "results" / "bidmc_rr_results.csv"
DEFAULT_METRICS_FILE = SCRIPT_DIR / "results" / "bidmc_rr_metrics.csv"
DEFAULT_PLOTS_DIR = SCRIPT_DIR / "plots"

PPG_COLUMN_CANDIDATES = (
    "ppg",
    "pleth",
    "photoplethysmogram",
    "ppg_signal",
)
REFERENCE_COLUMN_CANDIDATES = (
    "reference_rr_bpm",
    "resp_rate_bpm",
    "respiratory_rate",
    "resp_rate",
    "rr",
    "resp",
)
TIME_COLUMN_CANDIDATES = (
    "time_seconds",
    "time_s",
    "time",
    "timestamp",
)


def normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def resolve_column(
    frame: pd.DataFrame,
    explicit_name: str | None,
    candidates: Iterable[str],
    label: str,
) -> str:
    normalized_columns = {
        normalized_name(column): column for column in frame.columns
    }

    if explicit_name:
        if explicit_name in frame.columns:
            return explicit_name
        normalized_explicit = normalized_name(explicit_name)
        if normalized_explicit in normalized_columns:
            return normalized_columns[normalized_explicit]
        raise ValueError(
            f"{label} column {explicit_name!r} was not found; "
            f"available columns: {list(frame.columns)}"
        )

    for candidate in candidates:
        matched = normalized_columns.get(normalized_name(candidate))
        if matched is not None:
            return matched

    raise ValueError(
        f"could not identify a {label} column; available columns: "
        f"{list(frame.columns)}"
    )


def resolve_optional_column(
    frame: pd.DataFrame,
    explicit_name: str | None,
    candidates: Iterable[str],
) -> str | None:
    if explicit_name:
        return resolve_column(frame, explicit_name, candidates, "time")

    normalized_columns = {
        normalized_name(column): column for column in frame.columns
    }
    for candidate in candidates:
        matched = normalized_columns.get(normalized_name(candidate))
        if matched is not None:
            return matched
    return None


def infer_sampling_rate(frame: pd.DataFrame, time_column: str) -> float:
    numeric_time = pd.to_numeric(frame[time_column], errors="coerce")
    finite_numeric = numeric_time[np.isfinite(numeric_time)]

    if len(finite_numeric) >= 3:
        differences = np.diff(finite_numeric.to_numpy(dtype=float))
    else:
        parsed_time = pd.to_datetime(frame[time_column], errors="coerce", utc=True)
        valid_time = parsed_time.dropna()
        if len(valid_time) < 3:
            raise ValueError("not enough valid timestamps to infer sampling rate")
        differences = np.diff(valid_time.astype("int64").to_numpy() / 1e9)

    positive_differences = differences[
        np.isfinite(differences) & (differences > 0)
    ]
    if positive_differences.size == 0:
        raise ValueError("timestamps do not contain positive sample intervals")

    median_interval = float(np.median(positive_differences))
    sampling_rate_hz = 1.0 / median_interval
    if not math.isfinite(sampling_rate_hz) or sampling_rate_hz <= 0:
        raise ValueError("inferred sampling rate is invalid")
    return sampling_rate_hz


def infer_reference_kind(reference: np.ndarray) -> str:
    finite = reference[np.isfinite(reference)]
    if finite.size == 0:
        return "rate"

    plausible_rate_fraction = float(np.mean((finite >= 4) & (finite <= 60)))
    differences = np.abs(np.diff(finite))
    typical_change = float(np.median(differences)) if differences.size else 0.0
    if plausible_rate_fraction >= 0.95 and typical_change <= 2.0:
        return "rate"
    return "waveform"


def window_bounds(
    sample_count: int,
    sampling_rate_hz: float,
    window_seconds: float,
    step_seconds: float,
) -> list[tuple[int, int]]:
    window_samples = int(round(window_seconds * sampling_rate_hz))
    step_samples = max(1, int(round(step_seconds * sampling_rate_hz)))
    if sample_count < window_samples:
        return []
    return [
        (start, start + window_samples)
        for start in range(0, sample_count - window_samples + 1, step_samples)
    ]


def reference_rate_for_window(
    reference_window: np.ndarray,
    reference_kind: str,
    sampling_rate_hz: float,
    window_seconds: float,
) -> float | None:
    finite = reference_window[np.isfinite(reference_window)]
    if finite.size == 0:
        return None

    if reference_kind == "rate":
        plausible = finite[(finite >= 2) & (finite <= 80)]
        return float(np.median(plausible)) if plausible.size else None

    result = estimate_respiratory_rate(
        reference_window,
        sampling_rate_hz,
        window_seconds=window_seconds,
        step_seconds=window_seconds,
        min_confidence=0.0,
    )
    value = result["resp_rate_bpm"]
    return float(value) if value is not None else None


def safe_plot_name(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", path.stem)


def save_recording_plot(
    recording_results: pd.DataFrame,
    source_file: Path,
    plots_dir: Path,
    min_confidence: float,
) -> None:
    if recording_results.empty:
        return

    figure, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True)
    x = recording_results["window_center_seconds"]
    axes[0].plot(
        x,
        recording_results["reference_rr_bpm"],
        "o-",
        label="Reference RR",
        linewidth=1.4,
    )
    axes[0].plot(
        x,
        recording_results["predicted_rr_bpm"],
        "o-",
        label="PPG-derived RR",
        linewidth=1.4,
    )
    axes[0].set_ylabel("Breaths/min")
    axes[0].set_title(source_file.name)
    axes[0].grid(alpha=0.25)
    axes[0].legend()

    axes[1].plot(x, recording_results["rr_confidence"], "o-", color="tab:green")
    axes[1].axhline(
        min_confidence,
        color="tab:red",
        linestyle="--",
        label="Acceptance threshold",
    )
    axes[1].set_xlabel("Recording time (seconds)")
    axes[1].set_ylabel("Confidence")
    axes[1].set_ylim(-0.02, 1.02)
    axes[1].grid(alpha=0.25)
    axes[1].legend()

    figure.tight_layout()
    figure.savefig(
        plots_dir / f"{safe_plot_name(source_file)}_diagnostic.png",
        dpi=150,
    )
    plt.close(figure)


def save_summary_plots(results: pd.DataFrame, plots_dir: Path) -> None:
    accepted = results[
        results["accepted"]
        & results["predicted_rr_bpm"].notna()
        & results["reference_rr_bpm"].notna()
    ]
    if accepted.empty:
        return

    predicted = accepted["predicted_rr_bpm"].to_numpy(dtype=float)
    reference = accepted["reference_rr_bpm"].to_numpy(dtype=float)
    minimum = float(min(predicted.min(), reference.min()))
    maximum = float(max(predicted.max(), reference.max()))

    figure, axis = plt.subplots(figsize=(7, 7))
    axis.scatter(reference, predicted, alpha=0.65)
    axis.plot([minimum, maximum], [minimum, maximum], "k--", label="Identity")
    axis.set_xlabel("Reference RR (breaths/min)")
    axis.set_ylabel("PPG-derived RR (breaths/min)")
    axis.set_title("BIDMC respiratory-rate agreement")
    axis.grid(alpha=0.25)
    axis.legend()
    figure.tight_layout()
    figure.savefig(plots_dir / "bidmc_rr_scatter.png", dpi=150)
    plt.close(figure)

    differences = predicted - reference
    averages = (predicted + reference) / 2.0
    bias = float(np.mean(differences))
    standard_deviation = float(np.std(differences, ddof=1)) if len(differences) > 1 else 0.0

    figure, axis = plt.subplots(figsize=(8, 6))
    axis.scatter(averages, differences, alpha=0.65)
    axis.axhline(bias, color="tab:blue", label=f"Bias {bias:.2f}")
    axis.axhline(bias + 1.96 * standard_deviation, color="tab:red", linestyle="--")
    axis.axhline(bias - 1.96 * standard_deviation, color="tab:red", linestyle="--")
    axis.set_xlabel("Mean predicted/reference RR (breaths/min)")
    axis.set_ylabel("Prediction - reference (breaths/min)")
    axis.set_title("Bland-Altman diagnostic")
    axis.grid(alpha=0.25)
    axis.legend()
    figure.tight_layout()
    figure.savefig(plots_dir / "bidmc_rr_bland_altman.png", dpi=150)
    plt.close(figure)


def compute_metrics(results: pd.DataFrame) -> dict[str, float | int]:
    comparable = results[results["reference_rr_bpm"].notna()]
    accepted = comparable[
        comparable["accepted"] & comparable["predicted_rr_bpm"].notna()
    ]
    rejected_count = int(len(comparable) - len(accepted))

    metrics: dict[str, float | int] = {
        "total_windows": int(len(results)),
        "windows_with_reference": int(len(comparable)),
        "accepted_windows": int(len(accepted)),
        "rejected_low_confidence_windows": rejected_count,
        "percent_rejected_low_confidence": (
            100.0 * rejected_count / len(comparable) if len(comparable) else math.nan
        ),
        "mae_bpm": math.nan,
        "rmse_bpm": math.nan,
        "bias_bpm": math.nan,
        "percent_within_2_bpm": math.nan,
    }

    if not accepted.empty:
        errors = (
            accepted["predicted_rr_bpm"].to_numpy(dtype=float)
            - accepted["reference_rr_bpm"].to_numpy(dtype=float)
        )
        metrics.update(
            {
                "mae_bpm": float(np.mean(np.abs(errors))),
                "rmse_bpm": float(np.sqrt(np.mean(errors**2))),
                "bias_bpm": float(np.mean(errors)),
                "percent_within_2_bpm": float(100.0 * np.mean(np.abs(errors) <= 2.0)),
            }
        )

    return metrics


def evaluate_file(path: Path, args: argparse.Namespace) -> pd.DataFrame:
    frame = pd.read_csv(path)
    ppg_column = resolve_column(
        frame,
        args.ppg_column,
        PPG_COLUMN_CANDIDATES,
        "PPG",
    )
    reference_column = resolve_column(
        frame,
        args.reference_column,
        REFERENCE_COLUMN_CANDIDATES,
        "reference respiration",
    )
    time_column = resolve_optional_column(
        frame,
        args.time_column,
        TIME_COLUMN_CANDIDATES,
    )

    if args.sampling_rate > 0:
        sampling_rate_hz = float(args.sampling_rate)
    elif time_column is not None:
        sampling_rate_hz = infer_sampling_rate(frame, time_column)
    else:
        raise ValueError(
            "sampling rate was not supplied and no time column could be identified"
        )

    ppg = pd.to_numeric(frame[ppg_column], errors="coerce").to_numpy(dtype=float)
    reference = pd.to_numeric(
        frame[reference_column], errors="coerce"
    ).to_numpy(dtype=float)
    reference_kind = args.reference_kind
    if reference_kind == "auto":
        reference_kind = infer_reference_kind(reference)

    rows: list[dict[str, object]] = []
    bounds = window_bounds(
        len(frame),
        sampling_rate_hz,
        args.window_seconds,
        args.step_seconds,
    )
    if not bounds:
        raise ValueError(
            f"recording is shorter than one {args.window_seconds:g}-second window"
        )

    for window_index, (start, end) in enumerate(bounds):
        estimate = estimate_respiratory_rate(
            ppg[start:end],
            sampling_rate_hz,
            window_seconds=args.window_seconds,
            step_seconds=args.window_seconds,
            min_confidence=args.min_confidence,
        )
        reference_rr = reference_rate_for_window(
            reference[start:end],
            reference_kind,
            sampling_rate_hz,
            args.window_seconds,
        )
        predicted_rr = estimate["resp_rate_bpm"]
        accepted = predicted_rr is not None
        error = (
            float(predicted_rr - reference_rr)
            if predicted_rr is not None and reference_rr is not None
            else math.nan
        )
        rows.append(
            {
                "source_file": str(path),
                "window_index": window_index,
                "window_start_seconds": start / sampling_rate_hz,
                "window_end_seconds": end / sampling_rate_hz,
                "window_center_seconds": (start + end) / (2.0 * sampling_rate_hz),
                "sampling_rate_hz": sampling_rate_hz,
                "ppg_column": ppg_column,
                "reference_column": reference_column,
                "reference_kind": reference_kind,
                "predicted_rr_bpm": predicted_rr,
                "reference_rr_bpm": reference_rr,
                "rr_confidence": estimate["rr_confidence"],
                "rr_quality": estimate["rr_quality"],
                "accepted": accepted,
                "error_bpm": error,
                "absolute_error_bpm": abs(error) if math.isfinite(error) else math.nan,
                "within_2_bpm": abs(error) <= 2.0 if math.isfinite(error) else False,
                "rejection_reason": estimate["debug"].get("rejection_reason", ""),
                "debug_json": json.dumps(estimate["debug"], separators=(",", ":")),
            }
        )

    return pd.DataFrame(rows)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate baseline PPG-derived RR on local BIDMC CSV files."
    )
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--pattern", default="*.csv", help="Recursive CSV glob pattern")
    parser.add_argument("--ppg-column")
    parser.add_argument("--reference-column")
    parser.add_argument("--time-column")
    parser.add_argument(
        "--reference-kind",
        choices=("auto", "rate", "waveform"),
        default="auto",
    )
    parser.add_argument(
        "--sampling-rate",
        type=float,
        default=125.0,
        help="Samples/second; use 0 to infer it from the time column",
    )
    parser.add_argument("--window-seconds", type=float, default=32.0)
    parser.add_argument("--step-seconds", type=float, default=16.0)
    parser.add_argument("--min-confidence", type=float, default=0.35)
    parser.add_argument("--results-file", type=Path, default=DEFAULT_RESULTS_FILE)
    parser.add_argument("--metrics-file", type=Path, default=DEFAULT_METRICS_FILE)
    parser.add_argument("--plots-dir", type=Path, default=DEFAULT_PLOTS_DIR)
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()
    data_dir = args.data_dir.resolve()
    if not data_dir.exists():
        raise SystemExit(
            f"Data directory does not exist: {data_dir}\n"
            "Download BIDMC separately, create the directory, and place local CSV files there."
        )

    files = sorted(data_dir.rglob(args.pattern))
    if not files:
        raise SystemExit(f"No CSV files matching {args.pattern!r} found in {data_dir}")

    args.results_file.parent.mkdir(parents=True, exist_ok=True)
    args.metrics_file.parent.mkdir(parents=True, exist_ok=True)
    args.plots_dir.mkdir(parents=True, exist_ok=True)

    evaluated: list[pd.DataFrame] = []
    failures: list[tuple[Path, str]] = []
    for path in files:
        try:
            recording_results = evaluate_file(path, args)
        except (OSError, ValueError, pd.errors.ParserError) as error:
            failures.append((path, str(error)))
            print(f"SKIP {path.name}: {error}")
            continue

        evaluated.append(recording_results)
        save_recording_plot(
            recording_results,
            path,
            args.plots_dir,
            args.min_confidence,
        )
        print(f"OK   {path.name}: {len(recording_results)} windows")

    if not evaluated:
        failure_summary = "\n".join(
            f"- {path.name}: {message}" for path, message in failures
        )
        raise SystemExit(f"No files could be evaluated.\n{failure_summary}")

    results = pd.concat(evaluated, ignore_index=True)
    metrics = compute_metrics(results)
    results.to_csv(args.results_file, index=False)
    pd.DataFrame([metrics]).to_csv(args.metrics_file, index=False)
    save_summary_plots(results, args.plots_dir)

    print(f"\nResults: {args.results_file.resolve()}")
    print(f"Metrics: {args.metrics_file.resolve()}")
    print(f"Plots:   {args.plots_dir.resolve()}")
    print(json.dumps(metrics, indent=2, allow_nan=True))
    if failures:
        print(f"Skipped files: {len(failures)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
