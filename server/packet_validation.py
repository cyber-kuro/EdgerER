from dataclasses import dataclass
import math


VALIDATION_VERSION = "packet_validation_v1"

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


def validate_packet(packet):
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

    finger_detected = packet.get("finger_detected")
    if finger_detected is not None and not isinstance(finger_detected, bool):
        errors.append("finger_detected must be boolean or null")

    reasons = packet.get("reasons")
    if reasons is not None and (
        not isinstance(reasons, list)
        or any(not isinstance(reason, str) for reason in reasons)
    ):
        errors.append("reasons must be a list of strings or null")

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

    if parsed_identifier(packet, "device_type") is None:
        warnings.append("missing optional device_type")

    if packet.get("packet_sequence") is None:
        warnings.append("missing optional packet_sequence")

    return PacketValidationResult(
        errors=tuple(errors),
        warnings=tuple(warnings),
    )
