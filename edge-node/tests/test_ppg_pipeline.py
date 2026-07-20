import unittest
from types import SimpleNamespace

from acquisition.ppg_stream import PPGSample
from processing.ppg_pipeline import SharedPPGPipeline


def make_samples(duration_sec=12.0, interval_sec=0.04):
    count = int(duration_sec / interval_sec) + 1
    return [
        PPGSample(
            monotonic_time_sec=index * interval_sec,
            red_raw=90_000 + index,
            ir_raw=100_000 + index,
        )
        for index in range(count)
    ]


class FakeStream:
    def __init__(self, samples=None):
        self.samples = list(samples or [])

    def get_recent_window(self, seconds):
        if not self.samples:
            return []
        cutoff = self.samples[-1].monotonic_time_sec - seconds
        return [
            sample
            for sample in self.samples
            if sample.monotonic_time_sec >= cutoff
        ]


def pulse_result(rate=72.0):
    return {
        "heart_rate_bpm": rate,
        "spo2_percent": 98.0,
        "num_peaks": 12,
        "samples": 251,
        "ir_mean": 100_000.0,
        "ir_std": 1_000.0,
        "finger_detected": True,
        "signal_quality": "GOOD",
        "peak_interval_std": 0.03,
        "reasons": [],
    }


def rr_result(quality="GOOD", usable=True, rate=14.0, confidence=0.8):
    return SimpleNamespace(
        rr_quality=quality,
        usable=usable,
        resp_rate_bpm=rate,
        rr_confidence=confidence,
    )


class SharedPPGPipelineTests(unittest.TestCase):
    def pipeline(self, stream, **overrides):
        options = {
            "logger": None,
            "quality_analyzer": lambda data: SimpleNamespace(
                quality_classification="GOOD"
            ),
            "hr_min_duration_sec": 10.0,
            "hr_min_samples": 200,
            "rr_min_duration_sec": 45.0,
            "rr_min_samples": 1_000,
        }
        options.update(overrides)
        return SharedPPGPipeline(stream, **options)

    def test_cache_starts_unavailable(self):
        pipeline = self.pipeline(FakeStream())

        fields = pipeline.get_packet_fields(now_monotonic=0.0)

        self.assertIsNone(fields["heart_rate_bpm"])
        self.assertIsNone(fields["spo2_percent"])
        self.assertIsNone(fields["resp_rate_bpm"])
        self.assertEqual(fields["rr_quality"], "UNAVAILABLE")

    def test_hr_spo2_remain_unavailable_with_insufficient_samples(self):
        called = []
        pipeline = self.pipeline(
            FakeStream(make_samples(duration_sec=2.0)),
            hr_spo2_estimator=lambda samples: called.append(samples),
        )

        pipeline.refresh(now_monotonic=0.0)
        fields = pipeline.get_pulse_packet_fields(now_monotonic=0.0)

        self.assertEqual(called, [])
        self.assertIsNone(fields["heart_rate_bpm"])

    def test_hr_spo2_cache_updates_with_sufficient_samples(self):
        pipeline = self.pipeline(
            FakeStream(make_samples()),
            hr_spo2_estimator=lambda samples: pulse_result(),
        )

        pipeline.refresh(now_monotonic=100.0)
        fields = pipeline.get_pulse_packet_fields(now_monotonic=100.0)

        self.assertEqual(fields["heart_rate_bpm"], 72.0)
        self.assertEqual(fields["spo2_percent"], 98.0)

    def test_quality_cache_tracks_freshness(self):
        quality = SimpleNamespace(quality_classification="GOOD")
        pipeline = self.pipeline(
            FakeStream(make_samples()),
            quality_analyzer=lambda data: quality,
            hr_min_samples=100_000,
            quality_stale_after_sec=5.0,
        )
        pipeline.refresh(now_monotonic=10.0)

        self.assertIs(pipeline.get_quality_result(10.0), quality)
        self.assertIsNone(pipeline.get_quality_result(16.0))

    def test_usable_good_or_moderate_rr_sends_numeric_value(self):
        for quality in ("GOOD", "MODERATE"):
            with self.subTest(quality=quality):
                pipeline = self.pipeline(
                    FakeStream(make_samples(duration_sec=60.0)),
                    hr_min_samples=100_000,
                    rr_estimator=lambda data, q=quality: SimpleNamespace(
                        result=rr_result(quality=q)
                    ),
                )
                pipeline.refresh(now_monotonic=0.0)

                fields = pipeline.get_rr_packet_fields(now_monotonic=0.0)

                self.assertEqual(fields["resp_rate_bpm"], 14.0)
                self.assertEqual(fields["rr_quality"], quality)

    def test_low_confidence_or_unusable_rr_is_withheld(self):
        cases = (("LOW_CONFIDENCE", False), ("GOOD", False))
        for quality, usable in cases:
            with self.subTest(quality=quality, usable=usable):
                pipeline = self.pipeline(
                    FakeStream(make_samples(duration_sec=60.0)),
                    hr_min_samples=100_000,
                    rr_estimator=lambda data, q=quality, u=usable: SimpleNamespace(
                        result=rr_result(quality=q, usable=u)
                    ),
                )
                pipeline.refresh(now_monotonic=0.0)

                fields = pipeline.get_rr_packet_fields(now_monotonic=0.0)

                self.assertIsNone(fields["resp_rate_bpm"])

    def test_stale_hr_spo2_and_rr_are_withheld(self):
        pipeline = self.pipeline(
            FakeStream(make_samples(duration_sec=60.0)),
            hr_spo2_estimator=lambda samples: pulse_result(),
            rr_estimator=lambda data: SimpleNamespace(result=rr_result()),
            hr_stale_after_sec=5.0,
            rr_stale_after_sec=10.0,
        )
        pipeline.refresh(now_monotonic=100.0)

        hr_fields = pipeline.get_pulse_packet_fields(now_monotonic=106.0)
        rr_fields = pipeline.get_rr_packet_fields(now_monotonic=111.0)

        self.assertIsNone(hr_fields["heart_rate_bpm"])
        self.assertIsNone(hr_fields["spo2_percent"])
        self.assertIsNone(rr_fields["resp_rate_bpm"])
        self.assertEqual(rr_fields["rr_quality"], "UNAVAILABLE")

    def test_recoverable_estimator_exception_preserves_cached_state(self):
        calls = 0

        def estimator(samples):
            nonlocal calls
            calls += 1
            if calls == 1:
                return pulse_result(rate=70.0)
            raise RuntimeError("temporary processing failure")

        pipeline = self.pipeline(
            FakeStream(make_samples()),
            hr_spo2_estimator=estimator,
            hr_refresh_interval_sec=5.0,
            hr_stale_after_sec=20.0,
        )
        pipeline.refresh(now_monotonic=100.0)
        original = pipeline.state.hr_spo2_result
        pipeline.refresh(now_monotonic=105.0)

        self.assertIs(pipeline.state.hr_spo2_result, original)
        self.assertEqual(
            pipeline.get_pulse_packet_fields(105.0)["heart_rate_bpm"],
            70.0,
        )
        self.assertIn("temporary processing failure", pipeline.state.hr_spo2_error)


if __name__ == "__main__":
    unittest.main()
