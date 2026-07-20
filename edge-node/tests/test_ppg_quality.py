import math
import unittest

from processing.ppg_quality import PPGData, analyze_ppg_quality


def synthetic_ppg(duration_sec=60.0, interval_sec=0.04, ir_mean=100_000):
    sample_count = int(duration_sec / interval_sec) + 1
    times = [index * interval_sec for index in range(sample_count)]
    ir_values = [
        ir_mean + int(2_000 * math.sin(2.0 * math.pi * 1.2 * value))
        for value in times
    ]
    red_values = [
        90_000 + int(1_400 * math.sin(2.0 * math.pi * 1.2 * value + 0.2))
        for value in times
    ]
    return PPGData(
        elapsed_time_sec=times,
        monotonic_time_sec=[10_000.0 + value for value in times],
        red_raw=red_values,
        ir_raw=ir_values,
    )


class PPGQualityTests(unittest.TestCase):
    def test_good_sixty_second_recording_is_candidate_for_future_rr(self):
        result = analyze_ppg_quality(synthetic_ppg())

        self.assertEqual(result.quality_classification, "GOOD")
        self.assertTrue(result.usable_for_future_rr)
        self.assertEqual(result.missing_gap_count, 0)
        self.assertAlmostEqual(result.measured_sample_rate_hz, 25.0, places=3)
        self.assertGreaterEqual(result.quality_score, 0.8)

    def test_low_ir_level_is_classified_as_no_finger(self):
        result = analyze_ppg_quality(synthetic_ppg(ir_mean=30_000))

        self.assertEqual(result.quality_classification, "NO_FINGER")
        self.assertFalse(result.usable_for_future_rr)
        self.assertTrue(
            any("finger-detection" in reason for reason in result.usability_reasons)
        )

    def test_wrong_sample_rate_is_bad_timing(self):
        result = analyze_ppg_quality(
            synthetic_ppg(duration_sec=60.0, interval_sec=0.1)
        )

        self.assertEqual(result.quality_classification, "BAD_TIMING")
        self.assertFalse(result.usable_for_future_rr)

    def test_short_good_signal_is_not_yet_an_rr_candidate(self):
        result = analyze_ppg_quality(synthetic_ppg(duration_sec=20.0))

        self.assertEqual(result.quality_classification, "GOOD")
        self.assertFalse(result.usable_for_future_rr)
        self.assertTrue(
            any("shorter than" in reason for reason in result.usability_reasons)
        )


if __name__ == "__main__":
    unittest.main()
