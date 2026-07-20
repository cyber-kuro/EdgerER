import argparse
import csv
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from acquisition.ppg_stream import PPGStream  # noqa: E402
from config import LOG_DIR, PPG_RECORD_SECONDS  # noqa: E402


def parse_args():
    parser = argparse.ArgumentParser(
        description="Record a raw MAX30102 PPG window to CSV."
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=PPG_RECORD_SECONDS,
        help=f"recording duration (default: {PPG_RECORD_SECONDS:g} seconds)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="optional output CSV path (default: timestamped file in logs/)",
    )
    args = parser.parse_args()
    if args.seconds <= 0:
        parser.error("--seconds must be greater than zero")
    return args


def default_output_path():
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return LOG_DIR / f"ppg_{timestamp}.csv"


def write_csv(output_path, samples):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    first_time = samples[0].monotonic_time_sec if samples else 0.0

    with output_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(
            [
                "elapsed_time_sec",
                "monotonic_time_sec",
                "red_raw",
                "ir_raw",
            ]
        )
        for sample in samples:
            writer.writerow(
                [
                    f"{sample.monotonic_time_sec - first_time:.6f}",
                    f"{sample.monotonic_time_sec:.6f}",
                    sample.red_raw,
                    sample.ir_raw,
                ]
            )


def format_mean(values):
    return statistics.fmean(values) if values else float("nan")


def format_std(values):
    return statistics.pstdev(values) if values else float("nan")


def main():
    args = parse_args()
    output_path = (args.output or default_output_path()).resolve()
    stream = None
    interrupted = False

    try:
        stream = PPGStream()
        stream.start()
        print(f"Recording raw PPG for {args.seconds:g} seconds...")

        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

    except KeyboardInterrupt:
        interrupted = True
        print("\nRecording interrupted; saving samples collected so far.")

    finally:
        if stream is not None:
            stream.stop()

    if stream is None:
        return 1

    samples = stream.get_recent_window(max(args.seconds, 0.001) + 1.0)
    stats = stream.get_stats()
    stream.close()

    write_csv(output_path, samples)

    ir_values = [sample.ir_raw for sample in samples]
    red_values = [sample.red_raw for sample in samples]

    print(f"Sample count: {len(samples)}")
    print(
        "Approximate sample rate: "
        f"{stats['approximate_sample_rate_hz']:.2f} Hz"
    )
    print(f"IR mean: {format_mean(ir_values):.2f}")
    print(f"IR std: {format_std(ir_values):.2f}")
    print(f"RED mean: {format_mean(red_values):.2f}")
    print(f"RED std: {format_std(red_values):.2f}")
    print(f"Number of large timing gaps: {stats['large_timing_gaps']}")
    print(f"Output CSV path: {output_path}")

    if stats["read_errors"]:
        print(
            f"Sensor read errors: {stats['read_errors']} "
            f"(last: {stats['last_error']})"
        )
    if stats["fifo_overflow_events"]:
        print(
            "Observed FIFO overflow events: "
            f"{stats['fifo_overflow_events']}"
        )

    return 130 if interrupted else 0


if __name__ == "__main__":
    raise SystemExit(main())
