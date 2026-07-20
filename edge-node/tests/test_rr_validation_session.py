import csv
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from scripts.rr_validation_session import (
    RESULT_FIELDS,
    append_validation_row,
    build_validation_row,
    calculate_expected_rr,
    calculate_summary,
)


def fake_quality():
    return SimpleNamespace(
        quality_classification="GOOD",
        quality_score=0.91,
    )


def fake_rr_result(**overrides):
    values = {
        "resp_rate_bpm": 15.0,
        "rr_confidence": 0.84,
        "rr_quality": "GOOD",
        "usable": True,
        "reasons": [],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class RRValidationSessionTests(unittest.TestCase):
    def test_expected_rr_calculation(self):
        self.assertAlmostEqual(calculate_expected_rr(12, 60.0), 12.0)
        self.assertAlmostEqual(calculate_expected_rr(10, 50.0), 12.0)

    def test_row_creation_and_csv_append(self):
        timestamp = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "rr_validation.csv"
            results = Path(temp_dir) / "results.csv"
            row = build_validation_row(
                timestamp_utc=timestamp,
                file_path=source,
                condition="normal",
                duration_sec=60.0,
                manual_breath_count=14,
                rr_result=fake_rr_result(resp_rate_bpm=15.5),
                ppg_quality=fake_quality(),
                note="steady finger",
            )
            append_validation_row(results, row)
            with results.open("r", newline="", encoding="utf-8") as csv_file:
                saved_rows = list(csv.DictReader(csv_file))

        self.assertEqual(list(saved_rows[0].keys()), list(RESULT_FIELDS))
        self.assertEqual(saved_rows[0]["condition"], "normal")
        self.assertEqual(float(saved_rows[0]["expected_rr_bpm"]), 14.0)
        self.assertEqual(float(saved_rows[0]["error_bpm"]), 1.5)
        self.assertEqual(float(saved_rows[0]["absolute_error_bpm"]), 1.5)
        self.assertEqual(float(saved_rows[0]["sent_value"]), 15.5)

    def test_summary_calculation_uses_only_usable_errors(self):
        rows = [
            {
                "condition": "normal",
                "rr_quality": "GOOD",
                "usable": "True",
                "sent_value": "12.0",
                "error_bpm": "1.0",
            },
            {
                "condition": "fast",
                "rr_quality": "MODERATE",
                "usable": "True",
                "sent_value": "16.0",
                "error_bpm": "-3.0",
            },
            {
                "condition": "motion",
                "rr_quality": "LOW_CONFIDENCE",
                "usable": "False",
                "sent_value": "",
                "error_bpm": "8.0",
            },
        ]

        summary = calculate_summary(rows)

        self.assertEqual(summary["trial_count"], 3)
        self.assertEqual(summary["usable_trial_count"], 2)
        self.assertEqual(summary["withheld_trial_count"], 1)
        self.assertAlmostEqual(summary["mae_usable_bpm"], 2.0)
        self.assertAlmostEqual(summary["rmse_usable_bpm"], 5 ** 0.5)
        self.assertAlmostEqual(summary["within_two_percent_usable"], 50.0)
        self.assertEqual(summary["condition_counts"]["normal"], 1)
        self.assertEqual(summary["rr_quality_counts"]["LOW_CONFIDENCE"], 1)

    def test_low_confidence_null_estimate_does_not_crash(self):
        row = build_validation_row(
            timestamp_utc="2026-07-02T12:00:00+00:00",
            file_path="window.csv",
            condition="motion",
            duration_sec=60.0,
            manual_breath_count=16,
            rr_result=fake_rr_result(
                resp_rate_bpm=None,
                rr_confidence=0.2,
                rr_quality="LOW_CONFIDENCE",
                usable=False,
                reasons=["Competing peaks."],
            ),
            ppg_quality=fake_quality(),
        )

        self.assertEqual(row["estimated_rr_bpm"], "")
        self.assertEqual(row["sent_value"], "")
        self.assertEqual(row["error_bpm"], "")
        self.assertEqual(row["absolute_error_bpm"], "")
        self.assertIn("Competing peaks", row["rejection_reasons"])


if __name__ == "__main__":
    unittest.main()
