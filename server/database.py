from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import uuid

import state
from models import DEVICE_STATUSES, MAX_EVENTS_PER_PATIENT, MAX_HISTORY_PACKETS


DATABASE_PATH = Path(
    os.environ.get(
        "EDGEER_DATABASE_PATH",
        str(Path(__file__).resolve().with_name("edgeer.db")),
    )
)


def utc_now_string():
    return datetime.now(timezone.utc).isoformat()


def generate_encounter_id():
    return f"ENC-{uuid.uuid4()}"


def epoch_to_utc_string(value):
    if value is None:
        return None

    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def table_columns(connection, table_name):
    return {
        row["name"]
        for row in connection.execute(
            f"PRAGMA table_info({table_name})"
        ).fetchall()
    }


def ensure_column(connection, table_name, column_name, column_type):
    if column_name not in table_columns(connection, table_name):
        connection.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
        )


@contextmanager
def connect():
    connection = sqlite3.connect(DATABASE_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")

    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def migrate_existing_encounters(connection):
    now = utc_now_string()
    profile_rows = connection.execute(
        """
        SELECT
            patient_id,
            admission_time,
            dismissal_time,
            is_dismissed,
            is_priority
        FROM patient_profiles
        """
    ).fetchall()

    for row in profile_rows:
        existing = connection.execute(
            "SELECT encounter_id FROM encounters WHERE patient_id = ? LIMIT 1",
            (row["patient_id"],),
        ).fetchone()

        if existing is not None:
            continue

        active = 0 if row["is_dismissed"] else 1
        admission_time = row["admission_time"] or now
        connection.execute(
            """
            INSERT INTO encounters (
                encounter_id,
                patient_id,
                admission_time,
                dismissal_time,
                active,
                created_at,
                is_priority
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                generate_encounter_id(),
                row["patient_id"],
                admission_time,
                row["dismissal_time"],
                active,
                now,
                int(bool(row["is_priority"])) if active else 0,
            ),
        )

    patient_rows = connection.execute(
        """
        SELECT patient_id FROM latest_patient_state
        UNION
        SELECT patient_id FROM patient_events
        UNION
        SELECT patient_id FROM recent_vitals
        UNION
        SELECT patient_id FROM recheck_timers
        """
    ).fetchall()

    for row in patient_rows:
        patient_id = row["patient_id"]
        existing = connection.execute(
            "SELECT encounter_id FROM encounters WHERE patient_id = ? LIMIT 1",
            (patient_id,),
        ).fetchone()

        if existing is None:
            connection.execute(
                """
                INSERT INTO encounters (
                    encounter_id,
                    patient_id,
                    admission_time,
                    dismissal_time,
                    active,
                    created_at,
                    is_priority
                )
                VALUES (?, ?, ?, NULL, 1, ?, 0)
                """,
                (generate_encounter_id(), patient_id, now, now),
            )

    for table_name in (
        "patient_events",
        "latest_patient_state",
        "recent_vitals",
        "recheck_timers",
    ):
        connection.execute(
            f"""
            UPDATE {table_name}
            SET encounter_id = (
                SELECT encounter_id
                FROM encounters
                WHERE encounters.patient_id = {table_name}.patient_id
                ORDER BY active DESC, created_at DESC
                LIMIT 1
            )
            WHERE encounter_id IS NULL
            """
        )

    connection.execute(
        """
        UPDATE packet_logs
        SET encounter_id = (
            SELECT encounter_id
            FROM encounters
            WHERE encounters.patient_id = packet_logs.parsed_patient_id
            ORDER BY active DESC, created_at DESC
            LIMIT 1
        )
        WHERE encounter_id IS NULL
        AND parsed_patient_id IS NOT NULL
        """
    )


def migrate_existing_devices(connection):
    now = utc_now_string()

    connection.execute(
        """
        UPDATE encounters
        SET assigned_device_id = COALESCE(
            NULLIF(assigned_device_id, ''),
            (
                SELECT NULLIF(latest_patient_state.device_id, '')
                FROM latest_patient_state
                WHERE latest_patient_state.encounter_id = encounters.encounter_id
                LIMIT 1
            ),
            CASE WHEN active = 1 THEN (
                SELECT NULLIF(patient_profiles.assigned_device_id, '')
                FROM patient_profiles
                WHERE patient_profiles.patient_id = encounters.patient_id
                LIMIT 1
            ) END,
            ''
        )
        """
    )

    device_rows = connection.execute(
        """
        SELECT assigned_device_id AS device_id FROM patient_profiles
        WHERE assigned_device_id <> ''
        UNION
        SELECT device_id FROM latest_patient_state
        WHERE device_id <> ''
        UNION
        SELECT assigned_device_id FROM encounters
        WHERE assigned_device_id <> ''
        """
    ).fetchall()

    for row in device_rows:
        device_id = row["device_id"]
        latest = connection.execute(
            """
            SELECT packet_json, server_received_at
            FROM latest_patient_state
            WHERE device_id = ?
            ORDER BY server_received_at DESC
            LIMIT 1
            """,
            (device_id,),
        ).fetchone()
        active_assignment = connection.execute(
            """
            SELECT encounters.patient_id, encounters.encounter_id,
                   patient_profiles.location
            FROM encounters
            LEFT JOIN patient_profiles
                ON patient_profiles.patient_id = encounters.patient_id
            WHERE encounters.assigned_device_id = ?
            AND encounters.active = 1
            ORDER BY encounters.created_at DESC
            LIMIT 1
            """,
            (device_id,),
        ).fetchone()
        closed_assignment = connection.execute(
            """
            SELECT 1 FROM encounters
            WHERE assigned_device_id = ? AND active = 0
            LIMIT 1
            """,
            (device_id,),
        ).fetchone()

        packet = {}
        last_seen = None
        if latest is not None:
            try:
                packet = json.loads(latest["packet_json"])
            except (TypeError, json.JSONDecodeError):
                packet = {}
            last_seen = epoch_to_utc_string(latest["server_received_at"])

        if active_assignment is not None:
            assigned_patient_id = active_assignment["patient_id"]
            assigned_encounter_id = active_assignment["encounter_id"]
            status = "ACTIVE" if last_seen is not None else "ASSIGNED"
            location = active_assignment["location"] or ""
        else:
            assigned_patient_id = None
            assigned_encounter_id = None
            status = "NEEDS_CLEANING" if closed_assignment else "AVAILABLE"
            location = ""

        connection.execute(
            """
            INSERT OR IGNORE INTO devices (
                device_id,
                device_type,
                assigned_patient_id,
                assigned_encounter_id,
                status,
                battery_percent,
                wifi_rssi,
                last_seen,
                firmware_version,
                location,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                device_id,
                packet.get("device_type") or "UNKNOWN",
                assigned_patient_id,
                assigned_encounter_id,
                status,
                packet.get("battery_percent"),
                packet.get("wifi_rssi"),
                last_seen,
                packet.get("firmware_version") or "",
                location,
                now,
                now,
            ),
        )


def initialize_database():
    with connect() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS patient_profiles (
                patient_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL DEFAULT '',
                age TEXT NOT NULL DEFAULT '',
                chief_complaint TEXT NOT NULL DEFAULT '',
                triage_level TEXT NOT NULL DEFAULT 'Unknown',
                location TEXT NOT NULL DEFAULT '',
                assigned_device_id TEXT NOT NULL DEFAULT '',
                admission_time TEXT,
                dismissal_time TEXT,
                is_dismissed INTEGER NOT NULL DEFAULT 0,
                is_priority INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS patient_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id TEXT NOT NULL,
                encounter_id TEXT,
                event_time TEXT NOT NULL,
                event_type TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_patient_events_patient
            ON patient_events (patient_id, id);

            CREATE TABLE IF NOT EXISTS latest_patient_state (
                patient_id TEXT PRIMARY KEY,
                encounter_id TEXT,
                device_id TEXT NOT NULL DEFAULT '',
                latest_timestamp TEXT,
                server_received_at REAL,
                heart_rate_bpm REAL,
                spo2_percent REAL,
                object_temp_c REAL,
                resp_rate_bpm REAL,
                rr_source TEXT,
                rr_confidence REAL,
                rr_quality TEXT,
                finger_detected INTEGER,
                signal_quality TEXT,
                status TEXT,
                reasons_json TEXT NOT NULL DEFAULT '[]',
                packet_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS recent_vitals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id TEXT NOT NULL,
                encounter_id TEXT,
                server_received_at REAL NOT NULL,
                packet_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_recent_vitals_patient_time
            ON recent_vitals (patient_id, server_received_at);

            CREATE TABLE IF NOT EXISTS packet_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                raw_packet_json TEXT,
                parsed_patient_id TEXT,
                parsed_device_id TEXT,
                encounter_id TEXT,
                resp_rate_bpm REAL,
                rr_source TEXT,
                rr_confidence REAL,
                rr_quality TEXT,
                accepted INTEGER NOT NULL,
                validation_error TEXT,
                history_logged INTEGER,
                status_after_processing TEXT,
                latency_ms REAL,
                validation_version TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_packet_logs_received_at
            ON packet_logs (received_at);

            CREATE TABLE IF NOT EXISTS encounters (
                encounter_id TEXT PRIMARY KEY,
                patient_id TEXT NOT NULL,
                admission_time TEXT NOT NULL,
                dismissal_time TEXT,
                active INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                is_priority INTEGER NOT NULL DEFAULT 0,
                assigned_device_id TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                device_type TEXT NOT NULL DEFAULT 'UNKNOWN',
                assigned_patient_id TEXT,
                assigned_encounter_id TEXT,
                status TEXT NOT NULL DEFAULT 'AVAILABLE' CHECK (
                    status IN (
                        'AVAILABLE',
                        'ASSIGNED',
                        'ACTIVE',
                        'OFFLINE',
                        'LOW_BATTERY',
                        'SENSOR_FAULT',
                        'NEEDS_CLEANING'
                    )
                ),
                battery_percent REAL,
                wifi_rssi REAL,
                last_seen TEXT,
                firmware_version TEXT NOT NULL DEFAULT '',
                location TEXT NOT NULL DEFAULT '',
                last_packet_sequence INTEGER,
                last_device_send_time TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                retired_at TEXT,
                retired_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS recheck_timers (
                patient_id TEXT PRIMARY KEY,
                encounter_id TEXT,
                due_at REAL NOT NULL
            );
            """
        )

        ensure_column(connection, "patient_events", "encounter_id", "TEXT")
        ensure_column(connection, "latest_patient_state", "encounter_id", "TEXT")
        ensure_column(connection, "latest_patient_state", "resp_rate_bpm", "REAL")
        ensure_column(connection, "latest_patient_state", "rr_source", "TEXT")
        ensure_column(connection, "latest_patient_state", "rr_confidence", "REAL")
        ensure_column(connection, "latest_patient_state", "rr_quality", "TEXT")
        ensure_column(connection, "recent_vitals", "encounter_id", "TEXT")
        ensure_column(connection, "packet_logs", "encounter_id", "TEXT")
        ensure_column(connection, "packet_logs", "resp_rate_bpm", "REAL")
        ensure_column(connection, "packet_logs", "rr_source", "TEXT")
        ensure_column(connection, "packet_logs", "rr_confidence", "REAL")
        ensure_column(connection, "packet_logs", "rr_quality", "TEXT")
        ensure_column(connection, "recheck_timers", "encounter_id", "TEXT")
        ensure_column(connection, "devices", "last_packet_sequence", "INTEGER")
        ensure_column(connection, "devices", "last_device_send_time", "TEXT")
        ensure_column(
            connection,
            "devices",
            "is_active",
            "INTEGER NOT NULL DEFAULT 1",
        )
        ensure_column(connection, "devices", "retired_at", "TEXT")
        ensure_column(connection, "devices", "retired_reason", "TEXT")
        ensure_column(
            connection,
            "encounters",
            "assigned_device_id",
            "TEXT NOT NULL DEFAULT ''",
        )

        migrate_existing_encounters(connection)
        migrate_existing_devices(connection)

        connection.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_encounters_patient
            ON encounters (patient_id, created_at);

            CREATE UNIQUE INDEX IF NOT EXISTS idx_encounters_one_active_patient
            ON encounters (patient_id)
            WHERE active = 1;

            CREATE UNIQUE INDEX IF NOT EXISTS idx_encounters_one_active_device
            ON encounters (assigned_device_id)
            WHERE active = 1 AND assigned_device_id <> '';

            CREATE INDEX IF NOT EXISTS idx_patient_events_encounter
            ON patient_events (encounter_id, id);

            CREATE INDEX IF NOT EXISTS idx_recent_vitals_encounter_time
            ON recent_vitals (encounter_id, server_received_at);

            CREATE INDEX IF NOT EXISTS idx_packet_logs_encounter
            ON packet_logs (encounter_id, id);

            CREATE INDEX IF NOT EXISTS idx_recheck_timers_encounter
            ON recheck_timers (encounter_id);

            CREATE INDEX IF NOT EXISTS idx_devices_assignment
            ON devices (assigned_encounter_id, assigned_patient_id);
            """
        )


def clear_in_memory_state():
    state.latest_packets.clear()
    state.patients.clear()
    state.patient_events.clear()
    state.encounters.clear()
    state.active_encounters.clear()
    state.devices.clear()
    state.priority_patients.clear()
    state.recheck_due_at.clear()
    state.patient_admission_times.clear()
    state.patient_dismissal_times.clear()
    state.dismissed_patients.clear()
    state.patient_profiles.clear()


def load_state():
    clear_in_memory_state()

    with connect() as connection:
        encounter_rows = connection.execute(
            """
            SELECT
                encounter_id,
                patient_id,
                admission_time,
                dismissal_time,
                active,
                created_at,
                is_priority,
                assigned_device_id
            FROM encounters
            ORDER BY created_at, rowid
            """
        ).fetchall()

        for row in encounter_rows:
            encounter = {
                "encounter_id": row["encounter_id"],
                "patient_id": row["patient_id"],
                "admission_time": row["admission_time"],
                "dismissal_time": row["dismissal_time"],
                "active": bool(row["active"]),
                "created_at": row["created_at"],
                "is_priority": bool(row["is_priority"]),
                "assigned_device_id": row["assigned_device_id"] or "",
            }
            state.encounters[row["encounter_id"]] = encounter

            if encounter["active"]:
                state.active_encounters[row["patient_id"]] = row["encounter_id"]
                if encounter["is_priority"]:
                    state.priority_patients.add(row["patient_id"])

        device_rows = connection.execute(
            "SELECT * FROM devices ORDER BY device_id"
        ).fetchall()

        for row in device_rows:
            state.devices[row["device_id"]] = {
                "device_id": row["device_id"],
                "device_type": row["device_type"] or "UNKNOWN",
                "assigned_patient_id": row["assigned_patient_id"],
                "assigned_encounter_id": row["assigned_encounter_id"],
                "status": row["status"],
                "battery_percent": row["battery_percent"],
                "wifi_rssi": row["wifi_rssi"],
                "last_seen": row["last_seen"],
                "firmware_version": row["firmware_version"] or "",
                "location": row["location"] or "",
                "last_packet_sequence": row["last_packet_sequence"],
                "last_device_send_time": row["last_device_send_time"],
                "is_active": bool(row["is_active"]),
                "retired_at": row["retired_at"],
                "retired_reason": row["retired_reason"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }

        profile_rows = connection.execute(
            "SELECT * FROM patient_profiles ORDER BY rowid"
        ).fetchall()

        for row in profile_rows:
            patient_id = row["patient_id"]
            state.patient_profiles[patient_id] = {
                "patient_id": patient_id,
                "display_name": row["display_name"],
                "age": row["age"],
                "chief_complaint": row["chief_complaint"],
                "triage_level": row["triage_level"] or "Unknown",
                "location": row["location"],
                "assigned_device_id": row["assigned_device_id"],
                "admission_time": row["admission_time"],
            }

            active_encounter_id = state.active_encounters.get(patient_id)
            patient_encounters = [
                encounter
                for encounter in state.encounters.values()
                if encounter["patient_id"] == patient_id
            ]

            if active_encounter_id is not None:
                current_encounter = state.encounters[active_encounter_id]
            elif patient_encounters:
                current_encounter = max(
                    patient_encounters,
                    key=lambda encounter: (
                        encounter["created_at"],
                        encounter["encounter_id"],
                    ),
                )
            else:
                current_encounter = None

            if current_encounter is not None:
                state.patient_admission_times[patient_id] = current_encounter[
                    "admission_time"
                ]

                if current_encounter["dismissal_time"] is not None:
                    state.patient_dismissal_times[patient_id] = current_encounter[
                        "dismissal_time"
                    ]

                if not current_encounter["active"]:
                    state.dismissed_patients.add(patient_id)
            else:
                if row["admission_time"] is not None:
                    state.patient_admission_times[patient_id] = row[
                        "admission_time"
                    ]

                if row["dismissal_time"] is not None:
                    state.patient_dismissal_times[patient_id] = row[
                        "dismissal_time"
                    ]

                if row["is_dismissed"]:
                    state.dismissed_patients.add(patient_id)

        history_rows = connection.execute(
            """
            SELECT encounter_id, packet_json
            FROM (
                SELECT
                    recent_vitals.id,
                    recent_vitals.encounter_id,
                    recent_vitals.packet_json
                FROM recent_vitals
                JOIN encounters
                    ON encounters.encounter_id = recent_vitals.encounter_id
                WHERE encounters.active = 1
                ORDER BY id DESC
                LIMIT ?
            )
            ORDER BY id
            """,
            (MAX_HISTORY_PACKETS,),
        ).fetchall()

        for row in history_rows:
            packet = json.loads(row["packet_json"])
            packet["encounter_id"] = row["encounter_id"]
            state.latest_packets.append(packet)

        history_packets_by_key = {
            (
                packet.get("patient_id"),
                packet.get("_server_received_at"),
            ): packet
            for packet in state.latest_packets
        }

        latest_rows = connection.execute(
            "SELECT * FROM latest_patient_state ORDER BY rowid"
        ).fetchall()

        for row in latest_rows:
            packet = json.loads(row["packet_json"])
            packet["_server_received_at"] = row["server_received_at"]
            packet["encounter_id"] = row["encounter_id"]
            packet.setdefault("patient_id", row["patient_id"])
            packet.setdefault("device_id", row["device_id"])
            packet.setdefault("timestamp", row["latest_timestamp"])
            packet.setdefault("heart_rate_bpm", row["heart_rate_bpm"])
            packet.setdefault("spo2_percent", row["spo2_percent"])
            packet.setdefault("object_temp_c", row["object_temp_c"])
            packet.setdefault("resp_rate_bpm", row["resp_rate_bpm"])
            packet.setdefault("rr_source", row["rr_source"])
            packet.setdefault("rr_confidence", row["rr_confidence"])
            packet.setdefault("rr_quality", row["rr_quality"])
            packet.setdefault("signal_quality", row["signal_quality"])
            packet.setdefault("status", row["status"])
            packet.setdefault("reasons", json.loads(row["reasons_json"] or "[]"))

            if "finger_detected" not in packet:
                packet["finger_detected"] = (
                    None
                    if row["finger_detected"] is None
                    else bool(row["finger_detected"])
                )

            history_packet = history_packets_by_key.get(
                (row["patient_id"], row["server_received_at"])
            )
            state.patients[row["patient_id"]] = history_packet or packet

        event_rows = connection.execute(
            """
            SELECT patient_id, encounter_id, event_time, event_type, note
            FROM patient_events
            ORDER BY id
            """
        ).fetchall()

        for row in event_rows:
            patient_id = row["patient_id"]
            state.patient_events.setdefault(patient_id, []).append(
                {
                    "time": row["event_time"],
                    "encounter_id": row["encounter_id"],
                    "event_type": row["event_type"],
                    "note": row["note"],
                }
            )

        recheck_rows = connection.execute(
            """
            SELECT recheck_timers.encounter_id, recheck_timers.due_at
            FROM recheck_timers
            JOIN encounters
                ON encounters.encounter_id = recheck_timers.encounter_id
            WHERE encounters.active = 1
            """
        ).fetchall()

        for row in recheck_rows:
            state.recheck_due_at[row["encounter_id"]] = row["due_at"]


def save_patient_profile(patient_id):
    profile = state.patient_profiles.get(patient_id, {})

    with connect() as connection:
        connection.execute(
            """
            INSERT INTO patient_profiles (
                patient_id,
                display_name,
                age,
                chief_complaint,
                triage_level,
                location,
                assigned_device_id,
                admission_time,
                dismissal_time,
                is_dismissed,
                is_priority
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(patient_id) DO UPDATE SET
                display_name = excluded.display_name,
                age = excluded.age,
                chief_complaint = excluded.chief_complaint,
                triage_level = excluded.triage_level,
                location = excluded.location,
                assigned_device_id = excluded.assigned_device_id,
                admission_time = excluded.admission_time,
                dismissal_time = excluded.dismissal_time,
                is_dismissed = excluded.is_dismissed,
                is_priority = excluded.is_priority
            """,
            (
                patient_id,
                profile.get("display_name", patient_id),
                profile.get("age", ""),
                profile.get("chief_complaint", ""),
                profile.get("triage_level") or "Unknown",
                profile.get("location", ""),
                profile.get("assigned_device_id", ""),
                state.patient_admission_times.get(patient_id),
                state.patient_dismissal_times.get(patient_id),
                int(patient_id in state.dismissed_patients),
                int(patient_id in state.priority_patients),
            ),
        )


def save_encounter(encounter):
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO encounters (
                encounter_id,
                patient_id,
                admission_time,
                dismissal_time,
                active,
                created_at,
                is_priority,
                assigned_device_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(encounter_id) DO UPDATE SET
                patient_id = excluded.patient_id,
                admission_time = excluded.admission_time,
                dismissal_time = excluded.dismissal_time,
                active = excluded.active,
                created_at = excluded.created_at,
                is_priority = excluded.is_priority,
                assigned_device_id = excluded.assigned_device_id
            """,
            (
                encounter["encounter_id"],
                encounter["patient_id"],
                encounter["admission_time"],
                encounter.get("dismissal_time"),
                int(bool(encounter["active"])),
                encounter["created_at"],
                int(bool(encounter.get("is_priority", False))),
                encounter.get("assigned_device_id", ""),
            ),
        )


def save_device(device):
    status = device.get("status", "AVAILABLE")
    if status not in DEVICE_STATUSES:
        raise ValueError(f"invalid device status: {status}")

    with connect() as connection:
        connection.execute(
            """
            INSERT INTO devices (
                device_id,
                device_type,
                assigned_patient_id,
                assigned_encounter_id,
                status,
                battery_percent,
                wifi_rssi,
                last_seen,
                firmware_version,
                location,
                last_packet_sequence,
                last_device_send_time,
                is_active,
                retired_at,
                retired_reason,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET
                device_type = excluded.device_type,
                assigned_patient_id = excluded.assigned_patient_id,
                assigned_encounter_id = excluded.assigned_encounter_id,
                status = excluded.status,
                battery_percent = excluded.battery_percent,
                wifi_rssi = excluded.wifi_rssi,
                last_seen = excluded.last_seen,
                firmware_version = excluded.firmware_version,
                location = excluded.location,
                last_packet_sequence = excluded.last_packet_sequence,
                last_device_send_time = excluded.last_device_send_time,
                is_active = excluded.is_active,
                retired_at = excluded.retired_at,
                retired_reason = excluded.retired_reason,
                updated_at = excluded.updated_at
            """,
            (
                device["device_id"],
                device.get("device_type") or "UNKNOWN",
                device.get("assigned_patient_id"),
                device.get("assigned_encounter_id"),
                status,
                device.get("battery_percent"),
                device.get("wifi_rssi"),
                device.get("last_seen"),
                device.get("firmware_version") or "",
                device.get("location") or "",
                device.get("last_packet_sequence"),
                device.get("last_device_send_time"),
                int(bool(device.get("is_active", True))),
                device.get("retired_at"),
                device.get("retired_reason"),
                device["created_at"],
                device["updated_at"],
            ),
        )


def device_dependency_counts(device_id):
    with connect() as connection:
        row = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM packet_logs
                 WHERE parsed_device_id = ?) AS packet_logs,
                (SELECT COUNT(*) FROM encounters
                 WHERE assigned_device_id = ?) AS encounters,
                (SELECT COUNT(*) FROM patient_profiles
                 WHERE assigned_device_id = ?) AS patient_profiles,
                (SELECT COUNT(*) FROM latest_patient_state
                 WHERE device_id = ?) AS latest_patient_state
            """,
            (device_id, device_id, device_id, device_id),
        ).fetchone()

    return {
        "packet_logs": row["packet_logs"],
        "encounters": row["encounters"],
        "patient_profiles": row["patient_profiles"],
        "latest_patient_state": row["latest_patient_state"],
    }


def device_ids_with_historical_dependencies():
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT parsed_device_id AS device_id
            FROM packet_logs
            WHERE parsed_device_id IS NOT NULL AND parsed_device_id <> ''
            UNION
            SELECT assigned_device_id
            FROM encounters
            WHERE assigned_device_id <> ''
            UNION
            SELECT assigned_device_id
            FROM patient_profiles
            WHERE assigned_device_id <> ''
            UNION
            SELECT device_id
            FROM latest_patient_state
            WHERE device_id <> ''
            """
        ).fetchall()
    return {row["device_id"] for row in rows}


def delete_device(device_id):
    with connect() as connection:
        cursor = connection.execute(
            "DELETE FROM devices WHERE device_id = ?",
            (device_id,),
        )
    return cursor.rowcount > 0


def get_encounters(patient_id):
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT
                encounter_id,
                patient_id,
                admission_time,
                dismissal_time,
                active,
                created_at,
                is_priority,
                assigned_device_id
            FROM encounters
            WHERE patient_id = ?
            ORDER BY created_at DESC, rowid DESC
            """,
            (patient_id,),
        ).fetchall()

    return [
        {
            "encounter_id": row["encounter_id"],
            "patient_id": row["patient_id"],
            "admission_time": row["admission_time"],
            "dismissal_time": row["dismissal_time"],
            "active": bool(row["active"]),
            "created_at": row["created_at"],
            "is_priority": bool(row["is_priority"]),
            "assigned_device_id": row["assigned_device_id"] or "",
        }
        for row in rows
    ]


def get_encounter_history(encounter_id, limit=MAX_HISTORY_PACKETS):
    safe_limit = max(1, min(int(limit), MAX_HISTORY_PACKETS))

    with connect() as connection:
        rows = connection.execute(
            """
            SELECT encounter_id, packet_json
            FROM (
                SELECT id, encounter_id, packet_json
                FROM recent_vitals
                WHERE encounter_id = ?
                ORDER BY id DESC
                LIMIT ?
            )
            ORDER BY id
            """,
            (encounter_id, safe_limit),
        ).fetchall()

    output = []
    for row in rows:
        packet = json.loads(row["packet_json"])
        packet["encounter_id"] = row["encounter_id"]
        output.append(packet)

    return output


def save_latest_patient_state(patient_id, packet):
    finger_detected = packet.get("finger_detected")

    with connect() as connection:
        connection.execute(
            """
            INSERT INTO latest_patient_state (
                patient_id,
                encounter_id,
                device_id,
                latest_timestamp,
                server_received_at,
                heart_rate_bpm,
                spo2_percent,
                object_temp_c,
                resp_rate_bpm,
                rr_source,
                rr_confidence,
                rr_quality,
                finger_detected,
                signal_quality,
                status,
                reasons_json,
                packet_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(patient_id) DO UPDATE SET
                encounter_id = excluded.encounter_id,
                device_id = excluded.device_id,
                latest_timestamp = excluded.latest_timestamp,
                server_received_at = excluded.server_received_at,
                heart_rate_bpm = excluded.heart_rate_bpm,
                spo2_percent = excluded.spo2_percent,
                object_temp_c = excluded.object_temp_c,
                resp_rate_bpm = excluded.resp_rate_bpm,
                rr_source = excluded.rr_source,
                rr_confidence = excluded.rr_confidence,
                rr_quality = excluded.rr_quality,
                finger_detected = excluded.finger_detected,
                signal_quality = excluded.signal_quality,
                status = excluded.status,
                reasons_json = excluded.reasons_json,
                packet_json = excluded.packet_json
            """,
            (
                patient_id,
                packet.get("encounter_id"),
                packet.get("device_id", ""),
                packet.get("timestamp"),
                packet.get("_server_received_at"),
                packet.get("heart_rate_bpm"),
                packet.get("spo2_percent"),
                packet.get("object_temp_c"),
                packet.get("resp_rate_bpm"),
                packet.get("rr_source"),
                packet.get("rr_confidence"),
                packet.get("rr_quality"),
                None if finger_detected is None else int(bool(finger_detected)),
                packet.get("signal_quality"),
                packet.get("status"),
                json.dumps(packet.get("reasons") or [], ensure_ascii=False),
                json.dumps(packet, ensure_ascii=False),
            ),
        )


def append_history_packet(packet):
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO recent_vitals (
                patient_id,
                encounter_id,
                server_received_at,
                packet_json
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                packet.get("patient_id", "UNKNOWN"),
                packet.get("encounter_id"),
                packet["_server_received_at"],
                json.dumps(packet, ensure_ascii=False),
            ),
        )
        connection.execute(
            """
            DELETE FROM recent_vitals
            WHERE encounter_id = ?
            AND id NOT IN (
                SELECT id
                FROM recent_vitals
                WHERE encounter_id = ?
                ORDER BY id DESC
                LIMIT ?
            )
            """,
            (
                packet.get("encounter_id"),
                packet.get("encounter_id"),
                MAX_HISTORY_PACKETS,
            ),
        )


def save_packet_log(
    *,
    received_at,
    endpoint,
    raw_packet_json,
    parsed_patient_id,
    parsed_device_id,
    encounter_id,
    resp_rate_bpm,
    rr_source,
    rr_confidence,
    rr_quality,
    accepted,
    validation_error,
    history_logged,
    status_after_processing,
    latency_ms,
    validation_version,
):
    with connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO packet_logs (
                received_at,
                endpoint,
                raw_packet_json,
                parsed_patient_id,
                parsed_device_id,
                encounter_id,
                resp_rate_bpm,
                rr_source,
                rr_confidence,
                rr_quality,
                accepted,
                validation_error,
                history_logged,
                status_after_processing,
                latency_ms,
                validation_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                received_at,
                endpoint,
                raw_packet_json,
                parsed_patient_id,
                parsed_device_id,
                encounter_id,
                resp_rate_bpm,
                rr_source,
                rr_confidence,
                rr_quality,
                int(bool(accepted)),
                validation_error,
                (
                    None
                    if history_logged is None
                    else int(bool(history_logged))
                ),
                status_after_processing,
                latency_ms,
                validation_version,
            ),
        )
        return cursor.lastrowid


def get_recent_packet_logs(limit=50):
    safe_limit = max(1, min(int(limit), 500))

    with connect() as connection:
        rows = connection.execute(
            """
            SELECT
                id,
                received_at,
                endpoint,
                raw_packet_json,
                parsed_patient_id,
                parsed_device_id,
                encounter_id,
                resp_rate_bpm,
                rr_source,
                rr_confidence,
                rr_quality,
                accepted,
                validation_error,
                history_logged,
                status_after_processing,
                latency_ms,
                validation_version
            FROM packet_logs
            ORDER BY id DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()

    output = []

    for row in rows:
        packet_log = dict(row)
        packet_log["accepted"] = bool(packet_log["accepted"])

        if packet_log["history_logged"] is not None:
            packet_log["history_logged"] = bool(packet_log["history_logged"])

        try:
            raw_packet = json.loads(packet_log["raw_packet_json"] or "null")
        except (TypeError, json.JSONDecodeError):
            raw_packet = None

        if isinstance(raw_packet, dict):
            for field_name in (
                "resp_rate_bpm",
                "rr_source",
                "rr_confidence",
                "rr_quality",
            ):
                if packet_log[field_name] is None and field_name in raw_packet:
                    packet_log[field_name] = raw_packet[field_name]

        output.append(packet_log)

    return output


def save_patient_event(patient_id, event):
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO patient_events (
                patient_id,
                encounter_id,
                event_time,
                event_type,
                note
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                patient_id,
                event.get("encounter_id"),
                event["time"],
                event["event_type"],
                event.get("note", ""),
            ),
        )
        connection.execute(
            """
            DELETE FROM patient_events
            WHERE encounter_id = ?
            AND id NOT IN (
                SELECT id
                FROM patient_events
                WHERE encounter_id = ?
                ORDER BY id DESC
                LIMIT ?
            )
            """,
            (
                event.get("encounter_id"),
                event.get("encounter_id"),
                MAX_EVENTS_PER_PATIENT,
            ),
        )


def save_recheck_timer(patient_id, encounter_id):
    due_at = state.recheck_due_at.get(encounter_id)

    with connect() as connection:
        if due_at is None:
            connection.execute(
                "DELETE FROM recheck_timers WHERE patient_id = ?",
                (patient_id,),
            )
        else:
            connection.execute(
                """
                INSERT INTO recheck_timers (patient_id, encounter_id, due_at)
                VALUES (?, ?, ?)
                ON CONFLICT(patient_id) DO UPDATE SET
                    encounter_id = excluded.encounter_id,
                    due_at = excluded.due_at
                """,
                (patient_id, encounter_id, due_at),
            )
