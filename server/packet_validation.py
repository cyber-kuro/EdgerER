from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any, Optional

from models import (
    MAX_DEVICE_CLOCK_LEAD_SECONDS,
    MAX_DEVICE_PACKET_AGE_SECONDS,
    RR_CONFIDENCE_ELIGIBILITY_GATE,
)


VALIDATION_VERSION = "packet_validation_v1"

RELIABLE_RR_QUALITIES = {"GOOD", "MODERATE"}
RR_QUALITIES = {
    "GOOD",
    "MODERATE",
    "LOW_CONFIDENCE",
    "BAD_SIGNAL",
    "BAD_TIMING",
    "UNAVAILABLE",
}

NUMERIC_RANGES = {
    "heart_rate_bpm": (20, 250),
    "spo2_percent": (50, 100),
    "resp_rate_bpm": (4, 60),
    "object_temp_c": (30, 45),
    "ambient_temp_c": (0, 60),
    "systolic_bp": (50, 250),
    "diastolic_bp": (30, 150),
    "ppg_sqi": (0, 1),
    "rr_confidence": (0, 1),
    "battery_percent": (0, 100),
}


@dataclass(frozen=True)
class PacketValidationResult:
    errors: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def accepted(self):
        return not self.errors

    @property
    def validation_error(self):
        messages = list(self.errors)
        messages.extend(f"warning: {warning}" for warning in self.warnings)
        return "; ".join(messages) or None


def parsed_identifier(packet, field_name):
    if not isinstance(packet, dict):
        return None

    value = packet.get(field_name)
    if value is None:
        return None

    parsed = str(value).strip()
    return parsed or None


def parse_packet_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None

    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = f"{candidate[:-1]}+00:00"

    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.astimezone()

    return parsed.astimezone(timezone.utc)


def validate_packet(
    packet: Any,
    *,
    now_utc: Optional[datetime] = None,
) -> PacketValidationResult:
    errors = []
    warnings = []

    if not isinstance(packet, dict):
        return PacketValidationResult(
            errors=("packet must be a JSON object",),
            warnings=(),
        )

    patient_id = packet.get("patient_id")
    device_id = packet.get("device_id")

    if parsed_identifier(packet, "patient_id") is None:
        errors.append("missing patient_id")
    elif not isinstance(patient_id, str):
        errors.append("patient_id must be a string")

    if parsed_identifier(packet, "device_id") is None:
        errors.append("missing device_id")
    elif not isinstance(device_id, str):
        errors.append("device_id must be a string")

    for field_name in ("timestamp", "signal_quality", "status"):
        value = packet.get(field_name)
        if value is not None and not isinstance(value, str):
            errors.append(f"{field_name} must be a string or null")

    for field_name in ("device_type", "firmware_version", "device_send_time"):
        value = packet.get(field_name)
        if value is not None and not isinstance(value, str):
            errors.append(f"{field_name} must be a string or null")

    packet_sequence = packet.get("packet_sequence")
    if packet_sequence is not None and (
        isinstance(packet_sequence, bool)
        or not isinstance(packet_sequence, int)
        or packet_sequence < 0
    ):
        errors.append("packet_sequence must be a non-negative integer or null")

    device_send_time = packet.get("device_send_time")
    if isinstance(device_send_time, str):
        parsed_send_time = parse_packet_datetime(device_send_time)
        if parsed_send_time is None:
            errors.append("device_send_time must be an ISO-8601 timestamp")
        else:
            reference = now_utc or datetime.now(timezone.utc)
            if reference.tzinfo is None:
                reference = reference.replace(tzinfo=timezone.utc)
            reference = reference.astimezone(timezone.utc)
            age_seconds = (reference - parsed_send_time).total_seconds()
            if age_seconds > MAX_DEVICE_PACKET_AGE_SECONDS:
                errors.append(
                    "device_send_time is stale "
                    f"(older than {MAX_DEVICE_PACKET_AGE_SECONDS} seconds)"
                )
            elif age_seconds < -MAX_DEVICE_CLOCK_LEAD_SECONDS:
                errors.append(
                    "device_send_time is too far in the future "
                    f"(more than {MAX_DEVICE_CLOCK_LEAD_SECONDS} seconds)"
                )

    finger_detected = packet.get("finger_detected")
    if finger_detected is not None and not isinstance(finger_detected, bool):
        errors.append("finger_detected must be boolean or null")

    reasons = packet.get("reasons")
    if reasons is not None and (
        not isinstance(reasons, list)
        or any(not isinstance(reason, str) for reason in reasons)
    ):
        errors.append("reasons must be a list of strings or null")

    rr_source = packet.get("rr_source")
    if rr_source is not None and not isinstance(rr_source, str):
        errors.append("rr_source must be a string or null")

    rr_quality_value = packet.get("rr_quality")
    rr_quality = None
    if rr_quality_value is not None:
        if not isinstance(rr_quality_value, str):
            errors.append("rr_quality must be a string or null")
        else:
            rr_quality = rr_quality_value.strip().upper()
            if rr_quality not in RR_QUALITIES:
                errors.append(
                    "rr_quality must be one of " + ", ".join(sorted(RR_QUALITIES))
                )

    for field_name, (minimum, maximum) in NUMERIC_RANGES.items():
        value = packet.get(field_name)

        if value is None:
            continue

        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append(f"{field_name} must be numeric or null")
            continue

        if not math.isfinite(value):
            errors.append(f"{field_name} must be finite")
            continue

        if value < minimum or value > maximum:
            errors.append(
                f"{field_name} must be between {minimum} and {maximum}"
            )

    resp_rate = packet.get("resp_rate_bpm")
    rr_confidence = packet.get("rr_confidence")
    has_numeric_rr = (
        isinstance(resp_rate, (int, float))
        and not isinstance(resp_rate, bool)
        and math.isfinite(resp_rate)
    )
    has_numeric_confidence = (
        isinstance(rr_confidence, (int, float))
        and not isinstance(rr_confidence, bool)
        and math.isfinite(rr_confidence)
    )

    if has_numeric_rr:
        if rr_quality not in RELIABLE_RR_QUALITIES:
            errors.append(
                "resp_rate_bpm must be withheld (null) unless rr_quality is "
                "GOOD or MODERATE"
            )
        if not has_numeric_confidence:
            errors.append("numeric resp_rate_bpm requires numeric rr_confidence")
        elif rr_confidence < RR_CONFIDENCE_ELIGIBILITY_GATE:
            errors.append(
                "numeric resp_rate_bpm requires rr_confidence at or above "
                f"{RR_CONFIDENCE_ELIGIBILITY_GATE:.2f}"
            )
    elif resp_rate is None and rr_quality in RELIABLE_RR_QUALITIES:
        errors.append(
            "rr_quality GOOD or MODERATE requires numeric resp_rate_bpm"
        )

    if parsed_identifier(packet, "device_type") is None:
        warnings.append("missing optional device_type")

    if packet.get("packet_sequence") is None:
        warnings.append("missing optional packet_sequence")

    return PacketValidationResult(
        errors=tuple(errors),
        warnings=tuple(warnings),
    )
