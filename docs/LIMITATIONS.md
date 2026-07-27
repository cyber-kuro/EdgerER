# Limitations

- The system is an engineering prototype, not a certified medical device.
- Physiological sensors are low-cost modules rather than clinical reference instruments.
- The pulse-oximeter comparison used a consumer reference device and a small observation set.
- The RR evaluation set is small and exploratory.
- PPG-derived RR is indirect and may be unavailable under motion, pressure change, or ambiguous modulation.
- Temperature is a surface measurement and is not equivalent to core body temperature.
- The current physical implementation centers on one Raspberry Pi node.
- Simulator benchmarks used in-process FastAPI TestClient execution rather than a physical distributed network.
- No blood-pressure integration or complete NEWS2 implementation is included.
- No human-subject, demographic, clinical-outcome, or hospital-workflow validation was performed.
- No regulatory, production cybersecurity, or deployment certification was performed.
- Device and measurement thresholds are prototype configuration rules and should not be interpreted as validated clinical criteria.
