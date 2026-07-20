import time
import unittest

from acquisition.ppg_stream import PPGStream


class FakeMAX30102:
    def __init__(self, read_delay_sec=0.0):
        self.read_delay_sec = read_delay_sec
        self.sample_number = 0
        self.reset_count = 0

    def reset_fifo(self):
        self.reset_count += 1
        self.sample_number = 0

    def read_raw_sample(self):
        if self.read_delay_sec:
            time.sleep(self.read_delay_sec)
        self.sample_number += 1
        return 100_000 + self.sample_number, 120_000 + self.sample_number

    def get_fifo_state(self):
        pointer = self.sample_number & 0x1F
        return {
            "write_pointer": pointer,
            "read_pointer": pointer,
            "overflow_counter": 0,
            "unread_samples": 0,
        }


def wait_for_samples(stream, minimum, timeout_sec=1.0):
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if stream.get_stats()["sample_count_total"] >= minimum:
            return
        time.sleep(0.002)
    raise AssertionError(f"stream did not collect {minimum} samples")


class PPGStreamTests(unittest.TestCase):
    def test_collects_timestamped_raw_samples_and_stops(self):
        sensor = FakeMAX30102()
        stream = PPGStream(
            sensor=sensor,
            sample_interval_sec=0.005,
            ring_buffer_seconds=0.1,
            fifo_poll_interval_sec=0.01,
        )

        stream.start()
        wait_for_samples(stream, 6)
        self.assertTrue(stream.is_running())
        stream.stop()

        samples = stream.get_recent_window(0.1)
        stats = stream.get_stats()

        self.assertFalse(stream.is_running())
        self.assertGreaterEqual(len(samples), 6)
        self.assertEqual(sensor.reset_count, 1)
        self.assertGreater(samples[0].monotonic_time_sec, 0)
        self.assertEqual(samples[0].red_raw, 100_001)
        self.assertEqual(samples[0].ir_raw, 120_001)
        self.assertGreater(stats["approximate_sample_rate_hz"], 0)
        self.assertEqual(stats["read_errors"], 0)

    def test_ring_buffer_discards_oldest_samples_when_full(self):
        stream = PPGStream(
            sensor=FakeMAX30102(),
            sample_interval_sec=0.005,
            ring_buffer_seconds=0.025,
        )

        stream.start()
        wait_for_samples(stream, 12)
        stream.stop()

        samples = stream.get_recent_window(1.0)
        stats = stream.get_stats()

        self.assertEqual(stream.buffer_capacity, 5)
        self.assertEqual(len(samples), 5)
        self.assertGreaterEqual(stats["buffer_overwrites"], 7)
        self.assertGreater(samples[0].red_raw, 100_001)

    def test_reports_large_host_timing_gaps(self):
        stream = PPGStream(
            sensor=FakeMAX30102(read_delay_sec=0.02),
            sample_interval_sec=0.005,
            ring_buffer_seconds=0.2,
        )

        stream.start()
        wait_for_samples(stream, 4)
        stream.stop()
        stats = stream.get_stats()

        self.assertGreaterEqual(stats["large_timing_gaps"], 2)
        self.assertGreaterEqual(stats["estimated_dropped_samples"], 2)


if __name__ == "__main__":
    unittest.main()
