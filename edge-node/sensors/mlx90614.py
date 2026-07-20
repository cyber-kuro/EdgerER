import board
import busio
import adafruit_mlx90614


class MLX90614Sensor:
    def __init__(self):
        self.i2c = busio.I2C(board.SCL, board.SDA)
        self.sensor = adafruit_mlx90614.MLX90614(self.i2c)

    def read(self):
        return {
            "ambient_temp_c": round(self.sensor.ambient_temperature, 2),
            "object_temp_c": round(self.sensor.object_temperature, 2),
        }

    def close(self):
        self.i2c.deinit()


if __name__ == "__main__":
    import time

    temp_sensor = MLX90614Sensor()

    while True:
        print(temp_sensor.read())
        time.sleep(2)
