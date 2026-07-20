import time
from smbus2 import SMBus

from processing.ppg_quality import PPGData


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
        """Compatibility wrapper around the buffer-based pulse processor.

        New live code must use PPGStream and process its snapshots instead.
        This method temporarily retains the original blocking acquisition
        behavior for standalone scripts and calls the shared calculation.
        """
        red_values = []
        ir_values = []
        monotonic_times = []

        warmup_start = time.time()
        while time.time() - warmup_start < 1.0:
            self.read_fifo()
            time.sleep(sample_delay)

        start = time.time()
        while time.time() - start < duration_sec:
            red, ir = self.read_fifo()
            red_values.append(red)
            ir_values.append(ir)
            monotonic_times.append(time.monotonic())
            time.sleep(sample_delay)

        # Import lazily so the sensor driver remains importable independently
        # and tests can verify that this wrapper delegates the calculation.
        from processing.hr_spo2 import estimate_hr_spo2_from_samples

        elapsed_times = (
            [value - monotonic_times[0] for value in monotonic_times]
            if monotonic_times
            else []
        )
        data = PPGData(
            elapsed_time_sec=elapsed_times,
            monotonic_time_sec=monotonic_times,
            red_raw=red_values,
            ir_raw=ir_values,
        )
        return estimate_hr_spo2_from_samples(
            data,
            sample_rate_hz=1.0 / sample_delay,
        )


if __name__ == "__main__":
    sensor = MAX30102Sensor()

    while True:
        print("Place finger still...")
        print(sensor.estimate_heart_rate())
        time.sleep(2)
