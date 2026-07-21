import unittest

from utils.risk_engine import evaluate_risk


def packet(**updates):
    value = {
        "heart_rate_bpm": 72.0,
        "spo2_percent": 98.0,
        "object_temp_c": 36.8,
        "finger_detected": True,
        "signal_quality": "GOOD",
        "resp_rate_bpm": 16.0,
        "rr_quality": "GOOD",
        "rr_confidence": 0.8,
    }
    value.update(updates)
    return value


class RespiratoryRateLocalAlertTests(unittest.TestCase):
    def test_reliable_normal_rr_does_not_escalate(self):
        result = evaluate_risk(packet())
        self.assertEqual(result["status"], "GREEN")
        self.assertEqual(result["reasons"], [])

    def test_reliable_out_of_range_rr_produces_at_most_yellow(self):
        for rr in (6.0, 34.0):
            with self.subTest(rr=rr):
                result = evaluate_risk(packet(resp_rate_bpm=rr))
                self.assertEqual(result["status"], "YELLOW")
                self.assertIn("demonstration RR outside configured range", result["reasons"])

    def test_low_confidence_null_and_stale_rr_are_not_used(self):
        cases = (
            ({"resp_rate_bpm": 34.0, "rr_quality": "LOW_CONFIDENCE", "rr_confidence": 0.2}, True),
            ({"resp_rate_bpm": None, "rr_quality": "UNAVAILABLE", "rr_confidence": 0.0}, True),
            ({"resp_rate_bpm": 34.0, "rr_quality": "GOOD", "rr_confidence": 0.8}, False),
        )
        for updates, fresh in cases:
            with self.subTest(updates=updates, fresh=fresh):
                result = evaluate_risk(packet(**updates), rr_fresh=fresh)
                self.assertEqual(result["status"], "GREEN")

    def test_rr_alone_never_produces_a_severe_alert(self):
        for rr in (4.0, 60.0):
            result = evaluate_risk(packet(resp_rate_bpm=rr))
            self.assertNotIn(result["status"], {"ORANGE", "RED", "CRASHING"})

    def test_poor_rr_does_not_override_existing_hr_or_spo2_alert(self):
        result = evaluate_risk(
            packet(
                heart_rate_bpm=140.0,
                spo2_percent=88.0,
                resp_rate_bpm=None,
                rr_quality="BAD_SIGNAL",
                rr_confidence=0.0,
            )
        )
        self.assertEqual(result["status"], "RED")
        self.assertIn("critical oxygen level", result["reasons"])
        self.assertIn("critical heart rate", result["reasons"])

    def test_existing_hr_spo2_and_temperature_boundaries_are_unchanged(self):
        cases = (
            ({"spo2_percent": 93.0}, "YELLOW"),
            ({"spo2_percent": 89.0}, "RED"),
            ({"heart_rate_bpm": 111.0}, "YELLOW"),
            ({"heart_rate_bpm": 131.0}, "RED"),
            ({"object_temp_c": 38.0}, "YELLOW"),
            ({"object_temp_c": 39.1}, "RED"),
        )
        for updates, expected in cases:
            with self.subTest(updates=updates):
                result = evaluate_risk(packet(**updates))
                self.assertEqual(result["status"], expected)


if __name__ == "__main__":
    unittest.main()
