from datetime import datetime, timezone
import inspect
import unittest

import config
import main as node_main


class MainSharedPPGIntegrationTests(unittest.TestCase):
    @staticmethod
    def pulse_fields():
        return {
            "heart_rate_bpm": 72.0,
            "spo2_percent": 98.0,
            "finger_detected": True,
            "signal_quality": "GOOD",
        }

    def test_packet_preserves_metadata_and_temperature_sanitization(self):
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

        self.assertEqual(packet["patient_id"], config.PATIENT_ID)
        self.assertEqual(packet["device_id"], config.DEVICE_ID)
        self.assertEqual(packet["device_type"], "RASPBERRY_PI")
        self.assertEqual(packet["firmware_version"], config.FIRMWARE_VERSION)
        self.assertEqual(packet["packet_sequence"], 42)
        self.assertEqual(packet["timestamp"], "2026-07-01T20:15:30.123456+00:00")
        self.assertEqual(packet["device_send_time"], packet["timestamp"])
        self.assertIsNone(packet["ambient_temp_c"])
        self.assertIsNone(packet["object_temp_c"])
        self.assertEqual(packet["resp_rate_bpm"], 9.7)

    def test_first_packet_fields_can_be_unavailable(self):
        packet = node_main.build_packet(
            temp={"ambient_temp_c": 22.0, "object_temp_c": 36.8},
            motion=node_main.empty_motion_fields(),
            pulse={
                "heart_rate_bpm": None,
                "spo2_percent": None,
                "finger_detected": None,
                "signal_quality": "UNSTABLE",
            },
            packet_sequence=1,
        )

        self.assertIsNone(packet["heart_rate_bpm"])
        self.assertIsNone(packet["spo2_percent"])
        self.assertIsNone(packet["resp_rate_bpm"])
        self.assertEqual(packet["rr_quality"], "UNAVAILABLE")

    def test_main_uses_one_stream_and_no_legacy_direct_reader(self):
        source = inspect.getsource(node_main.main)

        self.assertEqual(source.count("MAX30102Sensor()"), 1)
        self.assertEqual(source.count("PPGStream("), 1)
        self.assertNotIn("estimate_heart_rate", source)
        self.assertNotIn("reset_fifo", source)


if __name__ == "__main__":
    unittest.main()
