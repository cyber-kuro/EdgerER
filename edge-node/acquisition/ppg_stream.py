import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional

from config import PPG_RING_BUFFER_SECONDS, PPG_SAMPLE_INTERVAL_SEC


@dataclass(frozen=True)
class PPGSample:
    """One raw MAX30102 sample pair and its host monotonic read time."""

    monotonic_time_sec: float
    red_raw: int
    ir_raw: int


class PPGStream:
    """Continuously collect raw MAX30102 samples into a bounded ring buffer.

    The stream must be the exclusive reader of its MAX30102 instance while it
    is running. In particular, do not call the legacy estimate_heart_rate()
    method concurrently because both consumers would remove samples from the
    same hardware FIFO.
    """

    def __init__(
        self,
        sensor: Optional[Any] = None,
        sample_interval_sec: float = PPG_SAMPLE_INTERVAL_SEC,
        ring_buffer_seconds: float = PPG_RING_BUFFER_SECONDS,
        fifo_poll_interval_sec: float = 1.0,
    ):
        if sample_interval_sec <= 0:
            raise ValueError("sample_interval_sec must be greater than zero")
        if ring_buffer_seconds <= 0:
            raise ValueError("ring_buffer_seconds must be greater than zero")

        self.sample_interval_sec = float(sample_interval_sec)
        self.ring_buffer_seconds = float(ring_buffer_seconds)
        self.fifo_poll_interval_sec = max(
            float(fifo_poll_interval_sec), self.sample_interval_sec
        )
        self.buffer_capacity = max(
            1, math.ceil(self.ring_buffer_seconds / self.sample_interval_sec)
        )

        self._owns_sensor = sensor is None
        if sensor is None:
            # Import lazily so buffer tests can run without Raspberry Pi I2C
            # packages or MAX30102 hardware installed.
            from sensors.max30102 import MAX30102Sensor

            sensor = MAX30102Sensor()
        self.sensor = sensor

        self._buffer: Deque[PPGSample] = deque(maxlen=self.buffer_capacity)
        self._data_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._closed = False

        self._reset_session_stats()

    def start(self):
        """Start acquisition in a daemon thread; repeated calls are safe."""
        with self._state_lock:
            if self._closed:
                raise RuntimeError("PPGStream is closed")
            if self._thread is not None and self._thread.is_alive():
                return

            self._reset_session_stats()
            self._stop_event.clear()

            reset_fifo = getattr(self.sensor, "reset_fifo", None)
            if callable(reset_fifo):
                reset_fifo()

            self._thread = threading.Thread(
                target=self._run,
                name="max30102-ppg-stream",
                daemon=True,
            )
            self._thread.start()

    def stop(self):
        """Request acquisition shutdown and wait for the thread to finish."""
        with self._state_lock:
            thread = self._thread
            self._stop_event.set()

        if thread is not None and thread is not threading.current_thread():
            thread.join()

        with self._state_lock:
            if self._thread is thread:
                self._thread = None

    def close(self):
        """Stop acquisition and close a sensor created by this stream."""
        self.stop()
        with self._state_lock:
            if self._closed:
                return
            self._closed = True

        if self._owns_sensor:
            close_sensor = getattr(self.sensor, "close", None)
            if callable(close_sensor):
                close_sensor()

    def is_running(self):
        with self._state_lock:
            return self._thread is not None and self._thread.is_alive()

    def get_recent_window(self, seconds: float) -> List[PPGSample]:
        """Return a snapshot covering up to `seconds` before the latest sample."""
        if seconds <= 0:
            raise ValueError("seconds must be greater than zero")

        with self._data_lock:
            if not self._buffer:
                return []
            cutoff = self._buffer[-1].monotonic_time_sec - float(seconds)
            return [
                sample
                for sample in self._buffer
                if sample.monotonic_time_sec >= cutoff
            ]

    def get_stats(self) -> Dict[str, Any]:
        """Return timing, buffer, read-error, and latest FIFO metadata."""
        with self._data_lock:
            samples = list(self._buffer)
            sample_count_total = self._sample_count_total
            buffer_overwrites = self._buffer_overwrites
            large_timing_gaps = self._large_timing_gaps
            estimated_dropped_samples = self._estimated_dropped_samples
            read_errors = self._read_errors
            last_error = self._last_error
            fifo_read_errors = self._fifo_read_errors
            last_fifo_error = self._last_fifo_error
            fifo_overflow_events = self._fifo_overflow_events
            fifo_state = dict(self._last_fifo_state)

        approximate_rate = 0.0
        if len(samples) >= 2:
            duration = (
                samples[-1].monotonic_time_sec
                - samples[0].monotonic_time_sec
            )
            if duration > 0:
                approximate_rate = (len(samples) - 1) / duration

        return {
            "is_running": self.is_running(),
            "sample_interval_sec": self.sample_interval_sec,
            "nominal_sample_rate_hz": 1.0 / self.sample_interval_sec,
            "approximate_sample_rate_hz": approximate_rate,
            "sample_count_total": sample_count_total,
            "buffer_sample_count": len(samples),
            "buffer_capacity": self.buffer_capacity,
            "buffer_overwrites": buffer_overwrites,
            "large_timing_gaps": large_timing_gaps,
            # This is inferred from host read timing; FIFO metadata below is
            # the stronger evidence that samples were actually lost.
            "estimated_dropped_samples": estimated_dropped_samples,
            "read_errors": read_errors,
            "last_error": last_error,
            "fifo_read_errors": fifo_read_errors,
            "last_fifo_error": last_fifo_error,
            "fifo_overflow_events": fifo_overflow_events,
            "fifo_write_pointer": fifo_state.get("write_pointer"),
            "fifo_read_pointer": fifo_state.get("read_pointer"),
            "fifo_unread_samples": fifo_state.get("unread_samples"),
            "fifo_overflow_counter": fifo_state.get("overflow_counter"),
            "oldest_monotonic_time_sec": (
                samples[0].monotonic_time_sec if samples else None
            ),
            "newest_monotonic_time_sec": (
                samples[-1].monotonic_time_sec if samples else None
            ),
        }

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def _reset_session_stats(self):
        with self._data_lock:
            self._buffer.clear()
            self._sample_count_total = 0
            self._buffer_overwrites = 0
            self._large_timing_gaps = 0
            self._estimated_dropped_samples = 0
            self._read_errors = 0
            self._last_error = None
            self._previous_sample_time: Optional[float] = None
            self._fifo_read_errors = 0
            self._last_fifo_error: Optional[str] = None
            self._fifo_overflow_events = 0
            self._last_fifo_state: Dict[str, Any] = {}

    def _run(self):
        next_deadline = time.monotonic()
        next_fifo_poll = next_deadline

        while not self._stop_event.is_set():
            next_deadline += self.sample_interval_sec
            wait_seconds = next_deadline - time.monotonic()
            if wait_seconds > 0 and self._stop_event.wait(wait_seconds):
                break

            try:
                read_raw_sample = getattr(self.sensor, "read_raw_sample", None)
                if callable(read_raw_sample):
                    red_raw, ir_raw = read_raw_sample()
                else:
                    red_raw, ir_raw = self.sensor.read_fifo()
                sample_time = time.monotonic()
            except Exception as exc:
                with self._data_lock:
                    self._read_errors += 1
                    self._last_error = f"{type(exc).__name__}: {exc}"
                # Resume from the current time after a blocking I2C failure;
                # otherwise the loop could spin through overdue deadlines.
                if time.monotonic() - next_deadline > self.sample_interval_sec * 4:
                    next_deadline = time.monotonic()
                continue

            self._append_sample(
                PPGSample(
                    monotonic_time_sec=sample_time,
                    red_raw=int(red_raw),
                    ir_raw=int(ir_raw),
                )
            )

            if sample_time >= next_fifo_poll:
                self._poll_fifo_state()
                next_fifo_poll = sample_time + self.fifo_poll_interval_sec

            # If a long stall put the scheduler far behind, resume from now
            # rather than spinning through many artificial catch-up reads.
            if time.monotonic() - next_deadline > self.sample_interval_sec * 4:
                next_deadline = time.monotonic()

    def _append_sample(self, sample: PPGSample):
        with self._data_lock:
            if self._previous_sample_time is not None:
                elapsed = sample.monotonic_time_sec - self._previous_sample_time
                if elapsed > self.sample_interval_sec * 1.5:
                    self._large_timing_gaps += 1
                    missed = max(
                        1,
                        int(round(elapsed / self.sample_interval_sec)) - 1,
                    )
                    self._estimated_dropped_samples += missed

            if len(self._buffer) == self.buffer_capacity:
                self._buffer_overwrites += 1

            self._buffer.append(sample)
            self._sample_count_total += 1
            self._previous_sample_time = sample.monotonic_time_sec

    def _poll_fifo_state(self):
        get_fifo_state = getattr(self.sensor, "get_fifo_state", None)
        if not callable(get_fifo_state):
            return

        try:
            state = dict(get_fifo_state())
        except Exception as exc:
            with self._data_lock:
                self._fifo_read_errors += 1
                self._last_fifo_error = f"{type(exc).__name__}: {exc}"
            return

        with self._data_lock:
            previous_counter = self._last_fifo_state.get("overflow_counter")
            current_counter = state.get("overflow_counter")
            if isinstance(current_counter, int):
                if isinstance(previous_counter, int):
                    if current_counter >= previous_counter:
                        self._fifo_overflow_events += (
                            current_counter - previous_counter
                        )
                    else:
                        # The hardware counter was reset or wrapped.
                        self._fifo_overflow_events += current_counter
                else:
                    self._fifo_overflow_events += current_counter
            self._last_fifo_state = state
