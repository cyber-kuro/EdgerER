import time
import random
import requests
from datetime import datetime


SERVER_URL = "http://localhost:8000/api/vitals"


NODES = [
    {
        "patient_id": "P101",
        "device_id": "SIM-101",
        "scenario": "stable",
    },
    {
        "patient_id": "P102",
        "device_id": "SIM-102",
        "scenario": "yellow_low_oxygen",
    },
    {
        "patient_id": "P103",
        "device_id": "SIM-103",
        "scenario": "orange_trend",
    },
    {
        "patient_id": "P104",
        "device_id": "SIM-104",
        "scenario": "crashing",
    },
    {
        "patient_id": "P105",
        "device_id": "SIM-105",
        "scenario": "no_finger",
    },
]


state = {
    "P103": {
        "hr": 78,
        "spo2": 98,
    },
    "P104": {
        "hr": 82,
        "spo2": 97,
    },
}


def now_string():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def evaluate_basic_status(hr, spo2, temp, finger_detected=True, signal_quality="GOOD"):
    if not finger_detected:
        return "GRAY", ["finger not detected / sensor not attached"]

    if signal_quality == "UNSTABLE":
        return "STABILIZING", ["heart-rate/oxygen signal stabilizing"]

    status = "GREEN"
    reasons = []

    if spo2 is not None:
        if spo2 < 90:
            status = "RED"
            reasons.append("critical oxygen level")
        elif spo2 < 94:
            status = "YELLOW"
            reasons.append("low oxygen level")

    if hr is not None:
        if hr > 130 or hr < 45:
            status = "RED"
            reasons.append("critical heart rate")
        elif hr > 110 or hr < 55:
            if status != "RED":
                status = "YELLOW"
            reasons.append("abnormal heart rate")

    if temp is not None:
        if temp > 39:
            status = "RED"
            reasons.append("high fever")
        elif temp > 37.8:
            if status != "RED":
                status = "YELLOW"
            reasons.append("elevated temperature")

    return status, reasons


def make_packet(node):
    patient_id = node["patient_id"]
    device_id = node["device_id"]
    scenario = node["scenario"]

    ambient_temp = round(random.uniform(24.5, 27.0), 2)
    object_temp = round(random.uniform(30.0, 32.0), 2)

    accel_x = round(random.uniform(-0.8, -0.6), 3)
    accel_y = round(random.uniform(0.35, 0.55), 3)
    accel_z = round(random.uniform(0.25, 0.45), 3)

    gyro_x = round(random.uniform(-7.0, -5.5), 3)
    gyro_y = round(random.uniform(1.0, 2.5), 3)
    gyro_z = round(random.uniform(-1.0, 0.5), 3)

    finger_detected = True
    signal_quality = "GOOD"

    if scenario == "stable":
        hr = round(random.uniform(68, 78), 1)
        spo2 = round(random.uniform(97, 99), 1)

    elif scenario == "yellow_low_oxygen":
        hr = round(random.uniform(84, 96), 1)
        spo2 = round(random.uniform(91, 93), 1)

    elif scenario == "orange_trend":
        state[patient_id]["hr"] += random.uniform(2.5, 4.0)
        state[patient_id]["spo2"] -= random.uniform(0.5, 0.9)

        hr = round(state[patient_id]["hr"], 1)
        spo2 = round(state[patient_id]["spo2"], 1)

        # Keep values from becoming impossible forever
        if state[patient_id]["hr"] > 118:
            state[patient_id]["hr"] = 78
        if state[patient_id]["spo2"] < 92:
            state[patient_id]["spo2"] = 98

    elif scenario == "crashing":
        state[patient_id]["hr"] += random.uniform(5.0, 8.0)
        state[patient_id]["spo2"] -= random.uniform(1.2, 2.0)

        hr = round(state[patient_id]["hr"], 1)
        spo2 = round(state[patient_id]["spo2"], 1)

        if state[patient_id]["hr"] > 140:
            state[patient_id]["hr"] = 82
        if state[patient_id]["spo2"] < 86:
            state[patient_id]["spo2"] = 97

    elif scenario == "no_finger":
        hr = None
        spo2 = None
        finger_detected = False
        signal_quality = "NO_FINGER"

    elif scenario == "unstable":
        hr = None
        spo2 = None
        signal_quality = "UNSTABLE"

    elif scenario == "red_low_oxygen":
        hr = round(random.uniform(95, 115), 1)
        spo2 = round(random.uniform(85, 89), 1)

    else:
        hr = round(random.uniform(70, 90), 1)
        spo2 = round(random.uniform(96, 99), 1)

    status, reasons = evaluate_basic_status(
        hr=hr,
        spo2=spo2,
        temp=object_temp,
        finger_detected=finger_detected,
        signal_quality=signal_quality,
    )

    packet = {
        "patient_id": patient_id,
        "device_id": device_id,
        "timestamp": now_string(),

        "ambient_temp_c": ambient_temp,
        "object_temp_c": object_temp,

        "accel_x_g": accel_x,
        "accel_y_g": accel_y,
        "accel_z_g": accel_z,

        "gyro_x_deg_s": gyro_x,
        "gyro_y_deg_s": gyro_y,
        "gyro_z_deg_s": gyro_z,

        "mpu_temp_c": round(random.uniform(28.0, 31.0), 2),

        "heart_rate_bpm": hr,
        "spo2_percent": spo2,

        "num_peaks": random.randint(8, 14) if hr is not None else 0,
        "samples": 244,

        "ir_mean": round(random.uniform(90000, 140000), 2) if finger_detected else round(random.uniform(300, 900), 2),
        "ir_std": round(random.uniform(300, 1200), 2) if finger_detected else round(random.uniform(5, 25), 2),

        "finger_detected": finger_detected,
        "signal_quality": signal_quality,

        "status": status,
        "reasons": reasons,
    }

    return packet


def send_packet(packet):
    try:
        response = requests.post(
            SERVER_URL,
            json=packet,
            timeout=2,
        )

        print(
            f"{packet['patient_id']} "
            f"{packet['status']} "
            f"HR={packet['heart_rate_bpm']} "
            f"SpO2={packet['spo2_percent']} "
            f"-> {response.status_code}"
        )

    except Exception as e:
        print(f"FAILED sending {packet['patient_id']}: {e}")


def main():
    print("Starting EdgeER dummy node simulator")
    print(f"Sending to: {SERVER_URL}")
    print("Press Ctrl+C to stop")

    while True:
        for node in NODES:
            packet = make_packet(node)
            send_packet(packet)

        print("-" * 80)
        time.sleep(3)


if __name__ == "__main__":
    main()
