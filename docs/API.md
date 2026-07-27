# API Overview

EdgeER uses FastAPI. When the server is running, interactive route documentation is available at:

- `/docs` - Swagger UI
- `/redoc` - ReDoc

## Confirmed public-facing routes

### `POST /api/vitals`

Receives a measurement packet from a physical or simulated device. The server validates identifiers, data types, configured ranges, timestamps, packet order, device lifecycle, encounter state, and assignment consistency.

Representative fields include:

```json
{
  "patient_id": "P001",
  "device_id": "PI-001",
  "device_type": "RASPBERRY_PI",
  "firmware_version": "0.1.0",
  "packet_sequence": 42,
  "timestamp": "2026-07-25T14:00:00+00:00",
  "device_send_time": "2026-07-25T14:00:00+00:00",
  "heart_rate_bpm": 72,
  "spo2_percent": 98,
  "object_temp_c": null,
  "resp_rate_bpm": 12.8,
  "rr_source": "PPG",
  "rr_confidence": 0.79,
  "rr_quality": "MODERATE"
}
```

### `GET /api/packet_logs`

Returns recent packet audit records. A `limit` query parameter is supported by the current implementation.

### `GET /api/devices`

Returns device-registry state. Retired-device inclusion is controlled by the implemented query behavior, such as `include_retired=true`.

## Workflow endpoints

Patient registration, encounter, assignment, device lifecycle, recheck, escalation, dismissal, readmission, and handoff endpoints are documented by the running FastAPI application. Consult `/docs` for the exact routes and request schemas of the checked-out version.

## Response interpretation

The server distinguishes HTTP success from application acceptance. A packet response should be checked for an explicit acceptance field and rejection reason rather than relying only on transport status.
