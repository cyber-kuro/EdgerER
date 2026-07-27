# Future Work

The following items are planned or research-ready extensions. They are not part of the current v1.0 scope.

## Hardware expansion

- [ ] ESP32-based patient node
- [ ] Additional MAX30102 physical nodes
- [ ] Multiple heterogeneous physical devices
- [ ] Battery-powered operation and battery telemetry
- [ ] Custom PCB and production enclosure

## Signal processing and evaluation

- [ ] Expanded PPG-derived RR evaluation
- [ ] Multi-participant evaluation
- [ ] Motion-aware quality gating using MPU6050
- [ ] Longer physical-node stability studies
- [ ] Expanded consumer and reference-device comparisons

## Networking and resilience

- [ ] Persistent local outbox during server or network interruption
- [ ] Physical Wi-Fi interruption and recovery studies
- [ ] Multi-node physical reconnection testing
- [ ] Long-duration server soak testing

## Structured clinical-input completeness

- [ ] Blood-pressure integration
- [ ] Supplemental-oxygen status
- [ ] SpO2 scale selection
- [ ] Consciousness or new-confusion input
- [ ] Optional manual RR fallback
- [ ] NEWS2 completeness state: COMPLETE, PENDING, or UNAVAILABLE
- [ ] Server-side numeric NEWS2 only when every required observation is valid

## Security and deployment

- [ ] Authentication and authorization
- [ ] HTTPS and secure device identity
- [ ] Production secret management
- [ ] Containerized deployment
- [ ] Formal cybersecurity review

## Research options

- [ ] Faculty-supervised PPG respiratory-rate study
- [ ] Distributed edge-system reliability study
- [ ] Simulated reassessment-workflow evaluation
- [ ] Human-participant or clinical work only with appropriate supervision and approvals
- [ ] Publication if supported by a future supervised research program

LLM-based diagnosis, recommendations, and free-form clinical handoff generation are not planned for the core system.
