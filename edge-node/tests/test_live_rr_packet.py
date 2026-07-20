import csv
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from scripts.test_live_rr_packet import (
    build_packet,
    live_rr_artifact_paths,
    parse_args,
    rr_packet_fields,
    save_ppg_window_csv,
)


class LiveRRPacketTests(unittest.TestCase):
    def test_good_usable_rr_is_included(self):
        result = SimpleNamespace(
            usable=True,
            rr_quality="GOOD",
            resp_rate_bpm=16.2,
            rr_confidence=0.87,
        )

        fields = rr_packet_fields(result)

        self.assertEqual(fields["resp_rate_bpm"], 16.2)
        self.assertEqual(fields["rr_source"], "PPG")
        self.assertEqual(fields["rr_confidence"], 0.87)
        self.assertEqual(fields["rr_quality"], "GOOD")

    def test_low_confidence_rr_is_withheld(self):
        result = SimpleNamespace(
            usable=False,
            rr_quality="LOW_CONFIDENCE",
            resp_rate_bpm=12.4,
            rr_confidence=0.31,
        )

        fields = rr_packet_fields(result)

        self.assertIsNone(fields["resp_rate_bpm"])
        self.assertEqual(fields["rr_source"], "PPG")
        self.assertEqual(fields["rr_confidence"], 0.31)
        self.assertEqual(fields["rr_quality"], "LOW_CONFIDENCE")

    def test_unusable_moderate_result_is_also_withheld(self):
        result = SimpleNamespace(
            usable=False,
            rr_quality="MODERATE",
            resp_rate_bpm=14.0,
            rr_confidence=0.55,
        )

        self.assertIsNone(rr_packet_fields(result)["resp_rate_bpm"])

    def test_packet_keeps_existing_fields_and_adds_only_rr_contract(self):
        temp = {"ambient_temp_c": 23.1, "object_temp_c": 36.8}
        pulse = {
            "heart_rate_bpm": 72.0,
            "spo2_percent": 98.0,
            "num_peaks": 12,
            "samples": 250,
            "ir_mean": 100_000.0,
            "ir_std": 1_000.0,
            "finger_detected": True,
            "signal_quality": "GOOD",
            "peak_interval_std": 0.04,
        }
        rr_fields = {
            "resp_rate_bpm": 15.0,
            "rr_source": "PPG",
            "rr_confidence": 0.82,
            "rr_quality": "GOOD",
        }

        packet = build_packet(
            temp,
            pulse,
            rr_fields,
            timestamp="2026-07-01 12:00:00",
        )

        self.assertEqual(packet["heart_rate_bpm"], 72.0)
        self.assertEqual(packet["spo2_percent"], 98.0)
        self.assertEqual(packet["resp_rate_bpm"], 15.0)
        self.assertEqual(packet["rr_source"], "PPG")
        self.assertIn("status", packet)
        self.assertIn("reasons", packet)
        self.assertNotIn("rr_rejection_reasons", packet)

    def test_save_window_is_enabled_by_default(self):
        args = parse_args(["--no-post"])

        self.assertTrue(args.save_window)

    def test_no_save_window_option_disables_debug_artifacts(self):
        args = parse_args(["--no-post", "--no-save-window"])

        self.assertFalse(args.save_window)

    def test_artifact_paths_use_live_rr_window_timestamp(self):
        paths = live_rr_artifact_paths(
            datetime(2026, 7, 1, 12, 34, 56, tzinfo=timezone.utc)
        )

        self.assertEqual(
            Path(paths["raw_csv_path"]).name,
            "live_rr_window_20260701T123456Z.csv",
        )
        self.assertEqual(
            Path(paths["rr_plot_path"]).name,
            "live_rr_window_20260701T123456Z_rr_diagnostic.png",
        )

    def test_save_ppg_window_csv_writes_expected_columns(self):
        data = SimpleNamespace(
            elapsed_time_sec=[0.0, 0.04],
            monotonic_time_sec=[100.0, 100.04],
            red_raw=[12345, 12355],
            ir_raw=[67890, 67910],
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "window.csv"

            save_ppg_window_csv(output_path, data)

            with output_path.open("r", newline="", encoding="utf-8") as csv_file:
                rows = list(csv.DictReader(csv_file))

        self.assertEqual(
            list(rows[0].keys()),
            [
                "elapsed_time_sec",
                "monotonic_time_sec",
                "red_raw",
                "ir_raw",
            ],
        )
        self.assertEqual(rows[1]["red_raw"], "12355")
        self.assertEqual(rows[1]["ir_raw"], "67910")


if __name__ == "__main__":
    unittest.main()
