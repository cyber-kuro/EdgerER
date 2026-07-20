from datetime import datetime, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import main as node_main


class MainRespiratoryRateIntegrationTests(unittest.TestCase):
    def setUp(self):
        node_main.latest_rr_fields = node_main.unavailable_rr_fields()
        node_main.latest_rr_updated_at = None
        node_main.rr_refresh_interval_sec = 300.0
        node_main.rr_window_seconds = 60.0

    @staticmethod
    def result(*, usable, quality, rate, confidence):
        return SimpleNamespace(
            usable=usable,
            rr_quality=quality,
            resp_rate_bpm=rate,
            rr_confidence=confidence,
        )

    @staticmethod
    def pulse_fields():
        return {
            "heart_rate_bpm": 72.0,
            "spo2_percent": 98.0,
            "finger_detected": True,
            "signal_quality": "GOOD",
        }

    def test_good_and_moderate_results_send_rr_value(self):
        for quality in ("GOOD", "MODERATE"):
            with self.subTest(quality=quality):
                fields = node_main.rr_packet_fields(
                    self.result(
                        usable=True,
                        quality=quality,
                        rate=9.7,
                        confidence=0.949,
                    )
                )
                self.assertEqual(fields["resp_rate_bpm"], 9.7)
                self.assertEqual(fields["rr_source"], "PPG")
                self.assertEqual(fields["rr_confidence"], 0.949)
                self.assertEqual(fields["rr_quality"], quality)

    def test_low_confidence_and_unusable_results_withhold_rr_value(self):
        cases = (
            (False, "LOW_CONFIDENCE"),
            (False, "BAD_SIGNAL"),
            (False, "BAD_TIMING"),
            (False, "UNAVAILABLE"),
            (False, "MODERATE"),
        )
        for usable, quality in cases:
            with self.subTest(usable=usable, quality=quality):
                fields = node_main.rr_packet_fields(
                    self.result(
                        usable=usable,
                        quality=quality,
                        rate=14.2,
                        confidence=0.233,
                    )
                )
                self.assertIsNone(fields["resp_rate_bpm"])
                self.assertEqual(fields["rr_source"], "PPG")
                self.assertEqual(fields["rr_confidence"], 0.233)
                self.assertEqual(fields["rr_quality"], quality)

    def test_first_packet_before_rr_refresh_sends_unavailable(self):
        packet = node_main.build_packet(
            temp={"ambient_temp_c": 22.0, "object_temp_c": 36.8},
            motion=node_main.empty_motion_fields(),
            pulse=self.pulse_fields(),
            packet_sequence=1,
            now_utc=datetime(2026, 7, 1, 20, 0, tzinfo=timezone.utc),
        )

        self.assertIsNone(packet["resp_rate_bpm"])
        self.assertEqual(packet["rr_source"], "PPG")
        self.assertEqual(packet["rr_confidence"], 0.0)
        self.assertEqual(packet["rr_quality"], "UNAVAILABLE")

    def test_packet_preserves_required_metadata_and_temperature_sanitization(self):
        now = datetime(2026, 7, 1, 20, 15, 30, 123456, tzinfo=timezone.utc)
        rr_fields = {
            "resp_rate_bpm": 9.7,
            "rr_source": "PPG",
            "rr_confidence": 0.949,
            "rr_quality": "GOOD",
        }

        packet = node_main.build_packet(
            temp={"ambient_temp_c": 61.0, "object_temp_c": 29.9},
            motion=node_main.empty_motion_fields(),
            pulse=self.pulse_fields(),
            packet_sequence=42,
            rr_fields=rr_fields,
            now_utc=now,
        )

        self.assertEqual(packet["device_type"], "RASPBERRY_PI")
        self.assertEqual(packet["firmware_version"], "0.2.0")
        self.assertEqual(packet["packet_sequence"], 42)
        self.assertEqual(packet["timestamp"], "2026-07-01T20:15:30.123456+00:00")
        self.assertEqual(
            packet["device_send_time"],
            "2026-07-01T20:15:30.123456+00:00",
        )
        self.assertIsNone(packet["ambient_temp_c"])
        self.assertIsNone(packet["object_temp_c"])
        self.assertEqual(packet["resp_rate_bpm"], 9.7)
        self.assertEqual(packet["status"], "GREEN")

        next_packet = node_main.build_packet(
            temp={"ambient_temp_c": 22.0, "object_temp_c": 36.8},
            motion=node_main.empty_motion_fields(),
            pulse=self.pulse_fields(),
            packet_sequence=43,
            rr_fields=rr_fields,
            now_utc=now,
        )
        self.assertEqual(next_packet["packet_sequence"], 43)
        self.assertEqual(next_packet["object_temp_c"], 36.8)

    def test_rr_cache_refresh_interval(self):
        self.assertTrue(node_main.rr_refresh_due(100.0))
        node_main.latest_rr_updated_at = 100.0
        self.assertFalse(node_main.rr_refresh_due(399.999))
        self.assertTrue(node_main.rr_refresh_due(400.0))

    def test_rr_refresh_completes_before_legacy_estimator_starts(self):
        events = []

        class FakePulseSensor:
            def estimate_heart_rate(self, duration_sec):
                events.append(("legacy", duration_sec))
                return self_pulse_fields()

        def self_pulse_fields():
            return self.pulse_fields()

        def fake_refresh(sensor):
            events.append(("rr_refresh_complete", sensor))
            return node_main.unavailable_rr_fields()

        pulse_sensor = FakePulseSensor()
        with patch.object(node_main, "rr_refresh_due", return_value=True), patch.object(
            node_main,
            "refresh_rr_cache",
            side_effect=fake_refresh,
        ):
            pulse = node_main.estimate_legacy_pulse_after_rr_refresh(pulse_sensor)

        self.assertEqual(events[0], ("rr_refresh_complete", pulse_sensor))
        self.assertEqual(events[1], ("legacy", 10))
        self.assertEqual(pulse["heart_rate_bpm"], 72.0)

    def test_refresh_cache_uses_same_gating_and_updates_timestamp(self):
        result = self.result(
            usable=True,
            quality="GOOD",
            rate=11.4,
            confidence=0.82,
        )
        analysis = SimpleNamespace(result=result)
        stats = {
            "approximate_sample_rate_hz": 25.0,
            "large_timing_gaps": 0,
            "fifo_overflow_events": 0,
        }

        with patch.object(
            node_main,
            "collect_ppg_window",
            return_value=([], stats),
        ), patch.object(
            node_main,
            "samples_to_ppg_data",
            return_value=SimpleNamespace(),
        ), patch.object(
            node_main,
            "estimate_respiratory_rate",
            return_value=analysis,
        ):
            fields = node_main.refresh_rr_cache(
                object(),
                monotonic_fn=lambda: 1234.5,
            )

        self.assertEqual(fields["resp_rate_bpm"], 11.4)
        self.assertEqual(fields["rr_quality"], "GOOD")
        self.assertEqual(node_main.latest_rr_updated_at, 1234.5)


if __name__ == "__main__":
    unittest.main()
