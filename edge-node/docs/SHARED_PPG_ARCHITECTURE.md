# Shared Continuous PPG Architecture

EdgeER v1.0 uses one MAX30102 hardware reader and multiple buffer-based signal
processors. The design prevents competing code paths from consuming or
resetting the same hardware FIFO.

## Data flow

```text
MAX30102 FIFO
    |
    v
PPGStream acquisition thread (only hardware reader)
    |
    v
timestamped RED/IR ring buffer
    |-- 12 s snapshot --> legacy-compatible HR/SpO2 calculation
    |-- 60 s snapshot --> PPG acquisition-quality analysis
    `-- 60 s snapshot --> research-only respiratory-rate estimator
                              |
                              v
                    freshness-aware measurement cache
                              |
                              v
                         main packet loop
```

`PPGStream` resets the FIFO once immediately before its acquisition thread
starts. During normal operation, `main.py` does not call `reset_fifo`,
`read_fifo`, `read_raw_sample`, or the blocking legacy
`estimate_heart_rate` wrapper. Each processor receives an independent list
snapshot containing immutable samples with monotonic timestamps.

The legacy sensor wrapper remains for standalone compatibility. It gathers a
window using the old blocking behavior, then delegates all calculation to
`processing/hr_spo2.py`. It must never run concurrently with a `PPGStream`
using the same sensor.

## Windows and refresh intervals

Defaults are defined in `config.py` and use seconds unless noted otherwise.

| Measurement | Window | Minimum input | Refresh | Stale after |
|---|---:|---:|---:|---:|
| HR/SpO2 | 12 s | 10 s and 200 samples | 6 s | 20 s |
| PPG quality | 60 s | 2 samples | 10 s | 30 s |
| PPG-derived RR | 60 s | 45 s and 1,000 samples | 300 s | 360 s |

Before a first result exists, the pipeline checks warm-up progress every five
seconds. It does not call an estimator until that estimator's minimum duration
and sample count are present. Expensive RR processing therefore does not run
on every two-second packet loop.

## Freshness and packet behavior

The pipeline records monotonic update times separately for HR/SpO2, PPG
quality, and RR. Cached values beyond their stale threshold are withheld.
Freshness remains internal and does not add fields to the server contract.

At startup, packets safely use null HR, SpO2, and RR values until the relevant
buffer windows mature. The pulse `signal_quality` reuses the existing
`UNSTABLE` packet state; no new server enum is introduced. RR always reports
source `PPG`. Before an RR result is available, confidence is `0.0` and quality
is `UNAVAILABLE`.

RR confidence gating is unchanged:

- A usable `GOOD` or `MODERATE` result may populate `resp_rate_bpm`.
- `LOW_CONFIDENCE`, `BAD_SIGNAL`, `BAD_TIMING`, `UNAVAILABLE`, or any unusable
  result sends `resp_rate_bpm` as null.
- RR does not participate in the Pi risk engine, LEDs, or buzzer.

The FastAPI server remains responsible for final workflow status, NEWS2
availability, alerting, priority, and handoff decisions.

## Failure handling

Individual acquisition failures are counted and exposed through
`PPGStream.get_stats()`; the acquisition thread continues to the next sample.
FIFO-register polling failures are tracked separately. Main logs acquisition
health once per minute rather than printing raw samples.

HR/SpO2, quality, and RR estimator exceptions are isolated. The pipeline logs
a concise technical error and retains the previous cached result. Freshness
still applies, so a retained result is eventually withheld if processing does
not recover. Temporary MLX90614 or MPU6050 read failures similarly produce
null fields for that packet instead of stopping the node.

Network POST behavior remains the existing best-effort request per packet;
this refactor does not implement a durable outbox.

## Cleanup and ownership

In normal operation, `main.py` owns the MAX30102 sensor and supplies it to one
`PPGStream`. Shutdown closes the stream first, which joins its acquisition
thread, and then closes the sensor once. A `PPGStream` that creates its own
sensor (as used by recording scripts) owns and closes that sensor itself.
`stop()` and `close()` are idempotent.

## Limitations

- HR/SpO2 processing preserves the prototype legacy algorithm and is not a
  clinically validated pulse-oximetry implementation.
- PPG-derived RR remains research/prototype output and is not clinically
  authoritative.
- Optical settings and quality thresholds still require target-hardware and
  subject-condition validation.
- Motion fields remain null in the live packet, matching the existing node
  behavior.
- The network path has no persistent retry/outbox in this release.
