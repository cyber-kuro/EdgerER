"""Baseline respiratory-rate estimation from a photoplethysmogram (PPG).

This module is research code. It is intentionally independent of the EdgeER
server and is not intended for clinical use without substantially more
validation.
"""

from __future__ import annotations

import math
from typing import Any, Iterable

import numpy as np
from scipy.signal import butter, detrend, sosfiltfilt, welch


DEFAULT_LOW_HZ = 0.08
DEFAULT_HIGH_HZ = 0.70
DEFAULT_WINDOW_SECONDS = 32.0
DEFAULT_MIN_CONFIDENCE = 0.35


def _quality_label(confidence: float) -> str:
    if confidence >= 0.65:
        return "GOOD"
    if confidence >= 0.40:
        return "FAIR"
    return "LOW"


def _rejected_result(reason: str, debug: dict[str, Any]) -> dict[str, Any]:
    debug["rejection_reason"] = reason
    return {
        "resp_rate_bpm": None,
        "rr_confidence": 0.0,
        "rr_quality": "REJECTED",
        "debug": debug,
    }


def _weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
    order = np.argsort(values)
    ordered_values = values[order]
    ordered_weights = weights[order]
    midpoint = 0.5 * float(np.sum(ordered_weights))
    index = int(np.searchsorted(np.cumsum(ordered_weights), midpoint))
    return float(ordered_values[min(index, len(ordered_values) - 1)])


def _window_bounds(
    sample_count: int,
    window_samples: int,
    step_samples: int,
) -> list[tuple[int, int]]:
    if sample_count <= window_samples:
        return [(0, sample_count)]

    starts = list(range(0, sample_count - window_samples + 1, step_samples))
    final_start = sample_count - window_samples
    if starts[-1] != final_start:
        starts.append(final_start)
    return [(start, start + window_samples) for start in starts]


def _parabolic_peak_frequency(
    frequencies: np.ndarray,
    power: np.ndarray,
    peak_index: int,
) -> float:
    if peak_index == 0 or peak_index == len(power) - 1:
        return float(frequencies[peak_index])

    left, center, right = np.log(
        np.maximum(
            power[peak_index - 1 : peak_index + 2],
            np.finfo(float).tiny,
        )
    )
    denominator = left - (2.0 * center) + right
    if abs(denominator) < np.finfo(float).eps:
        return float(frequencies[peak_index])

    offset = 0.5 * (left - right) / denominator
    offset = float(np.clip(offset, -0.5, 0.5))
    spacing = float(frequencies[1] - frequencies[0])
    return float(frequencies[peak_index] + offset * spacing)


def _estimate_window(
    ppg_window: np.ndarray,
    sampling_rate_hz: float,
    low_hz: float,
    high_hz: float,
    min_confidence: float,
) -> dict[str, Any]:
    debug: dict[str, Any] = {
        "sample_count": int(ppg_window.size),
        "duration_seconds": float(ppg_window.size / sampling_rate_hz),
    }

    raw_standard_deviation = float(np.std(ppg_window))
    debug["raw_standard_deviation"] = raw_standard_deviation
    if not math.isfinite(raw_standard_deviation) or raw_standard_deviation < 1e-10:
        return _rejected_result("signal is flat or non-finite", debug)

    centered = detrend(ppg_window, type="linear")
    nyquist_hz = sampling_rate_hz / 2.0
    effective_high_hz = min(high_hz, nyquist_hz * 0.95)
    if low_hz >= effective_high_hz:
        return _rejected_result(
            "sampling rate is too low for the requested respiratory band",
            debug,
        )

    filter_sos = butter(
        4,
        [low_hz, effective_high_hz],
        btype="bandpass",
        fs=sampling_rate_hz,
        output="sos",
    )
    try:
        filtered = sosfiltfilt(filter_sos, centered)
    except ValueError as error:
        return _rejected_result(f"signal is too short to filter: {error}", debug)

    filtered_standard_deviation = float(np.std(filtered))
    debug["filtered_standard_deviation"] = filtered_standard_deviation
    debug["respiratory_modulation_ratio"] = float(
        filtered_standard_deviation / raw_standard_deviation
    )
    if filtered_standard_deviation < 1e-12:
        return _rejected_result("no measurable respiratory-band power", debug)

    nperseg = min(ppg_window.size, max(64, int(round(32 * sampling_rate_hz))))
    frequencies, power = welch(
        filtered,
        fs=sampling_rate_hz,
        window="hann",
        nperseg=nperseg,
        noverlap=nperseg // 2 if ppg_window.size > nperseg else 0,
        detrend="constant",
        scaling="density",
    )

    band_mask = (frequencies >= low_hz) & (frequencies <= effective_high_hz)
    band_frequencies = frequencies[band_mask]
    band_power = power[band_mask]
    if band_power.size < 3 or not np.any(band_power > 0):
        return _rejected_result("insufficient respiratory-band PSD bins", debug)

    peak_index = int(np.argmax(band_power))
    peak_frequency_hz = _parabolic_peak_frequency(
        band_frequencies,
        band_power,
        peak_index,
    )
    frequency_resolution_hz = float(frequencies[1] - frequencies[0])
    peak_half_width_hz = max(0.03, 1.5 * frequency_resolution_hz)
    peak_mask = (
        np.abs(band_frequencies - peak_frequency_hz) <= peak_half_width_hz
    )

    total_band_power = float(np.sum(band_power))
    peak_neighborhood_power = float(np.sum(band_power[peak_mask]))
    peak_dominance = peak_neighborhood_power / max(
        total_band_power,
        np.finfo(float).tiny,
    )
    positive_power = band_power[band_power > 0]
    median_power = float(np.median(positive_power))
    peak_to_median = float(
        band_power[peak_index] / max(median_power, np.finfo(float).tiny)
    )

    probability = band_power / max(total_band_power, np.finfo(float).tiny)
    spectral_entropy = float(
        -np.sum(probability * np.log(np.maximum(probability, np.finfo(float).tiny)))
        / math.log(probability.size)
    )
    spectral_concentration = float(np.clip(1.0 - spectral_entropy, 0.0, 1.0))

    dominance_score = float(np.clip(peak_dominance / 0.45, 0.0, 1.0))
    contrast_score = float(np.clip((peak_to_median - 1.0) / 10.0, 0.0, 1.0))
    confidence = float(
        np.clip(
            0.55 * dominance_score
            + 0.25 * contrast_score
            + 0.20 * spectral_concentration,
            0.0,
            1.0,
        )
    )
    respiratory_rate_bpm = float(peak_frequency_hz * 60.0)

    debug.update(
        {
            "peak_frequency_hz": peak_frequency_hz,
            "frequency_resolution_hz": frequency_resolution_hz,
            "peak_dominance": peak_dominance,
            "peak_to_median_power": peak_to_median,
            "spectral_entropy": spectral_entropy,
            "total_band_power": total_band_power,
            "accepted": confidence >= min_confidence,
        }
    )

    if confidence < min_confidence:
        return _rejected_result(
            f"confidence {confidence:.3f} is below threshold {min_confidence:.3f}",
            {**debug, "candidate_resp_rate_bpm": respiratory_rate_bpm},
        )

    return {
        "resp_rate_bpm": respiratory_rate_bpm,
        "rr_confidence": confidence,
        "rr_quality": _quality_label(confidence),
        "debug": debug,
    }


def estimate_respiratory_rate(
    ppg_signal: Iterable[float] | np.ndarray,
    sampling_rate_hz: float,
    *,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
    step_seconds: float | None = None,
    low_hz: float = DEFAULT_LOW_HZ,
    high_hz: float = DEFAULT_HIGH_HZ,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> dict[str, Any]:
    """Estimate respiratory rate from respiratory modulation in a PPG.

    Long recordings are divided into overlapping windows. The accepted window
    estimates are combined with a confidence-weighted median, while disagreement
    between windows reduces the final confidence.

    Returns a dictionary containing ``resp_rate_bpm``, ``rr_confidence``,
    ``rr_quality``, and JSON-serializable ``debug`` information. A rejected
    estimate has ``resp_rate_bpm`` set to ``None``.
    """

    if not math.isfinite(sampling_rate_hz) or sampling_rate_hz <= 0:
        raise ValueError("sampling_rate_hz must be a positive finite number")
    if window_seconds <= 0:
        raise ValueError("window_seconds must be positive")
    if step_seconds is None:
        step_seconds = window_seconds / 2.0
    if step_seconds <= 0:
        raise ValueError("step_seconds must be positive")
    if not 0 <= min_confidence <= 1:
        raise ValueError("min_confidence must be between 0 and 1")
    if low_hz <= 0 or high_hz <= low_hz:
        raise ValueError("frequency band must satisfy 0 < low_hz < high_hz")

    signal_array = np.asarray(ppg_signal, dtype=float)
    if signal_array.ndim != 1:
        raise ValueError("ppg_signal must be a one-dimensional array")

    original_sample_count = int(signal_array.size)
    finite_mask = np.isfinite(signal_array)
    cleaned_signal = signal_array[finite_mask]
    removed_sample_count = int(original_sample_count - cleaned_signal.size)
    minimum_samples = max(32, int(math.ceil(20.0 * sampling_rate_hz)))
    debug: dict[str, Any] = {
        "sampling_rate_hz": float(sampling_rate_hz),
        "original_sample_count": original_sample_count,
        "valid_sample_count": int(cleaned_signal.size),
        "removed_non_finite_samples": removed_sample_count,
        "window_seconds": float(window_seconds),
        "step_seconds": float(step_seconds),
        "respiratory_band_hz": [float(low_hz), float(high_hz)],
        "min_confidence": float(min_confidence),
        "windows": [],
    }

    if cleaned_signal.size < minimum_samples:
        return _rejected_result(
            f"at least 20 seconds of valid PPG are required ({minimum_samples} samples)",
            debug,
        )

    requested_window_samples = max(1, int(round(window_seconds * sampling_rate_hz)))
    window_samples = min(requested_window_samples, cleaned_signal.size)
    step_samples = max(1, int(round(step_seconds * sampling_rate_hz)))
    bounds = _window_bounds(cleaned_signal.size, window_samples, step_samples)
    accepted_rates: list[float] = []
    accepted_confidences: list[float] = []

    for window_index, (start, end) in enumerate(bounds):
        window_result = _estimate_window(
            cleaned_signal[start:end],
            sampling_rate_hz,
            low_hz,
            high_hz,
            min_confidence,
        )
        window_debug = {
            "window_index": window_index,
            "start_sample": int(start),
            "end_sample": int(end),
            "start_seconds": float(start / sampling_rate_hz),
            "end_seconds": float(end / sampling_rate_hz),
            "resp_rate_bpm": window_result["resp_rate_bpm"],
            "rr_confidence": window_result["rr_confidence"],
            "rr_quality": window_result["rr_quality"],
            **window_result["debug"],
        }
        debug["windows"].append(window_debug)

        if window_result["resp_rate_bpm"] is not None:
            accepted_rates.append(float(window_result["resp_rate_bpm"]))
            accepted_confidences.append(float(window_result["rr_confidence"]))

    debug["window_count"] = len(bounds)
    debug["accepted_window_count"] = len(accepted_rates)
    if not accepted_rates:
        return _rejected_result("all windows were rejected", debug)

    rates = np.asarray(accepted_rates, dtype=float)
    confidences = np.asarray(accepted_confidences, dtype=float)
    combined_rate = _weighted_median(rates, confidences)
    median_absolute_deviation = float(np.median(np.abs(rates - combined_rate)))
    consistency_score = float(math.exp(-((median_absolute_deviation / 3.0) ** 2)))
    combined_confidence = float(
        np.clip(np.median(confidences) * consistency_score, 0.0, 1.0)
    )
    debug.update(
        {
            "window_rate_median_absolute_deviation_bpm": median_absolute_deviation,
            "window_consistency_score": consistency_score,
            "combined_confidence_before_threshold": combined_confidence,
        }
    )

    if combined_confidence < min_confidence:
        return _rejected_result(
            "accepted windows disagree too much for a confident combined estimate",
            {**debug, "candidate_resp_rate_bpm": combined_rate},
        )

    return {
        "resp_rate_bpm": float(combined_rate),
        "rr_confidence": combined_confidence,
        "rr_quality": _quality_label(combined_confidence),
        "debug": debug,
    }


__all__ = ["estimate_respiratory_rate"]
