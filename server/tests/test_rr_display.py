import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

from fastapi.testclient import TestClient


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import database
import server
import simulate_nodes
import state


class RespiratoryRateDisplayTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        database.DATABASE_PATH = (
            Path(self.temporary_directory.name) / "edgeer_rr_test.db"
        )
        self.client_context = TestClient(server.app)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.temporary_directory.cleanup()

    @staticmethod
    def packet(patient_id, device_id, **updates):
        packet = {
            "patient_id": patient_id,
            "device_id": device_id,
            "timestamp": "2026-07-01 12:00:00",
            "heart_rate_bpm": 78.0,
            "spo2_percent": 98.0,
            "object_temp_c": 36.7,
            "finger_detected": True,
            "signal_quality": "GOOD",
            "status": "GREEN",
            "reasons": [],
        }
        packet.update(updates)
        return packet

    def test_accepted_reliable_rr_is_persisted_displayed_and_handed_off(self):
        packet = self.packet(
            "P-RR-GOOD",
            "DEV-RR-GOOD",
            resp_rate_bpm=9.7,
            rr_source="PPG",
            rr_confidence=0.949,
            rr_quality="GOOD",
        )

        response = self.client.post("/api/vitals", json=packet)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["accepted"])

        patients = self.client.get("/api/patients").json()
        patient = next(item for item in patients if item["patient_id"] == packet["patient_id"])
        self.assertEqual(patient["resp_rate_bpm"], 9.7)
        self.assertEqual(patient["rr_source"], "PPG")
        self.assertEqual(patient["rr_confidence"], 0.949)
        self.assertEqual(patient["rr_quality"], "GOOD")
        self.assertEqual(patient["status"], "GREEN")
        self.assertFalse(patient["priority"])

        with database.connect() as connection:
            row = connection.execute(
                """
                SELECT resp_rate_bpm, rr_source, rr_confidence, rr_quality,
                       packet_json
                FROM latest_patient_state
                WHERE patient_id = ?
                """,
                (packet["patient_id"],),
            ).fetchone()
        self.assertEqual(row["resp_rate_bpm"], 9.7)
        self.assertEqual(row["rr_source"], "PPG")
        self.assertEqual(row["rr_confidence"], 0.949)
        self.assertEqual(row["rr_quality"], "GOOD")
        self.assertEqual(json.loads(row["packet_json"])["resp_rate_bpm"], 9.7)

        packet_log = self.client.get("/api/packet_logs?limit=1").json()[0]
        self.assertTrue(packet_log["accepted"])
        self.assertEqual(packet_log["resp_rate_bpm"], 9.7)
        self.assertEqual(packet_log["rr_source"], "PPG")
        self.assertEqual(packet_log["rr_confidence"], 0.949)
        self.assertEqual(packet_log["rr_quality"], "GOOD")

        handoff = self.client.get(
            f"/api/handoff/{packet['patient_id']}"
        ).json()
        self.assertIn(
            "Respiratory rate: 9.7 breaths/min, source PPG, "
            "quality GOOD, confidence 0.95",
            handoff["summary"],
        )
        self.assertIn("Not used to determine EdgeER priority status", handoff["summary"])
        self.assertNotIn("NEWS2", handoff["summary"])

    def test_accepted_withheld_rr_is_persisted_and_labeled_withheld(self):
        packet = self.packet(
            "P-RR-WITHHELD",
            "DEV-RR-WITHHELD",
            resp_rate_bpm=None,
            rr_source="PPG",
            rr_confidence=0.233,
            rr_quality="LOW_CONFIDENCE",
        )

        response = self.client.post("/api/vitals", json=packet)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["accepted"])
        patient = next(
            item
            for item in self.client.get("/api/patients").json()
            if item["patient_id"] == packet["patient_id"]
        )
        self.assertIsNone(patient["resp_rate_bpm"])
        self.assertEqual(patient["rr_quality"], "LOW_CONFIDENCE")
        self.assertEqual(patient["status"], "GREEN")
        self.assertFalse(patient["priority"])

        packet_log = self.client.get("/api/packet_logs?limit=1").json()[0]
        self.assertIsNone(packet_log["resp_rate_bpm"])
        self.assertEqual(packet_log["rr_source"], "PPG")
        self.assertEqual(packet_log["rr_confidence"], 0.233)
        self.assertEqual(packet_log["rr_quality"], "LOW_CONFIDENCE")

        handoff = self.client.get(
            f"/api/handoff/{packet['patient_id']}"
        ).json()
        self.assertIn(
            "Respiratory rate: withheld, source PPG, "
            "quality LOW_CONFIDENCE, confidence 0.23",
            handoff["summary"],
        )

    def test_existing_simulator_packet_without_rr_remains_compatible(self):
        packet = simulate_nodes.make_packet(simulate_nodes.NODES[0])

        response = self.client.post("/api/vitals", json=packet)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["accepted"])

    def test_existing_rr_range_validation_is_unchanged(self):
        packet = self.packet(
            "P-RR-INVALID",
            "DEV-RR-INVALID",
            resp_rate_bpm=61,
            rr_source="PPG",
            rr_confidence=0.8,
            rr_quality="GOOD",
        )

        response = self.client.post("/api/vitals", json=packet)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["accepted"])
        self.assertIn("resp_rate_bpm must be between 4 and 60", response.json()["error"])

    def test_malformed_packet_audit_still_works_without_rr_metadata(self):
        response = self.client.post(
            "/api/vitals",
            content=b'{"patient_id":',
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(response.status_code, 400)
        packet_log = self.client.get("/api/packet_logs?limit=1").json()[0]
        self.assertFalse(packet_log["accepted"])
        self.assertIn("malformed JSON", packet_log["validation_error"])
        self.assertIsNone(packet_log["resp_rate_bpm"])
        self.assertIsNone(packet_log["rr_quality"])

    def test_dashboard_contains_compact_and_detailed_rr_sections(self):
        dashboard = self.client.get("/")

        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("Research RR (PPG-derived)", dashboard.text)
        self.assertIn("PPG-derived RR", dashboard.text)
        self.assertIn('id="researchRrDetail"', dashboard.text)
        self.assertIn("/static/dashboard.js?v=20260720-rc1", dashboard.text)
        self.assertIn('id="regDevice"', dashboard.text)
        self.assertIn('id="toastRegion"', dashboard.text)
        self.assertIn("Investigational engineering prototype", dashboard.text)

    def test_patient_table_headers_and_rendered_cell_contracts_align(self):
        template = (PROJECT_DIR / "templates" / "index.html").read_text(
            encoding="utf-8"
        )
        javascript = (PROJECT_DIR / "static" / "dashboard.js").read_text(
            encoding="utf-8"
        )

        def headers_before(tbody_id):
            marker = f'<tbody id="{tbody_id}">'
            marker_index = template.index(marker)
            table_start = template.rfind("<table", 0, marker_index)
            table_prefix = template[table_start:marker_index]
            return [
                re.sub(r"<[^>]+>", "", value).strip()
                for value in re.findall(
                    r"<th[^>]*>(.*?)</th>", table_prefix, re.DOTALL
                )
            ]

        patient_headers = [
            "Patient",
            "Complaint",
            "Triage",
            "Location",
            "HR",
            "SpO₂",
            "Temp",
            "RR",
            "Status",
            "Reasons",
            "Last Seen",
            "Workflow",
            "Alert",
        ]
        detail_headers = ["Time", "HR", "SpO₂", "Temp", "PPG-derived RR", "Status"]
        self.assertEqual(headers_before("priorityPatientsTable"), patient_headers)
        self.assertEqual(headers_before("patientsTable"), patient_headers)
        self.assertEqual(headers_before("detailHistory"), detail_headers)

        patient_row_start = javascript.index("function patientRowHtml")
        patient_row_end = javascript.index("function renderPatientTable", patient_row_start)
        patient_row = javascript[patient_row_start:patient_row_end]
        self.assertEqual(
            re.findall(r'<td data-column="([^"]+)"', patient_row),
            [
                "patient",
                "complaint",
                "triage",
                "location",
                "hr",
                "spo2",
                "temp",
                "rr",
                "status",
                "reasons",
                "last-seen",
                "workflow",
                "alert",
            ],
        )

        detail_row_start = javascript.index("detailTable.innerHTML = patientHistory")
        detail_row_end = javascript.index("async function loadPatientEvents", detail_row_start)
        detail_row = javascript[detail_row_start:detail_row_end]
        self.assertEqual(
            re.findall(r'<td data-column="([^"]+)"', detail_row),
            ["timestamp", "hr", "spo2", "temp", "rr", "status"],
        )
        self.assertIn("function format_rr(packet)", javascript)
        self.assertIn("Stale / withheld", javascript)

    def test_dashboard_has_device_dropdown_and_feedback_states(self):
        template = (PROJECT_DIR / "templates" / "index.html").read_text(
            encoding="utf-8"
        )
        javascript = (PROJECT_DIR / "static" / "dashboard.js").read_text(
            encoding="utf-8"
        )

        self.assertRegex(template, r'<select id="regDevice"[^>]*disabled>')
        self.assertIn("Loading available devices", template)
        self.assertIn(
            "No online and available devices are currently available.",
            javascript,
        )
        self.assertIn("Register without a device", javascript)
        self.assertIn("device.assignable", javascript)
        self.assertIn("refreshAvailableDevices", javascript)
        self.assertIn("Could not load the device registry", javascript)
        self.assertIn("showToast", javascript)
        self.assertNotIn("window.alert", javascript)
        self.assertNotRegex(javascript, r"\balert\(")

    def test_device_api_exposes_authoritative_eligibility_fields(self):
        registration = self.client.post(
            "/api/register_device",
            json={"device_id": "D-ELIGIBLE", "device_type": "RASPBERRY_PI"},
        )
        self.assertEqual(registration.status_code, 200)

        offline = next(
            device for device in self.client.get("/api/devices").json()
            if device["device_id"] == "D-ELIGIBLE"
        )
        self.assertFalse(offline["online"])
        self.assertFalse(offline["assignable"])
        self.assertIn("offline", offline["unavailable_reason"])

        state.devices["D-ELIGIBLE"]["last_seen"] = database.utc_now_string()
        database.save_device(state.devices["D-ELIGIBLE"])
        online = next(
            device for device in self.client.get("/api/devices").json()
            if device["device_id"] == "D-ELIGIBLE"
        )
        self.assertTrue(online["online"])
        self.assertTrue(online["assignable"])
        self.assertIsNone(online["unavailable_reason"])

    def test_registration_api_returns_structured_device_errors(self):
        unknown = self.client.post(
            "/api/register_patient",
            json={
                "patient_id": "P-UNKNOWN-API",
                "assigned_device_id": "D-UNKNOWN-API",
            },
        )

        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(unknown.json()["code"], "UNKNOWN_DEVICE")
        self.assertEqual(unknown.json()["field"], "assigned_device_id")

        self.client.post(
            "/api/register_device",
            json={"device_id": "D-OFFLINE-API"},
        )
        offline = self.client.post(
            "/api/register_patient",
            json={
                "patient_id": "P-OFFLINE-API",
                "assigned_device_id": "D-OFFLINE-API",
            },
        )
        self.assertEqual(offline.status_code, 409)
        self.assertEqual(offline.json()["code"], "DEVICE_OFFLINE")

    def test_schema_contains_typed_rr_columns(self):
        expected = {
            "resp_rate_bpm",
            "rr_source",
            "rr_confidence",
            "rr_quality",
        }
        with database.connect() as connection:
            latest_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(latest_patient_state)"
                )
            }
            packet_log_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(packet_logs)")
            }

        self.assertTrue(expected <= latest_columns)
        self.assertTrue(expected <= packet_log_columns)


if __name__ == "__main__":
    unittest.main()
