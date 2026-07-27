from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest

from fastapi.testclient import TestClient


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import database
import server
import state
from scripts.audit_device_lifecycle import audit_database


class DeviceRetirementTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        database.DATABASE_PATH = (
            Path(self.temporary_directory.name) / "edgeer_device_retirement.db"
        )
        self.client_context = TestClient(server.app)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.temporary_directory.cleanup()

    @staticmethod
    def packet(patient_id, device_id, *, sequence=1):
        now = datetime.now(timezone.utc).isoformat()
        return {
            "patient_id": patient_id,
            "device_id": device_id,
            "device_type": "RASPBERRY_PI",
            "packet_sequence": sequence,
            "timestamp": now,
            "device_send_time": now,
            "heart_rate_bpm": 78.0,
            "spo2_percent": 98.0,
            "object_temp_c": 36.7,
            "finger_detected": True,
            "signal_quality": "GOOD",
            "status": "GREEN",
            "reasons": [],
        }

    def create_historical_device(self, patient_id="P-HISTORY", device_id="D-HISTORY"):
        accepted = self.client.post(
            "/api/vitals",
            json=self.packet(patient_id, device_id),
        )
        self.assertEqual(accepted.status_code, 200)
        dismissed = self.client.post(
            "/api/action",
            json={
                "patient_id": patient_id,
                "action_type": "Dismissed to monitoring room",
            },
        )
        self.assertEqual(dismissed.status_code, 200)
        return patient_id, device_id

    def test_unused_device_is_permanently_deleted(self):
        created = self.client.post(
            "/api/register_device",
            json={"device_id": "D-UNUSED", "device_type": "RASPBERRY_PI"},
        )
        self.assertEqual(created.status_code, 200)
        preview = next(
            device for device in self.client.get("/api/devices").json()
            if device["device_id"] == "D-UNUSED"
        )
        self.assertEqual(preview["removal_action"], "DELETE")

        removed = self.client.delete("/api/devices/D-UNUSED")

        self.assertEqual(removed.status_code, 200)
        self.assertEqual(removed.json()["outcome"], "deleted")
        self.assertNotIn("D-UNUSED", state.devices)
        self.assertFalse(
            any(
                device["device_id"] == "D-UNUSED"
                for device in self.client.get(
                    "/api/devices?include_retired=true"
                ).json()
            )
        )
        with database.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM devices WHERE device_id = ?",
                ("D-UNUSED",),
            ).fetchone()
        self.assertIsNone(row)

    def test_historical_device_is_retired_and_history_is_preserved(self):
        patient_id, device_id = self.create_historical_device()
        encounter_id = state.encounters[next(iter(state.encounters))]["encounter_id"]
        packet_count_before = len(self.client.get("/api/packet_logs").json())

        removed = self.client.delete(
            f"/api/devices/{device_id}",
            params={"reason": "Obsolete evaluation node"},
        )

        self.assertEqual(removed.status_code, 200)
        self.assertEqual(removed.json()["outcome"], "retired")
        retired = state.devices[device_id]
        self.assertFalse(retired["is_active"])
        self.assertIsNotNone(retired["retired_at"])
        self.assertEqual(retired["retired_reason"], "Obsolete evaluation node")
        packet_logs = self.client.get("/api/packet_logs").json()
        self.assertEqual(len(packet_logs), packet_count_before)
        historical_packet = next(
            log for log in packet_logs
            if log["parsed_device_id"] == device_id
        )
        self.assertEqual(historical_packet["device_lifecycle_state"], "RETIRED")

        encounters = self.client.get(f"/api/encounters/{patient_id}").json()
        self.assertTrue(
            any(
                encounter["encounter_id"] == encounter_id
                and encounter["assigned_device_id"] == device_id
                and encounter["assigned_device_lifecycle_state"] == "RETIRED"
                for encounter in encounters
            )
        )
        handoff = self.client.get(
            f"/api/handoff/{patient_id}",
            params={"encounter_id": encounter_id},
        ).json()
        self.assertEqual(handoff["assigned_device_id"], device_id)
        self.assertEqual(handoff["assigned_device_lifecycle_state"], "RETIRED")
        self.assertIn(f"Assigned device: {device_id} (RETIRED)", handoff["summary"])

    def test_assigned_device_is_blocked_and_unknown_returns_404(self):
        accepted = self.client.post(
            "/api/vitals",
            json=self.packet("P-ACTIVE", "D-ACTIVE"),
        )
        self.assertEqual(accepted.status_code, 200)

        blocked = self.client.delete("/api/devices/D-ACTIVE")
        missing = self.client.delete("/api/devices/D-MISSING")

        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()["code"], "DEVICE_ASSIGNED")
        self.assertIn("released", blocked.json()["error"])
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["code"], "UNKNOWN_DEVICE")

    def test_retired_device_is_hidden_unassignable_and_rejects_packets(self):
        _, device_id = self.create_historical_device(
            "P-RETIRED",
            "D-RETIRED",
        )
        self.client.delete(f"/api/devices/{device_id}")
        last_seen_before = state.devices[device_id]["last_seen"]

        active_devices = self.client.get("/api/devices").json()
        all_devices = self.client.get("/api/devices?include_retired=true").json()
        self.assertFalse(any(item["device_id"] == device_id for item in active_devices))
        retired = next(item for item in all_devices if item["device_id"] == device_id)
        self.assertEqual(retired["lifecycle_state"], "RETIRED")
        self.assertFalse(retired["assignable"])
        self.assertFalse(retired["online"])

        registration = self.client.post(
            "/api/register_patient",
            json={
                "patient_id": "P-NEW",
                "assigned_device_id": device_id,
            },
        )
        self.assertEqual(registration.status_code, 409)
        self.assertEqual(registration.json()["code"], "DEVICE_RETIRED")

        rejected = self.client.post(
            "/api/vitals",
            json=self.packet("P-RETIRED", device_id, sequence=2),
        )
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(rejected.json()["code"], "DEVICE_RETIRED")
        self.assertIn("retired", rejected.json()["error"].lower())
        self.assertEqual(state.devices[device_id]["last_seen"], last_seen_before)
        latest_log = self.client.get("/api/packet_logs?limit=1").json()[0]
        self.assertFalse(latest_log["accepted"])
        self.assertIn("retired", latest_log["validation_error"].lower())

    def test_retirement_survives_restart_and_restore_does_not_assign(self):
        _, device_id = self.create_historical_device(
            "P-RESTART-RETIRE",
            "D-RESTART-RETIRE",
        )
        self.client.delete(
            f"/api/devices/{device_id}",
            params={"reason": "Retirement persistence check"},
        )

        database.load_state()

        self.assertFalse(state.devices[device_id]["is_active"])
        self.assertEqual(
            state.devices[device_id]["retired_reason"],
            "Retirement persistence check",
        )
        restored = self.client.post(f"/api/devices/{device_id}/restore")
        self.assertEqual(restored.status_code, 200)
        self.assertTrue(restored.json()["restored"])
        device = state.devices[device_id]
        self.assertTrue(device["is_active"])
        self.assertIsNone(device["retired_at"])
        self.assertIsNone(device["retired_reason"])
        self.assertIsNone(device["assigned_patient_id"])
        self.assertIsNone(device["assigned_encounter_id"])
        self.assertEqual(device["status"], "AVAILABLE")
        restored_view = next(
            item for item in self.client.get("/api/devices").json()
            if item["device_id"] == device_id
        )
        self.assertTrue(restored_view["assignable"])

        repeated = self.client.post(f"/api/devices/{device_id}/restore")
        self.assertEqual(repeated.status_code, 409)
        self.assertEqual(repeated.json()["code"], "DEVICE_NOT_RETIRED")

    def test_offline_and_needs_cleaning_devices_remain_lifecycle_active(self):
        accepted = self.client.post(
            "/api/vitals",
            json=self.packet("P-LIFECYCLE", "D-LIFECYCLE"),
        )
        self.assertEqual(accepted.status_code, 200)
        device = state.devices["D-LIFECYCLE"]
        device["last_seen"] = "2000-01-01T00:00:00+00:00"
        database.save_device(device)

        offline = next(
            item for item in self.client.get("/api/devices").json()
            if item["device_id"] == "D-LIFECYCLE"
        )
        self.assertTrue(offline["is_active"])
        self.assertEqual(offline["lifecycle_state"], "ACTIVE")
        self.assertFalse(offline["online"])
        self.assertEqual(offline["status"], "OFFLINE")

        dismissed = self.client.post(
            "/api/action",
            json={
                "patient_id": "P-LIFECYCLE",
                "action_type": "Dismissed to monitoring room",
            },
        )
        self.assertEqual(dismissed.status_code, 200)
        released = state.devices["D-LIFECYCLE"]
        self.assertTrue(released["is_active"])
        self.assertEqual(released["status"], "NEEDS_CLEANING")
        self.assertIsNone(released["assigned_patient_id"])
        self.assertIsNone(released["assigned_encounter_id"])

    def test_packet_ingestion_never_changes_active_lifecycle_to_retired(self):
        first = self.client.post(
            "/api/vitals", json=self.packet("P-PACKET", "D-PACKET", sequence=1)
        )
        second = self.client.post(
            "/api/vitals", json=self.packet("P-PACKET", "D-PACKET", sequence=2)
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(state.devices["D-PACKET"]["is_active"])
        self.assertIsNone(state.devices["D-PACKET"]["retired_at"])

    def test_restore_preserves_only_a_valid_corrupt_active_assignment(self):
        accepted = self.client.post(
            "/api/vitals",
            json=self.packet("P-CORRUPT", "D-CORRUPT"),
        )
        self.assertEqual(accepted.status_code, 200)
        encounter_id = state.active_encounters["P-CORRUPT"]
        device = state.devices["D-CORRUPT"]
        device["is_active"] = False
        device["retired_at"] = database.utc_now_string()
        device["retired_reason"] = "incorrect legacy state"
        database.save_device(device)

        report = audit_database(database.DATABASE_PATH)
        self.assertTrue(
            any(
                item["code"] == "RETIRED_WITH_ACTIVE_ASSIGNMENT"
                and item["device_id"] == "D-CORRUPT"
                for item in report["inconsistencies"]
            )
        )

        restored = self.client.post("/api/devices/D-CORRUPT/restore")
        self.assertEqual(restored.status_code, 200)
        repaired = state.devices["D-CORRUPT"]
        self.assertTrue(repaired["is_active"])
        self.assertEqual(repaired["assigned_patient_id"], "P-CORRUPT")
        self.assertEqual(repaired["assigned_encounter_id"], encounter_id)
        self.assertEqual(
            state.encounters[encounter_id]["assigned_device_id"],
            "D-CORRUPT",
        )

    def test_restart_preserves_active_and_retired_lifecycle_states(self):
        active = self.client.post(
            "/api/register_device",
            json={"device_id": "D-STAYS-ACTIVE", "device_type": "SIMULATOR"},
        )
        self.assertEqual(active.status_code, 200)
        _, retired_id = self.create_historical_device(
            "P-STAYS-RETIRED", "D-STAYS-RETIRED"
        )
        self.client.delete(f"/api/devices/{retired_id}")

        database.load_state()

        self.assertTrue(state.devices["D-STAYS-ACTIVE"]["is_active"])
        self.assertFalse(state.devices[retired_id]["is_active"])

    def test_simulator_has_no_shutdown_retirement_path(self):
        simulator = (PROJECT_DIR / "simulate_nodes.py").read_text(encoding="utf-8")
        self.assertNotIn("remove_device", simulator)
        self.assertNotIn("retire_device", simulator)

    def test_schema_and_dashboard_expose_retirement_controls(self):
        with database.connect() as connection:
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(devices)")
            }
        self.assertTrue({"is_active", "retired_at", "retired_reason"} <= columns)

        template = (PROJECT_DIR / "templates" / "index.html").read_text(
            encoding="utf-8"
        )
        javascript = (PROJECT_DIR / "static" / "dashboard.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("Show retired devices", template)
        self.assertIn("deviceRemovalDialog", template)
        self.assertIn("data-remove-device", javascript)
        self.assertIn("data-restore-device", javascript)
        self.assertIn("await loadDevices()", javascript)
        self.assertIn("device.is_active && device.online", javascript)
        self.assertIn("device.is_active === false", javascript)
        self.assertIn("? '/api/devices?include_retired=true'", javascript)
        self.assertIn(": '/api/devices'", javascript)
        self.assertIn("responseDevices.filter", javascript)
        self.assertIn("showRetiredDevices').addEventListener('change'", javascript)
        self.assertIn("window.setInterval(refreshDashboard", javascript)

    def test_dashboard_title_is_edgeer_and_legacy_title_is_absent(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn("<title>EdgeER</title>", html)
        self.assertIn("<h1>EdgeER</h1>", html)
        self.assertNotIn("EdgeER Engineering Console", html)
        self.assertNotIn("Engineering Console", html)
        self.assertIn(
            "Investigational engineering prototype. Not intended for patient-care decisions.",
            html,
        )


if __name__ == "__main__":
    unittest.main()
