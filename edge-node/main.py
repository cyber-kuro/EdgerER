from datetime import datetime, timezone
import math
import time

import requests

from acquisition.ppg_stream import PPGStream
from config import (
    DEVICE_ID,
    DEVICE_TYPE,
    FIRMWARE_VERSION,
    PATIENT_ID,
    PPG_RING_BUFFER_SECONDS,
    PPG_SAMPLE_INTERVAL_SEC,
    SERVER_URL,
)
from processing.ppg_quality import PPGData
from processing.respiratory_rate import estimate_respiratory_rate
from utils.risk_engine import evaluate_risk


USABLE_RR_QUALITIES = {"GOOD", "MODERATE"}

latest_rr_fields = {
    "resp_rate_bpm": None,
    "rr_source": "PPG",
    "rr_confidence": 0.0,
    "rr_quality": "UNAVAILABLE",
}
latest_rr_updated_at = None
rr_refresh_interval_sec = 300.0
rr_window_seconds = 60.0


def close_safely(resource, name):
    if resource is None or not hasattr(resource, "close"):
        return
    try:
        resource.close()
    except Exception as exc:
        print(f"Failed to close {name}: {exc}")


def unavailable_rr_fields():
    return {
        "resp_rate_bpm": None,
        "rr_source": "PPG",
        "rr_confidence": 0.0,
        "rr_quality": "UNAVAILABLE",
    }


def rr_packet_fields(result):
    send_value = (
        result.usable
        and result.rr_quality in USABLE_RR_QUALITIES
        and result.resp_rate_bpm is not None
    )
    return {
        "resp_rate_bpm": result.resp_rate_bpm if send_value else None,
        "rr_source": "PPG",
        "rr_confidence": float(result.rr_confidence),
        "rr_quality": result.rr_quality,
    }


def samples_to_ppg_data(samples):
    if not samples:
        return PPGData([], [], [], [])
    first_time = samples[0].monotonic_time_sec
    return PPGData(
        elapsed_time_sec=[
            sample.monotonic_time_sec - first_time for sample in samples
        ],
        monotonic_time_sec=[sample.monotonic_time_sec for sample in samples],
        red_raw=[sample.red_raw for sample in samples],
        ir_raw=[sample.ir_raw for sample in samples],
    )


def collect_ppg_window(
    pulse_sensor,
    duration_sec,
    *,
    stream_factory=PPGStream,
    monotonic_fn=time.monotonic,
    sleep_fn=time.sleep,
):
    if not 45.0 <= duration_sec <= 60.0:
        raise ValueError("RR window must be between 45 and 60 seconds")

    stream = stream_factory(
        sensor=pulse_sensor,
        ring_buffer_seconds=max(
            PPG_RING_BUFFER_SECONDS,
            duration_sec + 5.0,
        ),
    )
    try:
        try:
            stream.start()
            print(f"Collecting {duration_sec:g} seconds of PPG for research RR...")
            started = monotonic_fn()
            deadline = started + duration_sec + PPG_SAMPLE_INTERVAL_SEC * 3.0
            next_progress = 10.0
            while monotonic_fn() < deadline:
                elapsed = monotonic_fn() - started
                if elapsed >= next_progress:
                    print(
                        f"  RR PPG capture: "
                        f"{min(elapsed, duration_sec):.0f}/{duration_sec:g} s"
                    )
                    next_progress += 10.0
                sleep_fn(min(0.25, max(0.0, deadline - monotonic_fn())))
        finally:
            # The acquisition thread must be fully joined before the legacy
            # HR/SpO2 estimator touches the same MAX30102 FIFO.
            stream.stop()

        samples = stream.get_recent_window(duration_sec + 1.0)
        stats = stream.get_stats()
        return samples, stats
    finally:
        stream.close()


def rr_refresh_due(now_monotonic=None):
    if latest_rr_updated_at is None:
        return True
    if now_monotonic is None:
        now_monotonic = time.monotonic()
    return now_monotonic - latest_rr_updated_at >= rr_refresh_interval_sec


def print_rr_cache_update(result, rr_fields):
    estimate = (
        f"{result.resp_rate_bpm:.1f} breaths/min"
        if result.resp_rate_bpm is not None
        else "unavailable"
    )
    disposition = (
        "sent"
        if rr_fields["resp_rate_bpm"] is not None
        else "withheld (null)"
    )
    print("Research RR cache refreshed")
    print(f"  Estimate: {estimate}")
    print(f"  Confidence: {result.rr_confidence:.3f}")
    print(f"  Quality: {result.rr_quality}")
    print(f"  Packet RR value: {disposition}")


def refresh_rr_cache(pulse_sensor, monotonic_fn=time.monotonic):
    global latest_rr_fields, latest_rr_updated_at

    try:
        samples, stats = collect_ppg_window(pulse_sensor, rr_window_seconds)
        print(
            f"Captured {len(samples)} RR samples at approximately "
            f"{stats.get('approximate_sample_rate_hz', 0.0):.2f} Hz; "
            f"timing gaps={stats.get('large_timing_gaps', 0)}, "
            f"FIFO overflows={stats.get('fifo_overflow_events', 0)}."
        )
        analysis = estimate_respiratory_rate(samples_to_ppg_data(samples))
        latest_rr_fields = rr_packet_fields(analysis.result)
        print_rr_cache_update(analysis.result, latest_rr_fields)
    except Exception as exc:
        latest_rr_fields = unavailable_rr_fields()
        print(f"Research RR refresh failed; RR withheld: {exc}")

    latest_rr_updated_at = monotonic_fn()
    return dict(latest_rr_fields)


def get_cached_rr_fields():
    return dict(latest_rr_fields)


def estimate_legacy_pulse_after_rr_refresh(pulse_sensor, now_monotonic=None):
    if rr_refresh_due(now_monotonic):
        refresh_rr_cache(pulse_sensor)

    # refresh_rr_cache returns only after PPGStream.stop() has joined its
    # acquisition thread. This remains the unchanged legacy estimator call.
    return pulse_sensor.estimate_heart_rate(duration_sec=10)


def sanitize_numeric(value, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < minimum or value > maximum:
        return None
    return value


def sanitize_temperature_fields(reading):
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


def empty_motion_fields():
    return {
        "accel_x_g": None,
        "accel_y_g": None,
        "accel_z_g": None,
        "gyro_x_deg_s": None,
        "gyro_y_deg_s": None,
        "gyro_z_deg_s": None,
        "mpu_temp_c": None,
    }


def utc_iso_string(now=None):
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    return now.isoformat()


def build_packet(
    temp,
    motion,
    pulse,
    packet_sequence,
    rr_fields=None,
    now_utc=None,
):
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
        **(rr_fields or get_cached_rr_fields()),
    }

    # evaluate_risk does not consume RR. Local alerts continue to use only
    # this unchanged risk result, never the research RR fields.
    packet.update(evaluate_risk(packet))
    return packet


def main():
    # Hardware imports stay inside main so cache/packet tests run off-Pi.
    from alerts.local_alerts import LocalAlerts
    from sensors.max30102 import MAX30102Sensor
    from sensors.mlx90614 import MLX90614Sensor
    from sensors.mpu6050_sensor import MPU6050Sensor

    temp_sensor = None
    motion_sensor = None
    pulse_sensor = None
    alerts = None
    packet_sequence = 0

    try:
        temp_sensor = MLX90614Sensor()
        motion_sensor = MPU6050Sensor()
        pulse_sensor = MAX30102Sensor()
        alerts = LocalAlerts()

        print("EdgeER patient node started")

        while True:
            pulse = estimate_legacy_pulse_after_rr_refresh(pulse_sensor)
            temp = temp_sensor.read()

            # Preserve the prototype's existing motion-packet behavior.
            motion_sensor.read()
            motion = empty_motion_fields()

            packet_sequence += 1
            packet = build_packet(
                temp=temp,
                motion=motion,
                pulse=pulse,
                packet_sequence=packet_sequence,
            )

            alerts.set_status(packet["status"])

            print(packet)
            print("-" * 80)

            try:
                requests.post(SERVER_URL, json=packet, timeout=2)
                print("Sent to server")
            except Exception as exc:
                print("Server send failed:", exc)

            print("-" * 80)
            time.sleep(2)

    except KeyboardInterrupt:
        print("EdgeER patient node stopping")

    finally:
        close_safely(alerts, "local alerts")
        close_safely(pulse_sensor, "MAX30102")
        close_safely(motion_sensor, "MPU6050")
        close_safely(temp_sensor, "MLX90614")


if __name__ == "__main__":
    main()
