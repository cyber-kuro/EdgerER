# Data Flow

## Physical-node path

1. MAX30102 produces RED and IR PPG samples.
2. A single acquisition thread timestamps samples with a monotonic clock.
3. Samples enter a thread-safe ring buffer.
4. Short windows are used for HR and SpO2 estimation.
5. Longer windows are used for PPG quality and experimental RR estimation.
6. Cached values are checked for freshness.
7. Unavailable or unreliable values are represented as null.
8. Temperature is read and sanitized against configured acceptance ranges.
9. The node builds a packet with device, patient, sequence, source, quality, and timing metadata.

## Server path

1. FastAPI receives the packet.
2. Required identifiers, types, configured ranges, timestamp freshness, packet order, device lifecycle, active encounter, and assignment consistency are checked.
3. Accepted and rejected packets are logged with reasons.
4. Accepted measurements update current patient and device state.
5. SQLite persists the encounter, history, events, and timers.
6. The dashboard polls current state and renders patient, device, workflow, quality, and freshness information.

## Withheld RR example

```json
{
  "resp_rate_bpm": null,
  "rr_source": "PPG",
  "rr_confidence": 0.23,
  "rr_quality": "LOW_CONFIDENCE"
}
```

This representation distinguishes measurement unavailability from a normal respiratory-rate value.
