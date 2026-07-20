import csv
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence

from config import PPG_SAMPLE_INTERVAL_SEC


REQUIRED_COLUMNS = (
    "elapsed_time_sec",
    "monotonic_time_sec",
    "red_raw",
    "ir_raw",
)

MIN_RR_RESEARCH_WINDOW_SEC = 45.0
LEGACY_FINGER_IR_MEAN_THRESHOLD = 50_000.0
LEGACY_WEAK_IR_STD_THRESHOLD = 50.0
MIN_ROBUST_IR_AMPLITUDE = 200.0
MAX30102_RAW_MAX = 262_143


@dataclass(frozen=True)
class PPGData:
    elapsed_time_sec: List[float]
    monotonic_time_sec: List[float]
    red_raw: List[int]
    ir_raw: List[int]


@dataclass(frozen=True)
class PPGQualityResult:
    sample_count: int
    recording_duration_sec: float
    measured_sample_rate_hz: float
    missing_gap_count: int
    estimated_missing_samples: int
    max_timing_gap_sec: float
    nonpositive_interval_count: int
    ir_mean: float
    ir_std: float
    red_mean: float
    red_std: float
    signal_amplitude: float
    red_signal_amplitude: float
    quality_score: float
    quality_classification: str
    usable_for_future_rr: bool
    usability_reasons: List[str]

    def as_summary_dict(self) -> Dict[str, object]:
        return {
            "sample_count": self.sample_count,
            "recording_duration_sec": self.recording_duration_sec,
            "measured_sample_rate_hz": self.measured_sample_rate_hz,
            "missing_gap_count": self.missing_gap_count,
            "estimated_missing_samples": self.estimated_missing_samples,
            "max_timing_gap_sec": self.max_timing_gap_sec,
            "nonpositive_interval_count": self.nonpositive_interval_count,
            "ir_mean": self.ir_mean,
            "ir_std": self.ir_std,
            "red_mean": self.red_mean,
            "red_std": self.red_std,
            "signal_amplitude": self.signal_amplitude,
            "red_signal_amplitude": self.red_signal_amplitude,
            "quality_score": self.quality_score,
            "quality_classification": self.quality_classification,
            "usable_for_future_rr": self.usable_for_future_rr,
            "usability_reasons": "; ".join(self.usability_reasons),
        }


def load_ppg_csv(csv_path) -> PPGData:
    """Load and validate a CSV written by scripts/record_ppg_csv.py."""
    path = Path(csv_path)
    elapsed_time_sec: List[float] = []
    monotonic_time_sec: List[float] = []
    red_raw: List[int] = []
    ir_raw: List[int] = []

    with path.open("r", newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file)
        fieldnames = set(reader.fieldnames or [])
        missing_columns = [
            column for column in REQUIRED_COLUMNS if column not in fieldnames
        ]
        if missing_columns:
            raise ValueError(
                "PPG CSV is missing required columns: "
                + ", ".join(missing_columns)
            )

        for row_number, row in enumerate(reader, start=2):
            if not any((value or "").strip() for value in row.values()):
                continue
            try:
                elapsed = float(row["elapsed_time_sec"])
                monotonic = float(row["monotonic_time_sec"])
                red = int(row["red_raw"])
                ir = int(row["ir_raw"])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Invalid numeric value in PPG CSV row {row_number}"
                ) from exc

            if not math.isfinite(elapsed) or not math.isfinite(monotonic):
                raise ValueError(
                    f"Non-finite timestamp in PPG CSV row {row_number}"
                )

            elapsed_time_sec.append(elapsed)
            monotonic_time_sec.append(monotonic)
            red_raw.append(red)
            ir_raw.append(ir)

    return PPGData(
        elapsed_time_sec=elapsed_time_sec,
        monotonic_time_sec=monotonic_time_sec,
        red_raw=red_raw,
        ir_raw=ir_raw,
    )


def analyze_ppg_quality(
    data: PPGData,
    expected_sample_interval_sec: float = PPG_SAMPLE_INTERVAL_SEC,
) -> PPGQualityResult:
    """Calculate engineering-quality metrics for one recorded PPG window.

    The score and labels are intentionally simple acquisition heuristics. They
    are not clinical classifications and do not estimate respiratory rate.
    """
    if expected_sample_interval_sec <= 0:
        raise ValueError("expected_sample_interval_sec must be greater than zero")

    sample_count = len(data.monotonic_time_sec)
    if not (
        sample_count
        == len(data.elapsed_time_sec)
        == len(data.red_raw)
        == len(data.ir_raw)
    ):
        raise ValueError("PPG data columns must contain the same number of rows")

    intervals = [
        current - previous
        for previous, current in zip(
            data.monotonic_time_sec, data.monotonic_time_sec[1:]
        )
    ]
    positive_intervals = [interval for interval in intervals if interval > 0]
    nonpositive_interval_count = len(intervals) - len(positive_intervals)
    gap_threshold_sec = expected_sample_interval_sec * 1.5
    timing_gaps = [
        interval for interval in positive_intervals if interval > gap_threshold_sec
    ]
    missing_gap_count = len(timing_gaps)
    estimated_missing_samples = sum(
        max(1, int(round(interval / expected_sample_interval_sec)) - 1)
        for interval in timing_gaps
    )
    max_timing_gap_sec = max(positive_intervals, default=0.0)

    recording_duration_sec = 0.0
    if sample_count >= 2:
        recording_duration_sec = max(
            0.0,
            data.monotonic_time_sec[-1] - data.monotonic_time_sec[0],
        )

    measured_sample_rate_hz = 0.0
    if recording_duration_sec > 0:
        measured_sample_rate_hz = (
            sample_count - 1
        ) / recording_duration_sec

    ir_mean = _mean(data.ir_raw)
    ir_std = _std(data.ir_raw)
    red_mean = _mean(data.red_raw)
    red_std = _std(data.red_raw)

    # The robust 95th-to-5th percentile range is less sensitive to isolated
    # I2C glitches than raw max-minus-min amplitude.
    signal_amplitude = _robust_amplitude(data.ir_raw)
    red_signal_amplitude = _robust_amplitude(data.red_raw)

    expected_rate_hz = 1.0 / expected_sample_interval_sec
    rate_error_fraction = 1.0
    if measured_sample_rate_hz > 0:
        rate_error_fraction = abs(
            measured_sample_rate_hz - expected_rate_hz
        ) / expected_rate_hz

    interval_count = max(1, len(intervals))
    gap_fraction = missing_gap_count / interval_count
    saturation_fraction = _saturation_fraction(data.red_raw, data.ir_raw)

    rate_score = _clamp(
        1.0 - max(0.0, rate_error_fraction - 0.02) / 0.18
    )
    gap_score = _clamp(1.0 - gap_fraction / 0.02)
    if nonpositive_interval_count:
        gap_score = 0.0
    timing_score = 0.65 * rate_score + 0.35 * gap_score

    variation_score = _clamp(
        (ir_std - LEGACY_WEAK_IR_STD_THRESHOLD)
        / (500.0 - LEGACY_WEAK_IR_STD_THRESHOLD)
    )
    amplitude_score = _clamp(
        (signal_amplitude - MIN_ROBUST_IR_AMPLITUDE)
        / (3_000.0 - MIN_ROBUST_IR_AMPLITUDE)
    )
    signal_score = 0.5 * variation_score + 0.5 * amplitude_score
    finger_score = _clamp(
        (ir_mean - 20_000.0)
        / (LEGACY_FINGER_IR_MEAN_THRESHOLD - 20_000.0)
    )
    saturation_score = _clamp(1.0 - saturation_fraction / 0.01)

    quality_score = _clamp(
        0.45 * timing_score
        + 0.40 * signal_score
        + 0.10 * finger_score
        + 0.05 * saturation_score
    )

    if sample_count < 2:
        quality_classification = "BAD_TIMING"
        quality_score = 0.0
    elif ir_mean < LEGACY_FINGER_IR_MEAN_THRESHOLD:
        quality_classification = "NO_FINGER"
        quality_score = min(quality_score, 0.15)
    elif (
        nonpositive_interval_count > 0
        or rate_error_fraction > 0.20
        or gap_fraction > 0.02
        or max_timing_gap_sec > expected_sample_interval_sec * 10.0
    ):
        quality_classification = "BAD_TIMING"
    elif (
        ir_std < LEGACY_WEAK_IR_STD_THRESHOLD
        or signal_amplitude < MIN_ROBUST_IR_AMPLITUDE
        or quality_score < 0.40
    ):
        quality_classification = "WEAK"
    elif (
        quality_score >= 0.80
        and missing_gap_count == 0
        and rate_error_fraction <= 0.05
    ):
        quality_classification = "GOOD"
    else:
        quality_classification = "MODERATE"

    usability_reasons = _rr_usability_reasons(
        sample_count=sample_count,
        duration_sec=recording_duration_sec,
        quality_classification=quality_classification,
        quality_score=quality_score,
        ir_mean=ir_mean,
        ir_std=ir_std,
        signal_amplitude=signal_amplitude,
        rate_error_fraction=rate_error_fraction,
        missing_gap_count=missing_gap_count,
        gap_fraction=gap_fraction,
        max_timing_gap_sec=max_timing_gap_sec,
        nonpositive_interval_count=nonpositive_interval_count,
        expected_sample_interval_sec=expected_sample_interval_sec,
    )

    return PPGQualityResult(
        sample_count=sample_count,
        recording_duration_sec=recording_duration_sec,
        measured_sample_rate_hz=measured_sample_rate_hz,
        missing_gap_count=missing_gap_count,
        estimated_missing_samples=estimated_missing_samples,
        max_timing_gap_sec=max_timing_gap_sec,
        nonpositive_interval_count=nonpositive_interval_count,
        ir_mean=ir_mean,
        ir_std=ir_std,
        red_mean=red_mean,
        red_std=red_std,
        signal_amplitude=signal_amplitude,
        red_signal_amplitude=red_signal_amplitude,
        quality_score=quality_score,
        quality_classification=quality_classification,
        usable_for_future_rr=not usability_reasons,
        usability_reasons=usability_reasons,
    )


def _rr_usability_reasons(
    *,
    sample_count,
    duration_sec,
    quality_classification,
    quality_score,
    ir_mean,
    ir_std,
    signal_amplitude,
    rate_error_fraction,
    missing_gap_count,
    gap_fraction,
    max_timing_gap_sec,
    nonpositive_interval_count,
    expected_sample_interval_sec,
):
    reasons: List[str] = []

    if sample_count < 2:
        reasons.append("Fewer than two valid samples were recorded.")
    if duration_sec < MIN_RR_RESEARCH_WINDOW_SEC:
        reasons.append(
            f"Recording is shorter than {MIN_RR_RESEARCH_WINDOW_SEC:.0f} seconds."
        )
    if quality_classification == "NO_FINGER":
        reasons.append(
            "IR level is below the current finger-detection threshold "
            f"({ir_mean:.1f} < {LEGACY_FINGER_IR_MEAN_THRESHOLD:.0f})."
        )
    if quality_classification == "WEAK":
        reasons.append(
            "PPG pulsatility is too weak for a future RR-development window "
            f"(IR std {ir_std:.1f}, robust amplitude {signal_amplitude:.1f})."
        )
    if quality_classification == "BAD_TIMING":
        reasons.append("Sample timing is not sufficiently reliable.")
    if nonpositive_interval_count:
        reasons.append(
            f"Found {nonpositive_interval_count} duplicate or reversed timestamps."
        )
    if rate_error_fraction > 0.10:
        reasons.append(
            f"Measured sample rate differs from nominal by "
            f"{rate_error_fraction * 100.0:.1f}%."
        )
    if missing_gap_count and gap_fraction > 0.002:
        reasons.append(
            f"Found {missing_gap_count} timing gaps, exceeding the 0.2% "
            "window tolerance."
        )
    if max_timing_gap_sec > expected_sample_interval_sec * 3.0:
        reasons.append(
            f"Maximum timing gap is {max_timing_gap_sec:.4f} seconds."
        )
    if quality_score < 0.50 and quality_classification not in {
        "NO_FINGER",
        "WEAK",
        "BAD_TIMING",
    }:
        reasons.append(f"Quality score is too low ({quality_score:.3f}).")

    return list(dict.fromkeys(reasons))


def _mean(values: Sequence[int]) -> float:
    return statistics.fmean(values) if values else 0.0


def _std(values: Sequence[int]) -> float:
    return statistics.pstdev(values) if values else 0.0


def _robust_amplitude(values: Sequence[int]) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    return _percentile(sorted_values, 0.95) - _percentile(sorted_values, 0.05)


def _percentile(sorted_values: Sequence[int], fraction: float) -> float:
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = (len(sorted_values) - 1) * fraction
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    lower = float(sorted_values[lower_index])
    upper = float(sorted_values[upper_index])
    return lower + (upper - lower) * (position - lower_index)


def _saturation_fraction(
    red_values: Sequence[int], ir_values: Sequence[int]
) -> float:
    values = list(red_values) + list(ir_values)
    if not values:
        return 1.0
    high_threshold = MAX30102_RAW_MAX * 0.99
    saturated = sum(
        1 for value in values if value <= 0 or value >= high_threshold
    )
    return saturated / len(values)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
