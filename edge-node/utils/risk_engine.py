def evaluate_risk(packet):
    hr = packet.get("heart_rate_bpm")
    spo2 = packet.get("spo2_percent")
    temp = packet.get("object_temp_c")
    finger_detected = packet.get("finger_detected")
    signal_quality = packet.get("signal_quality")

    if finger_detected is False:
        return {
            "status": "GRAY",
            "reasons": ["finger not detected / sensor not attached"]
        }

    if signal_quality == "UNSTABLE":
        return {
            "status": "STABILIZING",
            "reasons": ["heart-rate/oxygen signal stabilizing"]
        }

    status = "GREEN"
    reasons = []

    # Oxygen risk
    if spo2 is not None:
        if spo2 < 90:
            status = "RED"
            reasons.append("critical oxygen level")
        elif spo2 < 94:
            status = "YELLOW"
            reasons.append("low oxygen level")

    # Heart-rate risk
    if hr is not None:
        if hr > 130 or hr < 45:
            status = "RED"
            reasons.append("critical heart rate")
        elif hr > 110 or hr < 55:
            if status != "RED":
                status = "YELLOW"
            reasons.append("abnormal heart rate")

    # Temperature risk
    if temp is not None:
        if temp > 39:
            status = "RED"
            reasons.append("high fever")
        elif temp > 37.8:
            if status != "RED":
                status = "YELLOW"
            reasons.append("elevated temperature")

    return {
        "status": status,
        "reasons": reasons
    }