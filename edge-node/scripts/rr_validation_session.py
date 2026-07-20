import argparse
import csv
import math
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import LOG_DIR  # noqa: E402
from processing.ppg_quality import analyze_ppg_quality  # noqa: E402
from processing.respiratory_rate import estimate_respiratory_rate  # noqa: E402
from scripts.analyze_ppg_csv import save_plot as save_quality_plot  # noqa: E402
from scripts.estimate_rr_from_ppg_csv import (  # noqa: E402
    save_diagnostic_plot as save_rr_diagnostic_plot,
)
from scripts.test_live_rr_packet import (  # noqa: E402
    USABLE_RR_QUALITIES,
    close_safely,
    collect_ppg_window,
    samples_to_ppg_data,
    save_ppg_window_csv,
)


SESSION_DURATION_SEC = 60.0
PLOT_DIR = PROJECT_ROOT / "plots"
RESULTS_PATH = LOG_DIR / "rr_live_validation_results.csv"
CONDITIONS = ("slow", "normal", "fast", "motion", "poor_contact", "other")
RESULT_FIELDS = (
    "timestamp_utc",
    "file_path",
    "condition",
    "duration_sec",
    "manual_breath_count",
    "expected_rr_bpm",
    "estimated_rr_bpm",
    "rr_confidence",
    "rr_quality",
    "usable",
    "sent_value",
    "error_bpm",
    "absolute_error_bpm",
    "ppg_quality",
    "ppg_quality_score",
    "rejection_reasons",
    "note",
    "rr_plot_path",
    "quality_plot_path",
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Collect one controlled 60-second PPG validation trial, or summarize "
            "previous trials. Research/prototype use only."
        )
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="summarize logs/rr_live_validation_results.csv without using hardware",
    )
    return parser.parse_args(argv)


def calculate_expected_rr(manual_breath_count, duration_sec):
    """Convert a breath count over a measured window to breaths per minute."""
    if duration_sec <= 0:
        raise ValueError("duration_sec must be greater than zero")
    if manual_breath_count < 0:
        raise ValueError("manual_breath_count cannot be negative")
    return float(manual_breath_count) / float(duration_sec) * 60.0


def validation_artifact_paths(timestamp_utc=None):
    if timestamp_utc is None:
        timestamp_utc = datetime.now(timezone.utc)
    stamp = timestamp_utc.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem = f"rr_validation_{stamp}"
    return {
        "raw_csv_path": (LOG_DIR / f"{stem}.csv").resolve(),
        "rr_plot_path": (PLOT_DIR / f"{stem}_rr_diagnostic.png").resolve(),
        "quality_plot_path": (PLOT_DIR / f"{stem}_quality.png").resolve(),
    }


def build_validation_row(
    *,
    timestamp_utc,
    file_path,
    condition,
    duration_sec,
    manual_breath_count,
    rr_result,
    ppg_quality,
    note="",
    rr_plot_path="",
    quality_plot_path="",
):
    expected_rr = calculate_expected_rr(manual_breath_count, duration_sec)
    estimated_rr = rr_result.resp_rate_bpm
    error = None if estimated_rr is None else float(estimated_rr) - expected_rr
    sent_value = (
        float(estimated_rr)
        if (
            estimated_rr is not None
            and rr_result.usable
            and rr_result.rr_quality in USABLE_RR_QUALITIES
        )
        else None
    )
    timestamp_text = (
        timestamp_utc.astimezone(timezone.utc).isoformat()
        if isinstance(timestamp_utc, datetime)
        else str(timestamp_utc)
    )
    return {
        "timestamp_utc": timestamp_text,
        "file_path": str(Path(file_path).resolve()),
        "condition": condition,
        "duration_sec": round(float(duration_sec), 6),
        "manual_breath_count": float(manual_breath_count),
        "expected_rr_bpm": round(expected_rr, 6),
        "estimated_rr_bpm": _csv_number(estimated_rr),
        "rr_confidence": float(rr_result.rr_confidence),
        "rr_quality": rr_result.rr_quality,
        "usable": bool(rr_result.usable),
        "sent_value": _csv_number(sent_value),
        "error_bpm": _csv_number(error),
        "absolute_error_bpm": _csv_number(abs(error) if error is not None else None),
        "ppg_quality": ppg_quality.quality_classification,
        "ppg_quality_score": float(ppg_quality.quality_score),
        "rejection_reasons": "; ".join(rr_result.reasons),
        "note": note.strip(),
        "rr_plot_path": str(rr_plot_path) if rr_plot_path else "",
        "quality_plot_path": str(quality_plot_path) if quality_plot_path else "",
    }


def append_validation_row(results_path, row):
    results_path = Path(results_path)
    results_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not results_path.exists() or results_path.stat().st_size == 0
    with results_path.open("a", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=RESULT_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow({field: row.get(field, "") for field in RESULT_FIELDS})


def calculate_summary(rows):
    rows = list(rows)
    usable_errors = []
    withheld = 0
    condition_counts = Counter()
    quality_counts = Counter()

    for row in rows:
        condition_counts[(row.get("condition") or "UNSPECIFIED").strip()] += 1
        quality_counts[(row.get("rr_quality") or "UNAVAILABLE").strip()] += 1
        sent_value = _parse_optional_float(row.get("sent_value"))
        if sent_value is None:
            withheld += 1
        if _parse_bool(row.get("usable")):
            error = _parse_optional_float(row.get("error_bpm"))
            if error is not None:
                usable_errors.append(error)

    if usable_errors:
        mae = sum(abs(value) for value in usable_errors) / len(usable_errors)
        rmse = math.sqrt(
            sum(value * value for value in usable_errors) / len(usable_errors)
        )
        within_two_percent = (
            100.0
            * sum(abs(value) <= 2.0 for value in usable_errors)
            / len(usable_errors)
        )
    else:
        mae = None
        rmse = None
        within_two_percent = None

    return {
        "trial_count": len(rows),
        "usable_trial_count": sum(_parse_bool(row.get("usable")) for row in rows),
        "withheld_trial_count": withheld,
        "mae_usable_bpm": mae,
        "rmse_usable_bpm": rmse,
        "within_two_percent_usable": within_two_percent,
        "condition_counts": dict(sorted(condition_counts.items())),
        "rr_quality_counts": dict(sorted(quality_counts.items())),
    }


def load_validation_rows(results_path):
    results_path = Path(results_path)
    if not results_path.is_file():
        return []
    with results_path.open("r", newline="", encoding="utf-8-sig") as csv_file:
        return list(csv.DictReader(csv_file))


def print_summary(summary, results_path=RESULTS_PATH):
    print("RR live validation summary")
    print(f"Results CSV: {Path(results_path).resolve()}")
    print(f"Number of trials: {summary['trial_count']}")
    print(f"Usable trials: {summary['usable_trial_count']}")
    print(f"Withheld trials: {summary['withheld_trial_count']}")
    print(f"MAE for usable trials: {_metric(summary['mae_usable_bpm'], ' bpm')}")
    print(f"RMSE for usable trials: {_metric(summary['rmse_usable_bpm'], ' bpm')}")
    print(
        "Percent within +/-2 breaths/min for usable trials: "
        f"{_metric(summary['within_two_percent_usable'], '%')}"
    )
    print("Counts by condition:")
    _print_counts(summary["condition_counts"])
    print("Counts by rr_quality:")
    _print_counts(summary["rr_quality_counts"])


def prompt_trial_metadata(input_func=input):
    allowed = set(CONDITIONS)
    while True:
        condition = input_func(
            "Condition [slow/normal/fast/motion/poor_contact/other]: "
        ).strip().lower()
        if condition in allowed:
            break
        print("Please enter one of: " + ", ".join(CONDITIONS))

    while True:
        raw_count = input_func("Manual breath count during the recording: ").strip()
        try:
            manual_breath_count = float(raw_count)
            if not math.isfinite(manual_breath_count) or manual_breath_count < 0:
                raise ValueError
            break
        except ValueError:
            print("Please enter a non-negative numeric breath count.")

    note = input_func("Optional note (press Enter to skip): ").strip()
    return condition, manual_breath_count, note


def run_validation_trial(input_func=input):
    timestamp_utc = datetime.now(timezone.utc)
    paths = validation_artifact_paths(timestamp_utc)
    pulse_sensor = None

    try:
        # Keep hardware imports out of summary mode and unit tests.
        from sensors.max30102 import MAX30102Sensor

        pulse_sensor = MAX30102Sensor()
        samples, stream_stats = collect_ppg_window(
            pulse_sensor,
            SESSION_DURATION_SEC,
        )
    finally:
        close_safely(pulse_sensor, "MAX30102")

    data = samples_to_ppg_data(samples)
    save_ppg_window_csv(paths["raw_csv_path"], data)
    print(f"Raw PPG CSV: {paths['raw_csv_path']}")
    print(
        f"Captured {len(samples)} samples at approximately "
        f"{stream_stats['approximate_sample_rate_hz']:.2f} Hz."
    )

    quality = analyze_ppg_quality(data)
    rr_analysis = estimate_respiratory_rate(data)
    saved_rr_plot = ""
    saved_quality_plot = ""

    try:
        save_quality_plot(
            paths["quality_plot_path"],
            paths["raw_csv_path"],
            data,
            quality,
        )
        saved_quality_plot = paths["quality_plot_path"]
        print(f"PPG quality plot: {saved_quality_plot}")
    except (OSError, RuntimeError) as exc:
        print(f"PPG quality plot unavailable: {exc}")

    try:
        save_rr_diagnostic_plot(
            paths["rr_plot_path"],
            paths["raw_csv_path"],
            rr_analysis,
        )
        saved_rr_plot = paths["rr_plot_path"]
        print(f"RR diagnostic plot: {saved_rr_plot}")
    except (OSError, RuntimeError) as exc:
        print(f"RR diagnostic plot unavailable: {exc}")

    condition, manual_breath_count, note = prompt_trial_metadata(input_func)
    duration_sec = quality.recording_duration_sec
    row = build_validation_row(
        timestamp_utc=timestamp_utc,
        file_path=paths["raw_csv_path"],
        condition=condition,
        duration_sec=duration_sec,
        manual_breath_count=manual_breath_count,
        rr_result=rr_analysis.result,
        ppg_quality=quality,
        note=note,
        rr_plot_path=saved_rr_plot,
        quality_plot_path=saved_quality_plot,
    )
    append_validation_row(RESULTS_PATH, row)
    print_trial_summary(row, RESULTS_PATH)
    return 0


def print_trial_summary(row, results_path):
    estimated = _display_rr(row["estimated_rr_bpm"])
    error = _display_rr(row["error_bpm"], signed=True)
    sent = (
        f"eligible value {_display_rr(row['sent_value'])} (not posted)"
        if row["sent_value"] != ""
        else "withheld"
    )
    print()
    print("Validation trial summary")
    print(f"Manual RR: {float(row['expected_rr_bpm']):.2f} breaths/min")
    print(f"Estimated RR: {estimated}")
    print(f"Error: {error}")
    print(f"Quality: {row['rr_quality']} (PPG: {row['ppg_quality']})")
    print(f"Confidence: {float(row['rr_confidence']):.3f}")
    print(f"Sent or withheld: {sent}")
    print(f"Saved CSV row path: {Path(results_path).resolve()}")
    print("Prototype validation only; this is not clinical validation.")


def _csv_number(value):
    return "" if value is None else float(value)


def _parse_optional_float(value):
    if value is None or str(value).strip() == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_bool(value):
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _metric(value, suffix):
    return "N/A" if value is None else f"{value:.2f}{suffix}"


def _print_counts(counts):
    if not counts:
        print("  none")
        return
    for label, count in counts.items():
        print(f"  {label}: {count}")


def _display_rr(value, signed=False):
    number = _parse_optional_float(value)
    if number is None:
        return "UNAVAILABLE"
    format_spec = "+.2f" if signed else ".2f"
    return f"{number:{format_spec}} breaths/min"


def main(argv=None):
    args = parse_args(argv)
    if args.summary:
        rows = load_validation_rows(RESULTS_PATH)
        print_summary(calculate_summary(rows), RESULTS_PATH)
        return 0

    try:
        return run_validation_trial()
    except KeyboardInterrupt:
        print("Validation session interrupted; no result row was appended.")
        return 130
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Validation session failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
