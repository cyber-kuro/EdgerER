# Project Status

## Current status

**EdgeER is a v0.9 release candidate.**

The core software, dashboard workflows, shared PPG architecture, validation tooling, exploratory bench comparison, and technical report are complete. Final v1.0 designation depends on release hygiene, final physical-node stability confirmation, and public repository packaging.

## Implemented

- Shared continuous MAX30102 acquisition and buffered processing
- HR, SpO2, PPG quality, and experimental RR processing
- Confidence-gated RR transmission
- FastAPI ingestion, validation, and packet audit logs
- SQLite-backed patients, encounters, devices, events, timers, and history
- Registry-backed device assignment and lifecycle management
- Multipatient dashboard and workflow actions
- Multi-device simulator and reproducible validation suite
- Consumer-reference bench comparison
- Power observation summary
- Public technical report

## Pending future work

- ESP32-based patient node
- Additional physical MAX30102 nodes
- Expanded physical stability and networking studies
- Motion-aware quality gating using MPU6050
- Blood-pressure and structured clinical inputs
- NEWS2 completeness mode
- Authentication, HTTPS, and production cybersecurity
- Custom PCB, battery optimization, and enclosure
- Faculty-supervised or clinical validation studies

Pending items are documented in [docs/FUTURE_WORK.md](docs/FUTURE_WORK.md) and do not represent claims of current functionality.
