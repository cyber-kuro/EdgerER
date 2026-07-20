import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from acquisition.ppg_stream import PPGStream  # noqa: E402
from config import (  # noqa: E402
    DEVICE_ID,
    LOG_DIR,
    PATIENT_ID,
    PPG_RECORD_SECONDS,
    PPG_RING_BUFFER_SECONDS,
    PPG_SAMPLE_INTERVAL_SEC,
    SERVER_URL,
)
from processing.ppg_quality import PPGData, analyze_ppg_quality  # noqa: E402
from processing.respiratory_rate import estimate_respiratory_rate  # noqa: E402
from scripts.analyze_ppg_csv import (  # noqa: E402
    save_plot as save_quality_plot,
    save_summary as save_quality_summary,
)
from scripts.estimate_rr_from_ppg_csv import (  # noqa: E402
    save_diagnostic_plot as save_rr_diagnostic_plot,
    save_result_csv as save_rr_result_csv,
)
from utils.risk_engine import evaluate_risk  # noqa: E402


USABLE_RR_QUALITIES = {"GOOD", "MODERATE"}
PLOT_DIR = PROJECT_ROOT / "plots"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Collect one live PPG window, estimate research-only RR, run the "
            "unchanged legacy HR/SpO2 estimator, and POST one test packet."
        )
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=PPG_RECORD_SECONDS,
        help=f"PPG window from 45 to 60 seconds (default: {PPG_RECORD_SECONDS:g})",
    )
    parser.add_argument(
        "--server-url",
        default=SERVER_URL,
        help=f"one-shot POST destination (default: {SERVER_URL})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="HTTP timeout in seconds (default: 5)",
    )
    parser.add_argument(
        "--no-post",
        action="store_true",
        help="build and print the packet without sending it",
    )
    parser.add_argument(
        "--save-window",
        dest="save_window",
        action="store_true",
        help=(
            "save the captured raw PPG window plus quality/RR diagnostics "
            "(default)"
        ),
    )
    parser.add_argument(
        "--no-save-window",
        dest="save_window",
        action="store_false",
        help="skip saving live PPG debug CSVs and plots",
    )
    parser.set_defaults(save_window=True)
    args = parser.parse_args(argv)
    if not 45.0 <= args.seconds <= 60.0:
        parser.error("--seconds must be between 45 and 60")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    return args


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


def live_rr_artifact_paths(timestamp_utc=None):
    if timestamp_utc is None:
        timestamp_utc = datetime.now(timezone.utc)
    stem = f"live_rr_window_{timestamp_utc.strftime('%Y%m%dT%H%M%SZ')}"
    return {
        "raw_csv_path": (LOG_DIR / f"{stem}.csv").resolve(),
        "quality_summary_path": (
            LOG_DIR / f"{stem}_quality_summary.csv"
        ).resolve(),
        "quality_plot_path": (PLOT_DIR / f"{stem}_quality.png").resolve(),
        "rr_result_path": (LOG_DIR / f"{stem}_rr_result.csv").resolve(),
        "rr_plot_path": (PLOT_DIR / f"{stem}_rr_diagnostic.png").resolve(),
    }


def save_ppg_window_csv(output_path, data):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(
            csv_file,
            fieldnames=[
                "elapsed_time_sec",
                "monotonic_time_sec",
                "red_raw",
                "ir_raw",
            ],
        )
        writer.writeheader()
        for elapsed, monotonic, red, ir in zip(
            data.elapsed_time_sec,
            data.monotonic_time_sec,
            data.red_raw,
            data.ir_raw,
        ):
            writer.writerow(
                {
                    "elapsed_time_sec": elapsed,
                    "monotonic_time_sec": monotonic,
                    "red_raw": red,
                    "ir_raw": ir,
                }
            )


def save_live_rr_debug_artifacts(data, rr_analysis, timestamp_utc=None):
    """Save the exact live PPG window and reusable offline diagnostics."""
    paths = live_rr_artifact_paths(timestamp_utc)
    warnings = []

    try:
        save_ppg_window_csv(paths["raw_csv_path"], data)
    except OSError as exc:
        warnings.append(f"Unable to save raw PPG window CSV: {exc}")
        return paths, warnings

    try:
        quality_result = analyze_ppg_quality(data)
        save_quality_summary(
            paths["quality_summary_path"],
            paths["raw_csv_path"],
            quality_result,
        )
        try:
            save_quality_plot(
                paths["quality_plot_path"],
                paths["raw_csv_path"],
                data,
                quality_result,
            )
        except (OSError, RuntimeError) as exc:
            warnings.append(f"Unable to save PPG quality plot: {exc}")
    except (OSError, RuntimeError, ValueError) as exc:
        warnings.append(f"Unable to save PPG quality summary: {exc}")

    try:
        save_rr_result_csv(
            paths["rr_result_path"],
            paths["raw_csv_path"],
            rr_analysis.result,
        )
    except (OSError, RuntimeError) as exc:
        warnings.append(f"Unable to save RR result CSV: {exc}")

    try:
        save_rr_diagnostic_plot(
            paths["rr_plot_path"],
            paths["raw_csv_path"],
            rr_analysis,
        )
    except (OSError, RuntimeError) as exc:
        warnings.append(f"Unable to save RR diagnostic plot: {exc}")

    return paths, warnings


def print_live_rr_debug_paths(paths, warnings):
    print("Live RR debug artifacts")
    print(f"  Raw PPG CSV path: {paths['raw_csv_path']}")
    print(f"  PPG quality summary path: {paths['quality_summary_path']}")
    print(f"  RR result CSV path: {paths['rr_result_path']}")
    print(f"  RR diagnostic plot path: {paths['rr_plot_path']}")
    if "quality_plot_path" in paths:
        print(f"  PPG quality plot path: {paths['quality_plot_path']}")
    for warning in warnings:
        print(f"  Warning: {warning}")


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


def build_packet(temp, pulse, rr_fields, timestamp=None):
    motion = {
        "accel_x_g": None,
        "accel_y_g": None,
        "accel_z_g": None,
        "gyro_x_deg_s": None,
        "gyro_y_deg_s": None,
        "gyro_z_deg_s": None,
        "mpu_temp_c": None,
    }
    packet = {
        "patient_id": PATIENT_ID,
        "device_id": DEVICE_ID,
        "timestamp": timestamp or time.strftime("%Y-%m-%d %H:%M:%S"),
        **temp,
        **motion,
        **pulse,
        **rr_fields,
    }

    # Preserve the current packet contract for this staged live test. RR is
    # not consumed by evaluate_risk and does not drive local LEDs/buzzer.
    packet.update(evaluate_risk(packet))
    return packet


def collect_ppg_window(pulse_sensor, duration_sec):
    stream = PPGStream(
        sensor=pulse_sensor,
        ring_buffer_seconds=max(PPG_RING_BUFFER_SECONDS, duration_sec + 5.0),
    )
    try:
        stream.start()
        print(f"Collecting {duration_sec:g} seconds of continuous raw PPG...")
        started = time.monotonic()
        deadline = started + duration_sec + PPG_SAMPLE_INTERVAL_SEC * 3.0
        next_progress = 10.0
        while time.monotonic() < deadline:
            elapsed = time.monotonic() - started
            if elapsed >= next_progress:
                print(f"  PPG capture: {min(elapsed, duration_sec):.0f}/{duration_sec:g} s")
                next_progress += 10.0
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
    finally:
        stream.stop()

    samples = stream.get_recent_window(duration_sec + 1.0)
    stats = stream.get_stats()
    stream.close()
    return samples, stats


def print_rr_result(result, rr_diagnostic_plot_path=None):
    sent = (
        result.usable
        and result.rr_quality in USABLE_RR_QUALITIES
        and result.resp_rate_bpm is not None
    )
    estimate = (
        f"{result.resp_rate_bpm:.1f} breaths/min"
        if result.resp_rate_bpm is not None
        else "unavailable"
    )
    print("RR result")
    print(f"  Estimate: {estimate}")
    print(f"  Confidence: {result.rr_confidence:.3f}")
    print(f"  Quality: {result.rr_quality}")
    print(f"  Packet RR value: {'sent' if sent else 'withheld (null)'}")
    if result.reasons:
        print("  Reasons/notes:")
        for reason in result.reasons:
            print(f"  - {reason}")
    if result.rr_quality == "LOW_CONFIDENCE":
        plot_path = rr_diagnostic_plot_path or "not saved (--no-save-window)"
        print(
            "RR withheld because confidence was low. "
            f"Inspect the diagnostic plot at: {plot_path}"
        )


def close_safely(resource, name):
    if resource is None or not hasattr(resource, "close"):
        return
    try:
        resource.close()
    except Exception as exc:
        print(f"Failed to close {name}: {exc}")


def main():
    args = parse_args()
    pulse_sensor = None
    temp_sensor = None

    try:
        # Hardware imports stay inside main so pure packet tests work off-Pi.
        from sensors.max30102 import MAX30102Sensor
        from sensors.mlx90614 import MLX90614Sensor

        pulse_sensor = MAX30102Sensor()
        temp_sensor = MLX90614Sensor()

        samples, stream_stats = collect_ppg_window(pulse_sensor, args.seconds)
        print(
            f"Captured {len(samples)} samples at approximately "
            f"{stream_stats['approximate_sample_rate_hz']:.2f} Hz; "
            f"timing gaps={stream_stats['large_timing_gaps']}, "
            f"FIFO overflows={stream_stats['fifo_overflow_events']}."
        )

        ppg_data = samples_to_ppg_data(samples)
        rr_analysis = estimate_respiratory_rate(ppg_data)
        debug_paths = None
        if args.save_window:
            debug_paths, debug_warnings = save_live_rr_debug_artifacts(
                ppg_data,
                rr_analysis,
            )
            print_live_rr_debug_paths(debug_paths, debug_warnings)
        else:
            print("Live RR debug artifact saving disabled (--no-save-window).")

        rr_fields = rr_packet_fields(rr_analysis.result)
        rr_plot_path = debug_paths["rr_plot_path"] if debug_paths else None
        print_rr_result(rr_analysis.result, rr_plot_path)

        print("Running unchanged legacy 10-second HR/SpO2 estimator...")
        pulse_sensor.reset_fifo()
        pulse = pulse_sensor.estimate_heart_rate(duration_sec=10)
        temp = temp_sensor.read()
        packet = build_packet(temp, pulse, rr_fields)

        print("One-shot packet:")
        print(json.dumps(packet, indent=2, sort_keys=True))

        if args.no_post:
            print("Dry run complete; packet was not sent.")
            return 0

        print(f"POSTing one packet to {args.server_url} ...")
        response = requests.post(
            args.server_url,
            json=packet,
            timeout=args.timeout,
        )
        print(f"Server response: HTTP {response.status_code}")
        if not response.ok:
            print(response.text[:1000])
            print("Packet was rejected; live main.py was not changed.")
            return 4

        print("One test packet accepted by the server.")
        return 0

    except KeyboardInterrupt:
        print("Live RR packet test interrupted; no further packet will be sent.")
        return 130

    finally:
        close_safely(pulse_sensor, "MAX30102")
        close_safely(temp_sensor, "MLX90614")


if __name__ == "__main__":
    raise SystemExit(main())
