import math
from typing import Any, Dict, Optional

from config import RR_DEMO_MAX_BPM, RR_DEMO_MIN_BPM, RR_MIN_CONFIDENCE


RELIABLE_RR_QUALITIES = {"GOOD", "MODERATE"}


def _eligible_respiratory_rate(
    packet: Dict[str, Any],
    rr_fresh: bool,
) -> Optional[float]:
    if not rr_fresh:
        return None

    respiratory_rate = packet.get("resp_rate_bpm")
    confidence = packet.get("rr_confidence")
    quality = str(packet.get("rr_quality") or "").upper()

    if (
        isinstance(respiratory_rate, bool)
        or not isinstance(respiratory_rate, (int, float))
        or not math.isfinite(respiratory_rate)
    ):
        return None
    if quality not in RELIABLE_RR_QUALITIES:
        return None
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
        or confidence < RR_MIN_CONFIDENCE
    ):
        return None

    # A numeric RR reaches this function only after SharedPPGPipeline has
    # verified estimator usability, sufficient PPG data, and cache freshness.
    return float(respiratory_rate)


def evaluate_risk(
    packet: Dict[str, Any],
    *,
    rr_fresh: bool = True,
) -> Dict[str, Any]:
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

    # Research-only PPG-derived RR can contribute a conservative YELLOW local
    # indication. It never replaces or reduces an existing HR/SpO2/temperature
    # alert, and it can never create ORANGE, RED, or CRASHING.
    respiratory_rate = _eligible_respiratory_rate(packet, rr_fresh)
    if respiratory_rate is not None and (
        respiratory_rate < RR_DEMO_MIN_BPM
        or respiratory_rate > RR_DEMO_MAX_BPM
    ):
        if status == "GREEN":
            status = "YELLOW"
        reasons.append("demonstration RR outside configured range")

    return {
        "status": status,
        "reasons": reasons
    }
