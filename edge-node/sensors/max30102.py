import time
from smbus2 import SMBus
import numpy as np
from scipy.signal import butter, filtfilt, find_peaks


class MAX30102Sensor:
    ADDRESS = 0x57

    FIFO_WRITE_POINTER = 0x04
    FIFO_OVERFLOW_COUNTER = 0x05
    FIFO_READ_POINTER = 0x06
    FIFO_DATA = 0x07

    def __init__(self, bus_num=1):
        self.bus = SMBus(bus_num)
        self.setup()

    def write_reg(self, reg, value):
        self.bus.write_byte_data(self.ADDRESS, reg, value)

    def read_reg(self, reg):
        return self.bus.read_byte_data(self.ADDRESS, reg)

    def setup(self):
        # MODE_CONFIG reset. Reset also clears the FIFO pointers.
        self.write_reg(0x09, 0x40)
        time.sleep(0.2)

        # FIFO_CONFIG 0x4F:
        # - average four ADC samples into each FIFO sample
        # - FIFO rollover disabled
        # - almost-full threshold set to 0x0F
        self.write_reg(0x08, 0x4F)

        # MODE_CONFIG 0x03: SpO2 mode, with both red and IR channels active.
        self.write_reg(0x09, 0x03)

        # SPO2_CONFIG 0x27: 4096 nA ADC range, 100 samples/s, 411 us
        # pulse width (18-bit ADC resolution). With four-sample averaging,
        # the effective FIFO stream rate is nominally 25 samples/s.
        self.write_reg(0x0A, 0x27)

        # Red and IR LED pulse amplitudes. Keep these values unchanged until
        # optical settings are calibrated and validated on the target probe.
        self.write_reg(0x0C, 0x24)
        self.write_reg(0x0D, 0x24)

    def read_fifo(self):
        data = self.bus.read_i2c_block_data(self.ADDRESS, self.FIFO_DATA, 6)
        red = ((data[0] << 16) | (data[1] << 8) | data[2]) & 0x3FFFF
        ir = ((data[3] << 16) | (data[4] << 8) | data[5]) & 0x3FFFF
        return red, ir

    def read_raw_sample(self):
        """Read one unmodified red/IR sample pair from the sensor FIFO."""
        return self.read_fifo()

    def reset_fifo(self):
        """Clear FIFO pointers/counter without changing optical settings."""
        self.write_reg(self.FIFO_WRITE_POINTER, 0)
        self.write_reg(self.FIFO_OVERFLOW_COUNTER, 0)
        self.write_reg(self.FIFO_READ_POINTER, 0)

    def get_fifo_state(self):
        """Return a lightweight snapshot of the MAX30102 FIFO registers."""
        write_pointer = self.read_reg(self.FIFO_WRITE_POINTER) & 0x1F
        overflow_counter = self.read_reg(self.FIFO_OVERFLOW_COUNTER) & 0x1F
        read_pointer = self.read_reg(self.FIFO_READ_POINTER) & 0x1F

        return {
            "write_pointer": write_pointer,
            "read_pointer": read_pointer,
            "overflow_counter": overflow_counter,
            "unread_samples": (write_pointer - read_pointer) & 0x1F,
        }

    def close(self):
        self.bus.close()

    def estimate_heart_rate(self, duration_sec=6, sample_delay=0.04):
        red_values = []
        ir_values = []
        sample_rate = 1.0 / sample_delay

        warmup_start = time.time()
        while time.time() - warmup_start < 1.0:
            self.read_fifo()
            time.sleep(sample_delay)

        start = time.time()
        while time.time() - start < duration_sec:
            red, ir = self.read_fifo()
            red_values.append(red)
            ir_values.append(ir)
            time.sleep(sample_delay)

        red = np.array(red_values, dtype=float)
        ir = np.array(ir_values, dtype=float)

        red_mean = float(np.mean(red))
        ir_mean = float(np.mean(ir))
        ir_std = float(np.std(ir))

        if ir_mean < 50000:
            return {
                "heart_rate_bpm": None,
                "spo2_percent": None,
                "num_peaks": 0,
                "samples": len(ir),
                "ir_mean": round(ir_mean, 2),
                "ir_std": round(ir_std, 2),
                "finger_detected": False,
                "signal_quality": "NO_FINGER",
            }

        if ir_std < 50:
            return {
                "heart_rate_bpm": None,
                "spo2_percent": None,
                "num_peaks": 0,
                "samples": len(ir),
                "ir_mean": round(ir_mean, 2),
                "ir_std": round(ir_std, 2),
                "finger_detected": True,
                "signal_quality": "WEAK_SIGNAL",
            }

        ir_signal = ir - ir_mean
        red_signal = red - red_mean

        kernel = np.ones(5) / 5
        ir_smooth = np.convolve(ir_signal, kernel, mode="same")
        red_smooth = np.convolve(red_signal, kernel, mode="same")

        low = 0.67
        high = 3.0
        nyquist = 0.5 * sample_rate

        b, a = butter(2, [low / nyquist, high / nyquist], btype="band") #type: ignore

        try:
            ir_filtered = filtfilt(b, a, ir_smooth)
            red_filtered = filtfilt(b, a, red_smooth)
        except Exception:
            return {
                "heart_rate_bpm": None,
                "spo2_percent": None,
                "num_peaks": 0,
                "samples": len(ir),
                "ir_mean": round(ir_mean, 2),
                "ir_std": round(ir_std, 2),
                "finger_detected": True,
                "signal_quality": "UNSTABLE",
            }

        peaks, _ = find_peaks(
            ir_filtered,
            distance=8,
            prominence=np.std(ir_filtered) * 0.6,
        )

        if len(peaks) < 4:
            return {
                "heart_rate_bpm": None,
                "spo2_percent": None,
                "num_peaks": int(len(peaks)),
                "samples": len(ir),
                "ir_mean": round(ir_mean, 2),
                "ir_std": round(ir_std, 2),
                "finger_detected": True,
                "signal_quality": "UNSTABLE",
            }

        peak_intervals = np.diff(peaks) / sample_rate
        median_interval = float(np.median(peak_intervals))
        bpm = 60.0 / median_interval
        interval_std = float(np.std(peak_intervals))

        if bpm < 40 or bpm > 180 or interval_std > 0.35:
            return {
                "heart_rate_bpm": None,
                "spo2_percent": None,
                "num_peaks": int(len(peaks)),
                "samples": len(ir),
                "ir_mean": round(ir_mean, 2),
                "ir_std": round(ir_std, 2),
                "finger_detected": True,
                "signal_quality": "UNSTABLE",
            }

        red_ac = float(np.std(red_filtered))
        ir_ac = float(np.std(ir_filtered))

        if red_mean <= 0 or ir_mean <= 0 or ir_ac <= 0:
            spo2 = None
        else:
            ratio = (red_ac / red_mean) / (ir_ac / ir_mean)
            spo2 = 110.0 - 25.0 * ratio
            spo2 = max(70.0, min(100.0, spo2))

        return {
            "heart_rate_bpm": round(bpm, 1),
            "spo2_percent": round(spo2, 1) if spo2 is not None else None,
            "num_peaks": int(len(peaks)),
            "samples": len(ir),
            "ir_mean": round(ir_mean, 2),
            "ir_std": round(ir_std, 2),
            "finger_detected": True,
            "signal_quality": "GOOD",
            "peak_interval_std": round(interval_std, 3),
        }


if __name__ == "__main__":
    sensor = MAX30102Sensor()

    while True:
        print("Place finger still...")
        print(sensor.estimate_heart_rate())
        time.sleep(2)
