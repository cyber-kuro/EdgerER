from datetime import datetime, timezone
import math
import time
from typing import Any, Dict, Optional

import requests

from acquisition.ppg_stream import PPGStream
from config import (
    DEVICE_ID,
    DEVICE_TYPE,
    FIRMWARE_VERSION,
    PATIENT_ID,
    PPG_HEALTH_LOG_INTERVAL_SEC,
    PPG_RING_BUFFER_SECONDS,
    SERVER_URL,
)
from processing.ppg_pipeline import (
    SharedPPGPipeline,
    unavailable_rr_fields,
)
from utils.risk_engine import evaluate_risk


def close_safely(resource: Any, name: str) -> None:
    if resource is None or not hasattr(resource, "close"):
        return
    try:
        resource.close()
    except Exception as exc:
        print(f"Failed to close {name}: {exc}")


def sanitize_numeric(
    value: Any,
    minimum: float,
    maximum: float,
) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < minimum or value > maximum:
        return None
    return value


def sanitize_temperature_fields(reading: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    reading = reading or {}
    return {
        "ambient_temp_c": sanitize_numeric(
            reading.get("ambient_temp_c"),
            0,
            60,
        ),
        "object_temp_c": sanitize_numeric(
            reading.get("object_temp_c"),
            30,
            45,
        ),
    }


def empty_motion_fields() -> Dict[str, Any]:
    return {
        "accel_x_g": None,
        "accel_y_g": None,
        "accel_z_g": None,
        "gyro_x_deg_s": None,
        "gyro_y_deg_s": None,
        "gyro_z_deg_s": None,
        "mpu_temp_c": None,
    }


def utc_iso_string(now: Optional[datetime] = None) -> str:
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    return now.isoformat()


def build_packet(
    temp: Optional[Dict[str, Any]],
    motion: Optional[Dict[str, Any]],
    pulse: Dict[str, Any],
    packet_sequence: int,
    rr_fields: Optional[Dict[str, Any]] = None,
    now_utc: Optional[datetime] = None,
) -> Dict[str, Any]:
    send_time = utc_iso_string(now_utc)
    packet = {
        "patient_id": PATIENT_ID,
        "device_id": DEVICE_ID,
        "device_type": DEVICE_TYPE,
        "firmware_version": FIRMWARE_VERSION,
        "packet_sequence": int(packet_sequence),
        "timestamp": send_time,
        "device_send_time": send_time,
        **sanitize_temperature_fields(temp),
        **(motion or empty_motion_fields()),
        **pulse,
        **(rr_fields or unavailable_rr_fields()),
    }

    # RR remains research-only on the Pi. The existing risk engine and local
    # alert behavior do not consume respiratory-rate fields.
    packet.update(evaluate_risk(packet))
    return packet


def _read_temperature(temp_sensor: Any) -> Dict[str, Any]:
    try:
        return temp_sensor.read()
    except Exception as exc:
        print(f"Recoverable MLX90614 read error; temperature withheld: {exc}")
        return {}


def _poll_motion_preserving_packet_behavior(motion_sensor: Any) -> Dict[str, Any]:
    try:
        # Preserve the current packet contract: poll the MPU6050, but keep its
        # packet fields null until the existing motion workflow is completed.
        motion_sensor.read()
    except Exception as exc:
        print(f"Recoverable MPU6050 read error; motion withheld: {exc}")
    return empty_motion_fields()


def _print_stream_health(stream: PPGStream) -> None:
    stats = stream.get_stats()
    print(
        "PPG acquisition health: "
        f"rate={stats['approximate_sample_rate_hz']:.2f} Hz, "
        f"buffer={stats['buffer_sample_count']}/{stats['buffer_capacity']}, "
        f"gaps={stats['large_timing_gaps']}, "
        f"FIFO overflows={stats['fifo_overflow_events']}, "
        f"read errors={stats['read_errors']}."
    )
    if stats["last_error"]:
        print(f"  Last PPG acquisition error: {stats['last_error']}")


def main() -> None:
    # Hardware imports stay inside main so all processing and packet tests run
    # on development machines without Raspberry Pi I2C/GPIO packages.
    from alerts.local_alerts import LocalAlerts
    from sensors.max30102 import MAX30102Sensor
    from sensors.mlx90614 import MLX90614Sensor
    from sensors.mpu6050_sensor import MPU6050Sensor

    temp_sensor = None
    motion_sensor = None
    pulse_sensor = None
    ppg_stream = None
    alerts = None
    packet_sequence = 0

    try:
        temp_sensor = MLX90614Sensor()
        motion_sensor = MPU6050Sensor()
        pulse_sensor = MAX30102Sensor()
        alerts = LocalAlerts()

        # PPGStream is the only MAX30102 FIFO reader during normal operation.
        # It resets the FIFO once before its acquisition thread starts.
        ppg_stream = PPGStream(
            sensor=pulse_sensor,
            ring_buffer_seconds=PPG_RING_BUFFER_SECONDS,
        )
        pipeline = SharedPPGPipeline(ppg_stream, logger=print)
        ppg_stream.start()
        print(
            "Continuous PPG stream started; HR, SpO2, quality, and research RR "
            "will share buffered RED/IR snapshots."
        )
        print("Initial packets may report PPG measurements as unavailable during warm-up.")
        print("EdgeER patient node started")

        next_health_log = time.monotonic() + PPG_HEALTH_LOG_INTERVAL_SEC

        while True:
            now_monotonic = time.monotonic()
            if not ppg_stream.is_running():
                print("PPG acquisition thread stopped unexpectedly; restarting it.")
                ppg_stream.start()

            try:
                pipeline.refresh(now_monotonic)
            except Exception as exc:
                # Individual estimators are already isolated inside the
                # pipeline; this protects the node from an unexpected cache bug.
                print(f"Recoverable shared PPG pipeline error: {exc}")

            pulse = pipeline.get_pulse_packet_fields(now_monotonic)
            rr_fields = pipeline.get_rr_packet_fields(now_monotonic)
            temp = _read_temperature(temp_sensor)
            motion = _poll_motion_preserving_packet_behavior(motion_sensor)

            packet_sequence += 1
            packet = build_packet(
                temp=temp,
                motion=motion,
                pulse=pulse,
                packet_sequence=packet_sequence,
                rr_fields=rr_fields,
            )

            alerts.set_status(packet["status"])

            print(packet)
            print("-" * 80)

            try:
                requests.post(SERVER_URL, json=packet, timeout=2)
                print("Sent to server")
            except Exception as exc:
                print("Server send failed:", exc)

            if now_monotonic >= next_health_log:
                _print_stream_health(ppg_stream)
                next_health_log = now_monotonic + PPG_HEALTH_LOG_INTERVAL_SEC

            print("-" * 80)
            time.sleep(2)

    except KeyboardInterrupt:
        print("EdgeER patient node stopping")

    finally:
        close_safely(alerts, "local alerts")
        # The externally supplied MAX30102 is closed separately, exactly once,
        # after the acquisition thread has fully stopped.
        close_safely(ppg_stream, "PPG stream")
        close_safely(pulse_sensor, "MAX30102")
        close_safely(motion_sensor, "MPU6050")
        close_safely(temp_sensor, "MLX90614")


if __name__ == "__main__":
    main()
