from datetime import datetime, timezone
import json
import time

import database
from packet_validation import (
    VALIDATION_VERSION,
    parsed_identifier,
    validate_packet,
)
import state
from models import (
    ACTION_DEESCALATION,
    ACTION_DISMISSAL,
    ACTION_ESCALATION,
    ACTION_RECHECK,
    ACTION_SEEN_BY_NURSE,
    DEVICE_STATUS_ACTIVE,
    DEVICE_STATUS_ASSIGNED,
    DEVICE_STATUS_AVAILABLE,
    DEVICE_STATUS_NEEDS_CLEANING,
    DEVICE_STATUS_OFFLINE,
    MAX_EVENTS_PER_PATIENT,
    MAX_HISTORY_PACKETS,
    OFFLINE_AFTER_SECONDS,
    RECHECK_AFTER_SECONDS,
    STATUS_CRASHING,
    STATUS_GRAY,
    STATUS_NO_DATA,
    STATUS_OFFLINE,
    STATUS_ORANGE,
    TREND_ELIGIBLE_STATUSES,
)


def format_duration(seconds):
    if seconds < 60:
        return f"{seconds} seconds"

    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minutes"

    hours = minutes // 60
    remaining_minutes = minutes % 60
    return f"{hours} hours {remaining_minutes} minutes"


def datetime_string():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def combine_messages(*messages):
    return "; ".join(message for message in messages if message) or None


RELIABLE_RR_QUALITIES = {"GOOD", "MODERATE"}


def format_research_rr_handoff(packet):
    rr_fields = (
        "resp_rate_bpm",
        "rr_source",
        "rr_confidence",
        "rr_quality",
    )
    if not any(field_name in packet for field_name in rr_fields):
        return "Respiratory rate: not reported"

    resp_rate = packet.get("resp_rate_bpm")
    source = packet.get("rr_source") or "UNAVAILABLE"
    quality = str(packet.get("rr_quality") or "UNAVAILABLE").upper()
    confidence = packet.get("rr_confidence")
    confidence_text = (
        f"{confidence:.2f}"
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
        else "unavailable"
    )

    if resp_rate is not None and quality in RELIABLE_RR_QUALITIES:
        return (
            f"Respiratory rate: {resp_rate:.1f} breaths/min, "
            f"source {source}, quality {quality}, confidence {confidence_text}"
        )

    return (
        f"Respiratory rate: withheld, source {source}, "
        f"quality {quality}, confidence {confidence_text}"
    )


def new_device(device_id, *, device_type="UNKNOWN", firmware_version="", location=""):
    now = database.utc_now_string()
    return {
        "device_id": device_id,
        "device_type": device_type or "UNKNOWN",
        "assigned_patient_id": None,
        "assigned_encounter_id": None,
        "status": DEVICE_STATUS_AVAILABLE,
        "battery_percent": None,
        "wifi_rssi": None,
        "last_seen": None,
        "firmware_version": firmware_version or "",
        "location": location or "",
        "created_at": now,
        "updated_at": now,
    }


def register_device(device_profile):
    device_id = str(device_profile.get("device_id") or "").strip()
    if not device_id:
        return {"ok": False, "error": "missing device_id"}

    device = state.devices.get(device_id)
    created = device is None

    if created:
        device = new_device(
            device_id,
            device_type=device_profile.get("device_type") or "UNKNOWN",
            firmware_version=device_profile.get("firmware_version") or "",
            location=device_profile.get("location") or "",
        )
        state.devices[device_id] = device
    else:
        device_type = device_profile.get("device_type")
        firmware_version = device_profile.get("firmware_version")
        location = device_profile.get("location")

        if device_type:
            device["device_type"] = device_type
        if firmware_version:
            device["firmware_version"] = firmware_version
        if location:
            device["location"] = location
        device["updated_at"] = database.utc_now_string()

    database.save_device(device)
    return {"ok": True, "created": created, "device": device.copy()}


def device_assignment_conflict(device_id, patient_id, encounter_id=None):
    device = state.devices.get(device_id)

    if device is not None:
        assigned_encounter_id = device.get("assigned_encounter_id")
        if assigned_encounter_id and assigned_encounter_id != encounter_id:
            assigned_encounter = state.encounters.get(assigned_encounter_id)
            if assigned_encounter and assigned_encounter.get("active"):
                return (
                    f"device {device_id} is assigned to active patient "
                    f"{device.get('assigned_patient_id')} encounter "
                    f"{assigned_encounter_id}"
                )

    if encounter_id is not None:
        encounter = state.encounters.get(encounter_id)
        assigned_device_id = (
            encounter.get("assigned_device_id", "") if encounter else ""
        )
        if assigned_device_id and assigned_device_id != device_id:
            return (
                f"patient {patient_id} encounter {encounter_id} is already "
                f"assigned to device {assigned_device_id}"
            )

    return None


def assign_device_to_encounter(device_id, patient_id, encounter_id):
    conflict = device_assignment_conflict(device_id, patient_id, encounter_id)
    if conflict:
        return {"ok": False, "error": conflict}

    encounter = state.encounters.get(encounter_id)
    if encounter is None or not encounter.get("active"):
        return {"ok": False, "error": "encounter is not active"}
    if encounter.get("patient_id") != patient_id:
        return {"ok": False, "error": "encounter does not belong to patient"}

    device = state.devices.get(device_id)
    if device is None:
        registration = register_device({"device_id": device_id})
        device = registration["device"]
        state.devices[device_id] = device

    now = database.utc_now_string()
    device["assigned_patient_id"] = patient_id
    device["assigned_encounter_id"] = encounter_id
    device["status"] = DEVICE_STATUS_ASSIGNED
    device["location"] = state.patient_profiles.get(patient_id, {}).get(
        "location",
        device.get("location", ""),
    )
    device["updated_at"] = now
    encounter["assigned_device_id"] = device_id

    profile = state.patient_profiles.get(patient_id)
    if profile is not None:
        profile["assigned_device_id"] = device_id

    database.save_device(device)
    database.save_encounter(encounter)
    if profile is not None:
        database.save_patient_profile(patient_id)

    return {"ok": True, "device": device.copy()}


def assign_device(assignment):
    device_id = str(assignment.get("device_id") or "").strip()
    patient_id = str(assignment.get("patient_id") or "").strip()
    encounter_id = assignment.get("encounter_id")

    if not device_id:
        return {"ok": False, "error": "missing device_id"}
    if not patient_id:
        return {"ok": False, "error": "missing patient_id"}

    active_encounter_id = get_active_encounter_id(patient_id)
    if active_encounter_id is None:
        return {"ok": False, "error": "patient has no active encounter"}
    if encounter_id is None:
        encounter_id = active_encounter_id
    if encounter_id != active_encounter_id:
        return {"ok": False, "error": "patient has no matching active encounter"}

    return assign_device_to_encounter(device_id, patient_id, encounter_id)


def update_device_from_packet(packet, received_at):
    device_id = packet["device_id"]
    patient_id = packet["patient_id"]
    encounter_id = packet["encounter_id"]
    device = state.devices.get(device_id)
    auto_registered = device is None

    if auto_registered:
        device = new_device(
            device_id,
            device_type=packet.get("device_type") or "UNKNOWN",
            firmware_version=packet.get("firmware_version") or "",
            location=state.patient_profiles.get(patient_id, {}).get(
                "location",
                "",
            ),
        )
        state.devices[device_id] = device

    assignment = assign_device_to_encounter(
        device_id,
        patient_id,
        encounter_id,
    )
    if not assignment["ok"]:
        raise RuntimeError(assignment["error"])

    device = state.devices[device_id]
    if packet.get("device_type"):
        device["device_type"] = packet["device_type"]
    if packet.get("battery_percent") is not None:
        device["battery_percent"] = packet["battery_percent"]
    if packet.get("wifi_rssi") is not None:
        device["wifi_rssi"] = packet["wifi_rssi"]
    if packet.get("firmware_version"):
        device["firmware_version"] = packet["firmware_version"]

    device["last_seen"] = received_at
    device["status"] = DEVICE_STATUS_ACTIVE
    device["updated_at"] = database.utc_now_string()
    database.save_device(device)
    return auto_registered


def release_device_from_encounter(encounter_id):
    encounter = state.encounters.get(encounter_id)
    if encounter is None:
        return None

    device_id = encounter.get("assigned_device_id", "")
    if not device_id:
        return None

    device = state.devices.get(device_id)
    if device is not None and device.get("assigned_encounter_id") == encounter_id:
        device["assigned_patient_id"] = None
        device["assigned_encounter_id"] = None
        device["status"] = DEVICE_STATUS_NEEDS_CLEANING
        device["updated_at"] = database.utc_now_string()
        database.save_device(device)

    profile = state.patient_profiles.get(encounter["patient_id"])
    if profile is not None and profile.get("assigned_device_id") == device_id:
        profile["assigned_device_id"] = ""

    return device_id


def refresh_device_offline_statuses():
    now = datetime.now(timezone.utc)

    for device in state.devices.values():
        if not device.get("assigned_encounter_id") or not device.get("last_seen"):
            continue

        try:
            last_seen = datetime.fromisoformat(device["last_seen"])
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue

        if (now - last_seen).total_seconds() > OFFLINE_AFTER_SECONDS:
            if device.get("status") != DEVICE_STATUS_OFFLINE:
                device["status"] = DEVICE_STATUS_OFFLINE
                device["updated_at"] = database.utc_now_string()
                database.save_device(device)


def get_devices():
    refresh_device_offline_statuses()
    return [
        state.devices[device_id].copy()
        for device_id in sorted(state.devices)
    ]


def patient_encounters(patient_id):
    return [
        encounter
        for encounter in state.encounters.values()
        if encounter["patient_id"] == patient_id
    ]


def get_active_encounter_id(patient_id):
    return state.active_encounters.get(patient_id)


def get_current_encounter_id(patient_id):
    active_encounter_id = get_active_encounter_id(patient_id)
    if active_encounter_id is not None:
        return active_encounter_id

    encounters = patient_encounters(patient_id)
    if not encounters:
        return None

    latest_encounter = max(
        encounters,
        key=lambda encounter: (
            encounter["created_at"],
            encounter["encounter_id"],
        ),
    )
    return latest_encounter["encounter_id"]


def create_active_encounter(patient_id, admission_time=None):
    existing_encounter_id = get_active_encounter_id(patient_id)
    if existing_encounter_id is not None:
        return existing_encounter_id

    if admission_time is None:
        admission_time = datetime_string()

    encounter_id = database.generate_encounter_id()
    encounter = {
        "encounter_id": encounter_id,
        "patient_id": patient_id,
        "admission_time": admission_time,
        "dismissal_time": None,
        "active": True,
        "created_at": database.utc_now_string(),
        "is_priority": False,
        "assigned_device_id": "",
    }

    state.encounters[encounter_id] = encounter
    state.active_encounters[patient_id] = encounter_id
    state.patient_admission_times[patient_id] = admission_time
    state.patient_dismissal_times.pop(patient_id, None)
    state.dismissed_patients.discard(patient_id)
    state.priority_patients.discard(patient_id)
    database.save_encounter(encounter)
    return encounter_id


def close_encounter(encounter_id, dismissal_time):
    encounter = state.encounters[encounter_id]
    encounter["active"] = False
    encounter["dismissal_time"] = dismissal_time
    encounter["is_priority"] = False
    state.active_encounters.pop(encounter["patient_id"], None)
    database.save_encounter(encounter)


def packet_received_at_string():
    return datetime.now(timezone.utc).isoformat()


def packet_latency_ms(started_at):
    if started_at is None:
        return None

    return round((time.perf_counter() - started_at) * 1000, 3)


def save_packet_audit(
    *,
    packet,
    raw_packet_json,
    received_at,
    endpoint,
    accepted,
    validation_error,
    history_logged,
    status_after_processing,
    started_at,
):
    parsed_patient_id = parsed_identifier(packet, "patient_id")
    encounter_id = None

    if isinstance(packet, dict):
        encounter_id = packet.get("encounter_id")

    if encounter_id is None and parsed_patient_id is not None:
        encounter_id = get_active_encounter_id(parsed_patient_id)

    rr_packet = packet if isinstance(packet, dict) else {}

    return database.save_packet_log(
        received_at=received_at or packet_received_at_string(),
        endpoint=endpoint,
        raw_packet_json=raw_packet_json,
        parsed_patient_id=parsed_patient_id,
        parsed_device_id=parsed_identifier(packet, "device_id"),
        encounter_id=encounter_id,
        resp_rate_bpm=rr_packet.get("resp_rate_bpm"),
        rr_source=rr_packet.get("rr_source"),
        rr_confidence=rr_packet.get("rr_confidence"),
        rr_quality=rr_packet.get("rr_quality"),
        accepted=accepted,
        validation_error=validation_error,
        history_logged=history_logged,
        status_after_processing=status_after_processing,
        latency_ms=packet_latency_ms(started_at),
        validation_version=VALIDATION_VERSION,
    )


def reject_vitals_packet(
    *,
    raw_packet_json,
    error,
    packet=None,
    received_at=None,
    endpoint="/api/vitals",
    started_at=None,
):
    save_packet_audit(
        packet=packet,
        raw_packet_json=raw_packet_json,
        received_at=received_at,
        endpoint=endpoint,
        accepted=False,
        validation_error=error,
        history_logged=None,
        status_after_processing=None,
        started_at=started_at,
    )

    return {
        "received": True,
        "accepted": False,
        "error": error,
    }


def add_patient_event(patient_id, event_type, note="", encounter_id=None):
    if encounter_id is None:
        encounter_id = get_current_encounter_id(patient_id)

    if patient_id not in state.patient_events:
        state.patient_events[patient_id] = []

    event = {
        "time": datetime_string(),
        "encounter_id": encounter_id,
        "event_type": event_type,
        "note": note,
    }

    state.patient_events[patient_id].append(event)

    encounter_events = [
        item
        for item in state.patient_events[patient_id]
        if item.get("encounter_id") == encounter_id
    ]

    if len(encounter_events) > MAX_EVENTS_PER_PATIENT:
        oldest_encounter_event = encounter_events[0]
        state.patient_events[patient_id].remove(oldest_encounter_event)

    database.save_patient_event(patient_id, event)

    return event


def analyze_trend(patient_id, encounter_id=None, history=None):
    if encounter_id is None:
        encounter_id = get_current_encounter_id(patient_id)

    if history is None:
        history = state.latest_packets

    patient_history = [
        packet for packet in history
        if packet.get("patient_id") == patient_id
        and packet.get("encounter_id") == encounter_id
        and packet.get("signal_quality") == "GOOD"
        and packet.get("finger_detected") is True
    ]

    recent = patient_history[-5:]

    if len(recent) < 5:
        return None

    hrs = [
        packet.get("heart_rate_bpm")
        for packet in recent
        if packet.get("heart_rate_bpm") is not None
    ]

    spo2s = [
        packet.get("spo2_percent")
        for packet in recent
        if packet.get("spo2_percent") is not None
    ]

    reasons = []

    if len(hrs) >= 5 and len(spo2s) >= 5:
        if hrs[-1] - hrs[0] >= 25 or spo2s[0] - spo2s[-1] >= 6:
            return {
                "status": STATUS_CRASHING,
                "reasons": ["rapid deterioration detected"],
            }

    if len(hrs) >= 5 and hrs[-1] - hrs[0] >= 15:
        reasons.append("heart rate rising trend")

    if len(spo2s) >= 5 and spo2s[0] - spo2s[-1] >= 3:
        reasons.append("oxygen saturation falling trend")

    if reasons:
        return {
            "status": STATUS_ORANGE,
            "reasons": reasons,
        }

    return None


def compute_patient_status(
    patient_id,
    packet,
    encounter_id=None,
    history=None,
):
    received_at = packet.get("_server_received_at")

    if received_at is None:
        return {
            "status": STATUS_NO_DATA,
            "reasons": ["registered, waiting for device data"],
            "seconds_since_seen": None,
        }

    seconds_since_seen = int(time.time() - received_at)

    if seconds_since_seen > OFFLINE_AFTER_SECONDS:
        return {
            "status": STATUS_OFFLINE,
            "reasons": [
                f"device offline for {format_duration(seconds_since_seen)}"
            ],
            "seconds_since_seen": seconds_since_seen,
        }

    if packet.get("finger_detected") is False:
        return {
            "status": STATUS_GRAY,
            "reasons": ["finger not detected / sensor not attached"],
            "seconds_since_seen": seconds_since_seen,
        }

    status = packet.get("status", "UNKNOWN")
    reasons = list(packet.get("reasons") or [])
    if encounter_id is None:
        encounter_id = packet.get("encounter_id")

    trend = analyze_trend(patient_id, encounter_id, history)

    if trend and status in TREND_ELIGIBLE_STATUSES:
        status = trend["status"]
        reasons.extend(trend["reasons"])

    return {
        "status": status,
        "reasons": reasons,
        "seconds_since_seen": seconds_since_seen,
    }


def receive_vitals(
    packet,
    *,
    raw_packet_json=None,
    received_at=None,
    endpoint="/api/vitals",
    started_at=None,
):
    if started_at is None:
        started_at = time.perf_counter()

    if received_at is None:
        received_at = packet_received_at_string()

    if raw_packet_json is None:
        raw_packet_json = json.dumps(packet, ensure_ascii=False)

    validation = validate_packet(packet)

    if not validation.accepted:
        return reject_vitals_packet(
            raw_packet_json=raw_packet_json,
            error=validation.validation_error,
            packet=packet,
            received_at=received_at,
            endpoint=endpoint,
            started_at=started_at,
        )

    packet["patient_id"] = packet["patient_id"].strip()
    packet["device_id"] = packet["device_id"].strip()
    patient_id = packet["patient_id"]
    device_id = packet["device_id"]
    active_encounter_id = get_active_encounter_id(patient_id)
    assignment_error = device_assignment_conflict(
        device_id,
        patient_id,
        active_encounter_id,
    )

    if assignment_error:
        return reject_vitals_packet(
            raw_packet_json=raw_packet_json,
            error=combine_messages(
                f"device assignment error: {assignment_error}",
                validation.validation_error,
            ),
            packet=packet,
            received_at=received_at,
            endpoint=endpoint,
            started_at=started_at,
        )

    packet["_server_received_at"] = time.time()

    profile_created = False
    encounter_created = False

    if patient_id not in state.patient_profiles:
        admission_time = datetime_string()
        state.patient_profiles[patient_id] = {
            "patient_id": patient_id,
            "display_name": patient_id,
            "age": "",
            "chief_complaint": "",
            "triage_level": "Unknown",
            "location": "",
            "assigned_device_id": device_id,
            "admission_time": admission_time,
        }
        profile_created = True

    encounter_id = get_active_encounter_id(patient_id)

    if encounter_id is None:
        admission_time = datetime_string()
        encounter_id = create_active_encounter(patient_id, admission_time)
        encounter_created = True
        state.patient_profiles[patient_id]["admission_time"] = admission_time

    packet["encounter_id"] = encounter_id

    auto_registered = update_device_from_packet(packet, received_at)

    state.patients[patient_id] = packet
    database.save_latest_patient_state(patient_id, packet)

    if profile_created or encounter_created:
        database.save_patient_profile(patient_id)

    finger_detected = packet.get("finger_detected")
    status = packet.get("status")
    previous_history = next(
        (
            history_packet
            for history_packet in reversed(state.latest_packets)
            if history_packet.get("patient_id") == patient_id
            and history_packet.get("encounter_id") == encounter_id
        ),
        None,
    )

    history_logged = True

    if finger_detected is False or status == STATUS_GRAY:
        if (
            previous_history
            and (
                previous_history.get("finger_detected") is False
                or previous_history.get("status") == STATUS_GRAY
            )
        ):
            print("Updated current state only, not history")
            history_logged = False

    if history_logged:
        state.latest_packets.append(packet)

        if len(state.latest_packets) > MAX_HISTORY_PACKETS:
            state.latest_packets.pop(0)

        database.append_history_packet(packet)
        print(packet)

    status_after_processing = compute_patient_status(
        patient_id,
        state.patients[patient_id],
        encounter_id,
    )["status"]

    save_packet_audit(
        packet=packet,
        raw_packet_json=raw_packet_json,
        received_at=received_at,
        endpoint=endpoint,
        accepted=True,
        validation_error=combine_messages(
            validation.validation_error,
            "warning: unknown device auto-registered"
            if auto_registered
            else None,
        ),
        history_logged=history_logged,
        status_after_processing=status_after_processing,
        started_at=started_at,
    )

    return {
        "received": True,
        "accepted": True,
        "logged": history_logged,
    }


def get_latest():
    if not state.latest_packets:
        return {"status": STATUS_OFFLINE, "reasons": ["no data received yet"]}

    packet = state.latest_packets[-1]
    seconds_since_seen = time.time() - packet["_server_received_at"]

    if seconds_since_seen > OFFLINE_AFTER_SECONDS:
        offline_packet = packet.copy()
        offline_packet["status"] = STATUS_OFFLINE
        offline_packet["reasons"] = [
            f"device offline for {int(seconds_since_seen)} seconds"
        ]
        offline_packet["seconds_since_seen"] = int(seconds_since_seen)
        return offline_packet

    packet["seconds_since_seen"] = int(seconds_since_seen)
    return packet


def get_history():
    return [
        packet
        for packet in state.latest_packets
        if packet.get("encounter_id")
        == get_current_encounter_id(packet.get("patient_id"))
    ]


def get_packet_logs(limit=50):
    return database.get_recent_packet_logs(limit)


def get_patients():
    refresh_device_offline_statuses()
    output = []

    for patient_id, packet in state.patients.items():
        encounter_id = get_active_encounter_id(patient_id)

        if encounter_id is None or patient_id in state.dismissed_patients:
            continue

        if packet.get("encounter_id") != encounter_id:
            continue

        patient = packet.copy()
        computed_status = compute_patient_status(
            patient_id,
            packet,
            encounter_id,
        )
        patient.update(computed_status)
        patient["encounter_id"] = encounter_id

        patient["priority"] = patient_id in state.priority_patients

        due_time = state.recheck_due_at.get(encounter_id)
        patient["recheck_due"] = False
        patient["recheck_seconds_left"] = None

        if due_time is not None:
            seconds_left = int(due_time - time.time())

            if seconds_left <= 0:
                patient["recheck_due"] = True
                patient["recheck_seconds_left"] = 0
            else:
                patient["recheck_seconds_left"] = seconds_left

        profile = state.patient_profiles.get(patient_id, {})

        patient["display_name"] = profile.get("display_name", patient_id)
        patient["age"] = profile.get("age", "")
        patient["chief_complaint"] = profile.get("chief_complaint", "")
        patient["triage_level"] = profile.get("triage_level") or "Unknown"
        patient["location"] = profile.get("location", "")
        encounter = state.encounters[encounter_id]
        patient["assigned_device_id"] = encounter.get(
            "assigned_device_id",
            profile.get("assigned_device_id", patient.get("device_id", "")),
        )
        patient["admission_time"] = encounter["admission_time"]
        patient["dismissal_time"] = encounter["dismissal_time"] or "Pending"

        output.append(patient)

    return output


def add_action(action):
    patient_id = action.get("patient_id")
    action_type = action.get("action_type")
    note = action.get("note", "")

    if not patient_id:
        return {
            "ok": False,
            "error": "missing patient_id",
        }

    if not action_type:
        return {
            "ok": False,
            "error": "missing action_type",
        }

    encounter_id = get_active_encounter_id(patient_id)

    if encounter_id is None:
        return {
            "ok": False,
            "error": "patient has no active encounter",
        }

    encounter = state.encounters[encounter_id]

    if action_type == ACTION_ESCALATION:
        state.priority_patients.add(patient_id)
        encounter["is_priority"] = True

    if action_type == ACTION_DEESCALATION:
        if patient_id not in state.priority_patients:
            return {
                "ok": False,
                "ignored": True,
                "error": "patient is not in Priority Patients",
            }

        state.priority_patients.discard(patient_id)
        encounter["is_priority"] = False

        state.recheck_due_at.pop(encounter_id, None)

    if action_type == ACTION_DISMISSAL:
        dismissal_time = datetime_string()
        state.dismissed_patients.add(patient_id)
        state.patient_dismissal_times[patient_id] = dismissal_time
        state.priority_patients.discard(patient_id)
        state.recheck_due_at.pop(encounter_id, None)
        release_device_from_encounter(encounter_id)
        close_encounter(encounter_id, dismissal_time)

    if action_type == ACTION_RECHECK:
        state.recheck_due_at[encounter_id] = time.time() + RECHECK_AFTER_SECONDS

    if action_type == ACTION_SEEN_BY_NURSE:
        state.recheck_due_at.pop(encounter_id, None)

    database.save_encounter(encounter)
    database.save_patient_profile(patient_id)
    database.save_recheck_timer(patient_id, encounter_id)

    event = add_patient_event(
        patient_id=patient_id,
        event_type=action_type,
        note=note,
        encounter_id=encounter_id,
    )

    return {
        "ok": True,
        "event": event,
    }


def get_patient_events(patient_id):
    encounter_id = get_current_encounter_id(patient_id)
    return [
        event
        for event in state.patient_events.get(patient_id, [])
        if event.get("encounter_id") == encounter_id
    ]


def get_encounters(patient_id):
    return database.get_encounters(patient_id)


def get_handoff(patient_id, encounter_id=None):
    if encounter_id is None:
        encounter_id = get_current_encounter_id(patient_id)

    encounter = state.encounters.get(encounter_id)

    if encounter is None or encounter["patient_id"] != patient_id:
        return {
            "ok": False,
            "error": "encounter not found",
        }

    encounter_history = database.get_encounter_history(encounter_id)
    current_latest = state.patients.get(patient_id)

    if (
        current_latest is not None
        and current_latest.get("encounter_id") == encounter_id
    ):
        latest = current_latest
    elif encounter_history:
        latest = encounter_history[-1]
    else:
        latest = {
            "patient_id": patient_id,
            "encounter_id": encounter_id,
            "device_id": "",
            "_server_received_at": None,
            "heart_rate_bpm": None,
            "spo2_percent": None,
            "object_temp_c": None,
            "finger_detected": None,
            "signal_quality": "NO_DATA",
            "status": STATUS_NO_DATA,
            "reasons": ["registered, waiting for device data"],
        }

    computed_status = compute_patient_status(
        patient_id,
        latest,
        encounter_id,
        encounter_history,
    )
    profile = state.patient_profiles.get(patient_id, {})

    display_name = profile.get("display_name", patient_id)
    age = profile.get("age", "")
    chief_complaint = profile.get("chief_complaint", "")
    triage_level = profile.get("triage_level") or "Unknown"
    location = profile.get("location", "")
    assigned_device_id = (
        encounter.get("assigned_device_id")
        or latest.get("device_id", "")
    )
    assigned_device = state.devices.get(assigned_device_id, {})
    assigned_device_type = assigned_device.get("device_type") or "UNKNOWN"

    admission_time = encounter["admission_time"]
    dismissal_time = encounter["dismissal_time"] or "Pending"

    patient_history = [
        packet for packet in encounter_history
        if packet.get("finger_detected") is True
        and packet.get("signal_quality") == "GOOD"
    ]

    events = [
        event
        for event in state.patient_events.get(patient_id, [])
        if event.get("encounter_id") == encounter_id
    ]
    status = computed_status["status"]
    reasons = computed_status["reasons"]

    hr = latest.get("heart_rate_bpm")
    spo2 = latest.get("spo2_percent")
    temp = latest.get("object_temp_c")
    rr_handoff_line = format_research_rr_handoff(latest)

    trend_lines = []

    if len(patient_history) >= 2:
        first = patient_history[0]
        last = patient_history[-1]

        first_hr = first.get("heart_rate_bpm")
        last_hr = last.get("heart_rate_bpm")
        first_spo2 = first.get("spo2_percent")
        last_spo2 = last.get("spo2_percent")

        if first_hr is not None and last_hr is not None:
            hr_change = round(last_hr - first_hr, 1)

            if hr_change > 0:
                trend_lines.append(f"Heart rate increased by {hr_change} bpm.")
            elif hr_change < 0:
                trend_lines.append(
                    f"Heart rate decreased by {abs(hr_change)} bpm."
                )
            else:
                trend_lines.append("Heart rate remained stable.")

        if first_spo2 is not None and last_spo2 is not None:
            spo2_change = round(last_spo2 - first_spo2, 1)

            if spo2_change < 0:
                trend_lines.append(f"SpO₂ dropped by {abs(spo2_change)}%.")
            elif spo2_change > 0:
                trend_lines.append(f"SpO₂ increased by {spo2_change}%.")
            else:
                trend_lines.append("SpO₂ remained stable.")

    if not trend_lines:
        trend_lines.append("Not enough stable readings to determine trend.")

    recent_events = events[-8:]
    event_lines = []

    if recent_events:
        for event in recent_events:
            note = event.get("note", "")
            if note:
                event_lines.append(
                    f"{event.get('time')} — {event.get('event_type')}: {note}"
                )
            else:
                event_lines.append(
                    f"{event.get('time')} — {event.get('event_type')}"
                )
    else:
        event_lines.append("No workflow events recorded yet.")

    if status in ["RED", STATUS_CRASHING]:
        recommendation = "Immediate reassessment / provider review recommended."
    elif status == STATUS_ORANGE:
        recommendation = "Priority reassessment recommended due to worsening trend."
    elif status == "YELLOW":
        recommendation = "Routine reassessment recommended."
    elif status == STATUS_GRAY:
        recommendation = "Sensor contact should be checked."
    elif status == STATUS_OFFLINE:
        recommendation = "Device connection should be checked."
    else:
        recommendation = "Patient currently appears stable."

    summary = f"""
Patient {patient_id} Handoff Summary

Encounter ID:
{encounter_id}

Patient profile:
Name/code: {display_name}
Age: {age if age else "--"}
Chief complaint: {chief_complaint if chief_complaint else "--"}
Triage level: {triage_level if triage_level else "--"}
Location: {location if location else "--"}
Assigned device: {assigned_device_id if assigned_device_id else "--"}
Device type: {assigned_device_type if assigned_device_id else "--"}

Admission time:
{admission_time}

Dismissal time:
{dismissal_time}

Current status:
{status}

Reasons:
{", ".join(reasons) if reasons else "No active warning reasons."}

Latest vitals:
Heart rate: {hr if hr is not None else "--"} bpm
SpO₂: {spo2 if spo2 is not None else "--"} %
Temperature: {temp if temp is not None else "--"} °C

Research RR (PPG-derived; informational only):
{rr_handoff_line}
Not used to determine EdgeER priority status.

Recent trend:
{chr(10).join(trend_lines)}

Recent timeline:
{chr(10).join(event_lines)}

Suggested attention:
{recommendation}
""".strip()

    return {
        "ok": True,
        "patient_id": patient_id,
        "encounter_id": encounter_id,
        "assigned_device_id": assigned_device_id,
        "assigned_device_type": (
            assigned_device_type if assigned_device_id else None
        ),
        "summary": summary,
    }


def register_patient(profile):
    patient_id = str(profile.get("patient_id") or "").strip()

    if not patient_id:
        return {
            "ok": False,
            "error": "missing patient_id",
        }

    active_encounter_id = get_active_encounter_id(patient_id)
    had_previous_encounter = bool(patient_encounters(patient_id))
    is_readmission = active_encounter_id is None and had_previous_encounter
    requested_device_id = str(
        profile.get("assigned_device_id") or ""
    ).strip()
    existing_device_id = ""

    if active_encounter_id is not None:
        existing_device_id = state.encounters[active_encounter_id].get(
            "assigned_device_id",
            "",
        )

    assigned_device_id = requested_device_id or existing_device_id

    if assigned_device_id:
        assignment_error = device_assignment_conflict(
            assigned_device_id,
            patient_id,
            active_encounter_id,
        )
        if assignment_error:
            return {
                "ok": False,
                "error": f"device assignment error: {assignment_error}",
            }

    if active_encounter_id is None:
        admission_time = datetime_string()
        encounter_id = create_active_encounter(patient_id, admission_time)
        should_save_placeholder = True
    else:
        encounter_id = active_encounter_id
        admission_time = state.encounters[encounter_id]["admission_time"]
        should_save_placeholder = patient_id not in state.patients

    state.patient_profiles[patient_id] = {
        "patient_id": patient_id,
        "display_name": profile.get("display_name", patient_id),
        "age": profile.get("age", ""),
        "chief_complaint": profile.get("chief_complaint", ""),
        "triage_level": profile.get("triage_level") or "Unknown",
        "location": profile.get("location", ""),
        "assigned_device_id": assigned_device_id,
        "admission_time": admission_time,
    }

    if should_save_placeholder:
        state.patients[patient_id] = {
            "patient_id": patient_id,
            "encounter_id": encounter_id,
            "device_id": assigned_device_id,
            "timestamp": admission_time,
            "_server_received_at": None,
            "heart_rate_bpm": None,
            "spo2_percent": None,
            "object_temp_c": None,
            "finger_detected": None,
            "signal_quality": "NO_DATA",
            "status": STATUS_NO_DATA,
            "reasons": ["registered, waiting for device data"],
        }

    database.save_patient_profile(patient_id)

    if assigned_device_id:
        assignment = assign_device_to_encounter(
            assigned_device_id,
            patient_id,
            encounter_id,
        )
        if not assignment["ok"]:
            return assignment

    if should_save_placeholder:
        database.save_latest_patient_state(
            patient_id,
            state.patients[patient_id],
        )

    database.save_encounter(state.encounters[encounter_id])
    database.save_recheck_timer(patient_id, encounter_id)

    add_patient_event(
        patient_id=patient_id,
        event_type="Patient readmitted" if is_readmission else "Patient registered",
        note=(
            f"Complaint: {profile.get('chief_complaint', '')}, "
            f"Location: {profile.get('location', '')}"
        ),
        encounter_id=encounter_id,
    )

    return {
        "ok": True,
        "profile": state.patient_profiles[patient_id],
        "encounter_id": encounter_id,
    }
