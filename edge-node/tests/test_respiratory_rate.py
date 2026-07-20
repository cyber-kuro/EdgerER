import unittest

import numpy as np

from processing.ppg_quality import PPGData
from processing.respiratory_rate import estimate_respiratory_rate


def synthetic_ppg(
    respiratory_frequency_hz=0.25,
    duration_sec=60.0,
    sample_interval_sec=0.04,
    respiratory_amplitude=1_200.0,
    noise_std=80.0,
):
    rng = np.random.default_rng(20260629)
    time_values = np.arange(0.0, duration_sec, sample_interval_sec)
    heartbeat = 2_000.0 * np.sin(2.0 * np.pi * 1.2 * time_values)
    respiratory_baseline = respiratory_amplitude * np.sin(
        2.0 * np.pi * respiratory_frequency_hz * time_values
    )
    respiratory_amplitude_modulation = (
        500.0
        * np.sin(2.0 * np.pi * respiratory_frequency_hz * time_values)
        * np.sin(2.0 * np.pi * 1.2 * time_values)
    )
    startup_transient = np.where(
        time_values < 4.0,
        8_000.0 * np.exp(-time_values),
        0.0,
    )
    ir_values = (
        100_000.0
        + heartbeat
        + respiratory_baseline
        + respiratory_amplitude_modulation
        + startup_transient
        + rng.normal(0.0, noise_std, len(time_values))
    )
    red_values = (
        90_000.0
        + 1_400.0 * np.sin(2.0 * np.pi * 1.2 * time_values + 0.2)
        + 0.7 * respiratory_baseline
        + rng.normal(0.0, noise_std, len(time_values))
    )

    return PPGData(
        elapsed_time_sec=time_values.tolist(),
        monotonic_time_sec=(30_000.0 + time_values).tolist(),
        red_raw=red_values.tolist(),
        ir_raw=ir_values.tolist(),
    )


class RespiratoryRateTests(unittest.TestCase):
    def test_estimates_fifteen_breaths_per_minute(self):
        analysis = estimate_respiratory_rate(
            synthetic_ppg(respiratory_frequency_hz=0.25)
        )
        result = analysis.result

        self.assertTrue(result.usable)
        self.assertIn(result.rr_quality, {"GOOD", "MODERATE"})
        self.assertIsNotNone(result.resp_rate_bpm)
        self.assertAlmostEqual(result.resp_rate_bpm, 15.0, delta=1.0)
        self.assertAlmostEqual(result.dominant_frequency_hz, 0.25, delta=0.02)
        self.assertGreaterEqual(result.rr_confidence, 0.5)
        self.assertEqual(result.rr_source, "PPG")
        summary = result.as_summary_dict()
        self.assertAlmostEqual(summary["peak_1_rr_bpm"], 15.0, delta=1.0)
        self.assertIn("peak_5_relative_power", summary)

    def test_estimates_twenty_four_breaths_per_minute(self):
        analysis = estimate_respiratory_rate(
            synthetic_ppg(respiratory_frequency_hz=0.40)
        )
        result = analysis.result

        self.assertTrue(result.usable)
        self.assertIsNotNone(result.resp_rate_bpm)
        self.assertAlmostEqual(result.resp_rate_bpm, 24.0, delta=1.0)

    def test_weak_constant_signal_is_rejected(self):
        time_values = np.arange(0.0, 60.0, 0.04)
        data = PPGData(
            elapsed_time_sec=time_values.tolist(),
            monotonic_time_sec=(1_000.0 + time_values).tolist(),
            red_raw=np.full(len(time_values), 90_000.0).tolist(),
            ir_raw=np.full(len(time_values), 100_000.0).tolist(),
        )

        result = estimate_respiratory_rate(data).result

        self.assertFalse(result.usable)
        self.assertIsNone(result.resp_rate_bpm)
        self.assertEqual(result.rr_quality, "BAD_SIGNAL")

    def test_bad_sample_timing_is_rejected(self):
        data = synthetic_ppg(sample_interval_sec=0.10)
        result = estimate_respiratory_rate(data).result

        self.assertFalse(result.usable)
        self.assertIsNone(result.resp_rate_bpm)
        self.assertEqual(result.rr_quality, "BAD_TIMING")

    def test_short_recording_is_unavailable(self):
        data = synthetic_ppg(duration_sec=20.0)
        result = estimate_respiratory_rate(data).result

        self.assertFalse(result.usable)
        self.assertIsNone(result.resp_rate_bpm)
        self.assertEqual(result.rr_quality, "UNAVAILABLE")

    def test_nonfinite_signal_row_is_removed(self):
        data = synthetic_ppg()
        data.ir_raw[300] = float("nan")
        analysis = estimate_respiratory_rate(data)

        self.assertEqual(analysis.result.removed_nonfinite_samples, 1)
        self.assertTrue(analysis.result.usable)

    def test_nondominant_respiratory_content_is_low_confidence(self):
        data = synthetic_ppg(
            respiratory_amplitude=0.0,
            noise_std=300.0,
        )
        result = estimate_respiratory_rate(data).result

        self.assertFalse(result.usable)
        self.assertIsNone(result.resp_rate_bpm)
        self.assertEqual(result.rr_quality, "LOW_CONFIDENCE")

    def test_two_nearly_equal_respiratory_peaks_are_rejected_as_ambiguous(self):
        time_values = np.arange(0.0, 60.0, 0.04)
        heartbeat = 2_000.0 * np.sin(2.0 * np.pi * 1.2 * time_values)
        first_peak = 1_100.0 * np.sin(2.0 * np.pi * 0.20 * time_values)
        second_peak = 1_100.0 * np.sin(2.0 * np.pi * 0.32 * time_values)
        data = PPGData(
            elapsed_time_sec=time_values.tolist(),
            monotonic_time_sec=(5_000.0 + time_values).tolist(),
            ir_raw=(100_000.0 + heartbeat + first_peak + second_peak).tolist(),
            red_raw=(
                90_000.0
                + 0.7 * heartbeat
                + 0.7 * first_peak
                + 0.7 * second_peak
            ).tolist(),
        )

        result = estimate_respiratory_rate(data).result

        self.assertGreaterEqual(result.peak_ambiguity, 0.70)
        self.assertEqual(result.rr_quality, "LOW_CONFIDENCE")
        self.assertFalse(result.usable)
        self.assertIsNone(result.resp_rate_bpm)
        self.assertTrue(any("competing" in reason for reason in result.reasons))

    def test_supported_fast_peak_beats_stronger_ir_only_low_frequency_drift(self):
        source = synthetic_ppg(
            respiratory_frequency_hz=17.0 / 60.0,
            respiratory_amplitude=500.0,
        )
        time_values = np.asarray(source.elapsed_time_sec)
        low_frequency_drift = 1_800.0 * np.sin(
            2.0 * np.pi * 0.127 * time_values
        )
        data = PPGData(
            elapsed_time_sec=source.elapsed_time_sec,
            monotonic_time_sec=source.monotonic_time_sec,
            red_raw=source.red_raw,
            ir_raw=(np.asarray(source.ir_raw) + low_frequency_drift).tolist(),
        )

        result = estimate_respiratory_rate(data).result

        self.assertTrue(result.usable)
        self.assertIsNotNone(result.resp_rate_bpm)
        self.assertAlmostEqual(result.resp_rate_bpm, 17.0, delta=1.0)
        self.assertGreaterEqual(len(result.top_peaks), 2)
        self.assertAlmostEqual(result.top_peaks[1].breaths_per_min, 7.6, delta=1.0)


if __name__ == "__main__":
    unittest.main()
