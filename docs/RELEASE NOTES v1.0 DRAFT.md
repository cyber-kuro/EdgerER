# EdgeER v1.0.0 - Draft Release Notes

EdgeER v1.0.0 packages the project as a portfolio-ready investigational engineering prototype.

## Highlights

- Raspberry Pi physical sensing node
- Shared continuous MAX30102 PPG architecture
- HR, SpO2, temperature, and confidence-gated experimental RR
- FastAPI server and SQLite persistence
- Multipatient dashboard
- Device registry, assignment, retirement, and restoration
- Encounter, priority, recheck, dismissal, readmission, and handoff workflows
- Multi-device simulator and fault scenarios
- Reproducible software-validation suite
- Public technical report and supporting bench data

## Validation summary

- 83 automated tests passed
- 45/45 packet-validation cases passed
- 31/31 device-lifecycle checks passed
- 3/3 workflow scenarios passed
- 10/10 simulated reconnect cycles passed
- 20/20 shared-PPG architecture assertions passed

## Safety boundary

EdgeER is not a medical device and is not intended for patient-care decisions.

## Planned extensions

ESP32 nodes, larger physical fleets, expanded RR evaluation, structured NEWS2 completeness handling, networking improvements, and supervised research remain future work.
