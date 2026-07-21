# EdgeER v0.9.0 Release-Candidate Verification

EdgeER is an investigational engineering prototype. It is not a medical device
and is not intended for patient-care decisions.

## Device assignment behavior

The registration dialog loads the live device registry whenever it opens and
refreshes with the dashboard polling cycle. The current selection is preserved
only while the backend still reports that device as assignable. Registration
without a device remains an explicit supported path.

A device is assignable only when it is known to the registry, has been seen
within 15 seconds, has no active encounter assignment, and has registry state
`AVAILABLE`. `OFFLINE`, `ASSIGNED`, `ACTIVE`, `LOW_BATTERY`, `SENSOR_FAULT`, and
`NEEDS_CLEANING` devices are rejected. The backend repeats this validation at
submission time under a workflow lock, and SQLite prevents the same non-empty
device ID from appearing on two active encounters.

Dismissal unlinks the live registry assignment and leaves the closed
encounter's device ID in history. The registry device becomes
`NEEDS_CLEANING`. The device panel's **Mark cleaned** action changes an
unassigned device to `AVAILABLE`; it is offered for selection only while it is
also online. Readmission always creates a new encounter and does not reuse the
previous device automatically.

Registry removal is lifecycle-aware. An unassigned device with no packet,
encounter, profile, or latest-state references is permanently deleted. An
unassigned device with historical references is retired instead: its prior
operational state and history remain available through **Show retired
devices**, but it is excluded from active counts and assignment. Assigned
devices must be released first. Retired-device packets are rejected and
audited without reactivating the registry entry. A retired device can be
restored explicitly; restoration clears retirement metadata but does not
assign it to an encounter.

Packets from a dismissed patient are audited and rejected until explicit
readmission. An assigned offline device remains linked and may reconnect.
Exact repeated `(device_id, packet_sequence, device_send_time)` identities and
device send times older than five minutes are rejected and audited.

## RR and local indication

Freshness, sufficient PPG input, estimator usability, `GOOD` or `MODERATE`
quality, and confidence of at least 0.50 are required before RR can influence
the provisional Pi indication. The configurable demonstration range is 8–30
breaths/min. An eligible out-of-range value can raise `GREEN` to `YELLOW` only.
RR alone cannot create a severe alert. Unavailable, withheld, poor-quality,
null, or stale RR is technical state and is not treated as deterioration.

## Start the server on Windows

```powershell
Set-Location 'C:\Users\hassa\Documents\EdgeER\server'
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn server:app --host 0.0.0.0 --port 8000
```

Open `http://127.0.0.1:8000/` in a browser.

## Copy changed edge-node files from PowerShell

Run from `C:\Users\hassa\Documents\EdgeER`:

```powershell
scp .\edge-node\config.py edgeer@192.168.0.11:/home/edgeer/edgeer/edger/config.py
scp .\edge-node\main.py edgeer@192.168.0.11:/home/edgeer/edgeer/edger/main.py
scp .\edge-node\utils\risk_engine.py edgeer@192.168.0.11:/home/edgeer/edgeer/edger/utils/risk_engine.py
scp .\edge-node\tests\test_rr_local_alerts.py edgeer@192.168.0.11:/home/edgeer/edgeer/edger/tests/test_rr_local_alerts.py
scp .\edge-node\README.md edgeer@192.168.0.11:/home/edgeer/edgeer/edger/README.md
scp .\edge-node\docs\SHARED_PPG_ARCHITECTURE.md edgeer@192.168.0.11:/home/edgeer/edgeer/edger/docs/SHARED_PPG_ARCHITECTURE.md
```

## Compile, test, and run on the Pi

```bash
cd /home/edgeer/edgeer/edger
python3 -m pip install -r requirements.txt
python3 -m compileall -q .
python3 -m unittest discover -s tests -v
python3 main.py
```

## Manual browser and hardware checklist

- [ ] Start the FastAPI server and open the dashboard.
- [ ] Start one physical Pi node; confirm no second MAX30102 reader starts.
- [ ] Confirm the Pi appears online in the device registry.
- [ ] Open registration; confirm only online, available devices appear.
- [ ] Register a patient and assign the Pi.
- [ ] Confirm live HR, SpO2, temperature, and status values appear.
- [ ] After RR warm-up/refresh, confirm RR value, quality, confidence, source,
      and freshness presentation.
- [ ] Confirm only eligible out-of-range RR can add a local `YELLOW` state.
- [ ] Confirm withheld or poor-quality RR creates no physiological warning.
- [ ] Escalate and de-escalate the patient; confirm priority placement.
- [ ] Set a recheck; confirm countdown and due state.
- [ ] Add nurse, doctor-notified, and medication timeline actions; confirm only
      the intended actions clear the timer.
- [ ] Disconnect the node for more than 15 seconds; confirm patient and device
      show `OFFLINE` without losing the assignment.
- [ ] Reconnect the node; confirm the same assignment returns to `ACTIVE`.
- [ ] Generate a handoff and review current-encounter content.
- [ ] Dismiss the patient; confirm the active row disappears and the device
      becomes `NEEDS_CLEANING`.
- [ ] Leave the old node packet configuration running briefly; confirm packets
      are rejected and do not readmit the patient.
- [ ] Mark the device cleaned; confirm it is selectable only while online.
- [ ] Readmit the patient; confirm a new `NO DATA` encounter appears with no
      automatic device reuse.
- [ ] Confirm the prior encounter remains available through encounter/handoff
      API history.
- [ ] Assign an available device to the new encounter and confirm live recovery.
- [ ] Restart the server; confirm encounter, assignment, priority, device, and
      recheck state restoration.
- [ ] Check the dashboard near 1366×768, 1920×1080, tablet width, and a narrow
      laptop window; confirm focus states and horizontal table scrolling.

## Current limitations

- Sensor algorithms and thresholds remain investigational engineering logic.
- PPG-derived RR requires physical Pi verification for timing, freshness, and
  LED behavior under real signal conditions.
- Device cleaning completion is a deliberate manual registry transition.
- There is no durable network outbox, authentication, or production deployment
  infrastructure in this checkpoint.
- Reference pulse-oximeter bench comparison remains a separate future activity.
