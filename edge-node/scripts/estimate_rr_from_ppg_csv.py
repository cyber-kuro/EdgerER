import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import LOG_DIR  # noqa: E402
from processing.respiratory_rate import (  # noqa: E402
    DEFAULT_STARTUP_DISCARD_SEC,
    RespiratoryRateAnalysis,
    estimate_respiratory_rate,
    load_ppg_csv_for_rr,
)


PLOT_DIR = PROJECT_ROOT / "plots"


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Estimate research-only respiratory rate from a recorded EdgeER "
            "PPG CSV. This estimator is not clinically validated."
        )
    )
    parser.add_argument(
        "csv_path",
        type=Path,
        help="CSV produced by scripts/record_ppg_csv.py",
    )
    parser.add_argument(
        "--discard-seconds",
        type=float,
        default=DEFAULT_STARTUP_DISCARD_SEC,
        help=(
            "startup transient to discard, from 3 to 5 seconds "
            f"(default: {DEFAULT_STARTUP_DISCARD_SEC:g})"
        ),
    )
    parser.add_argument(
        "--result",
        type=Path,
        help="optional result CSV path (default: logs/<name>_rr_result.csv)",
    )
    parser.add_argument(
        "--plot",
        type=Path,
        help="optional plot path (default: plots/<name>_rr_diagnostic.png)",
    )
    return parser.parse_args()


def save_result_csv(output_path, source_path, result):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "source_csv": str(source_path.resolve()),
        "analysis_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        **result.as_summary_dict(),
        "clinical_validation": "NOT_CLINICALLY_VALIDATED",
    }
    with output_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=list(row.keys()))
        writer.writeheader()
        writer.writerow(row)


def save_diagnostic_plot(output_path, source_path, analysis):
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "Plotting requires matplotlib and numpy. Install them in the Pi "
            "environment before running this offline tool."
        ) from exc

    result = analysis.result
    diagnostics = analysis.diagnostics
    output_path.parent.mkdir(parents=True, exist_ok=True)

    figure, axes = plt.subplots(3, 1, figsize=(12, 10))
    figure.suptitle(
        f"Offline PPG-derived RR diagnostic: {result.rr_quality}\n"
        f"{source_path.name} — not clinically validated"
    )

    if len(diagnostics.raw_time_sec):
        axes[0].plot(
            diagnostics.raw_time_sec,
            diagnostics.raw_ir,
            color="darkred",
            linewidth=0.8,
        )
    else:
        _show_no_data(axes[0], "No finite raw IR samples")
    axes[0].set_title("Raw IR PPG")
    axes[0].set_xlabel("Time (seconds)")
    axes[0].set_ylabel("IR raw")

    if len(diagnostics.analysis_time_sec):
        axes[1].plot(
            diagnostics.analysis_time_sec,
            diagnostics.filtered_ir,
            color="navy",
            linewidth=0.9,
        )
    else:
        _show_no_data(axes[1], "Respiratory-band signal unavailable")
    axes[1].set_title("Detrended IR, bandpass filtered 0.08–0.70 Hz")
    axes[1].set_xlabel("Time (seconds)")
    axes[1].set_ylabel("Filtered amplitude")

    if len(diagnostics.psd_frequency_hz):
        axes[2].plot(
            diagnostics.psd_frequency_hz,
            diagnostics.psd_power,
            color="black",
            linewidth=1.0,
            label="IR baseline",
        )
        axes[2].plot(
            diagnostics.psd_frequency_hz,
            diagnostics.psd_envelope_power,
            color="navy",
            linewidth=0.9,
            alpha=0.8,
            label="IR pulse envelope",
        )
        axes[2].plot(
            diagnostics.psd_frequency_hz,
            diagnostics.psd_red_power,
            color="firebrick",
            linewidth=0.8,
            alpha=0.65,
            label="RED baseline",
        )
        for number, peak in enumerate(result.top_peaks, start=1):
            peak_power = float(
                np.interp(
                    peak.frequency_hz,
                    diagnostics.psd_frequency_hz,
                    diagnostics.psd_power,
                )
            )
            color = "crimson" if number == 1 else "darkorange"
            axes[2].scatter(
                [peak.frequency_hz],
                [peak_power],
                color=color,
                s=30 if number == 1 else 22,
                zorder=3,
            )
            axes[2].annotate(
                f"{number}: {peak.breaths_per_min:.1f}/min",
                (peak.frequency_hz, peak_power),
                xytext=(4, 5),
                textcoords="offset points",
                fontsize=8,
                color=color,
            )
        if result.dominant_frequency_hz is not None:
            axes[2].axvline(
                result.dominant_frequency_hz,
                color="crimson",
                linestyle="--",
                linewidth=1.0,
                label=(
                    f"Selected {result.dominant_frequency_hz:.3f} Hz "
                    f"({result.dominant_frequency_hz * 60.0:.1f}/min)"
                ),
            )
        axes[2].legend()
    else:
        _show_no_data(axes[2], "PSD unavailable")
    axes[2].set_title("Welch power spectral density in respiratory band")
    axes[2].set_xlabel("Frequency (Hz)")
    axes[2].set_ylabel("PSD")

    for axis in axes:
        axis.grid(True, alpha=0.25)

    figure.tight_layout(rect=(0.0, 0.0, 1.0, 0.94))
    figure.savefig(output_path, dpi=150)
    plt.close(figure)


def _show_no_data(axis, message):
    axis.text(
        0.5,
        0.5,
        message,
        ha="center",
        va="center",
        transform=axis.transAxes,
    )


def print_report(source_path, analysis, result_path, plot_path):
    result = analysis.result
    rr_text = (
        f"{result.resp_rate_bpm:.1f} breaths/min"
        if result.resp_rate_bpm is not None
        else "UNAVAILABLE"
    )
    frequency_text = (
        f"{result.dominant_frequency_hz:.6f} Hz"
        if result.dominant_frequency_hz is not None
        else "UNAVAILABLE"
    )

    print("Offline PPG-derived respiratory-rate estimate")
    print(f"Source CSV: {source_path.resolve()}")
    print(f"Estimated RR: {rr_text}")
    print(f"Confidence: {result.rr_confidence:.3f}")
    print(f"Quality: {result.rr_quality}")
    print(f"Usable: {'YES' if result.usable else 'NO'}")
    print(f"Dominant frequency: {frequency_text}")
    print(f"RR source: {result.rr_source}")
    print(f"Result CSV: {result_path}")
    print(f"Plot path: {plot_path}")

    if result.top_peaks:
        print("Top respiratory-band peaks:")
        for number, peak in enumerate(result.top_peaks, start=1):
            print(
                f"- {number}: {peak.frequency_hz:.6f} Hz, "
                f"{peak.breaths_per_min:.2f} breaths/min, "
                f"relative power {peak.relative_power:.3f}, "
                f"selection score {peak.selection_score:.3f}"
            )

    if result.reasons:
        print("Reasons/notes:")
        for reason in result.reasons:
            print(f"- {reason}")

    print("Research output only; this estimator is not clinically validated.")


def main():
    args = parse_args()
    source_path = args.csv_path.resolve()
    if not source_path.is_file():
        print(f"PPG CSV not found: {source_path}", file=sys.stderr)
        return 2

    try:
        data = load_ppg_csv_for_rr(source_path)
        analysis: RespiratoryRateAnalysis = estimate_respiratory_rate(
            data,
            startup_discard_sec=args.discard_seconds,
        )
    except (OSError, ValueError) as exc:
        print(f"Unable to estimate RR from PPG CSV: {exc}", file=sys.stderr)
        return 2

    result_path = (
        args.result or LOG_DIR / f"{source_path.stem}_rr_result.csv"
    ).resolve()
    plot_path = (
        args.plot or PLOT_DIR / f"{source_path.stem}_rr_diagnostic.png"
    ).resolve()

    try:
        save_result_csv(result_path, source_path, analysis.result)
        save_diagnostic_plot(plot_path, source_path, analysis)
    except (OSError, RuntimeError) as exc:
        print(f"Unable to save RR analysis: {exc}", file=sys.stderr)
        return 3

    print_report(source_path, analysis, result_path, plot_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
