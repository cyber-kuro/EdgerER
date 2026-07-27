# Demo Script

Target duration: 2-4 minutes.

## 1. Introduction

“EdgeER is an investigational edge-assisted multipatient monitoring and reassessment workflow prototype. It combines low-cost sensing, quality-aware signal processing, centralized workflow management, and fault simulation.”

## 2. Physical node

Show the Raspberry Pi, MAX30102, MLX90614, LED/buzzer, and terminal. Explain that one continuous reader feeds a shared PPG buffer.

## 3. Device and patient registration

Show the device registry, open the patient registration form, select an online available device from the dropdown, and create an encounter.

## 4. Live measurements

Show HR, SpO2, temperature, RR quality, RR confidence, and a withheld RR example. Emphasize that unavailable measurements are not silently replaced.

## 5. Workflow

Demonstrate priority escalation, a recheck timer, timeline actions, dismissal, readmission, and a structured handoff.

## 6. Fault behavior

Run several simulated devices. Show gradual worsening, an offline device, reconnection, and an intentionally rejected invalid packet.

## 7. Closing

“EdgeER is an engineering prototype and is not intended for patient-care decisions. Planned extensions include ESP32 nodes, expanded hardware evaluation, and structured clinical-input completeness.”
