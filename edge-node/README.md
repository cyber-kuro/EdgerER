# EdgeER Raspberry Pi Node

EdgeER is an investigational engineering prototype for multipatient monitoring
and reassessment workflow demonstrations. It is not a medical device and is not
intended for patient-care decisions.

The Raspberry Pi node owns one continuous MAX30102 PPG stream. HR, SpO2, PPG
quality, and research-only respiratory rate use snapshots from that shared
buffer. `main.py` assembles the existing packet contract, applies a provisional
local indication, drives the LEDs/buzzer, and sends packets to the FastAPI
server. The server remains authoritative for encounter, priority, reassessment,
and dashboard workflow state.

## Conservative RR-aware local indication

PPG-derived RR is eligible for physiological warning behavior only when the
shared pipeline emits a fresh numeric result that is usable, has `GOOD` or
`MODERATE` quality, and meets the existing 0.50 confidence gate. Sufficient PPG
duration and sample count are enforced before the estimator runs.

The configurable prototype demonstration range is 8–30 breaths/min. An
eligible value outside that range may raise a `GREEN` local state to `YELLOW`.
RR never creates `ORANGE`, `RED`, or `CRASHING`, and it never reduces or
overrides an existing HR, SpO2, or temperature alert.

Missing, stale, null, `LOW_CONFIDENCE`, `BAD_SIGNAL`, `BAD_TIMING`, or
`UNAVAILABLE` RR is a technical measurement state, not a physiological
measurement. Its candidate value is not used. The current node logs that state
through the PPG pipeline and does not add a new, potentially confusing LED
pattern.

These thresholds are unvalidated engineering demonstration settings. They are
not clinical criteria.

## Raspberry Pi verification

Run these commands on the Pi from the deployed project directory:

```bash
cd /home/edgeer/edgeer/edger
python3 -m pip install -r requirements.txt
python3 -m compileall -q .
python3 -m unittest discover -s tests -v
python3 main.py
```

Do not run `main.py` on Windows: it initializes Raspberry Pi I2C and GPIO
hardware. Processing, packet-building, and risk-engine tests are hardware
independent and may run on a development machine with the Python dependencies
installed.

See `docs/SHARED_PPG_ARCHITECTURE.md` for acquisition ownership, refresh
intervals, cache freshness, and failure behavior.
