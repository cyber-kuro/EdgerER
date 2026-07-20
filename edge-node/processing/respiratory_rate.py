import csv
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
from scipy.signal import butter, find_peaks, hilbert, sosfiltfilt, welch

from config import PPG_SAMPLE_INTERVAL_SEC
from processing.ppg_quality import (
    MIN_RR_RESEARCH_WINDOW_SEC,
    PPGData,
    REQUIRED_COLUMNS,
    analyze_ppg_quality,
)


RR_SOURCE = "PPG"
RESPIRATORY_BAND_LOW_HZ = 0.08
RESPIRATORY_BAND_HIGH_HZ = 0.70
DEFAULT_STARTUP_DISCARD_SEC = 5.0
LOWER_EDGE_DRIFT_END_HZ = 0.18
HIGH_AMBIGUITY_THRESHOLD = 0.70


@dataclass(frozen=True)
class RespiratoryPeak:
    frequency_hz: float
    breaths_per_min: float
    relative_power: float
    selection_score: float
    ir_relative_power: float
    red_relative_power: float
    envelope_relative_power: float
    stability_score: float


@dataclass(frozen=True)
class RespiratoryRateResult:
    resp_rate_bpm: Optional[float]
    rr_source: str
    rr_confidence: float
    rr_quality: str
    usable: bool
    dominant_frequency_hz: Optional[float]
    analysis_duration_sec: float
    measured_sample_rate_hz: float
    startup_discard_sec: float
    removed_nonfinite_samples: int
    peak_to_noise_ratio: float
    peak_band_power_fraction: float
    peak_to_second_peak_ratio: float
    peak_ambiguity: float
    input_signal_quality: str
    input_signal_quality_score: float
    top_peaks: List[RespiratoryPeak]
    reasons: List[str]

    def as_summary_dict(self) -> Dict[str, object]:
        summary = {
            "resp_rate_bpm": (
                "" if self.resp_rate_bpm is None else self.resp_rate_bpm
            ),
            "rr_source": self.rr_source,
            "rr_confidence": self.rr_confidence,
            "rr_quality": self.rr_quality,
            "usable": self.usable,
            "dominant_frequency_hz": (
                "" if self.dominant_frequency_hz is None else self.dominant_frequency_hz
            ),
            "analysis_duration_sec": self.analysis_duration_sec,
            "measured_sample_rate_hz": self.measured_sample_rate_hz,
            "startup_discard_sec": self.startup_discard_sec,
            "removed_nonfinite_samples": self.removed_nonfinite_samples,
            "peak_to_noise_ratio": self.peak_to_noise_ratio,
            "peak_band_power_fraction": self.peak_band_power_fraction,
            "peak_to_second_peak_ratio": self.peak_to_second_peak_ratio,
            "peak_ambiguity": self.peak_ambiguity,
            "input_signal_quality": self.input_signal_quality,
            "input_signal_quality_score": self.input_signal_quality_score,
            "reasons": "; ".join(self.reasons),
        }
        for number in range(1, 6):
            peak = self.top_peaks[number - 1] if number <= len(self.top_peaks) else None
            summary[f"peak_{number}_frequency_hz"] = (
                peak.frequency_hz if peak else ""
            )
            summary[f"peak_{number}_rr_bpm"] = (
                peak.breaths_per_min if peak else ""
            )
            summary[f"peak_{number}_relative_power"] = (
                peak.relative_power if peak else ""
            )
            summary[f"peak_{number}_selection_score"] = (
                peak.selection_score if peak else ""
            )
        return summary


@dataclass(frozen=True)
class RespiratoryRateDiagnostics:
    raw_time_sec: np.ndarray
    raw_ir: np.ndarray
    analysis_time_sec: np.ndarray
    filtered_ir: np.ndarray
    psd_frequency_hz: np.ndarray
    psd_power: np.ndarray
    psd_red_power: np.ndarray
    psd_envelope_power: np.ndarray


@dataclass(frozen=True)
class RespiratoryRateAnalysis:
    result: RespiratoryRateResult
    diagnostics: RespiratoryRateDiagnostics


def load_ppg_csv_for_rr(csv_path) -> PPGData:
    """Load a recorder CSV while allowing non-finite signal rows to be removed."""
    path = Path(csv_path)
    columns = {name: [] for name in REQUIRED_COLUMNS}
    with path.open("r", newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file)
        fieldnames = set(reader.fieldnames or [])
        missing = [name for name in REQUIRED_COLUMNS if name not in fieldnames]
        if missing:
            raise ValueError(
                "PPG CSV is missing required columns: " + ", ".join(missing)
            )
        for row_number, row in enumerate(reader, start=2):
            if not any((value or "").strip() for value in row.values()):
                continue
            try:
                for name in REQUIRED_COLUMNS:
                    columns[name].append(float(row[name]))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Invalid numeric value in PPG CSV row {row_number}"
                ) from exc
    return PPGData(
        elapsed_time_sec=columns["elapsed_time_sec"],
        monotonic_time_sec=columns["monotonic_time_sec"],
        red_raw=columns["red_raw"],
        ir_raw=columns["ir_raw"],
    )


def estimate_respiratory_rate(
    data: PPGData,
    startup_discard_sec: float = DEFAULT_STARTUP_DISCARD_SEC,
    respiratory_band_low_hz: float = RESPIRATORY_BAND_LOW_HZ,
    respiratory_band_high_hz: float = RESPIRATORY_BAND_HIGH_HZ,
    remove_moving_average_baseline: bool = True,
    suppress_lower_edge_drift: bool = True,
) -> RespiratoryRateAnalysis:
    """Estimate research-only RR from recorded PPG; never used by the live node."""
    if not 3.0 <= startup_discard_sec <= 5.0:
        raise ValueError("startup_discard_sec must be between 3 and 5 seconds")
    if not 0 < respiratory_band_low_hz < respiratory_band_high_hz:
        raise ValueError("invalid respiratory frequency band")

    cleaned, removed_nonfinite = _remove_nonfinite_rows(data)
    raw_time, raw_ir = _raw_diagnostics(cleaned)
    empty_diagnostics = _empty_diagnostics(raw_time, raw_ir)
    if len(cleaned.monotonic_time_sec) < 2:
        return _unavailable(
            "UNAVAILABLE",
            "Fewer than two finite PPG samples are available.",
            startup_discard_sec,
            removed_nonfinite,
            empty_diagnostics,
        )

    input_quality = analyze_ppg_quality(cleaned)
    if input_quality.quality_classification in {"NO_FINGER", "WEAK"}:
        return _unavailable(
            "BAD_SIGNAL",
            f"Input PPG quality is {input_quality.quality_classification}.",
            startup_discard_sec,
            removed_nonfinite,
            empty_diagnostics,
            input_quality,
        )

    expected_rate_hz = 1.0 / PPG_SAMPLE_INTERVAL_SEC
    rate_error = abs(input_quality.measured_sample_rate_hz - expected_rate_hz) / expected_rate_hz
    interval_count = max(1, input_quality.sample_count - 1)
    gap_fraction = input_quality.missing_gap_count / interval_count
    if (
        input_quality.quality_classification == "BAD_TIMING"
        or input_quality.nonpositive_interval_count > 0
        or rate_error > 0.10
        or gap_fraction > 0.002
        or input_quality.max_timing_gap_sec > PPG_SAMPLE_INTERVAL_SEC * 3.0
    ):
        return _unavailable(
            "BAD_TIMING",
            "Input timestamps contain excessive gaps, reversals, or sample-rate error.",
            startup_discard_sec,
            removed_nonfinite,
            empty_diagnostics,
            input_quality,
        )
    if input_quality.recording_duration_sec < MIN_RR_RESEARCH_WINDOW_SEC:
        return _unavailable(
            "UNAVAILABLE",
            "Recording is shorter than the 45-second minimum research window.",
            startup_discard_sec,
            removed_nonfinite,
            empty_diagnostics,
            input_quality,
        )

    monotonic = np.asarray(cleaned.monotonic_time_sec, dtype=float)
    relative_time = monotonic - monotonic[0]
    keep = relative_time >= startup_discard_sec
    analysis_time = relative_time[keep]
    analysis_ir = np.asarray(cleaned.ir_raw, dtype=float)[keep]
    analysis_red = np.asarray(cleaned.red_raw, dtype=float)[keep]
    if len(analysis_time) < 2:
        return _unavailable(
            "UNAVAILABLE",
            "No samples remain after startup-transient removal.",
            startup_discard_sec,
            removed_nonfinite,
            empty_diagnostics,
            input_quality,
        )

    intervals = np.diff(analysis_time)
    intervals = intervals[intervals > 0]
    if not len(intervals):
        return _unavailable(
            "BAD_TIMING",
            "No positive timestamp intervals remain after preprocessing.",
            startup_discard_sec,
            removed_nonfinite,
            empty_diagnostics,
            input_quality,
        )
    sample_rate_hz = 1.0 / float(np.median(intervals))
    if sample_rate_hz <= respiratory_band_high_hz * 2.0:
        return _unavailable(
            "BAD_TIMING",
            "Sample rate is too low for the configured respiratory band.",
            startup_discard_sec,
            removed_nonfinite,
            empty_diagnostics,
            input_quality,
        )

    uniform_time = np.arange(
        analysis_time[0], analysis_time[-1] + 0.5 / sample_rate_hz, 1.0 / sample_rate_hz
    )
    uniform_ir = np.interp(uniform_time, analysis_time, analysis_ir)
    uniform_red = np.interp(uniform_time, analysis_time, analysis_red)
    analysis_duration = float(uniform_time[-1] - uniform_time[0])

    try:
        filtered_ir, filtered_red, filtered_envelope = _preprocess_surrogates(
            uniform_ir,
            uniform_red,
            sample_rate_hz,
            respiratory_band_low_hz,
            respiratory_band_high_hz,
            remove_moving_average_baseline,
        )
    except ValueError as exc:
        return _unavailable(
            "UNAVAILABLE",
            f"Respiratory preprocessing failed: {exc}",
            startup_discard_sec,
            removed_nonfinite,
            empty_diagnostics,
            input_quality,
            analysis_duration,
        )

    frequencies, ir_power, nfft = _welch_band(
        filtered_ir, sample_rate_hz, respiratory_band_low_hz, respiratory_band_high_hz
    )
    _, red_power, _ = _welch_band(
        filtered_red,
        sample_rate_hz,
        respiratory_band_low_hz,
        respiratory_band_high_hz,
        nfft,
    )
    _, envelope_power, _ = _welch_band(
        filtered_envelope,
        sample_rate_hz,
        respiratory_band_low_hz,
        respiratory_band_high_hz,
        nfft,
    )
    diagnostics = RespiratoryRateDiagnostics(
        raw_time_sec=raw_time,
        raw_ir=raw_ir,
        analysis_time_sec=uniform_time,
        filtered_ir=filtered_ir,
        psd_frequency_hz=frequencies,
        psd_power=ir_power,
        psd_red_power=red_power,
        psd_envelope_power=envelope_power,
    )
    if len(ir_power) < 3 or not np.any(ir_power + envelope_power > 0):
        return _unavailable(
            "BAD_SIGNAL",
            "Respiratory-band PSD contains no usable power.",
            startup_discard_sec,
            removed_nonfinite,
            diagnostics,
            input_quality,
            analysis_duration,
        )

    half = len(filtered_ir) // 2
    half1_support = _half_support(
        filtered_ir[:half], filtered_envelope[:half], sample_rate_hz, frequencies, nfft
    )
    half2_support = _half_support(
        filtered_ir[half:], filtered_envelope[half:], sample_rate_hz, frequencies, nfft
    )
    top_peaks, primary_support, _ = _rank_peaks(
        frequencies,
        ir_power,
        red_power,
        envelope_power,
        half1_support,
        half2_support,
        suppress_lower_edge_drift,
    )
    if not top_peaks:
        return _unavailable(
            "BAD_SIGNAL",
            "No respiratory-band spectral peaks were found.",
            startup_discard_sec,
            removed_nonfinite,
            diagnostics,
            input_quality,
            analysis_duration,
        )

    selected = top_peaks[0]
    selected_index = int(np.argmin(np.abs(frequencies - selected.frequency_hz)))
    second_score = top_peaks[1].selection_score if len(top_peaks) > 1 else 0.0
    ambiguity = second_score / max(selected.selection_score, np.finfo(float).tiny)
    peak_to_second = (
        selected.selection_score / second_score if second_score > 0 else 1_000_000.0
    )
    peak_to_noise = _peak_to_noise_ratio(primary_support, selected_index)
    band_fraction = _peak_band_power_fraction(
        frequencies, primary_support, selected.frequency_hz
    )

    spectral_score = (
        0.30 * _scaled_score(peak_to_noise, 3.0, 10.0)
        + 0.25 * _scaled_score(band_fraction, 0.10, 0.35)
        + 0.25 * selected.stability_score
        + 0.20 * selected.relative_power
    )
    timing_score = 0.6 * _clamp(1.0 - rate_error / 0.10) + 0.4 * _clamp(
        1.0 - gap_fraction / 0.002
    )
    duration_score = _scaled_score(analysis_duration, 35.0, 55.0)
    finite_score = _clamp(
        1.0 - removed_nonfinite / max(1, len(data.monotonic_time_sec)) / 0.01
    )
    confidence = _clamp(
        0.50 * spectral_score
        + 0.25 * input_quality.quality_score
        + 0.15 * timing_score
        + 0.05 * duration_score
        + 0.05 * finite_score
    )

    reasons = []
    high_ambiguity = ambiguity >= HIGH_AMBIGUITY_THRESHOLD
    if high_ambiguity:
        confidence *= 0.35
        reasons.append(
            "A competing respiratory-band peak is nearly as strong as the selected peak "
            f"(relative score {ambiguity:.3f})."
        )
    elif ambiguity >= 0.60:
        confidence *= 0.60
        reasons.append(
            "A substantial competing respiratory-band peak reduces confidence "
            f"(relative score {ambiguity:.3f})."
        )
    if spectral_score < 0.45:
        reasons.append("The selected respiratory-band peak is not sufficiently dominant.")
    if selected.stability_score < 0.30:
        reasons.append("The selected peak is not stable across both halves of the recording.")
    if removed_nonfinite:
        reasons.append(f"Removed {removed_nonfinite} non-finite rows.")

    strongest_ir_frequency = float(frequencies[int(np.argmax(ir_power))])
    if abs(strongest_ir_frequency - selected.frequency_hz) >= 0.04:
        reasons.append(
            "Peak ranking suppressed a stronger raw low-frequency component in favor of "
            "cross-channel/envelope/stability evidence."
        )

    at_edge = (
        selected.frequency_hz <= respiratory_band_low_hz + 0.01
        or selected.frequency_hz >= respiratory_band_high_hz - 0.01
    )
    if at_edge:
        confidence = min(confidence, 0.49)
        reasons.append("The selected peak is at the respiratory-band boundary.")

    reject = (
        high_ambiguity
        or spectral_score < 0.45
        or selected.stability_score < 0.20
        or confidence < 0.50
        or at_edge
    )
    if reject:
        rr_quality = "LOW_CONFIDENCE"
        usable = False
        resp_rate_bpm = None
        confidence = min(confidence, 0.49)
    else:
        usable = True
        resp_rate_bpm = selected.breaths_per_min
        rr_quality = (
            "GOOD"
            if confidence >= 0.80 and ambiguity < 0.50 and selected.stability_score >= 0.50
            else "MODERATE"
        )

    result = RespiratoryRateResult(
        resp_rate_bpm=round(resp_rate_bpm, 1) if resp_rate_bpm is not None else None,
        rr_source=RR_SOURCE,
        rr_confidence=round(float(confidence), 3),
        rr_quality=rr_quality,
        usable=usable,
        dominant_frequency_hz=round(selected.frequency_hz, 6),
        analysis_duration_sec=round(analysis_duration, 3),
        measured_sample_rate_hz=round(input_quality.measured_sample_rate_hz, 6),
        startup_discard_sec=startup_discard_sec,
        removed_nonfinite_samples=removed_nonfinite,
        peak_to_noise_ratio=round(float(peak_to_noise), 3),
        peak_band_power_fraction=round(float(band_fraction), 3),
        peak_to_second_peak_ratio=round(float(peak_to_second), 3),
        peak_ambiguity=round(float(ambiguity), 3),
        input_signal_quality=input_quality.quality_classification,
        input_signal_quality_score=round(input_quality.quality_score, 3),
        top_peaks=top_peaks,
        reasons=reasons,
    )
    return RespiratoryRateAnalysis(result=result, diagnostics=diagnostics)


def _preprocess_surrogates(ir, red, sample_rate_hz, low_hz, high_hz, remove_baseline):
    ir_clean = _robust_detrend(ir)
    red_clean = _robust_detrend(red)
    if remove_baseline:
        window = max(3, int(round(sample_rate_hz * 12.0)))
        ir_clean -= _moving_average(ir_clean, window)
        red_clean -= _moving_average(red_clean, window)
    ir_z = _zscore(ir_clean)
    red_z = _zscore(red_clean)
    filtered_ir = _bandpass(ir_z, sample_rate_hz, low_hz, high_hz, order=4)
    filtered_red = _bandpass(red_z, sample_rate_hz, low_hz, high_hz, order=4)

    cardiac_high = min(3.0, sample_rate_hz * 0.45)
    if cardiac_high <= 0.70:
        envelope_filtered = np.zeros_like(ir_z)
    else:
        cardiac = _bandpass(ir_z, sample_rate_hz, 0.70, cardiac_high, order=3)
        envelope = np.abs(hilbert(cardiac))
        envelope = _robust_detrend(envelope)
        if remove_baseline:
            envelope -= _moving_average(envelope, max(3, int(sample_rate_hz * 12.0)))
        envelope_filtered = _bandpass(
            _zscore(envelope), sample_rate_hz, low_hz, high_hz, order=4
        )
    return filtered_ir, filtered_red, envelope_filtered


def _robust_detrend(values):
    values = np.asarray(values, dtype=float)
    if len(values) < 4:
        return values - np.median(values)
    x = np.linspace(-1.0, 1.0, len(values))
    mask = np.ones(len(values), dtype=bool)
    trend = np.full(len(values), np.median(values))
    for _ in range(3):
        if np.count_nonzero(mask) < 4:
            break
        coefficients = np.polyfit(x[mask], values[mask], deg=2)
        trend = np.polyval(coefficients, x)
        residual = values - trend
        median = np.median(residual[mask])
        mad = np.median(np.abs(residual[mask] - median))
        if mad <= np.finfo(float).eps:
            break
        mask = np.abs(residual - median) <= 4.0 * 1.4826 * mad
    return values - trend


def _moving_average(values, window_size):
    window_size = min(len(values), max(1, int(window_size)))
    if window_size % 2 == 0:
        window_size += 1
    if window_size > len(values):
        window_size = len(values) if len(values) % 2 else max(1, len(values) - 1)
    kernel = np.ones(window_size, dtype=float) / window_size
    padding = window_size // 2
    padded = np.pad(values, padding, mode="reflect")
    return np.convolve(padded, kernel, mode="valid")


def _zscore(values):
    centered = np.asarray(values, dtype=float) - np.mean(values)
    standard_deviation = float(np.std(centered))
    if standard_deviation <= np.finfo(float).eps:
        return np.zeros_like(centered)
    return centered / standard_deviation


def _bandpass(values, sample_rate_hz, low_hz, high_hz, order):
    sos = butter(
        order, [low_hz, high_hz], btype="bandpass", fs=sample_rate_hz, output="sos"
    )
    return sosfiltfilt(sos, values)


def _welch_band(values, sample_rate_hz, low_hz, high_hz, nfft=None):
    duration = len(values) / sample_rate_hz
    nperseg = min(len(values), max(8, int(sample_rate_hz * min(40.0, duration))))
    if nfft is None:
        target = max(nperseg, int(sample_rate_hz * 120.0))
        nfft = 1 << int(math.ceil(math.log2(target)))
    frequencies, power = welch(
        values,
        fs=sample_rate_hz,
        window="hann",
        nperseg=nperseg,
        noverlap=nperseg // 2,
        nfft=nfft,
        detrend="constant",
        scaling="density",
    )
    mask = (frequencies >= low_hz) & (frequencies <= high_hz)
    return frequencies[mask], power[mask], nfft


def _half_support(ir, envelope, sample_rate_hz, target_frequencies, nfft):
    frequencies, ir_power, _ = _welch_band(
        ir, sample_rate_hz, target_frequencies[0], target_frequencies[-1], nfft
    )
    _, envelope_power, _ = _welch_band(
        envelope, sample_rate_hz, target_frequencies[0], target_frequencies[-1], nfft
    )
    ir_interp = np.interp(target_frequencies, frequencies, ir_power)
    envelope_interp = np.interp(target_frequencies, frequencies, envelope_power)
    return np.maximum(_normalize(ir_interp), 0.75 * _normalize(envelope_interp))


def _rank_peaks(
    frequencies,
    ir_power,
    red_power,
    envelope_power,
    half1_support,
    half2_support,
    suppress_lower_edge_drift,
):
    ir_relative = _normalize(ir_power)
    red_relative = _normalize(red_power)
    envelope_relative = _normalize(envelope_power)
    primary = np.maximum(ir_relative, 0.75 * envelope_relative)
    stability = np.sqrt(np.maximum(0.0, half1_support * half2_support))
    cross_channel = np.sqrt(np.maximum(0.0, primary * red_relative))
    score = (
        0.40 * ir_relative
        + 0.25 * envelope_relative
        + 0.10 * red_relative
        + 0.10 * cross_channel
        + 0.15 * stability
    )
    if suppress_lower_edge_drift:
        score *= _lower_edge_weight(frequencies)

    bin_width = float(frequencies[1] - frequencies[0])
    distance = max(1, int(round(0.04 / bin_width)))
    candidate_indices = set(find_peaks(score, distance=distance)[0].tolist())
    candidate_indices.update(find_peaks(primary, distance=distance)[0].tolist())
    candidate_indices.add(int(np.argmax(score)))
    ordered = sorted(candidate_indices, key=lambda index: float(score[index]), reverse=True)

    selected_indices = []
    for index in ordered:
        if all(abs(index - existing) >= distance for existing in selected_indices):
            selected_indices.append(index)
        if len(selected_indices) == 5:
            break

    peaks = []
    for index in selected_indices:
        frequency = _interpolate_peak_frequency(frequencies, primary, index)
        peaks.append(
            RespiratoryPeak(
                frequency_hz=round(float(frequency), 6),
                breaths_per_min=round(float(frequency * 60.0), 3),
                relative_power=round(float(primary[index]), 4),
                selection_score=round(float(score[index]), 4),
                ir_relative_power=round(float(ir_relative[index]), 4),
                red_relative_power=round(float(red_relative[index]), 4),
                envelope_relative_power=round(float(envelope_relative[index]), 4),
                stability_score=round(float(stability[index]), 4),
            )
        )
    return peaks, primary, score


def _lower_edge_weight(frequencies):
    weight = np.ones_like(frequencies, dtype=float)
    low = RESPIRATORY_BAND_LOW_HZ
    lower_region = frequencies < LOWER_EDGE_DRIFT_END_HZ
    weight[lower_region] = 0.10 + 0.90 * np.clip(
        (frequencies[lower_region] - low) / (LOWER_EDGE_DRIFT_END_HZ - low), 0.0, 1.0
    )
    return weight


def _normalize(values):
    values = np.asarray(values, dtype=float)
    maximum = float(np.max(values)) if len(values) else 0.0
    return values / maximum if maximum > 0 else np.zeros_like(values)


def _remove_nonfinite_rows(data):
    arrays = [
        np.asarray(data.elapsed_time_sec, dtype=float),
        np.asarray(data.monotonic_time_sec, dtype=float),
        np.asarray(data.red_raw, dtype=float),
        np.asarray(data.ir_raw, dtype=float),
    ]
    if len({len(values) for values in arrays}) != 1:
        raise ValueError("PPG data columns must contain the same number of rows")
    mask = np.logical_and.reduce([np.isfinite(values) for values in arrays])
    removed = int(len(arrays[0]) - np.count_nonzero(mask))
    return (
        PPGData(
            elapsed_time_sec=arrays[0][mask].tolist(),
            monotonic_time_sec=arrays[1][mask].tolist(),
            red_raw=arrays[2][mask].tolist(),
            ir_raw=arrays[3][mask].tolist(),
        ),
        removed,
    )


def _raw_diagnostics(data):
    monotonic = np.asarray(data.monotonic_time_sec, dtype=float)
    if len(monotonic):
        monotonic -= monotonic[0]
    return monotonic, np.asarray(data.ir_raw, dtype=float)


def _empty_diagnostics(raw_time, raw_ir):
    empty = np.array([], dtype=float)
    return RespiratoryRateDiagnostics(
        raw_time, raw_ir, empty, empty, empty, empty, empty, empty
    )


def _unavailable(
    quality,
    reason,
    startup_discard_sec,
    removed_nonfinite,
    diagnostics,
    input_quality=None,
    analysis_duration=0.0,
):
    result = RespiratoryRateResult(
        resp_rate_bpm=None,
        rr_source=RR_SOURCE,
        rr_confidence=0.0,
        rr_quality=quality,
        usable=False,
        dominant_frequency_hz=None,
        analysis_duration_sec=round(float(analysis_duration), 3),
        measured_sample_rate_hz=(
            round(input_quality.measured_sample_rate_hz, 6) if input_quality else 0.0
        ),
        startup_discard_sec=startup_discard_sec,
        removed_nonfinite_samples=removed_nonfinite,
        peak_to_noise_ratio=0.0,
        peak_band_power_fraction=0.0,
        peak_to_second_peak_ratio=0.0,
        peak_ambiguity=0.0,
        input_signal_quality=(input_quality.quality_classification if input_quality else "UNAVAILABLE"),
        input_signal_quality_score=(round(input_quality.quality_score, 3) if input_quality else 0.0),
        top_peaks=[],
        reasons=[reason],
    )
    return RespiratoryRateAnalysis(result, diagnostics)


def _interpolate_peak_frequency(frequencies, power, index):
    frequency = float(frequencies[index])
    if index == 0 or index == len(power) - 1:
        return frequency
    values = [math.log(max(float(power[i]), np.finfo(float).tiny)) for i in (index - 1, index, index + 1)]
    denominator = values[0] - 2.0 * values[1] + values[2]
    if abs(denominator) < 1e-12:
        return frequency
    offset = _clamp_signed(0.5 * (values[0] - values[2]) / denominator)
    return frequency + offset * float(frequencies[1] - frequencies[0])


def _peak_to_noise_ratio(power, index):
    exclusion = max(1, int(round(len(power) * 0.04)))
    noise = np.concatenate([power[: max(0, index - exclusion)], power[index + exclusion + 1 :]])
    peak = float(power[index])
    floor = float(np.median(noise)) if len(noise) else 0.0
    return min(1_000_000.0, peak / max(floor, peak * 1e-12, np.finfo(float).tiny))


def _peak_band_power_fraction(frequencies, power, frequency):
    total = float(np.sum(power))
    if total <= 0:
        return 0.0
    return float(np.sum(power[np.abs(frequencies - frequency) <= 0.03]) / total)


def _scaled_score(value, low, high):
    return _clamp((float(value) - low) / (high - low)) if high > low else 0.0


def _clamp(value):
    return max(0.0, min(1.0, float(value)))


def _clamp_signed(value):
    return max(-1.0, min(1.0, float(value)))
