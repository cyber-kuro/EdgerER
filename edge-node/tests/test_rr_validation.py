import math
import unittest

from scripts.validate_rr_recordings import (
    calculate_summary,
    expected_range_warning,
)


class RRValidationSummaryTests(unittest.TestCase):
    def test_summary_uses_only_usable_numeric_estimates(self):
        results = [
            {"usable": True, "error_bpm": "1.000"},
            {"usable": True, "error_bpm": "-2.000"},
            {"usable": True, "error_bpm": "4.000"},
            {"usable": False, "error_bpm": "5.000"},
        ]

        summary = calculate_summary(results)

        self.assertEqual(summary["usable_count"], 3)
        self.assertAlmostEqual(summary["mae"], 7.0 / 3.0)
        self.assertAlmostEqual(summary["rmse"], math.sqrt(7.0))
        self.assertAlmostEqual(summary["bias"], 1.0)
        self.assertAlmostEqual(summary["within_2_percent"], 200.0 / 3.0)
        self.assertAlmostEqual(summary["within_3_percent"], 200.0 / 3.0)
        self.assertEqual(summary["all_file_count"], 4)
        self.assertAlmostEqual(summary["all_file_mae"], 3.0)

    def test_summary_is_unavailable_when_every_recording_is_rejected(self):
        summary = calculate_summary(
            [{"usable": False, "error_bpm": ""}]
        )

        self.assertEqual(summary["usable_count"], 0)
        self.assertIsNone(summary["mae"])
        self.assertIsNone(summary["rmse"])
        self.assertIsNone(summary["bias"])

    def test_expected_rate_below_lower_band_gets_range_warning(self):
        warning = expected_range_warning(4.0)

        self.assertIn("below", warning)
        self.assertIn("4.8", warning)
        self.assertIn("outside", warning)
        self.assertEqual(expected_range_warning(10.0), "")


if __name__ == "__main__":
    unittest.main()
