# Portfolio Summary

## One-sentence description

EdgeER is a Raspberry Pi and FastAPI edge-monitoring prototype that combines continuous physiological signal acquisition, confidence-aware measurement handling, persistent multipatient workflow management, fault simulation, and reproducible engineering validation.

## Resume bullets

- Built an edge-assisted multipatient monitoring prototype using Raspberry Pi, MAX30102 PPG, MLX90614 temperature sensing, FastAPI, JavaScript, and SQLite.
- Designed a single-reader shared PPG pipeline supporting buffered HR, SpO2, quality, and experimental respiratory-rate processing without competing sensor access.
- Implemented confidence-gated RR handling that withholds ambiguous measurements while preserving source, quality, and confidence metadata.
- Developed persistent patient, encounter, device, recheck, priority, dismissal, readmission, audit-log, and handoff workflows.
- Created a deterministic multi-device validation suite covering packet rules, device lifecycle, workflow persistence, reconnection, database integrity, and 1-20 simulated-device loads.

## Technical interview themes

- Why one hardware reader is safer than competing FIFO consumers
- Separation of acquisition, processing, transport, persistence, and presentation
- Server-authoritative device and encounter state
- Difference between invalid, missing, stale, and low-confidence data
- Regression testing with temporary databases and deterministic simulator seeds
- Honest limits of consumer-reference and exploratory sensor evaluation
