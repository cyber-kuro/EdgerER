"""Buffer-based heart-rate and SpO2 processing for MAX30102 PPG data.

This module contains no sensor, GPIO, I2C, FIFO, or acquisition operations.
The calculation intentionally stays close to the original MAX30102 prototype
algorithm so the shared-stream refactor does not silently replace it.
"""

from typing import Any, Dict, Optional, Sequence, Tuple, Union

import numpy as np
from scipy.signal import butter, filtfilt, find_peaks

from config import PPG_SAMPLE_INTERVAL_SEC
from processing.ppg_quality import PPGData


PulseFields = Dict[str, Any]


def estimate_hr_spo2_from_samples(
    samples_or_ppg_data: Union[Sequence[Any], PPGData],
    sample_rate_hz: Optional[float] = None,
) -> PulseFields:
    """Estimate HR/SpO2 from an already-collected timestamped PPG window.

    ``samples_or_ppg_data`` may be a :class:`PPGData` instance or a sequence
    of objects exposing ``monotonic_time_sec``, ``red_raw``, and ``ir_raw``.
    The function never reads hardware and never waits for samples.
    """
    monotonic, red, ir = _extract_arrays(samples_or_ppg_data)
    sample_count = len(ir)
    if sample_count == 0:
        return _unavailable_result(
            samples=0,
            ir_mean=0.0,
            ir_std=0.0,
            finger_detected=None,
            signal_quality="UNAVAILABLE",
            reason="No buffered PPG samples are available.",
        )

    finite = np.isfinite(monotonic) & np.isfinite(red) & np.isfinite(ir)
    monotonic = monotonic[finite]
    red = red[finite]
    ir = ir[finite]
    sample_count = len(ir)
    if sample_count == 0:
        return _unavailable_result(
            samples=0,
            ir_mean=0.0,
            ir_std=0.0,
            finger_detected=None,
            signal_quality="UNAVAILABLE",
            reason="No finite buffered PPG samples are available.",
        )

    red_mean = float(np.mean(red))
    ir_mean = float(np.mean(ir))
    ir_std = float(np.std(ir))

    if ir_mean < 50_000:
        return _unavailable_result(
            samples=sample_count,
            ir_mean=ir_mean,
            ir_std=ir_std,
            finger_detected=False,
            signal_quality="NO_FINGER",
            reason="IR level is below the legacy finger-detection threshold.",
        )

    if ir_std < 50:
        return _unavailable_result(
            samples=sample_count,
            ir_mean=ir_mean,
            ir_std=ir_std,
            finger_detected=True,
            signal_quality="WEAK_SIGNAL",
            reason="IR variation is below the legacy signal threshold.",
        )

    measured_rate_hz = _resolve_sample_rate(monotonic, sample_rate_hz)
    if measured_rate_hz <= 6.0:
        return _unavailable_result(
            samples=sample_count,
            ir_mean=ir_mean,
            ir_std=ir_std,
            finger_detected=True,
            signal_quality="UNSTABLE",
            reason="Measured sample rate is too low for the legacy pulse filter.",
        )

    ir_signal = ir - ir_mean
    red_signal = red - red_mean

    kernel = np.ones(5) / 5
    ir_smooth = np.convolve(ir_signal, kernel, mode="same")
    red_smooth = np.convolve(red_signal, kernel, mode="same")

    low_hz = 0.67
    high_hz = 3.0
    nyquist_hz = 0.5 * measured_rate_hz

    try:
        b, a = butter(
            2,
            [low_hz / nyquist_hz, high_hz / nyquist_hz],
            btype="band",
        )
        ir_filtered = filtfilt(b, a, ir_smooth)
        red_filtered = filtfilt(b, a, red_smooth)
    except Exception as exc:
        return _unavailable_result(
            samples=sample_count,
            ir_mean=ir_mean,
            ir_std=ir_std,
            finger_detected=True,
            signal_quality="UNSTABLE",
            reason=f"Legacy pulse filtering failed: {type(exc).__name__}.",
        )

    # The old implementation used eight samples at a nominal 25 Hz. Scale
    # that same 0.32-second separation when host timing measures a nearby rate.
    minimum_peak_distance = max(1, int(round(measured_rate_hz * 0.32)))
    peaks, _ = find_peaks(
        ir_filtered,
        distance=minimum_peak_distance,
        prominence=np.std(ir_filtered) * 0.6,
    )

    if len(peaks) < 4:
        return _unavailable_result(
            samples=sample_count,
            ir_mean=ir_mean,
            ir_std=ir_std,
            finger_detected=True,
            signal_quality="UNSTABLE",
            num_peaks=int(len(peaks)),
            reason="Fewer than four pulse peaks were detected.",
        )

    peak_intervals = np.diff(peaks) / measured_rate_hz
    median_interval = float(np.median(peak_intervals))
    interval_std = float(np.std(peak_intervals))
    bpm = 60.0 / median_interval if median_interval > 0 else float("nan")

    if not np.isfinite(bpm) or bpm < 40 or bpm > 180 or interval_std > 0.35:
        return _unavailable_result(
            samples=sample_count,
            ir_mean=ir_mean,
            ir_std=ir_std,
            finger_detected=True,
            signal_quality="UNSTABLE",
            num_peaks=int(len(peaks)),
            peak_interval_std=interval_std,
            reason="Pulse rate or peak timing is outside legacy acceptance limits.",
        )

    red_ac = float(np.std(red_filtered))
    ir_ac = float(np.std(ir_filtered))

    if red_mean <= 0 or ir_mean <= 0 or ir_ac <= 0:
        spo2 = None
    else:
        ratio = (red_ac / red_mean) / (ir_ac / ir_mean)
        spo2 = max(70.0, min(100.0, 110.0 - 25.0 * ratio))

    return {
        "heart_rate_bpm": round(bpm, 1),
        "spo2_percent": round(spo2, 1) if spo2 is not None else None,
        "num_peaks": int(len(peaks)),
        "samples": sample_count,
        "ir_mean": round(ir_mean, 2),
        "ir_std": round(ir_std, 2),
        "finger_detected": True,
        "signal_quality": "GOOD",
        "peak_interval_std": round(interval_std, 3),
        "reasons": [],
    }


def _extract_arrays(
    samples_or_ppg_data: Union[Sequence[Any], PPGData],
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    if isinstance(samples_or_ppg_data, PPGData):
        monotonic_values = samples_or_ppg_data.monotonic_time_sec
        red_values = samples_or_ppg_data.red_raw
        ir_values = samples_or_ppg_data.ir_raw
    else:
        samples = list(samples_or_ppg_data)
        try:
            monotonic_values = [sample.monotonic_time_sec for sample in samples]
            red_values = [sample.red_raw for sample in samples]
            ir_values = [sample.ir_raw for sample in samples]
        except AttributeError as exc:
            raise TypeError(
                "samples must expose monotonic_time_sec, red_raw, and ir_raw"
            ) from exc

    if not (
        len(monotonic_values) == len(red_values) == len(ir_values)
    ):
        raise ValueError("PPG data columns must contain the same number of rows")

    return (
        np.asarray(monotonic_values, dtype=float),
        np.asarray(red_values, dtype=float),
        np.asarray(ir_values, dtype=float),
    )


def _resolve_sample_rate(
    monotonic: np.ndarray,
    requested_rate_hz: Optional[float],
) -> float:
    if requested_rate_hz is not None:
        if not np.isfinite(requested_rate_hz) or requested_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be finite and greater than zero")
        return float(requested_rate_hz)

    if len(monotonic) >= 2:
        duration = float(monotonic[-1] - monotonic[0])
        if duration > 0:
            return (len(monotonic) - 1) / duration
    return 1.0 / PPG_SAMPLE_INTERVAL_SEC


def _unavailable_result(
    *,
    samples: int,
    ir_mean: float,
    ir_std: float,
    finger_detected: Optional[bool],
    signal_quality: str,
    reason: str,
    num_peaks: int = 0,
    peak_interval_std: Optional[float] = None,
) -> PulseFields:
    return {
        "heart_rate_bpm": None,
        "spo2_percent": None,
        "num_peaks": int(num_peaks),
        "samples": int(samples),
        "ir_mean": round(float(ir_mean), 2),
        "ir_std": round(float(ir_std), 2),
        "finger_detected": finger_detected,
        "signal_quality": signal_quality,
        "peak_interval_std": (
            round(float(peak_interval_std), 3)
            if peak_interval_std is not None
            else None
        ),
        "reasons": [reason],
    }
