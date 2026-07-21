from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import time
import unittest


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import database
import services
import state


class ReleaseCandidateWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        database.DATABASE_PATH = (
            Path(self.temporary_directory.name) / "edgeer_release_candidate.db"
        )
        database.initialize_database()
        database.load_state()

    def tearDown(self):
        self.temporary_directory.cleanup()

    @staticmethod
    def packet(patient_id, device_id, **updates):
        now = datetime.now(timezone.utc).isoformat()
        packet = {
            "patient_id": patient_id,
            "device_id": device_id,
            "device_type": "RASPBERRY_PI",
            "packet_sequence": 1,
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
        packet.update(updates)
        return packet

    @staticmethod
    def register_online_device(device_id, status="AVAILABLE", **updates):
        result = services.register_device(
            {
                "device_id": device_id,
                "device_type": updates.pop("device_type", "RASPBERRY_PI"),
            }
        )
        device = state.devices[device_id]
        device["last_seen"] = datetime.now(timezone.utc).isoformat()
        device["status"] = status
        device.update(updates)
        database.save_device(device)
        return result

    def device_view(self, device_id):
        return next(
            device for device in services.get_devices()
            if device["device_id"] == device_id
        )

    def test_only_online_available_devices_are_assignable(self):
        self.register_online_device("D-AVAILABLE")
        self.register_online_device(
            "D-OFFLINE",
            last_seen=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
        )
        self.register_online_device("D-CLEANING", status="NEEDS_CLEANING")
        self.register_online_device("D-FAULT", status="SENSOR_FAULT")
        self.register_online_device("D-ASSIGNED")
        assigned = services.register_patient(
            {"patient_id": "P-ASSIGNED", "assigned_device_id": "D-ASSIGNED"}
        )
        self.assertTrue(assigned["ok"])

        views = {device["device_id"]: device for device in services.get_devices()}

        self.assertTrue(views["D-AVAILABLE"]["online"])
        self.assertTrue(views["D-AVAILABLE"]["assignable"])
        self.assertFalse(views["D-OFFLINE"]["online"])
        self.assertFalse(views["D-OFFLINE"]["assignable"])
        self.assertFalse(views["D-CLEANING"]["assignable"])
        self.assertFalse(views["D-FAULT"]["assignable"])
        self.assertFalse(views["D-ASSIGNED"]["assignable"])

    def test_unknown_offline_cleaning_and_fault_devices_are_rejected(self):
        unknown = services.register_patient(
            {"patient_id": "P-UNKNOWN", "assigned_device_id": "UNKNOWN"}
        )
        self.assertFalse(unknown["ok"])
        self.assertEqual(unknown["code"], "UNKNOWN_DEVICE")
        self.assertNotIn("UNKNOWN", state.devices)

        cases = (
            ("D-OFFLINE", "AVAILABLE", datetime.now(timezone.utc) - timedelta(minutes=5), "DEVICE_OFFLINE"),
            ("D-CLEAN", "NEEDS_CLEANING", datetime.now(timezone.utc), "DEVICE_NEEDS_CLEANING"),
            ("D-FAULT", "SENSOR_FAULT", datetime.now(timezone.utc), "DEVICE_SENSOR_FAULT"),
        )
        for device_id, status, last_seen, code in cases:
            with self.subTest(device_id=device_id):
                self.register_online_device(
                    device_id,
                    status=status,
                    last_seen=last_seen.isoformat(),
                )
                result = services.register_patient(
                    {
                        "patient_id": f"P-{device_id}",
                        "assigned_device_id": device_id,
                    }
                )
                self.assertFalse(result["ok"])
                self.assertEqual(result["code"], code)

    def test_assignment_race_allows_only_one_active_patient(self):
        self.register_online_device("D-RACE")

        def register(patient_id):
            return services.register_patient(
                {"patient_id": patient_id, "assigned_device_id": "D-RACE"}
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(register, ("P-RACE-1", "P-RACE-2")))

        self.assertEqual(sum(bool(result["ok"]) for result in results), 1)
        active_assignments = [
            encounter for encounter in state.encounters.values()
            if encounter["active"]
            and encounter["assigned_device_id"] == "D-RACE"
        ]
        self.assertEqual(len(active_assignments), 1)

    def test_dismissal_requires_cleaning_then_device_can_be_released(self):
        self.register_online_device("D-RELEASE")
        registration = services.register_patient(
            {"patient_id": "P-RELEASE", "assigned_device_id": "D-RELEASE"}
        )
        self.assertTrue(registration["ok"])

        dismissal = services.add_action(
            {
                "patient_id": "P-RELEASE",
                "action_type": "Dismissed to monitoring room",
            }
        )
        self.assertTrue(dismissal["ok"])
        self.assertEqual(state.devices["D-RELEASE"]["status"], "NEEDS_CLEANING")
        self.assertFalse(self.device_view("D-RELEASE")["assignable"])

        cleaned = services.mark_device_clean({"device_id": "D-RELEASE"})
        self.assertTrue(cleaned["ok"])
        self.assertEqual(state.devices["D-RELEASE"]["status"], "AVAILABLE")
        self.assertTrue(self.device_view("D-RELEASE")["assignable"])

    def test_readmission_preserves_prior_history_and_does_not_reuse_device(self):
        self.register_online_device("D-FIRST")
        registration = services.register_patient(
            {"patient_id": "P-READMIT", "assigned_device_id": "D-FIRST"}
        )
        first_encounter = registration["encounter_id"]
        accepted = services.receive_vitals(self.packet("P-READMIT", "D-FIRST"))
        self.assertTrue(accepted["accepted"])
        services.add_action(
            {
                "patient_id": "P-READMIT",
                "action_type": "Dismissed to monitoring room",
            }
        )

        readmission = services.register_patient({"patient_id": "P-READMIT"})
        second_encounter = readmission["encounter_id"]

        self.assertNotEqual(first_encounter, second_encounter)
        self.assertEqual(
            state.encounters[second_encounter]["assigned_device_id"], ""
        )
        encounters = services.get_encounters("P-READMIT")
        self.assertEqual(len(encounters), 2)
        prior_handoff = services.get_handoff("P-READMIT", first_encounter)
        self.assertIn("Heart rate: 78.0 bpm", prior_handoff["summary"])
        current = next(
            patient for patient in services.get_patients()
            if patient["patient_id"] == "P-READMIT"
        )
        self.assertEqual(current["encounter_id"], second_encounter)
        self.assertEqual(current["status"], "NO DATA")

    def test_post_dismissal_packet_is_rejected_without_readmission(self):
        first = services.receive_vitals(self.packet("P-LATE", "D-LATE"))
        self.assertTrue(first["accepted"])
        services.add_action(
            {
                "patient_id": "P-LATE",
                "action_type": "Dismissed to monitoring room",
            }
        )

        late_packet = self.packet("P-LATE", "D-LATE", packet_sequence=2)
        result = services.receive_vitals(late_packet)

        self.assertFalse(result["accepted"])
        self.assertEqual(result["code"], "INACTIVE_ENCOUNTER")
        self.assertIsNone(services.get_active_encounter_id("P-LATE"))
        self.assertEqual(state.devices["D-LATE"]["status"], "NEEDS_CLEANING")
        audit = services.get_packet_logs(1)[0]
        self.assertFalse(audit["accepted"])
        self.assertIn("dismissed", audit["validation_error"])

    def test_duplicate_packet_identity_and_stale_packet_are_rejected(self):
        first_packet = self.packet("P-DUP", "D-DUP")
        first = services.receive_vitals(first_packet.copy())
        duplicate = services.receive_vitals(first_packet.copy())

        self.assertTrue(first["accepted"])
        self.assertFalse(duplicate["accepted"])
        self.assertEqual(duplicate["code"], "DUPLICATE_PACKET")

        stale_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        stale = services.receive_vitals(
            self.packet(
                "P-STALE",
                "D-STALE",
                packet_sequence=2,
                timestamp=stale_time,
                device_send_time=stale_time,
            )
        )
        self.assertFalse(stale["accepted"])
        self.assertIn("stale", stale["error"])

    def test_wrong_assignment_is_rejected_and_reconnect_is_preserved(self):
        self.register_online_device("D-LINK")
        registration = services.register_patient(
            {"patient_id": "P-LINK", "assigned_device_id": "D-LINK"}
        )
        self.assertTrue(registration["ok"])

        state.devices["D-LINK"]["status"] = "OFFLINE"
        database.save_device(state.devices["D-LINK"])
        reconnect = services.receive_vitals(self.packet("P-LINK", "D-LINK"))
        self.assertTrue(reconnect["accepted"])
        self.assertEqual(state.devices["D-LINK"]["status"], "ACTIVE")

        wrong = services.receive_vitals(self.packet("P-OTHER", "D-LINK", packet_sequence=2))
        self.assertFalse(wrong["accepted"])
        self.assertEqual(wrong["code"], "DEVICE_ASSIGNED")

    def test_recheck_actions_and_priority_workflow_are_consistent(self):
        services.register_patient({"patient_id": "P-WORKFLOW"})
        encounter_id = services.get_active_encounter_id("P-WORKFLOW")

        escalation = services.add_action(
            {"patient_id": "P-WORKFLOW", "action_type": "Escalation"}
        )
        self.assertTrue(escalation["ok"])
        self.assertIn("P-WORKFLOW", state.priority_patients)

        services.add_action(
            {"patient_id": "P-WORKFLOW", "action_type": "Recheck in 15 min"}
        )
        due_at = state.recheck_due_at[encounter_id]
        services.add_action(
            {"patient_id": "P-WORKFLOW", "action_type": "Doctor notified"}
        )
        self.assertEqual(state.recheck_due_at[encounter_id], due_at)
        services.add_action(
            {"patient_id": "P-WORKFLOW", "action_type": "Medication given"}
        )
        self.assertEqual(state.recheck_due_at[encounter_id], due_at)
        services.add_action(
            {"patient_id": "P-WORKFLOW", "action_type": "Seen by nurse"}
        )
        self.assertNotIn(encounter_id, state.recheck_due_at)

        deescalation = services.add_action(
            {
                "patient_id": "P-WORKFLOW",
                "action_type": "De-escalated / dismissed from priority",
            }
        )
        self.assertTrue(deescalation["ok"])
        self.assertNotIn("P-WORKFLOW", state.priority_patients)

    def test_state_restores_active_assignment_and_packet_identity(self):
        accepted_packet = self.packet("P-RESTART", "D-RESTART", packet_sequence=9)
        accepted = services.receive_vitals(accepted_packet)
        self.assertTrue(accepted["accepted"])
        encounter_id = services.get_active_encounter_id("P-RESTART")

        database.load_state()

        self.assertEqual(
            state.active_encounters["P-RESTART"], encounter_id
        )
        device = state.devices["D-RESTART"]
        self.assertEqual(device["assigned_encounter_id"], encounter_id)
        self.assertEqual(device["last_packet_sequence"], 9)
        duplicate = services.receive_vitals(accepted_packet.copy())
        self.assertEqual(duplicate["code"], "DUPLICATE_PACKET")

    def test_rr_cross_field_validation_accepts_only_consistent_payloads(self):
        withheld = services.receive_vitals(
            self.packet(
                "P-RR-LOW",
                "D-RR-LOW",
                resp_rate_bpm=None,
                rr_source="PPG",
                rr_confidence=0.2,
                rr_quality="LOW_CONFIDENCE",
            )
        )
        self.assertTrue(withheld["accepted"])

        reliable = services.receive_vitals(
            self.packet(
                "P-RR-GOOD",
                "D-RR-GOOD",
                resp_rate_bpm=16.0,
                rr_source="PPG",
                rr_confidence=0.8,
                rr_quality="GOOD",
            )
        )
        self.assertTrue(reliable["accepted"])

        inconsistent = services.receive_vitals(
            self.packet(
                "P-RR-BAD",
                "D-RR-BAD",
                resp_rate_bpm=16.0,
                rr_source="PPG",
                rr_confidence=0.2,
                rr_quality="LOW_CONFIDENCE",
            )
        )
        self.assertFalse(inconsistent["accepted"])
        self.assertIn("must be withheld", inconsistent["error"])

    def test_handoff_withholds_stale_rr_and_makes_no_recommendation(self):
        accepted = services.receive_vitals(
            self.packet(
                "P-RR-STALE-HANDOFF",
                "D-RR-STALE-HANDOFF",
                resp_rate_bpm=16.0,
                rr_source="PPG",
                rr_confidence=0.8,
                rr_quality="GOOD",
            )
        )
        self.assertTrue(accepted["accepted"])

        state.patients["P-RR-STALE-HANDOFF"]["_server_received_at"] = (
            time.time() - 30
        )
        handoff = services.get_handoff("P-RR-STALE-HANDOFF")

        self.assertIn("Respiratory rate: withheld (stale)", handoff["summary"])
        self.assertIn(
            "provides no diagnosis, treatment, or clinical recommendation",
            handoff["summary"],
        )
        self.assertNotIn("Suggested attention", handoff["summary"])


if __name__ == "__main__":
    unittest.main()
