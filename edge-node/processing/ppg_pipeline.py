"""Shared snapshot-processing and freshness cache for continuous PPG."""

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence

from config import (
    HR_SPO2_MIN_DURATION_SEC,
    HR_SPO2_MIN_SAMPLES,
    HR_SPO2_REFRESH_INTERVAL_SEC,
    HR_SPO2_STALE_AFTER_SEC,
    HR_SPO2_WINDOW_SEC,
    PPG_QUALITY_REFRESH_INTERVAL_SEC,
    PPG_QUALITY_STALE_AFTER_SEC,
    PPG_QUALITY_WINDOW_SEC,
    PPG_WARMUP_RETRY_INTERVAL_SEC,
    RR_MIN_DURATION_SEC,
    RR_MIN_SAMPLES,
    RR_REFRESH_INTERVAL_SEC,
    RR_STALE_AFTER_SEC,
    RR_WINDOW_SEC,
)
from processing.hr_spo2 import estimate_hr_spo2_from_samples
from processing.ppg_quality import PPGData, PPGQualityResult, analyze_ppg_quality
from processing.respiratory_rate import (
    RespiratoryRateResult,
    estimate_respiratory_rate,
)


USABLE_RR_QUALITIES = {"GOOD", "MODERATE"}


class TimestampedPPGSample(Protocol):
    monotonic_time_sec: float
    red_raw: int
    ir_raw: int


class PPGSnapshotSource(Protocol):
    def get_recent_window(self, seconds: float) -> List[TimestampedPPGSample]:
        ...


@dataclass
class PPGCacheState:
    """Latest calculations plus monotonic freshness and diagnostic state."""

    hr_spo2_result: Optional[Dict[str, Any]] = None
    hr_spo2_updated_at: Optional[float] = None
    quality_result: Optional[PPGQualityResult] = None
    quality_updated_at: Optional[float] = None
    rr_result: Optional[RespiratoryRateResult] = None
    rr_updated_at: Optional[float] = None
    hr_spo2_error: Optional[str] = None
    quality_error: Optional[str] = None
    rr_error: Optional[str] = None


def samples_to_ppg_data(samples: Sequence[TimestampedPPGSample]) -> PPGData:
    """Convert an immutable stream snapshot into processing-friendly arrays."""
    snapshot = list(samples)
    if not snapshot:
        return PPGData([], [], [], [])
    first_time = snapshot[0].monotonic_time_sec
    return PPGData(
        elapsed_time_sec=[
            sample.monotonic_time_sec - first_time for sample in snapshot
        ],
        monotonic_time_sec=[sample.monotonic_time_sec for sample in snapshot],
        red_raw=[sample.red_raw for sample in snapshot],
        ir_raw=[sample.ir_raw for sample in snapshot],
    )


def unavailable_pulse_fields() -> Dict[str, Any]:
    return {
        "heart_rate_bpm": None,
        "spo2_percent": None,
        "num_peaks": 0,
        "samples": 0,
        "ir_mean": 0.0,
        "ir_std": 0.0,
        "finger_detected": None,
        # Reuse the existing server/local-alert quality state during warm-up
        # and stale periods instead of introducing a new packet enum value.
        "signal_quality": "UNSTABLE",
    }


def unavailable_rr_fields() -> Dict[str, Any]:
    return {
        "resp_rate_bpm": None,
        "rr_source": "PPG",
        "rr_confidence": 0.0,
        "rr_quality": "UNAVAILABLE",
    }


def rr_packet_fields(result: RespiratoryRateResult) -> Dict[str, Any]:
    send_value = (
        result.usable
        and result.rr_quality in USABLE_RR_QUALITIES
        and result.resp_rate_bpm is not None
    )
    return {
        "resp_rate_bpm": result.resp_rate_bpm if send_value else None,
        "rr_source": "PPG",
        "rr_confidence": float(result.rr_confidence),
        "rr_quality": result.rr_quality,
    }


class SharedPPGPipeline:
    """Refresh cached PPG measurements from one shared stream's snapshots.

    This class never reads MAX30102 hardware. Calls to ``refresh`` are cheap
    when no configured refresh is due and estimator failures preserve the last
    successful cached state until its freshness threshold expires.
    """

    def __init__(
        self,
        stream: PPGSnapshotSource,
        *,
        monotonic_fn: Callable[[], float] = time.monotonic,
        logger: Optional[Callable[[str], None]] = None,
        hr_spo2_estimator: Callable[..., Dict[str, Any]] = (
            estimate_hr_spo2_from_samples
        ),
        quality_analyzer: Callable[[PPGData], PPGQualityResult] = (
            analyze_ppg_quality
        ),
        rr_estimator: Callable[..., Any] = estimate_respiratory_rate,
        hr_window_sec: float = HR_SPO2_WINDOW_SEC,
        hr_min_duration_sec: float = HR_SPO2_MIN_DURATION_SEC,
        hr_min_samples: int = HR_SPO2_MIN_SAMPLES,
        hr_refresh_interval_sec: float = HR_SPO2_REFRESH_INTERVAL_SEC,
        hr_stale_after_sec: float = HR_SPO2_STALE_AFTER_SEC,
        quality_window_sec: float = PPG_QUALITY_WINDOW_SEC,
        quality_refresh_interval_sec: float = PPG_QUALITY_REFRESH_INTERVAL_SEC,
        quality_stale_after_sec: float = PPG_QUALITY_STALE_AFTER_SEC,
        rr_window_sec: float = RR_WINDOW_SEC,
        rr_min_duration_sec: float = RR_MIN_DURATION_SEC,
        rr_min_samples: int = RR_MIN_SAMPLES,
        rr_refresh_interval_sec: float = RR_REFRESH_INTERVAL_SEC,
        rr_stale_after_sec: float = RR_STALE_AFTER_SEC,
        warmup_retry_interval_sec: float = PPG_WARMUP_RETRY_INTERVAL_SEC,
    ) -> None:
        self.stream = stream
        self.monotonic_fn = monotonic_fn
        self.logger = logger
        self.hr_spo2_estimator = hr_spo2_estimator
        self.quality_analyzer = quality_analyzer
        self.rr_estimator = rr_estimator

        self.hr_window_sec = float(hr_window_sec)
        self.hr_min_duration_sec = float(hr_min_duration_sec)
        self.hr_min_samples = int(hr_min_samples)
        self.hr_refresh_interval_sec = float(hr_refresh_interval_sec)
        self.hr_stale_after_sec = float(hr_stale_after_sec)
        self.quality_window_sec = float(quality_window_sec)
        self.quality_refresh_interval_sec = float(quality_refresh_interval_sec)
        self.quality_stale_after_sec = float(quality_stale_after_sec)
        self.rr_window_sec = float(rr_window_sec)
        self.rr_min_duration_sec = float(rr_min_duration_sec)
        self.rr_min_samples = int(rr_min_samples)
        self.rr_refresh_interval_sec = float(rr_refresh_interval_sec)
        self.rr_stale_after_sec = float(rr_stale_after_sec)
        self.warmup_retry_interval_sec = float(warmup_retry_interval_sec)

        self.state = PPGCacheState()
        self._last_hr_attempt: Optional[float] = None
        self._last_quality_attempt: Optional[float] = None
        self._last_rr_attempt: Optional[float] = None
        self._reported_stale = set()

    def refresh(self, now_monotonic: Optional[float] = None) -> None:
        """Run only the processing refreshes currently due."""
        now = self.monotonic_fn() if now_monotonic is None else now_monotonic
        if self._due(
            self._last_quality_attempt,
            self.state.quality_result,
            self.quality_refresh_interval_sec,
            now,
        ):
            self._refresh_quality(now)
        if self._due(
            self._last_hr_attempt,
            self.state.hr_spo2_result,
            self.hr_refresh_interval_sec,
            now,
        ):
            self._refresh_hr_spo2(now)
        if self._due(
            self._last_rr_attempt,
            self.state.rr_result,
            self.rr_refresh_interval_sec,
            now,
        ):
            self._refresh_rr(now)

    def get_packet_fields(
        self,
        now_monotonic: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Return server-compatible pulse/RR fields with stale values withheld."""
        now = self.monotonic_fn() if now_monotonic is None else now_monotonic
        fields = self._fresh_pulse_fields(now)
        fields.update(self._fresh_rr_fields(now))
        return fields

    def get_pulse_packet_fields(
        self,
        now_monotonic: Optional[float] = None,
    ) -> Dict[str, Any]:
        now = self.monotonic_fn() if now_monotonic is None else now_monotonic
        return self._fresh_pulse_fields(now)

    def get_rr_packet_fields(
        self,
        now_monotonic: Optional[float] = None,
    ) -> Dict[str, Any]:
        now = self.monotonic_fn() if now_monotonic is None else now_monotonic
        return self._fresh_rr_fields(now)

    def freshness(self, now_monotonic: Optional[float] = None) -> Dict[str, Any]:
        """Expose internal ages/freshness without adding packet fields."""
        now = self.monotonic_fn() if now_monotonic is None else now_monotonic
        return {
            "hr_spo2_age_sec": _age(now, self.state.hr_spo2_updated_at),
            "hr_spo2_fresh": _is_fresh(
                now,
                self.state.hr_spo2_updated_at,
                self.hr_stale_after_sec,
            ),
            "quality_age_sec": _age(now, self.state.quality_updated_at),
            "quality_fresh": _is_fresh(
                now,
                self.state.quality_updated_at,
                self.quality_stale_after_sec,
            ),
            "rr_age_sec": _age(now, self.state.rr_updated_at),
            "rr_fresh": _is_fresh(
                now,
                self.state.rr_updated_at,
                self.rr_stale_after_sec,
            ),
        }

    def get_quality_result(
        self,
        now_monotonic: Optional[float] = None,
    ) -> Optional[PPGQualityResult]:
        now = self.monotonic_fn() if now_monotonic is None else now_monotonic
        if not _is_fresh(
            now,
            self.state.quality_updated_at,
            self.quality_stale_after_sec,
        ):
            self._report_stale_once("quality")
            return None
        return self.state.quality_result

    def _refresh_hr_spo2(self, now: float) -> None:
        self._last_hr_attempt = now
        samples = self.stream.get_recent_window(self.hr_window_sec)
        if not _has_minimum_window(
            samples,
            self.hr_min_samples,
            self.hr_min_duration_sec,
        ):
            self._log(
                "PPG warm-up: HR/SpO2 unavailable "
                f"({len(samples)} samples, {_duration(samples):.1f}s)."
            )
            return

        try:
            result = self.hr_spo2_estimator(samples)
        except Exception as exc:
            self.state.hr_spo2_error = _error_text(exc)
            self._log(
                "Recoverable HR/SpO2 processing error; cached value retained: "
                f"{self.state.hr_spo2_error}"
            )
            return

        self.state.hr_spo2_result = dict(result)
        self.state.hr_spo2_updated_at = now
        self.state.hr_spo2_error = None
        self._reported_stale.discard("hr_spo2")
        self._log(
            "HR/SpO2 cache refreshed: "
            f"HR={_measurement(result.get('heart_rate_bpm'))}, "
            f"SpO2={_measurement(result.get('spo2_percent'))}, "
            f"quality={result.get('signal_quality', 'UNAVAILABLE')}."
        )

    def _refresh_quality(self, now: float) -> None:
        self._last_quality_attempt = now
        samples = self.stream.get_recent_window(self.quality_window_sec)
        if len(samples) < 2:
            self._log("PPG warm-up: signal-quality analysis needs more samples.")
            return
        try:
            result = self.quality_analyzer(samples_to_ppg_data(samples))
        except Exception as exc:
            self.state.quality_error = _error_text(exc)
            self._log(
                "Recoverable PPG-quality processing error; cached value retained: "
                f"{self.state.quality_error}"
            )
            return

        self.state.quality_result = result
        self.state.quality_updated_at = now
        self.state.quality_error = None
        self._reported_stale.discard("quality")

    def _refresh_rr(self, now: float) -> None:
        self._last_rr_attempt = now
        samples = self.stream.get_recent_window(self.rr_window_sec)
        if not _has_minimum_window(
            samples,
            self.rr_min_samples,
            self.rr_min_duration_sec,
        ):
            self._log(
                "PPG warm-up: RR unavailable "
                f"({len(samples)} samples, {_duration(samples):.1f}s)."
            )
            return

        try:
            analysis = self.rr_estimator(samples_to_ppg_data(samples))
            result = analysis.result
        except Exception as exc:
            self.state.rr_error = _error_text(exc)
            self._log(
                "Recoverable RR processing error; cached value retained: "
                f"{self.state.rr_error}"
            )
            return

        self.state.rr_result = result
        self.state.rr_updated_at = now
        self.state.rr_error = None
        self._reported_stale.discard("rr")
        packet_fields = rr_packet_fields(result)
        disposition = (
            "sent"
            if packet_fields["resp_rate_bpm"] is not None
            else "withheld (null)"
        )
        self._log(
            "RR cache refreshed: "
            f"estimate={_measurement(result.resp_rate_bpm)}, "
            f"confidence={result.rr_confidence:.3f}, "
            f"quality={result.rr_quality}, packet={disposition}."
        )

    def _fresh_pulse_fields(self, now: float) -> Dict[str, Any]:
        if self.state.hr_spo2_result is None or not _is_fresh(
            now,
            self.state.hr_spo2_updated_at,
            self.hr_stale_after_sec,
        ):
            if self.state.hr_spo2_result is not None:
                self._report_stale_once("hr_spo2")
            return unavailable_pulse_fields()

        # Internal processor reasons are deliberately not added to the packet;
        # evaluate_risk owns the existing top-level reasons field.
        return {
            key: value
            for key, value in self.state.hr_spo2_result.items()
            if key != "reasons"
            and not (key == "peak_interval_std" and value is None)
        }

    def _fresh_rr_fields(self, now: float) -> Dict[str, Any]:
        if self.state.rr_result is None or not _is_fresh(
            now,
            self.state.rr_updated_at,
            self.rr_stale_after_sec,
        ):
            if self.state.rr_result is not None:
                self._report_stale_once("rr")
            return unavailable_rr_fields()
        return rr_packet_fields(self.state.rr_result)

    def _due(
        self,
        last_attempt: Optional[float],
        cached_result: Optional[Any],
        refresh_interval: float,
        now: float,
    ) -> bool:
        if last_attempt is None:
            return True
        interval = (
            refresh_interval
            if cached_result is not None
            else self.warmup_retry_interval_sec
        )
        return now - last_attempt >= interval

    def _report_stale_once(self, name: str) -> None:
        if name in self._reported_stale:
            return
        self._reported_stale.add(name)
        self._log(f"Cached {name} measurement is stale and will be withheld.")

    def _log(self, message: str) -> None:
        if self.logger is not None:
            self.logger(message)


def _duration(samples: Sequence[TimestampedPPGSample]) -> float:
    if len(samples) < 2:
        return 0.0
    return max(
        0.0,
        float(samples[-1].monotonic_time_sec - samples[0].monotonic_time_sec),
    )


def _has_minimum_window(
    samples: Sequence[TimestampedPPGSample],
    minimum_samples: int,
    minimum_duration_sec: float,
) -> bool:
    return (
        len(samples) >= minimum_samples
        and _duration(samples) >= minimum_duration_sec
    )


def _age(now: float, updated_at: Optional[float]) -> Optional[float]:
    return None if updated_at is None else max(0.0, now - updated_at)


def _is_fresh(
    now: float,
    updated_at: Optional[float],
    stale_after_sec: float,
) -> bool:
    return updated_at is not None and now - updated_at <= stale_after_sec


def _measurement(value: Any) -> str:
    return "unavailable" if value is None else str(value)


def _error_text(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"
