import importlib
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from processing.hr_spo2 import estimate_hr_spo2_from_samples
from processing.ppg_quality import PPGData


def synthetic_pulse_data(duration_sec=12.0, sample_interval_sec=0.04):
    times = np.arange(0.0, duration_sec, sample_interval_sec)
    pulse_hz = 1.25
    ir = 100_000.0 + 2_500.0 * np.sin(2.0 * np.pi * pulse_hz * times)
    red = 90_000.0 + 1_700.0 * np.sin(
        2.0 * np.pi * pulse_hz * times + 0.12
    )
    return PPGData(
        elapsed_time_sec=times.tolist(),
        monotonic_time_sec=(10_000.0 + times).tolist(),
        red_raw=red.tolist(),
        ir_raw=ir.tolist(),
    )


class HRSpO2ProcessingTests(unittest.TestCase):
    def test_pure_estimator_uses_buffered_data(self):
        result = estimate_hr_spo2_from_samples(synthetic_pulse_data())

        self.assertEqual(result["signal_quality"], "GOOD")
        self.assertTrue(result["finger_detected"])
        self.assertAlmostEqual(result["heart_rate_bpm"], 75.0, delta=1.0)
        self.assertIsNotNone(result["spo2_percent"])
        self.assertEqual(result["samples"], 300)

    def test_pure_estimator_accepts_timestamped_sample_objects(self):
        data = synthetic_pulse_data()
        samples = [
            SimpleNamespace(
                monotonic_time_sec=monotonic,
                red_raw=red,
                ir_raw=ir,
            )
            for monotonic, red, ir in zip(
                data.monotonic_time_sec,
                data.red_raw,
                data.ir_raw,
            )
        ]

        result = estimate_hr_spo2_from_samples(samples)

        self.assertAlmostEqual(result["heart_rate_bpm"], 75.0, delta=1.0)

    def test_legacy_sensor_wrapper_delegates_shared_calculation(self):
        fake_smbus_module = types.ModuleType("smbus2")
        fake_smbus_module.SMBus = object
        with patch.dict(sys.modules, {"smbus2": fake_smbus_module}):
            sensor_module = importlib.import_module("sensors.max30102")

        sensor = sensor_module.MAX30102Sensor.__new__(
            sensor_module.MAX30102Sensor
        )
        sensor.read_fifo = lambda: (90_000, 100_000)
        delegated_result = {"heart_rate_bpm": 72.0}

        with patch.object(
            sensor_module.time,
            "time",
            side_effect=[0.0, 1.1, 2.0, 2.2],
        ), patch(
            "processing.hr_spo2.estimate_hr_spo2_from_samples",
            return_value=delegated_result,
        ) as shared_estimator:
            result = sensor.estimate_heart_rate(
                duration_sec=0.1,
                sample_delay=0.04,
            )

        self.assertIs(result, delegated_result)
        shared_estimator.assert_called_once()
        self.assertIsInstance(shared_estimator.call_args.args[0], PPGData)
        self.assertEqual(shared_estimator.call_args.kwargs["sample_rate_hz"], 25.0)


if __name__ == "__main__":
    unittest.main()
