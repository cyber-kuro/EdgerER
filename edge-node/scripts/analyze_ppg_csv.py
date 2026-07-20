import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import LOG_DIR  # noqa: E402
from processing.ppg_quality import (  # noqa: E402
    PPGData,
    PPGQualityResult,
    analyze_ppg_quality,
    load_ppg_csv,
)


PLOT_DIR = PROJECT_ROOT / "plots"


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Analyze the acquisition quality of a raw EdgeER PPG CSV. "
            "This tool does not estimate respiratory rate."
        )
    )
    parser.add_argument(
        "csv_path",
        type=Path,
        help="CSV produced by scripts/record_ppg_csv.py",
    )
    parser.add_argument(
        "--summary",
        type=Path,
        help="optional summary CSV path (default: logs/<name>_quality_summary.csv)",
    )
    parser.add_argument(
        "--plot",
        type=Path,
        help="optional plot path (default: plots/<name>_quality.png)",
    )
    return parser.parse_args()


def save_summary(output_path, source_path, result):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "source_csv": str(source_path.resolve()),
        "analysis_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        **result.as_summary_dict(),
    }

    with output_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=list(summary.keys()))
        writer.writeheader()
        writer.writerow(summary)


def save_plot(output_path, source_path, data, result):
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError(
            "Plotting requires matplotlib. Install it in the Pi environment "
            "with: python3 -m pip install matplotlib"
        ) from exc

    output_path.parent.mkdir(parents=True, exist_ok=True)
    figure, axes = plt.subplots(3, 1, figsize=(12, 9), sharex=True)
    figure.suptitle(
        f"PPG quality: {result.quality_classification} "
        f"(score {result.quality_score:.3f})\n{source_path.name}"
    )

    if data.elapsed_time_sec:
        axes[0].plot(
            data.elapsed_time_sec,
            data.ir_raw,
            color="darkred",
            linewidth=0.8,
        )
        axes[1].plot(
            data.elapsed_time_sec,
            data.red_raw,
            color="firebrick",
            linewidth=0.8,
        )

        detrended_ir = detrend_ir(data.ir_raw, result.measured_sample_rate_hz)
        axes[2].plot(
            data.elapsed_time_sec,
            detrended_ir,
            color="navy",
            linewidth=0.8,
        )
    else:
        for axis in axes:
            axis.text(
                0.5,
                0.5,
                "No samples",
                ha="center",
                va="center",
                transform=axis.transAxes,
            )

    axes[0].set_ylabel("IR raw")
    axes[0].set_title("Raw IR signal")
    axes[1].set_ylabel("RED raw")
    axes[1].set_title("Raw RED signal")
    axes[2].set_ylabel("IR detrended")
    axes[2].set_xlabel("Elapsed time (seconds)")
    axes[2].set_title("IR minus 1-second moving average (visual aid only)")

    for axis in axes:
        axis.grid(True, alpha=0.25)

    figure.tight_layout(rect=(0.0, 0.0, 1.0, 0.94))
    figure.savefig(output_path, dpi=150)
    plt.close(figure)


def detrend_ir(ir_values, measured_sample_rate_hz):
    """Remove a one-second moving average for visualization only."""
    if not ir_values:
        return []

    sample_rate = measured_sample_rate_hz if measured_sample_rate_hz > 0 else 25.0
    window_size = max(3, int(round(sample_rate)))
    half_window = window_size // 2
    prefix_sum = [0.0]
    for value in ir_values:
        prefix_sum.append(prefix_sum[-1] + value)

    detrended = []
    for index, value in enumerate(ir_values):
        start = max(0, index - half_window)
        end = min(len(ir_values), index + half_window + 1)
        local_mean = (prefix_sum[end] - prefix_sum[start]) / (end - start)
        detrended.append(value - local_mean)
    return detrended


def print_report(source_path, result, summary_path, plot_path):
    print("PPG quality analysis")
    print(f"Source CSV: {source_path.resolve()}")
    print(f"Sample count: {result.sample_count}")
    print(f"Recording duration: {result.recording_duration_sec:.3f} seconds")
    print(f"Measured sample rate: {result.measured_sample_rate_hz:.3f} Hz")
    print(f"Missing/gap count: {result.missing_gap_count}")
    print(f"Estimated missing samples: {result.estimated_missing_samples}")
    print(f"Maximum timing gap: {result.max_timing_gap_sec:.6f} seconds")
    print(f"IR mean: {result.ir_mean:.2f}")
    print(f"IR std: {result.ir_std:.2f}")
    print(f"RED mean: {result.red_mean:.2f}")
    print(f"RED std: {result.red_std:.2f}")
    print(f"Signal amplitude (IR P95-P05): {result.signal_amplitude:.2f}")
    print(f"Signal-quality score: {result.quality_score:.3f}")
    print(f"Quality classification: {result.quality_classification}")
    print()

    usable_text = "YES" if result.usable_for_future_rr else "NO"
    print(f"Usable for future respiratory-rate development: {usable_text}")
    if result.usability_reasons:
        print("Reasons:")
        for reason in result.usability_reasons:
            print(f"- {reason}")
    else:
        print(
            "The duration, host timing, and simple signal-strength checks pass. "
            "This does not constitute an RR estimate or clinical validation."
        )

    print()
    print(f"Summary CSV: {summary_path}")
    print(f"Plot: {plot_path}")


def main():
    args = parse_args()
    source_path = args.csv_path.resolve()
    if not source_path.is_file():
        print(f"PPG CSV not found: {source_path}", file=sys.stderr)
        return 2

    try:
        data: PPGData = load_ppg_csv(source_path)
        result: PPGQualityResult = analyze_ppg_quality(data)
    except (OSError, ValueError) as exc:
        print(f"Unable to analyze PPG CSV: {exc}", file=sys.stderr)
        return 2

    summary_path = (
        args.summary or LOG_DIR / f"{source_path.stem}_quality_summary.csv"
    ).resolve()
    plot_path = (
        args.plot or PLOT_DIR / f"{source_path.stem}_quality.png"
    ).resolve()

    try:
        save_summary(summary_path, source_path, result)
        save_plot(plot_path, source_path, data, result)
    except (OSError, RuntimeError) as exc:
        print(f"Unable to save PPG analysis: {exc}", file=sys.stderr)
        return 3

    print_report(source_path, result, summary_path, plot_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
