import argparse
import csv
import math
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import LOG_DIR  # noqa: E402
from processing.respiratory_rate import (  # noqa: E402
    RESPIRATORY_BAND_LOW_HZ,
    estimate_respiratory_rate,
    load_ppg_csv_for_rr,
)


DEFAULT_MANIFEST_PATH = LOG_DIR / "rr_validation_manifest.csv"
DEFAULT_RESULTS_PATH = LOG_DIR / "rr_validation_results.csv"

MANIFEST_COLUMNS = (
    "file_path",
    "condition",
    "manual_breath_count",
    "duration_sec",
    "expected_rr_bpm",
)

RESULT_COLUMNS = (
    "file_path",
    "condition",
    "expected_rr_bpm",
    "estimated_rr_bpm",
    "rr_confidence",
    "rr_quality",
    "usable",
    "error_bpm",
    "absolute_error_bpm",
    "rejection_reasons",
)


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Batch the offline PPG-derived RR estimator against a prototype "
            "validation manifest. This is not clinical validation."
        )
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST_PATH,
        help=f"manifest CSV (default: {DEFAULT_MANIFEST_PATH})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_RESULTS_PATH,
        help=f"results CSV (default: {DEFAULT_RESULTS_PATH})",
    )
    return parser.parse_args()


def load_manifest(manifest_path):
    with manifest_path.open("r", newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file)
        fieldnames = set(reader.fieldnames or [])
        missing_columns = [
            column for column in MANIFEST_COLUMNS if column not in fieldnames
        ]
        if missing_columns:
            raise ValueError(
                "RR validation manifest is missing required columns: "
                + ", ".join(missing_columns)
            )
        return list(reader)


def validate_recording(row, row_number, manifest_path):
    manifest_file_path = (row.get("file_path") or "").strip()
    condition = (row.get("condition") or "").strip()

    base_result = {
        "file_path": manifest_file_path,
        "condition": condition,
        "expected_rr_bpm": "",
        "estimated_rr_bpm": "",
        "rr_confidence": 0.0,
        "rr_quality": "UNAVAILABLE",
        "usable": False,
        "error_bpm": "",
        "absolute_error_bpm": "",
        "rejection_reasons": "",
    }

    try:
        if not manifest_file_path:
            raise ValueError("file_path is empty")

        manual_breath_count = _required_float(
            row, "manual_breath_count", row_number
        )
        duration_sec = _required_float(row, "duration_sec", row_number)
        expected_rr_bpm = _required_float(
            row, "expected_rr_bpm", row_number
        )
        base_result["expected_rr_bpm"] = _format_number(expected_rr_bpm)

        if manual_breath_count < 0:
            raise ValueError("manual_breath_count cannot be negative")
        if duration_sec <= 0:
            raise ValueError("duration_sec must be greater than zero")
        if expected_rr_bpm < 0:
            raise ValueError("expected_rr_bpm cannot be negative")

        range_warning = expected_range_warning(expected_rr_bpm)
        outside_lower_band = bool(range_warning)

        recording_path = resolve_recording_path(
            manifest_file_path, manifest_path
        )
        if not recording_path.is_file():
            raise FileNotFoundError(f"recording not found: {recording_path}")

        data = load_ppg_csv_for_rr(recording_path)
        analysis = estimate_respiratory_rate(data)
        estimate = analysis.result

        base_result["rr_confidence"] = f"{estimate.rr_confidence:.3f}"
        base_result["rr_quality"] = estimate.rr_quality
        base_result["usable"] = estimate.usable and not outside_lower_band
        reasons = list(estimate.reasons)
        if range_warning:
            reasons.append(range_warning)
        base_result["rejection_reasons"] = "; ".join(reasons)

        candidate_rr_bpm = estimate.resp_rate_bpm
        if candidate_rr_bpm is None and estimate.dominant_frequency_hz is not None:
            candidate_rr_bpm = estimate.dominant_frequency_hz * 60.0

        if candidate_rr_bpm is not None:
            error_bpm = candidate_rr_bpm - expected_rr_bpm
            base_result["estimated_rr_bpm"] = _format_number(
                candidate_rr_bpm
            )
            base_result["error_bpm"] = _format_number(error_bpm)
            base_result["absolute_error_bpm"] = _format_number(
                abs(error_bpm)
            )

    except Exception as exc:
        base_result["rejection_reasons"] = (
            f"Manifest row {row_number}: {type(exc).__name__}: {exc}"
        )

    return base_result


def resolve_recording_path(file_path, manifest_path):
    path = Path(file_path).expanduser()
    if path.is_absolute():
        return path

    project_candidate = PROJECT_ROOT / path
    manifest_candidate = manifest_path.parent / path
    if project_candidate.exists():
        return project_candidate
    if manifest_candidate.exists():
        return manifest_candidate

    # A bare filename in a logs/ manifest most naturally refers to a sibling.
    if len(path.parts) == 1:
        return manifest_candidate
    return project_candidate


def calculate_summary(results):
    usable_errors = [
        float(row["error_bpm"])
        for row in results
        if row["usable"] and row["error_bpm"] != ""
    ]
    all_errors = [
        float(row["error_bpm"])
        for row in results
        if row["error_bpm"] != ""
    ]
    summary = {
        "usable_count": len(usable_errors),
        "mae": None,
        "rmse": None,
        "bias": None,
        "within_2_percent": None,
        "within_3_percent": None,
        "all_file_count": len(all_errors),
        "all_file_mae": (
            sum(abs(error) for error in all_errors) / len(all_errors)
            if all_errors
            else None
        ),
    }
    if not usable_errors:
        return summary

    count = len(usable_errors)
    absolute_errors = [abs(error) for error in usable_errors]
    summary.update(
        {
            "mae": sum(absolute_errors) / count,
            "rmse": math.sqrt(
                sum(error * error for error in usable_errors) / count
            ),
            "bias": sum(usable_errors) / count,
            "within_2_percent": (
                100.0 * sum(error <= 2.0 for error in absolute_errors) / count
            ),
            "within_3_percent": (
                100.0 * sum(error <= 3.0 for error in absolute_errors) / count
            ),
        }
    )
    return summary


def save_results(output_path, results):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=RESULT_COLUMNS)
        writer.writeheader()
        writer.writerows(results)


def _legacy_print_summary(manifest_path, output_path, total_count, summary):
    print("Offline PPG-derived RR prototype validation")
    print(f"Manifest: {manifest_path}")
    print(f"Results: {output_path}")
    print(f"Usable files: {summary['usable_count']} / {total_count}")

    if summary["usable_count"] == 0:
        print("MAE: N/A")
        print("RMSE: N/A")
        print("Bias: N/A")
        print("Within ±2 breaths/min: N/A")
        print("Within ±3 breaths/min: N/A")
    else:
        print(f"MAE: {summary['mae']:.3f} breaths/min")
        print(f"RMSE: {summary['rmse']:.3f} breaths/min")
        print(f"Bias: {summary['bias']:.3f} breaths/min")
        print(
            "Within ±2 breaths/min: "
            f"{summary['within_2_percent']:.1f}%"
        )
        print(
            "Within ±3 breaths/min: "
            f"{summary['within_3_percent']:.1f}%"
        )

    print("Prototype validation only; this is not clinical validation.")


def print_result_table(results):
    print("Per-file results")
    header = (
        f"{'condition':<18} {'expected':>9} {'estimate':>9} "
        f"{'conf':>6} {'quality':<16} {'usable':<6} {'abs err':>8}"
    )
    print(header)
    print("-" * len(header))
    for row in results:
        print(
            f"{row['condition'][:18]:<18} "
            f"{str(row['expected_rr_bpm']):>9} "
            f"{str(row['estimated_rr_bpm'] or '-'):>9} "
            f"{str(row['rr_confidence']):>6} "
            f"{str(row['rr_quality']):<16} "
            f"{str(row['usable']):<6} "
            f"{str(row['absolute_error_bpm'] or '-'):>8}"
        )

    usable = [row for row in results if row["usable"]]
    rejected = [row for row in results if not row["usable"]]
    print()
    print(f"Usable estimates ({len(usable)}):")
    if usable:
        for row in usable:
            print(f"- {row['file_path']} ({row['condition']})")
    else:
        print("- none")

    print()
    print(f"Failed/rejected/low-confidence files ({len(rejected)}):")
    if rejected:
        for row in rejected:
            reason = row["rejection_reasons"] or row["rr_quality"]
            print(f"- {row['file_path']} ({row['condition']}): {reason}")
    else:
        print("- none")


def print_summary(manifest_path, output_path, total_count, summary):
    print("Offline PPG-derived RR prototype validation")
    print(f"Manifest: {manifest_path}")
    print(f"Results: {output_path}")
    print(f"Usable files: {summary['usable_count']} / {total_count}")

    if summary["usable_count"] == 0:
        print("Usable-only MAE: N/A")
        print("Usable-only RMSE: N/A")
        print("Usable-only bias: N/A")
        print("Within +/-2 breaths/min: N/A")
        print("Within +/-3 breaths/min: N/A")
    else:
        print(f"Usable-only MAE: {summary['mae']:.3f} breaths/min")
        print(f"Usable-only RMSE: {summary['rmse']:.3f} breaths/min")
        print(f"Usable-only bias: {summary['bias']:.3f} breaths/min")
        print(
            "Within +/-2 breaths/min: "
            f"{summary['within_2_percent']:.1f}%"
        )
        print(
            "Within +/-3 breaths/min: "
            f"{summary['within_3_percent']:.1f}%"
        )

    if summary["all_file_mae"] is None:
        print("All-file MAE (including rejected candidates): N/A")
    else:
        print(
            "All-file MAE (including rejected candidates): "
            f"{summary['all_file_mae']:.3f} breaths/min "
            f"across {summary['all_file_count']} files"
        )
    print("Prototype validation only; this is not clinical validation.")


def _required_float(row, column, row_number):
    value = (row.get(column) or "").strip()
    if not value:
        raise ValueError(f"{column} is empty in manifest row {row_number}")
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(
            f"{column} is not numeric in manifest row {row_number}"
        ) from exc
    if not math.isfinite(parsed):
        raise ValueError(
            f"{column} is not finite in manifest row {row_number}"
        )
    return parsed


def expected_range_warning(expected_rr_bpm):
    lower_detectable_bpm = RESPIRATORY_BAND_LOW_HZ * 60.0
    if expected_rr_bpm >= lower_detectable_bpm:
        return ""
    return (
        f"Expected RR {expected_rr_bpm:.3f} is below the current lower "
        f"detectable band ({lower_detectable_bpm:.1f} breaths/min); this test "
        "is outside the current algorithm range."
    )


def _format_number(value):
    return f"{float(value):.3f}"


def main():
    args = parse_args()
    manifest_path = args.manifest.resolve()
    output_path = args.output.resolve()

    if not manifest_path.is_file():
        print(f"RR validation manifest not found: {manifest_path}", file=sys.stderr)
        return 2

    try:
        manifest_rows = load_manifest(manifest_path)
    except (OSError, ValueError) as exc:
        print(f"Unable to load RR validation manifest: {exc}", file=sys.stderr)
        return 2

    results = [
        validate_recording(row, row_number, manifest_path)
        for row_number, row in enumerate(manifest_rows, start=2)
    ]
    summary = calculate_summary(results)

    try:
        save_results(output_path, results)
    except OSError as exc:
        print(f"Unable to save RR validation results: {exc}", file=sys.stderr)
        return 3

    print_result_table(results)
    print()
    print_summary(
        manifest_path,
        output_path,
        total_count=len(results),
        summary=summary,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
