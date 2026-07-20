import time
from smbus2 import SMBus


class MPU6050Sensor:
    def __init__(self, bus_num=1, address=0x68):
        self.bus = SMBus(bus_num)
        self.address = address

        # Wake up MPU6050
        self.bus.write_byte_data(self.address, 0x6B, 0x00)
        time.sleep(0.1)

    def _read_word(self, register):
        high = self.bus.read_byte_data(self.address, register)
        low = self.bus.read_byte_data(self.address, register + 1)
        value = (high << 8) + low

        if value >= 0x8000:
            value = -((65535 - value) + 1)

        return value

    def read(self):
        accel_x = self._read_word(0x3B) / 16384.0
        accel_y = self._read_word(0x3D) / 16384.0
        accel_z = self._read_word(0x3F) / 16384.0

        temp_raw = self._read_word(0x41)
        temp_c = (temp_raw / 340.0) + 36.53

        gyro_x = self._read_word(0x43) / 131.0
        gyro_y = self._read_word(0x45) / 131.0
        gyro_z = self._read_word(0x47) / 131.0

        return {
            "accel_x_g": round(accel_x, 3),
            "accel_y_g": round(accel_y, 3),
            "accel_z_g": round(accel_z, 3),
            "gyro_x_deg_s": round(gyro_x, 3),
            "gyro_y_deg_s": round(gyro_y, 3),
            "gyro_z_deg_s": round(gyro_z, 3),
            "mpu_temp_c": round(temp_c, 2),
        }

    def close(self):
        self.bus.close()


if __name__ == "__main__":
    mpu = MPU6050Sensor()

    while True:
        print(mpu.read())
        time.sleep(1)
