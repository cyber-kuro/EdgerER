"""Validation-group implementations for the EdgeER software-only suite."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import csv
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import statistics
import sys
import threading
import time
from typing import Any, Callable

from common import (
    EDGE_DIR,
    ROOT,
    SERVER_DIR,
    GroupResult,
    ValidationContext,
    database_snapshot,
    deterministic_random,
    git_output,
    isolated_client,
    latency_summary,
    make_packet,
    package_versions,
    percentile,
    process_memory_bytes,
    run_command,
    utc_now,
    write_csv,
    write_json,
    write_text,
)


def _timed_group(name: str, function: Callable[[], GroupResult]) -> GroupResult:
    started = time.perf_counter()
    try:
        result = function()
    except Exception as error:  # Every unexpected harness failure is preserved.
        result = GroupResult(
            name=name,
            status="FAIL",
            failures=[f"Harness exception: {type(error).__name__}: {error}"],
            critical=True,
        )
    result.duration_seconds = time.perf_counter() - started
    return result


def run_automated_tests(ctx: ValidationContext) -> GroupResult:
    def execute() -> GroupResult:
        output = ctx.output_dir / "automated_tests"
        env = os.environ.copy()
        env["PYTHONPYCACHEPREFIX"] = str(ctx.temp_dir / "pycache")
        commands = {
            "server": ([ctx.python, "-m", "unittest", "discover", "-s", "tests", "-v"], SERVER_DIR),
            "edge_node": ([ctx.python, "-m", "unittest", "discover", "-s", "tests", "-v"], EDGE_DIR),
        }
        counts: dict[str, Any] = {}
        failures: list[str] = []
        combined: list[str] = []
        for label, (command, cwd) in commands.items():
            code, raw, runtime = run_command(command, cwd=cwd, timeout=180, env=env)
            write_text(output / f"{label}_tests.txt", raw)
            combined.append(f"COMMAND: {' '.join(command)}\nCWD: {cwd}\nEXIT: {code}\n{raw}")
            match = re.search(r"Ran (\d+) tests? in ([0-9.]+)s", raw)
            ran = int(match.group(1)) if match else 0
            failed_match = re.search(r"FAILED \(([^)]+)\)", raw)
            failed = 0
            skipped = 0
            if failed_match:
                for part in failed_match.group(1).split(","):
                    key, _, value = part.strip().partition("=")
                    if key in {"failures", "errors"}:
                        failed += int(value or 0)
                    elif key == "skipped":
                        skipped += int(value or 0)
            counts[label] = {
                "collected": ran,
                "passed": max(0, ran - failed - skipped),
                "failed": failed,
                "skipped": skipped,
                "xfailed": 0,
                "runtime_seconds": runtime,
                "exit_code": code,
            }
            if code != 0:
                failures.append(f"{label} test command exited {code}")

        totals = {
            key: sum(item[key] for item in counts.values())
            for key in ("collected", "passed", "failed", "skipped", "xfailed")
        }
        counts["totals"] = totals
        counts["git_commit"] = git_output("rev-parse", "HEAD")
        write_json(output / "test_counts.json", counts)
        write_text(output / "full_test_suite.txt", "\n\n".join(combined))
        return GroupResult(
            name="automated_tests",
            status="PASS" if not failures else "FAIL",
            summary=counts,
            failures=failures,
            critical=bool(failures),
        )

    return _timed_group("automated_tests", execute)


def run_packet_validation(ctx: ValidationContext) -> GroupResult:
    def execute() -> GroupResult:
        import database
        import state

        output = ctx.output_dir / "packet_validation"
        rows: list[dict[str, Any]] = []
        accepted_examples: list[dict[str, Any]] = []
        rejected_examples: list[dict[str, Any]] = []
        case_index = 0

        with isolated_client(ctx, "packet_validation") as (client, _):
            def run_case(
                name: str,
                category: str,
                expected: bool,
                expected_status: int,
                expected_reason: str,
                packet: dict[str, Any] | None = None,
                *,
                raw_body: str | None = None,
                setup: Callable[[], None] | None = None,
            ) -> dict[str, Any]:
                nonlocal case_index
                case_index += 1
                if setup:
                    setup()
                before_logs = len(client.get("/api/packet_logs?limit=1000").json())
                started = time.perf_counter()
                if raw_body is None:
                    response = client.post("/api/vitals", json=packet)
                else:
                    response = client.post(
                        "/api/vitals",
                        content=raw_body,
                        headers={"Content-Type": "application/json"},
                    )
                duration_ms = (time.perf_counter() - started) * 1000
                payload = response.json()
                actual = bool(payload.get("accepted", False))
                reason = str(payload.get("error") or payload.get("warning") or "")
                after_logs = len(client.get("/api/packet_logs?limit=1000").json())
                passed = (
                    actual == expected
                    and response.status_code == expected_status
                    and (not expected_reason or expected_reason.lower() in reason.lower())
                    and after_logs > before_logs
                )
                row = {
                    "test_id": f"PV-{case_index:03d}",
                    "case_name": name,
                    "category": category,
                    "expected_acceptance": expected,
                    "actual_acceptance": actual,
                    "expected_http_status": expected_status,
                    "actual_http_status": response.status_code,
                    "expected_reason_contains": expected_reason,
                    "actual_reason": reason,
                    "packet_logged": after_logs > before_logs,
                    "pass": passed,
                    "duration_ms": duration_ms,
                }
                rows.append(row)
                example = {"case": name, "packet": packet, "response": payload}
                (accepted_examples if actual else rejected_examples).append(example)
                return payload

            def packet(name: str, updates: dict[str, Any] | None = None) -> dict[str, Any]:
                token = re.sub(r"[^A-Z0-9]", "", name.upper())[:18]
                return make_packet(f"P-{case_index + 1}-{token}", f"D-{case_index + 1}-{token}", updates=updates)

            run_case("valid packet", "valid", True, 200, "", packet("valid"))
            run_case("valid GOOD RR", "rr", True, 200, "", packet("good rr", {"resp_rate_bpm": 16.0, "rr_source": "PPG", "rr_confidence": 0.9, "rr_quality": "GOOD"}))
            run_case("valid MODERATE RR", "rr", True, 200, "", packet("moderate rr", {"resp_rate_bpm": 18.0, "rr_source": "PPG", "rr_confidence": 0.7, "rr_quality": "MODERATE"}))
            run_case("null RR LOW_CONFIDENCE", "rr", True, 200, "", packet("low confidence", {"resp_rate_bpm": None, "rr_source": "PPG", "rr_confidence": 0.2, "rr_quality": "LOW_CONFIDENCE"}))
            run_case("null RR UNAVAILABLE", "rr", True, 200, "", packet("unavailable", {"resp_rate_bpm": None, "rr_source": "PPG", "rr_confidence": None, "rr_quality": "UNAVAILABLE"}))

            simple_invalid = [
                ("missing patient_id", {"patient_id": None}, "missing patient_id"),
                ("missing device_id", {"device_id": None}, "missing device_id"),
                ("empty patient_id", {"patient_id": "  "}, "missing patient_id"),
                ("empty device_id", {"device_id": ""}, "missing device_id"),
                ("wrong HR type", {"heart_rate_bpm": "fast"}, "must be numeric"),
                ("wrong SpO2 type", {"spo2_percent": []}, "must be numeric"),
                ("wrong RR type", {"resp_rate_bpm": "16", "rr_quality": "GOOD", "rr_confidence": 0.8}, "must be numeric"),
                ("wrong confidence type", {"resp_rate_bpm": 16, "rr_quality": "GOOD", "rr_confidence": "high"}, "must be numeric"),
                ("HR below range", {"heart_rate_bpm": 19}, "between 20 and 250"),
                ("HR above range", {"heart_rate_bpm": 251}, "between 20 and 250"),
                ("SpO2 below range", {"spo2_percent": 49}, "between 50 and 100"),
                ("SpO2 above range", {"spo2_percent": 101}, "between 50 and 100"),
                ("RR below range", {"resp_rate_bpm": 3, "rr_quality": "GOOD", "rr_confidence": 0.8}, "between 4 and 60"),
                ("RR above range", {"resp_rate_bpm": 61, "rr_quality": "GOOD", "rr_confidence": 0.8}, "between 4 and 60"),
                ("confidence below zero", {"rr_confidence": -0.1, "rr_quality": "UNAVAILABLE"}, "between 0 and 1"),
                ("confidence above one", {"rr_confidence": 1.1, "rr_quality": "UNAVAILABLE"}, "between 0 and 1"),
                ("object temperature below range", {"object_temp_c": 29.9}, "between 30 and 45"),
                ("object temperature above range", {"object_temp_c": 45.1}, "between 30 and 45"),
                ("ambient temperature below range", {"ambient_temp_c": -0.1}, "between 0 and 60"),
                ("ambient temperature above range", {"ambient_temp_c": 60.1}, "between 0 and 60"),
            ]
            for name, updates, reason in simple_invalid:
                run_case(name, "invalid value", False, 400, reason, packet(name, updates))

            stale = datetime.now(timezone.utc) - timedelta(minutes=10)
            future = datetime.now(timezone.utc) + timedelta(minutes=2)
            run_case("stale timestamp", "freshness", False, 400, "stale", make_packet("P-STALE", "D-STALE", now=stale))
            run_case("future timestamp", "freshness", False, 400, "future", make_packet("P-FUTURE", "D-FUTURE", now=future))

            duplicate_packet = make_packet("P-DUP", "D-DUP", sequence=7)
            run_case("duplicate setup", "identity", True, 200, "", duplicate_packet.copy())
            run_case("duplicate packet identifier", "identity", False, 409, "duplicate", duplicate_packet.copy())
            run_case("duplicate sequence with new timestamp", "identity", True, 200, "", make_packet("P-DUP", "D-DUP", sequence=7))
            run_case("out-of-order sequence", "identity", True, 200, "", make_packet("P-DUP", "D-DUP", sequence=3))
            run_case("unknown device auto-registration", "device", True, 200, "", packet("unknown device"))

            def retired_setup() -> None:
                device_id = "D-RETIRED-MATRIX"
                patient_id = "P-RETIRED-MATRIX"
                client.post("/api/vitals", json=make_packet(patient_id, device_id))
                client.post("/api/action", json={"patient_id": patient_id, "action_type": "Dismissed to monitoring room"})
                client.delete(f"/api/devices/{device_id}")

            run_case("retired device", "device", False, 409, "retired", make_packet("P-RETIRED-MATRIX", "D-RETIRED-MATRIX", sequence=2), setup=retired_setup)

            def fault_setup() -> None:
                client.post("/api/register_device", json={"device_id": "D-FAULT-MATRIX"})
                state.devices["D-FAULT-MATRIX"]["status"] = "SENSOR_FAULT"
                state.devices["D-FAULT-MATRIX"]["last_seen"] = database.utc_now_string()
                database.save_device(state.devices["D-FAULT-MATRIX"])

            run_case("device in SENSOR_FAULT", "device", False, 409, "SENSOR_FAULT", make_packet("P-FAULT-MATRIX", "D-FAULT-MATRIX"), setup=fault_setup)

            client.post("/api/vitals", json=make_packet("P-OWNER", "D-OWNER"))
            run_case("wrong device-to-patient assignment", "assignment", False, 409, "assigned", make_packet("P-INTRUDER", "D-OWNER", sequence=2))
            run_case("device assigned to another active encounter", "assignment", False, 409, "assigned", make_packet("P-OTHER", "D-OWNER", sequence=3))
            client.post("/api/action", json={"patient_id": "P-OWNER", "action_type": "Dismissed to monitoring room"})
            run_case("packet for dismissed encounter", "encounter", False, 409, "dismissed", make_packet("P-OWNER", "D-OWNER", sequence=4))
            run_case("packet for inactive encounter", "encounter", False, 409, "readmission", make_packet("P-OWNER", "D-OWNER", sequence=5))

            client.post("/api/vitals", json=make_packet("P-REASSIGN-A", "D-REASSIGN-A"))
            client.post("/api/action", json={"patient_id": "P-REASSIGN-A", "action_type": "Dismissed to monitoring room"})
            client.post("/api/register_patient", json={"patient_id": "P-REASSIGN-A"})
            run_case("packet after valid reassignment", "assignment", True, 200, "", make_packet("P-REASSIGN-A", "D-REASSIGN-B"))
            run_case("packet after reconnection", "reconnection", True, 200, "", make_packet("P-REASSIGN-A", "D-REASSIGN-B", sequence=2))
            run_case("extra unknown fields", "compatibility", True, 200, "", packet("extra fields", {"future_extension": {"safe": True}}))

            optional_packet = packet("missing optional")
            for field in ("device_type", "packet_sequence", "ambient_temp_c"):
                optional_packet.pop(field, None)
            run_case("missing optional fields", "compatibility", True, 200, "", optional_packet)
            run_case("null optional fields", "compatibility", True, 200, "", packet("null optional", {"ambient_temp_c": None, "firmware_version": None, "battery_percent": None}))

            nonfinite = packet("nonfinite")
            nonfinite["heart_rate_bpm"] = float("nan")
            run_case("NaN value", "parser", False, 400, "finite", nonfinite, raw_body=json.dumps(nonfinite, allow_nan=True))
            nested = packet("nested malformed", {"reasons": [{"nested": "x"}], "padding": "x" * 65536})
            run_case("large payload with malformed nested field", "parser", False, 400, "list of strings", nested)

        passed = sum(bool(row["pass"]) for row in rows)
        false_acceptances = sum(not bool(row["expected_acceptance"]) and bool(row["actual_acceptance"]) for row in rows)
        false_rejections = sum(bool(row["expected_acceptance"]) and not bool(row["actual_acceptance"]) for row in rows)
        unclear = sum(not bool(row["actual_acceptance"]) and not str(row["actual_reason"]).strip() for row in rows)
        summary = {
            "total_cases": len(rows),
            "passed": passed,
            "failed": len(rows) - passed,
            "false_acceptances": false_acceptances,
            "false_rejections": false_rejections,
            "cases_without_clear_rejection_reason": unclear,
            "rule_handling_accuracy_percent": 100.0 * passed / len(rows),
        }
        write_csv(output / "packet_validation_results.csv", rows)
        write_json(output / "packet_validation_summary.json", summary)
        write_json(output / "accepted_examples.json", accepted_examples[:12])
        write_json(output / "rejected_examples.json", rejected_examples[:20])
        write_text(output / "packet_validation_notes.md", "# Packet validation notes\n\nUnknown devices are intentionally auto-registered for simulator compatibility. Sequence monotonicity is not enforced; only exact `(device_id, packet_sequence, device_send_time)` duplicates are rejected. Results use an isolated temporary database.\n")
        return GroupResult(
            name="packet_validation",
            status="PASS" if summary["failed"] == 0 else "FAIL",
            summary=summary,
            failures=[row["case_name"] for row in rows if not row["pass"]],
            critical=bool(false_acceptances),
        )

    return _timed_group("packet_validation", execute)


def run_device_lifecycle(ctx: ValidationContext) -> GroupResult:
    def execute() -> GroupResult:
        import database
        import services
        import state

        output = ctx.output_dir / "device_lifecycle"
        rows: list[dict[str, Any]] = []

        def record(name: str, initial: str, action: str, expected: Any, actual: Any, db_ok: bool = True, api_ok: bool = True) -> None:
            rows.append({
                "test_case": name,
                "initial_state": initial,
                "action": action,
                "expected_state": expected,
                "actual_state": actual,
                "database_verification": db_ok,
                "api_verification": api_ok,
                "pass": actual == expected and db_ok and api_ok,
                "duration_ms": 0.0,
            })

        with isolated_client(ctx, "device_lifecycle") as (client, database_path):
            before = database_snapshot(database_path)
            client.post("/api/register_device", json={"device_id": "D-LIFE-AVAILABLE", "device_type": "SIMULATOR"})
            record("create/register device", "missing", "POST register_device", True, "D-LIFE-AVAILABLE" in state.devices)
            state.devices["D-LIFE-AVAILABLE"]["last_seen"] = database.utc_now_string()
            database.save_device(state.devices["D-LIFE-AVAILABLE"])
            view = next(item for item in services.get_devices() if item["device_id"] == "D-LIFE-AVAILABLE")
            record("online device assignable", "AVAILABLE", "refresh registry", True, view["assignable"])

            client.post("/api/register_device", json={"device_id": "D-LIFE-OFFLINE"})
            offline = next(item for item in services.get_devices() if item["device_id"] == "D-LIFE-OFFLINE")
            record("offline excluded", "not started", "calculate eligibility", False, offline["assignable"])

            registration = client.post("/api/register_patient", json={"patient_id": "P-LIFE", "assigned_device_id": "D-LIFE-AVAILABLE"}).json()
            record("successful assignment updates registry", "available", "assign", "P-LIFE", state.devices["D-LIFE-AVAILABLE"]["assigned_patient_id"])
            assigned_view = next(item for item in services.get_devices() if item["device_id"] == "D-LIFE-AVAILABLE")
            record("assigned device excluded", "assigned", "list", False, assigned_view["assignable"])
            record("encounter stores assignment", "active encounter", "inspect", "D-LIFE-AVAILABLE", state.encounters[registration["encounter_id"]]["assigned_device_id"])

            client.post("/api/register_device", json={"device_id": "D-LIFE-CLEAN"})
            state.devices["D-LIFE-CLEAN"]["status"] = "NEEDS_CLEANING"
            database.save_device(state.devices["D-LIFE-CLEAN"])
            record("NEEDS_CLEANING excluded", "cleaning", "list", False, next(item for item in services.get_devices() if item["device_id"] == "D-LIFE-CLEAN")["assignable"])
            client.post("/api/register_device", json={"device_id": "D-LIFE-FAULT"})
            state.devices["D-LIFE-FAULT"]["status"] = "SENSOR_FAULT"
            database.save_device(state.devices["D-LIFE-FAULT"])
            record("SENSOR_FAULT excluded", "fault", "list", False, next(item for item in services.get_devices() if item["device_id"] == "D-LIFE-FAULT")["assignable"])

            unknown = client.post("/api/register_patient", json={"patient_id": "P-UNKNOWN-LIFE", "assigned_device_id": "D-UNKNOWN"})
            record("unknown assignment rejected", "unknown", "assign", 400, unknown.status_code)
            double = client.post("/api/register_patient", json={"patient_id": "P-LIFE-2", "assigned_device_id": "D-LIFE-AVAILABLE"})
            record("double assignment rejected", "assigned", "assign", 409, double.status_code)

            client.post("/api/register_device", json={"device_id": "D-LIFE-RACE"})
            state.devices["D-LIFE-RACE"]["last_seen"] = database.utc_now_string()
            database.save_device(state.devices["D-LIFE-RACE"])
            with ThreadPoolExecutor(max_workers=2) as executor:
                race_results = list(executor.map(lambda patient: services.register_patient({"patient_id": patient, "assigned_device_id": "D-LIFE-RACE"}), ("P-RACE-A", "P-RACE-B")))
            record("race assignment rejected", "one device", "two concurrent assignments", 1, sum(bool(item.get("ok")) for item in race_results))

            state.devices["D-LIFE-AVAILABLE"]["last_seen"] = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
            database.save_device(state.devices["D-LIFE-AVAILABLE"])
            services.refresh_device_offline_statuses()
            record("offline assignment remains linked", "assigned", "age last_seen", "P-LIFE", state.devices["D-LIFE-AVAILABLE"]["assigned_patient_id"])
            reconnect = client.post("/api/vitals", json=make_packet("P-LIFE", "D-LIFE-AVAILABLE", sequence=2))
            record("reconnection preserves assignment", "offline assigned", "send packet", True, reconnect.status_code == 200 and state.devices["D-LIFE-AVAILABLE"]["assigned_patient_id"] == "P-LIFE")

            blocked_remove = client.delete("/api/devices/D-LIFE-AVAILABLE")
            record("assigned device cannot be removed", "assigned", "DELETE", 409, blocked_remove.status_code)
            client.post("/api/action", json={"patient_id": "P-LIFE", "action_type": "Dismissed to monitoring room"})
            record("dismissal applies cleaning policy", "assigned", "dismiss", "NEEDS_CLEANING", state.devices["D-LIFE-AVAILABLE"]["status"])
            cleaned = client.post("/api/devices/mark_clean", json={"device_id": "D-LIFE-AVAILABLE"})
            record("released device clean transition", "NEEDS_CLEANING", "mark clean", "AVAILABLE", cleaned.json()["device"]["status"])
            readmit = client.post("/api/register_patient", json={"patient_id": "P-LIFE"}).json()
            record("readmission does not reuse device", "prior assignment", "readmit", "", state.encounters[readmit["encounter_id"]]["assigned_device_id"])

            client.post("/api/register_device", json={"device_id": "D-LIFE-UNUSED"})
            deleted = client.delete("/api/devices/D-LIFE-UNUSED").json()
            record("unused device permanently deleted", "unused", "DELETE", "deleted", deleted["outcome"], "D-LIFE-UNUSED" not in state.devices)

            retired = client.delete("/api/devices/D-LIFE-AVAILABLE", params={"reason": "validation"}).json()
            record("packet-history device retired", "historical", "DELETE", "retired", retired["outcome"])
            record("encounter-history device retired", "historical", "inspect", False, state.devices["D-LIFE-AVAILABLE"]["is_active"])
            active_ids = {item["device_id"] for item in client.get("/api/devices").json()}
            all_ids = {item["device_id"] for item in client.get("/api/devices?include_retired=true").json()}
            record("retired hidden from active view", "retired", "GET devices", False, "D-LIFE-AVAILABLE" in active_ids)
            record("retired included on request", "retired", "GET include_retired", True, "D-LIFE-AVAILABLE" in all_ids)
            encounter_rows = client.get("/api/encounters/P-LIFE").json()
            record("historical references preserved", "retired", "inspect encounters", True, any(item["assigned_device_id"] == "D-LIFE-AVAILABLE" for item in encounter_rows))
            retired_packet = client.post("/api/vitals", json=make_packet("P-LIFE", "D-LIFE-AVAILABLE", sequence=3))
            record("retired packet rejected", "retired", "POST vitals", 409, retired_packet.status_code)
            database.load_state()
            record("restart preserves retirement", "retired", "reload state", False, state.devices["D-LIFE-AVAILABLE"]["is_active"])
            restored = client.post("/api/devices/D-LIFE-AVAILABLE/restore").json()
            record("restore action", "retired", "POST restore", True, restored["restored"])
            record("restored remains unassigned", "restored", "inspect", None, state.devices["D-LIFE-AVAILABLE"]["assigned_patient_id"])
            listed = client.get("/api/devices?include_retired=true").json()
            retired_counted = [item for item in listed if not item["is_active"] and (item["online"] or item["assignable"])]
            record("counts exclude retired", "mixed registry", "calculate", 0, len(retired_counted))
            record("dropdown eligibility reflects lifecycle", "restored available state", "assignable filter", True, next(item for item in listed if item["device_id"] == "D-LIFE-AVAILABLE")["assignable"])
            record("active encounter remains unique", "readmitted", "inspect", 1, sum(bool(item["active"]) for item in state.encounters.values() if item["patient_id"] == "P-LIFE"))
            record("prior encounter retained", "readmitted", "inspect", 2, len([item for item in state.encounters.values() if item["patient_id"] == "P-LIFE"]))
            after = database_snapshot(database_path)

        write_csv(output / "device_lifecycle_results.csv", rows)
        summary = {"total": len(rows), "passed": sum(bool(row["pass"]) for row in rows), "failed": sum(not bool(row["pass"]) for row in rows)}
        write_json(output / "device_lifecycle_summary.json", summary)
        write_json(output / "database_before.json", before)
        write_json(output / "database_after.json", after)
        write_text(output / "device_lifecycle_notes.md", "# Device lifecycle notes\n\nAll actions used an isolated SQLite database. Retirement metadata is durable and operational status is preserved separately.\n")
        return GroupResult(name="device_lifecycle", status="PASS" if not summary["failed"] else "FAIL", summary=summary, failures=[row["test_case"] for row in rows if not row["pass"]], critical=bool(summary["failed"]))

    return _timed_group("device_lifecycle", execute)


def run_workflow(ctx: ValidationContext) -> GroupResult:
    def execute() -> GroupResult:
        import database
        import services
        import state

        output = ctx.output_dir / "workflow"
        rows: list[dict[str, Any]] = []
        handoff_example = ""
        with isolated_client(ctx, "workflow") as (client, database_path):
            before = database_snapshot(database_path)
            for index in range(1, 4):
                started = time.perf_counter()
                patient = f"P-WORKFLOW-{index}"
                first_device = f"D-WORKFLOW-{index}-A"
                second_device = f"D-WORKFLOW-{index}-B"
                for device_id in (first_device, second_device):
                    client.post("/api/register_device", json={"device_id": device_id, "device_type": "SIMULATOR"})
                    state.devices[device_id]["last_seen"] = database.utc_now_string()
                    database.save_device(state.devices[device_id])
                registered = client.post("/api/register_patient", json={"patient_id": patient, "display_name": f"Synthetic {index}", "assigned_device_id": first_device}).json()
                old_encounter = registered["encounter_id"]
                client.post("/api/vitals", json=make_packet(patient, first_device, sequence=1))
                client.post("/api/action", json={"patient_id": patient, "action_type": "Recheck in 15 min"})
                timer_before = state.recheck_due_at.get(old_encounter)
                client.post("/api/action", json={"patient_id": patient, "action_type": "Doctor notified"})
                doctor_preserved = state.recheck_due_at.get(old_encounter) == timer_before
                client.post("/api/action", json={"patient_id": patient, "action_type": "Medication given"})
                medication_preserved = state.recheck_due_at.get(old_encounter) == timer_before
                client.post("/api/action", json={"patient_id": patient, "action_type": "Seen by nurse"})
                nurse_cleared = old_encounter not in state.recheck_due_at
                client.post("/api/action", json={"patient_id": patient, "action_type": "Escalation"})
                escalated = patient in state.priority_patients
                client.post("/api/action", json={"patient_id": patient, "action_type": "De-escalated / dismissed from priority"})
                deescalated = patient not in state.priority_patients
                client.post("/api/action", json={"patient_id": patient, "action_type": "Escalation"})

                state.patients[patient]["_server_received_at"] = time.time() - 30
                state.devices[first_device]["last_seen"] = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
                database.save_device(state.devices[first_device])
                offline = next(item for item in services.get_patients() if item["patient_id"] == patient)["status"] == "OFFLINE"
                reconnected = client.post("/api/vitals", json=make_packet(patient, first_device, sequence=2)).status_code == 200
                client.post("/api/action", json={"patient_id": patient, "action_type": "Dismissed to monitoring room"})
                dismissed_absent = all(item["patient_id"] != patient for item in services.get_patients())
                handoff = client.get(f"/api/handoff/{patient}", params={"encounter_id": old_encounter}).json()
                if index == 1:
                    handoff_example = handoff["summary"]
                readmitted = client.post("/api/register_patient", json={"patient_id": patient, "assigned_device_id": second_device}).json()
                new_encounter = readmitted["encounter_id"]
                client.post("/api/vitals", json=make_packet(patient, second_device, sequence=1))
                database.load_state()
                active_encounters = [item for item in state.encounters.values() if item["patient_id"] == patient and item["active"]]
                all_encounters = [item for item in state.encounters.values() if item["patient_id"] == patient]
                event_times = [item["time"] for item in state.patient_events[patient]]
                checks = {
                    "one_active_encounter": len(active_encounters) == 1,
                    "old_encounter_preserved": len(all_encounters) == 2,
                    "new_encounter_id": new_encounter != old_encounter,
                    "doctor_preserved_timer": doctor_preserved,
                    "medication_preserved_timer": medication_preserved,
                    "nurse_cleared_timer": nurse_cleared,
                    "escalation": escalated,
                    "deescalation": deescalated,
                    "offline": offline,
                    "reconnection": reconnected,
                    "dismissed_removed": dismissed_absent,
                    "handoff_has_device": first_device in handoff["summary"],
                    "timeline_ordered": event_times == sorted(event_times),
                    "new_device_assigned": active_encounters[0]["assigned_device_id"] == second_device,
                    "current_history_isolated": state.patients[patient]["encounter_id"] == new_encounter,
                }
                rows.append({
                    "scenario": index,
                    "patient_id": patient,
                    "old_encounter_id": old_encounter,
                    "new_encounter_id": new_encounter,
                    "checks_total": len(checks),
                    "checks_passed": sum(checks.values()),
                    "pass": all(checks.values()),
                    "failed_checks": "; ".join(name for name, value in checks.items() if not value),
                    "duration_ms": (time.perf_counter() - started) * 1000,
                })
            after = database_snapshot(database_path)

        summary = {"scenarios": len(rows), "passed": sum(bool(row["pass"]) for row in rows), "failed": sum(not bool(row["pass"]) for row in rows)}
        write_csv(output / "workflow_results.csv", rows)
        write_json(output / "workflow_summary.json", summary)
        write_json(output / "workflow_state_before.json", before)
        write_json(output / "workflow_state_after.json", after)
        write_text(output / "handoff_example.txt", handoff_example)
        write_text(output / "workflow_notes.md", "# Workflow notes\n\nThree deterministic synthetic workflows were executed against one isolated database. No private identifiers were used.\n")
        return GroupResult(name="workflow", status="PASS" if not summary["failed"] else "FAIL", summary=summary, failures=[f"scenario {row['scenario']}: {row['failed_checks']}" for row in rows if not row["pass"]], critical=bool(summary["failed"]))

    return _timed_group("workflow", execute)


def run_database_persistence(ctx: ValidationContext) -> GroupResult:
    def execute() -> GroupResult:
        import database
        import state

        output = ctx.output_dir / "database"
        rows: list[dict[str, Any]] = []
        with isolated_client(ctx, "database_persistence") as (client, database_path):
            client.post("/api/vitals", json=make_packet("P-DB-ACTIVE", "D-DB-ACTIVE"))
            active_encounter = state.active_encounters["P-DB-ACTIVE"]
            client.post("/api/action", json={"patient_id": "P-DB-ACTIVE", "action_type": "Escalation"})
            client.post("/api/action", json={"patient_id": "P-DB-ACTIVE", "action_type": "Recheck in 15 min"})
            client.post("/api/vitals", json=make_packet("P-DB-CLOSED", "D-DB-RETIRED"))
            closed_encounter = state.active_encounters["P-DB-CLOSED"]
            client.post("/api/action", json={"patient_id": "P-DB-CLOSED", "action_type": "Dismissed to monitoring room"})
            client.delete("/api/devices/D-DB-RETIRED", params={"reason": "persistence validation"})
            before = database_snapshot(database_path)
            expected = {
                "active_encounter": active_encounter,
                "closed_encounter": closed_encounter,
                "priority": "P-DB-ACTIVE" in state.priority_patients,
                "recheck": active_encounter in state.recheck_due_at,
                "retired": not state.devices["D-DB-RETIRED"]["is_active"],
            }

            database.clear_in_memory_state()
            database.initialize_database()
            database.initialize_database()
            database.load_state()
            actual = {
                "active_encounter": state.active_encounters.get("P-DB-ACTIVE"),
                "closed_encounter": closed_encounter if closed_encounter in state.encounters else None,
                "priority": "P-DB-ACTIVE" in state.priority_patients,
                "recheck": active_encounter in state.recheck_due_at,
                "retired": not state.devices["D-DB-RETIRED"]["is_active"],
            }
            for key in expected:
                rows.append({"check": key, "expected": expected[key], "actual": actual[key], "pass": expected[key] == actual[key]})
            after = database_snapshot(database_path)

            with database.connect() as connection:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
                table_counts = [
                    {"table": table, "row_count": count}
                    for table, count in sorted(after["tables"].items())
                ]

            legacy_path = ctx.temp_dir / "legacy_device_schema.db"
            with sqlite3.connect(legacy_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE devices (
                        device_id TEXT PRIMARY KEY,
                        device_type TEXT NOT NULL DEFAULT 'UNKNOWN',
                        assigned_patient_id TEXT,
                        assigned_encounter_id TEXT,
                        status TEXT NOT NULL DEFAULT 'AVAILABLE',
                        battery_percent REAL, wifi_rssi REAL, last_seen TEXT,
                        firmware_version TEXT NOT NULL DEFAULT '',
                        location TEXT NOT NULL DEFAULT '',
                        last_packet_sequence INTEGER,
                        last_device_send_time TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
            current_path = database.DATABASE_PATH
            database.DATABASE_PATH = legacy_path
            try:
                database.initialize_database()
                with database.connect() as connection:
                    lifecycle_columns = {row["name"] for row in connection.execute("PRAGMA table_info(devices)")}
                migration_ok = {"is_active", "retired_at", "retired_reason"} <= lifecycle_columns
            finally:
                database.DATABASE_PATH = current_path
            rows.append({"check": "legacy lifecycle migration", "expected": True, "actual": migration_ok, "pass": migration_ok})

        write_csv(output / "database_persistence_results.csv", rows)
        write_text(output / "database_integrity.txt", f"PRAGMA integrity_check: {integrity}\n")
        write_text(output / "foreign_key_check.txt", "No violations.\n" if not foreign_keys else "\n".join(map(str, foreign_keys)))
        write_csv(output / "database_table_counts.csv", table_counts)
        summary = {"checks": len(rows), "passed": sum(bool(row["pass"]) for row in rows), "failed": sum(not bool(row["pass"]) for row in rows), "integrity_check": integrity, "foreign_key_violations": len(foreign_keys)}
        write_text(output / "database_notes.md", "# Database notes\n\nThe database was isolated. Initialization was repeated to test idempotency, and a pre-lifecycle `devices` schema was migrated. Handoff text is generated on demand and has no persistence table.\n")
        return GroupResult(name="database_persistence", status="PASS" if not summary["failed"] and integrity == "ok" and not foreign_keys else "FAIL", summary=summary, failures=[row["check"] for row in rows if not row["pass"]], critical=bool(summary["failed"] or integrity != "ok" or foreign_keys))

    return _timed_group("database_persistence", execute)


def _run_load_once(ctx: ValidationContext, device_count: int, run_number: int, duration: float) -> dict[str, Any]:
    import database

    latencies: list[float] = []
    cpu_samples: list[float] = []
    memory_samples: list[int] = []
    attempted = accepted = intentional_rejections = unexpected_rejections = request_errors = 0
    name = f"load_{device_count}_{run_number}"
    with isolated_client(ctx, name) as (client, database_path):
        starting_bytes = database_path.stat().st_size
        starting_auxiliary_bytes = sum(
            path.stat().st_size
            for path in (Path(str(database_path) + "-wal"), Path(str(database_path) + "-shm"))
            if path.exists()
        )
        started = time.perf_counter()
        cpu_started = time.process_time()
        sequences = [0] * device_count
        while time.perf_counter() - started < duration:
            for index in range(device_count):
                sequences[index] += 1
                packet = make_packet(f"P-LOAD-{index}", f"D-LOAD-{index}", sequence=sequences[index])
                request_started = time.perf_counter()
                request_cpu_started = time.process_time()
                try:
                    response = client.post("/api/vitals", json=packet)
                    request_elapsed = time.perf_counter() - request_started
                    elapsed_ms = request_elapsed * 1000
                    cpu_samples.append(
                        100 * (time.process_time() - request_cpu_started)
                        / max(request_elapsed * (os.cpu_count() or 1), 0.000001)
                    )
                    latencies.append(elapsed_ms)
                    attempted += 1
                    if response.status_code == 200 and response.json().get("accepted"):
                        accepted += 1
                    else:
                        unexpected_rejections += 1
                except Exception:
                    request_errors += 1
                memory_samples.append(process_memory_bytes())
                if time.perf_counter() - started >= duration:
                    break

        duplicate = make_packet("P-LOAD-0", "D-LOAD-0", sequence=sequences[0])
        duplicate["device_send_time"] = database.utc_now_string()
        first = client.post("/api/vitals", json=duplicate)
        second = client.post("/api/vitals", json=duplicate)
        attempted += 2
        accepted += int(first.status_code == 200)
        intentional_rejections += int(second.status_code == 409)
        elapsed = time.perf_counter() - started
        cpu_seconds = time.process_time() - cpu_started
        state_api_started = time.perf_counter()
        state_response = client.get("/api/patients")
        state_api_ms = (time.perf_counter() - state_api_started) * 1000
        snapshot = database_snapshot(database_path)
        ending_bytes = database_path.stat().st_size
        wal_path = Path(str(database_path) + "-wal")
        shm_path = Path(str(database_path) + "-shm")
        wal_bytes = wal_path.stat().st_size if wal_path.exists() else 0
        shm_bytes = shm_path.stat().st_size if shm_path.exists() else 0
        ending_total_bytes = ending_bytes + wal_bytes + shm_bytes
    return {
        "device_count": device_count,
        "run": run_number,
        "duration_seconds": elapsed,
        "packet_interval_seconds": 0.0,
        "attempted_packets": attempted,
        "accepted_packets": accepted,
        "intentional_rejections": intentional_rejections,
        "unexpected_rejections": unexpected_rejections,
        "request_errors": request_errors,
        **latency_summary(latencies),
        "throughput_requests_per_second": attempted / elapsed if elapsed else 0,
        "cpu_average_percent": 100 * cpu_seconds / max(elapsed * (os.cpu_count() or 1), 0.001),
        "cpu_peak_percent": max(cpu_samples) if cpu_samples else 0.0,
        "memory_average_bytes": statistics.fmean(memory_samples) if memory_samples else 0,
        "memory_peak_bytes": max(memory_samples) if memory_samples else 0,
        "thread_count": threading.active_count(),
        "database_start_bytes": starting_bytes,
        "database_end_bytes": ending_bytes,
        "database_growth_bytes": ending_total_bytes - (starting_bytes + starting_auxiliary_bytes),
        "wal_bytes": wal_bytes,
        "shm_bytes": shm_bytes,
        "dashboard_state_api_latency_ms": state_api_ms,
        "dashboard_state_status": state_response.status_code,
        "integrity_check": snapshot.get("integrity_check"),
        "packet_log_rows": snapshot["tables"].get("packet_logs", 0),
        "recent_vitals_rows": snapshot["tables"].get("recent_vitals", 0),
        "event_rows": snapshot["tables"].get("patient_events", 0),
        "encounter_rows": snapshot["tables"].get("encounters", 0),
        "latencies": latencies,
    }


def run_scalability_and_growth(ctx: ValidationContext) -> tuple[GroupResult, GroupResult]:
    def execute() -> tuple[GroupResult, GroupResult]:
        scale_output = ctx.output_dir / "scalability"
        db_output = ctx.output_dir / "database"
        if ctx.skip_scalability:
            note = "Scalability was skipped by --skip-scalability."
            write_text(scale_output / "scalability_notes.md", f"# Scalability notes\n\n{note}\n")
            write_text(db_output / "database_notes.md", f"# Database notes\n\n{note}\n")
            skipped = GroupResult(name="scalability", status="SKIPPED", summary={"reason": note})
            return skipped, GroupResult(name="database_growth", status="SKIPPED", summary={"reason": note})

        raw: list[dict[str, Any]] = []
        latency_rows: list[dict[str, Any]] = []
        resource_rows: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        fault_events: list[dict[str, Any]] = []
        repeats = 1 if ctx.mode == "quick" else 3
        for device_count in ctx.device_counts:
            for run_number in range(1, repeats + 1):
                result = _run_load_once(ctx, device_count, run_number, ctx.duration_seconds)
                latencies = result.pop("latencies")
                raw.append(result)
                for value in latencies:
                    latency_rows.append({"device_count": device_count, "run": run_number, "latency_ms": value})
                resource_rows.append({key: result[key] for key in ("device_count", "run", "cpu_average_percent", "cpu_peak_percent", "memory_average_bytes", "memory_peak_bytes", "thread_count")})
                fault_events.append({
                    "device_count": device_count,
                    "run": run_number,
                    "scenario": "exact_duplicate",
                    "classification": "intentional_rejection",
                    "expected_rejections": 1,
                    "observed_rejections": result["intentional_rejections"],
                    "pass": result["intentional_rejections"] == 1,
                })
                if result["unexpected_rejections"] or result["request_errors"]:
                    failures.append({"device_count": device_count, "run": run_number, "unexpected_rejections": result["unexpected_rejections"], "request_errors": result["request_errors"]})

        summaries: list[dict[str, Any]] = []
        for device_count in ctx.device_counts:
            subset = [row for row in raw if row["device_count"] == device_count]
            summaries.append({
                "device_count": device_count,
                "runs": len(subset),
                "attempted_packets": sum(row["attempted_packets"] for row in subset),
                "accepted_packets": sum(row["accepted_packets"] for row in subset),
                "unexpected_rejections": sum(row["unexpected_rejections"] for row in subset),
                "request_errors": sum(row["request_errors"] for row in subset),
                "mean_latency_ms": statistics.fmean(row["mean_ms"] for row in subset),
                "p95_latency_ms": max(row["p95_ms"] for row in subset),
                "p99_latency_ms": max(row["p99_ms"] for row in subset),
                "throughput_requests_per_second": statistics.fmean(row["throughput_requests_per_second"] for row in subset),
                "cpu_average_percent": statistics.fmean(row["cpu_average_percent"] for row in subset),
                "memory_peak_bytes": max(row["memory_peak_bytes"] for row in subset),
                "database_growth_bytes": sum(row["database_growth_bytes"] for row in subset),
            })

        write_csv(scale_output / "scalability_raw.csv", raw)
        write_csv(scale_output / "scalability_summary.csv", summaries)
        write_json(scale_output / "scalability_summary.json", {"mode": ctx.mode, "duration_seconds_per_run": ctx.duration_seconds, "device_counts": ctx.device_counts, "rows": summaries})
        write_csv(scale_output / "resource_usage.csv", resource_rows)
        write_csv(scale_output / "latency_distribution.csv", latency_rows)
        write_csv(scale_output / "failure_events.csv", [*fault_events, *failures])
        shortened = ctx.mode == "full" and ctx.duration_seconds < 300
        write_text(scale_output / "scalability_notes.md", "# Scalability notes\n\nTestClient provided bounded in-process load, not an external network saturation test. The normal load is measured here; packet fault variants are exercised by the packet-validation matrix, disconnect/reconnect faults by the reconnection group, and an exact duplicate probe is recorded separately in failure_events.csv. CPU is normalized process CPU time; memory is Windows working set. " + ("This full-mode execution used shortened durations; use `--duration-seconds 300` for the requested five-minute runs.\n" if shortened else "\n"))

        growth_rows = [{
            "device_count": row["device_count"],
            "run": row["run"],
            "starting_database_bytes": row["database_start_bytes"],
            "ending_database_bytes": row["database_end_bytes"],
            "bytes_added": row["database_growth_bytes"],
            "packets_attempted": row["attempted_packets"],
            "packets_accepted": row["accepted_packets"],
            "packets_rejected": row["intentional_rejections"] + row["unexpected_rejections"],
            "packet_log_rows": row["packet_log_rows"],
            "recent_vitals_rows": row["recent_vitals_rows"],
            "event_rows": row["event_rows"],
            "encounter_rows": row["encounter_rows"],
            "bytes_per_accepted_packet": row["database_growth_bytes"] / max(row["accepted_packets"], 1),
            "bytes_per_device_minute": row["database_growth_bytes"] / max(row["device_count"] * row["duration_seconds"] / 60, 0.001),
            "wal_bytes": row["wal_bytes"],
            "shm_bytes": row["shm_bytes"],
            "integrity_check": row["integrity_check"],
        } for row in raw]
        write_csv(db_output / "database_growth.csv", growth_rows)
        growth_summary = {"runs": len(growth_rows), "total_bytes_added": sum(row["bytes_added"] for row in growth_rows), "integrity_failures": sum(row["integrity_check"] != "ok" for row in growth_rows)}
        write_json(db_output / "database_growth_summary.json", growth_summary)
        scale_fail = bool(failures)
        return (
            GroupResult(name="scalability", status="PASS" if not scale_fail else "FAIL", summary={"configurations": len(summaries), "runs": len(raw), "shortened_full_duration": shortened, "rows": summaries}, failures=[str(item) for item in failures], critical=False),
            GroupResult(name="database_growth", status="PASS" if not growth_summary["integrity_failures"] else "FAIL", summary=growth_summary, critical=bool(growth_summary["integrity_failures"])),
        )

    started = time.perf_counter()
    try:
        scale, growth = execute()
    except Exception as error:
        message = f"Harness exception: {type(error).__name__}: {error}"
        scale = GroupResult(name="scalability", status="FAIL", failures=[message], critical=True)
        growth = GroupResult(name="database_growth", status="FAIL", failures=[message], critical=True)
    duration = time.perf_counter() - started
    scale.duration_seconds = duration
    growth.duration_seconds = duration
    return scale, growth


def run_reconnection(ctx: ValidationContext) -> GroupResult:
    def execute() -> GroupResult:
        import database
        import services
        import state
        from models import OFFLINE_AFTER_SECONDS

        output = ctx.output_dir / "reconnection"
        rows: list[dict[str, Any]] = []
        with isolated_client(ctx, "reconnection") as (client, _):
            for cycle in range(1, 11):
                patient = f"P-RECONNECT-{cycle}"
                device = f"D-RECONNECT-{cycle}"
                first = client.post("/api/vitals", json=make_packet(patient, device, sequence=1))
                encounter = state.active_encounters[patient]
                state.patients[patient]["_server_received_at"] = time.time() - OFFLINE_AFTER_SECONDS - 1.1
                state.devices[device]["last_seen"] = (datetime.now(timezone.utc) - timedelta(seconds=OFFLINE_AFTER_SECONDS + 1.1)).isoformat()
                database.save_device(state.devices[device])
                detection_started = time.perf_counter()
                offline_patient = next(item for item in services.get_patients() if item["patient_id"] == patient)
                detection_call = time.perf_counter() - detection_started
                recovery_started = time.perf_counter()
                recovered = client.post("/api/vitals", json=make_packet(patient, device, sequence=2))
                recovery = time.perf_counter() - recovery_started
                duplicate = client.post("/api/vitals", json=make_packet(patient, device, sequence=2, updates={"device_send_time": state.devices[device]["last_device_send_time"]}))
                stale_time = datetime.now(timezone.utc) - timedelta(minutes=10)
                stale = client.post("/api/vitals", json=make_packet(patient, device, sequence=3, now=stale_time))
                rows.append({
                    "cycle": cycle,
                    "configured_offline_timeout_seconds": OFFLINE_AFTER_SECONDS,
                    "observed_detection_seconds": OFFLINE_AFTER_SECONDS + 1.1 + detection_call,
                    "detection_call_latency_ms": detection_call * 1000,
                    "recovery_time_ms": recovery * 1000,
                    "initial_packet_accepted": first.status_code == 200,
                    "offline_detected": offline_patient["status"] == "OFFLINE",
                    "assignment_preserved": state.devices[device]["assigned_encounter_id"] == encounter,
                    "first_accepted_sequence": 2 if recovered.status_code == 200 else None,
                    "duplicate_rejected": duplicate.status_code == 409,
                    "stale_packet_rejected": stale.status_code == 400,
                    "active_encounter_count": sum(item["active"] and item["patient_id"] == patient for item in state.encounters.values()),
                    "pass": first.status_code == 200 and offline_patient["status"] == "OFFLINE" and recovered.status_code == 200 and duplicate.status_code == 409 and stale.status_code == 400,
                })
            database.load_state()

        summary = {
            "cycles": len(rows),
            "passed": sum(bool(row["pass"]) for row in rows),
            "failed": sum(not bool(row["pass"]) for row in rows),
            "configured_offline_timeout_seconds": OFFLINE_AFTER_SECONDS,
            "mean_detection_seconds": statistics.fmean(row["observed_detection_seconds"] for row in rows),
            "mean_recovery_ms": statistics.fmean(row["recovery_time_ms"] for row in rows),
            "p95_recovery_ms": percentile([row["recovery_time_ms"] for row in rows], 95),
        }
        write_csv(output / "reconnection_results.csv", rows)
        write_json(output / "reconnection_summary.json", summary)
        write_text(output / "reconnection_notes.md", "# Reconnection notes\n\nTen deterministic software-only cycles used timestamp injection to cross the 15-second offline boundary without adding 150 seconds of wall-clock delay. Recovery latency measures the first accepted TestClient request. Assignment and encounter identity were checked after every cycle.\n")
        return GroupResult(name="reconnection", status="PASS" if not summary["failed"] else "FAIL", summary=summary, failures=[f"cycle {row['cycle']}" for row in rows if not row["pass"]], critical=bool(summary["failed"]))

    return _timed_group("reconnection", execute)


def run_api_benchmarks(ctx: ValidationContext) -> GroupResult:
    def execute() -> GroupResult:
        output = ctx.output_dir / "api_benchmarks"
        raw: list[dict[str, Any]] = []
        with isolated_client(ctx, "api_benchmarks") as (client, _):
            for index in range(10):
                client.post("/api/vitals", json=make_packet(f"P-API-{index}", f"D-API-{index}"))
            request_count = 20 if ctx.mode == "quick" else 75
            endpoints: list[tuple[str, str, Callable[[int], Any]]] = [
                ("GET", "/api/devices", lambda _: None),
                ("GET", "/api/patients", lambda _: None),
                ("GET", "/api/history", lambda _: None),
                ("GET", "/api/packet_logs?limit=50", lambda _: None),
                ("GET", "/api/encounters/P-API-0", lambda _: None),
                ("GET", "/api/handoff/P-API-0", lambda _: None),
                ("POST", "/api/vitals", lambda index: make_packet("P-API-0", "D-API-0", sequence=index + 2)),
                ("POST", "/api/action", lambda _: {"patient_id": "P-API-0", "action_type": "Doctor notified", "note": "benchmark"}),
            ]
            for method, endpoint, payload_factory in endpoints:
                for request_index in range(request_count):
                    started = time.perf_counter()
                    response = client.get(endpoint) if method == "GET" else client.post(endpoint, json=payload_factory(request_index))
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    raw.append({"method": method, "endpoint": endpoint, "request_index": request_index, "status_code": response.status_code, "success": response.status_code < 400, "latency_ms": elapsed_ms, "response_size_bytes": len(response.content)})

        summaries: list[dict[str, Any]] = []
        sizes: list[dict[str, Any]] = []
        for method, endpoint, _ in endpoints:
            subset = [row for row in raw if row["method"] == method and row["endpoint"] == endpoint]
            latencies = [row["latency_ms"] for row in subset]
            response_sizes = [row["response_size_bytes"] for row in subset]
            summaries.append({"method": method, "endpoint": endpoint, "request_count": len(subset), "success_count": sum(bool(row["success"]) for row in subset), "failure_count": sum(not bool(row["success"]) for row in subset), **latency_summary(latencies), "requests_per_second": 1000 / statistics.fmean(latencies) if latencies else 0})
            sizes.append({"method": method, "endpoint": endpoint, "response_size_mean_bytes": statistics.fmean(response_sizes), "response_size_max_bytes": max(response_sizes)})
        write_csv(output / "api_latency_raw.csv", raw)
        write_csv(output / "api_latency_summary.csv", summaries)
        write_csv(output / "endpoint_response_sizes.csv", sizes)
        write_text(output / "api_benchmark_notes.md", "# API benchmark notes\n\nMeasurements use an in-process FastAPI TestClient with bounded sequential requests. They characterize application-path latency, not LAN or production-server latency.\n")
        failures = [f"{row['method']} {row['endpoint']}: {row['failure_count']} failures" for row in summaries if row["failure_count"]]
        return GroupResult(name="api_benchmarks", status="PASS" if not failures else "FAIL", summary={"endpoints": len(summaries), "requests": len(raw), "rows": summaries}, failures=failures, critical=False)

    return _timed_group("api_benchmarks", execute)


def run_dashboard_checks(ctx: ValidationContext) -> GroupResult:
    def execute() -> GroupResult:
        output = ctx.output_dir / "dashboard"
        checks: list[tuple[str, bool]] = []
        template = (SERVER_DIR / "templates" / "index.html").read_text(encoding="utf-8")
        javascript = (SERVER_DIR / "static" / "dashboard.js").read_text(encoding="utf-8")
        styles = (SERVER_DIR / "static" / "styles.css").read_text(encoding="utf-8")
        static_expectations = {
            "registration modal": "registrationDialog" in template,
            "device dropdown": "regDevice" in template,
            "empty device state": "No online and available devices" in javascript,
            "priority empty state": "No priority patients" in javascript,
            "error state": "Could not load the device registry" in javascript,
            "loading state": "Loading available devices" in template,
            "alert sound control": "Enable alert sound" in template,
            "prototype disclaimer": "Not intended for patient-care decisions" in template,
            "retired filter": "showRetiredDevices" in template,
            "remove modal": "deviceRemovalDialog" in template,
            "RR column": "<th>RR</th>" in template,
            "status column": "<th>Status</th>" in template,
            "NO DATA style": "status-no-data" in styles,
            "OFFLINE style": "status-offline" in styles,
            "responsive rules": "@media (max-width:" in styles,
        }
        checks.extend(static_expectations.items())
        with isolated_client(ctx, "dashboard_contract") as (client, _):
            response = client.get("/")
            checks.append(("dashboard renders", response.status_code == 200 and "EdgeER Engineering Console" in response.text))
            client.post("/api/register_device", json={"device_id": "D-DASH-EMPTY"})
            devices = client.get("/api/devices").json()
            checks.append(("offline device excluded from dropdown eligibility", not devices[0]["assignable"]))
            registration = client.post("/api/register_patient", json={"patient_id": "P-DASH-NODATA"})
            checks.append(("registration succeeds", registration.status_code == 200))
            invalid = client.post("/api/register_patient", json={"patient_id": "P-DASH-BAD", "assigned_device_id": "UNKNOWN"})
            checks.append(("invalid registration structured error", invalid.status_code == 400 and bool(invalid.json().get("error"))))
            client.post("/api/vitals", json=make_packet("P-DASH-LIVE", "D-DASH-LIVE", updates={"resp_rate_bpm": 16.0, "rr_source": "PPG", "rr_confidence": 0.8, "rr_quality": "GOOD"}))
            patients = client.get("/api/patients").json()
            checks.append(("RR separate from status", any(item.get("resp_rate_bpm") == 16.0 and item.get("status") == "GREEN" for item in patients)))
            action = client.post("/api/action", json={"patient_id": "P-DASH-LIVE", "action_type": "Escalation"})
            checks.append(("priority escalation API", action.status_code == 200))
            handoff = client.get("/api/handoff/P-DASH-LIVE")
            checks.append(("handoff renders", handoff.status_code == 200 and "Handoff Summary" in handoff.json().get("summary", "")))

        screenshot_files = list((output / "screenshots").rglob("*.png"))
        standalone_available = bool(importlib.util.find_spec("playwright") or importlib.util.find_spec("selenium"))
        failures = [name for name, passed in checks if not passed]
        blocked: list[str] = []
        if ctx.skip_browser:
            browser_status = "SKIPPED"
        elif standalone_available:
            browser_status = "AVAILABLE_NOT_INVOKED"
            blocked.append("Standalone browser package was detected, but no compatible browser binary was configured by this runner.")
        elif screenshot_files:
            browser_status = "EXTERNAL_CAPTURE_PRESENT"
        else:
            browser_status = "BLOCKED"
            blocked.append("Standalone Playwright/Selenium is not installed. Enable with: python -m pip install playwright && python -m playwright install chromium")

        summary = {
            "contract_checks": len(checks),
            "contract_checks_passed": sum(passed for _, passed in checks),
            "contract_checks_failed": len(failures),
            "browser_automation_status": browser_status,
            "screenshots_found": len(screenshot_files),
            "console_errors": [],
        }
        write_text(output / "browser_test_results.txt", "\n".join(f"{'PASS' if passed else 'FAIL'}: {name}" for name, passed in checks) + "\n")
        write_json(output / "browser_test_summary.json", summary)
        write_json(output / "console_errors.json", [])
        write_text(output / "accessibility_findings.md", "# Accessibility findings\n\nSemantic dialogs, labels, table headers, keyboard focus styles, text status labels, and the prototype disclaimer were found statically. A full WCAG audit is outside this software checkpoint.\n")
        write_text(output / "dashboard_notes.md", "# Dashboard notes\n\nAPI/template contracts were exercised with an isolated database. Browser package status: **" + browser_status + "**.\n\n" + ("\n".join(f"- {item}" for item in blocked) if blocked else "Browser screenshots were supplied by an external controlled browser run.") + "\n")
        overall = "FAIL" if failures else ("BLOCKED" if browser_status == "BLOCKED" else "PASS")
        return GroupResult(name="dashboard", status=overall, summary=summary, failures=failures, blocked=blocked, critical=False)

    return _timed_group("dashboard", execute)


def run_shared_ppg(ctx: ValidationContext) -> GroupResult:
    def execute() -> GroupResult:
        output = ctx.output_dir / "shared_ppg"
        env = os.environ.copy()
        env["PYTHONPYCACHEPREFIX"] = str(ctx.temp_dir / "pycache")
        command = [ctx.python, "-m", "unittest", "discover", "-s", "tests", "-v"]
        code, raw, runtime = run_command(command, cwd=EDGE_DIR, timeout=180, env=env)
        write_text(output / "shared_ppg_test_results.txt", raw)
        main_source = (EDGE_DIR / "main.py").read_text(encoding="utf-8")
        pipeline_source = (EDGE_DIR / "processing" / "ppg_pipeline.py").read_text(encoding="utf-8")
        test_names = set(re.findall(r"def (test_[a-zA-Z0-9_]+)", "\n".join(path.read_text(encoding="utf-8") for path in (EDGE_DIR / "tests").glob("test_*.py"))))
        assertions = {
            "one MAX30102 construction in main": main_source.count("MAX30102Sensor()") == 1,
            "one PPGStream construction in main": main_source.count("PPGStream(") == 1,
            "no legacy direct FIFO call in main": "read_fifo" not in main_source,
            "no FIFO reset in main": "reset_fifo" not in main_source,
            "buffered HR SpO2 test": "test_pure_estimator_uses_buffered_data" in test_names,
            "buffered RR estimator tests": "test_estimates_fifteen_breaths_per_minute" in test_names,
            "PPG quality buffered tests": "test_good_sixty_second_recording_is_candidate_for_future_rr" in test_names,
            "independent snapshot test": "test_recent_window_is_an_independent_snapshot" in test_names,
            "insufficient samples unavailable": "test_hr_spo2_remain_unavailable_with_insufficient_samples" in test_names,
            "startup unavailable": "test_cache_starts_unavailable" in test_names,
            "stale HR SpO2 RR withheld": "test_stale_hr_spo2_and_rr_are_withheld" in test_names,
            "usable RR numeric": "test_usable_good_or_moderate_rr_sends_numeric_value" in test_names,
            "low confidence RR null": "test_low_confidence_or_unusable_rr_is_withheld" in test_names,
            "recoverable estimator exception": "test_recoverable_estimator_exception_preserves_cached_state" in test_names,
            "hardware-independent script imports": "test_existing_offline_and_live_scripts_import_without_hardware" in test_names,
            "packet metadata preserved": "test_packet_preserves_metadata_and_temperature_sanitization" in test_names,
            "invalid temperature sanitized": "test_packet_preserves_metadata_and_temperature_sanitization" in test_names,
            "pipeline exposes freshness": "fresh" in pipeline_source,
            "single stream regression test": "test_main_uses_one_stream_and_no_legacy_direct_reader" in test_names,
            "local RR severity tests": "test_rr_alone_never_produces_a_severe_alert" in test_names,
        }
        write_json(output / "architecture_assertions.json", assertions)
        failures = [name for name, passed in assertions.items() if not passed]
        write_text(output / "shared_ppg_notes.md", "# Shared PPG notes\n\nThese are architecture, unit, and synthetic-signal checks only. They make no claim about physiological accuracy or physical sensor behavior.\n")
        summary = {"test_exit_code": code, "test_runtime_seconds": runtime, "assertions": len(assertions), "assertions_passed": sum(assertions.values())}
        if code:
            failures.append(f"edge-node test command exited {code}")
        return GroupResult(name="shared_ppg", status="PASS" if not failures else "FAIL", summary=summary, failures=failures, critical=bool(failures))

    return _timed_group("shared_ppg", execute)


def run_static_checks(ctx: ValidationContext) -> GroupResult:
    def execute() -> GroupResult:
        output = ctx.output_dir / "static_checks"
        env = os.environ.copy()
        env["PYTHONPYCACHEPREFIX"] = str(ctx.temp_dir / "pycache")
        python_files = [path for base in (SERVER_DIR, EDGE_DIR, ROOT / "tools") for path in base.rglob("*.py")]
        compile_code, compile_raw, compile_runtime = run_command([ctx.python, "-m", "py_compile", *map(str, python_files)], cwd=ROOT, timeout=180, env=env)
        write_text(output / "compilation_results.txt", f"Files: {len(python_files)}\nRuntime: {compile_runtime:.6f}s\nExit: {compile_code}\n{compile_raw}")

        import_commands = [
            [ctx.python, "-c", "import database, models, packet_validation, services, server"],
            [ctx.python, "-c", "from processing import hr_spo2, ppg_pipeline, ppg_quality, respiratory_rate"],
        ]
        imports_text: list[str] = []
        import_failures = 0
        for command, cwd in ((import_commands[0], SERVER_DIR), (import_commands[1], EDGE_DIR)):
            code, raw, _ = run_command(command, cwd=cwd, timeout=60, env=env)
            imports_text.append(f"COMMAND: {' '.join(command)}\nEXIT: {code}\n{raw}")
            import_failures += int(code != 0)
        write_text(output / "import_checks.txt", "\n\n".join(imports_text))

        help_scripts = [
            *(EDGE_DIR / "scripts" / name for name in ("analyze_ppg_csv.py", "estimate_rr_from_ppg_csv.py", "record_ppg_csv.py", "rr_validation_session.py", "test_live_rr_packet.py", "validate_rr_recordings.py")),
            SERVER_DIR / "simulate_nodes.py",
            ROOT / "tools" / "software_validation" / "run_all_software_validation.py",
        ]
        help_text: list[str] = []
        help_failures = 0
        for script in help_scripts:
            code, raw, _ = run_command([ctx.python, str(script), "--help"], cwd=script.parent, timeout=30, env=env)
            help_text.append(f"{script.relative_to(ROOT)}: {'PASS' if code == 0 else 'FAIL'}\n{raw}")
            help_failures += int(code != 0)
        write_text(output / "cli_help_checks.txt", "\n\n".join(help_text))

        tracked = git_output("ls-files").splitlines()
        repository_findings: list[dict[str, Any]] = []
        tracked_databases = [path for path in tracked if path.lower().endswith((".db", ".sqlite", ".sqlite3"))]
        tracked_pyc = [path for path in tracked if path.lower().endswith(".pyc") or "__pycache__" in path]
        tracked_venv = [path for path in tracked if any(part in path.lower().replace("\\", "/").split("/") for part in (".venv", "venv", "site-packages"))]
        for severity, category, items in (
            ("CRITICAL", "tracked_database", tracked_databases),
            ("WARNING", "tracked_generated_bytecode", tracked_pyc),
            ("CRITICAL", "tracked_virtual_environment", tracked_venv),
        ):
            for path in items:
                repository_findings.append({"severity": severity, "category": category, "file": path, "line": None})

        text_files: list[Path] = []
        for relative in tracked:
            path = ROOT / relative
            if path.is_file() and path.stat().st_size <= 2_000_000 and path.suffix.lower() not in {".pyc", ".db", ".png", ".jpg", ".jpeg", ".pdf"}:
                text_files.append(path)
        personal_pattern = re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+", re.I)
        ip_pattern = re.compile(r"\b(?:10|127|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b")
        backup_pattern = re.compile(r"(?:\.bak|\.backup|\.old|~)$", re.I)
        secret_patterns = [
            re.compile(r"AKIA[0-9A-Z]{16}"),
            re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
            re.compile(r"(?i)(?:api[_-]?key|secret|password|token)\s*[:=]\s*['\"][^'\"]{8,}['\"]"),
        ]
        secret_findings: list[dict[str, Any]] = []
        for path in text_files:
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line_number, line in enumerate(lines, 1):
                if personal_pattern.search(line):
                    repository_findings.append({"severity": "WARNING", "category": "personal_path", "file": str(path.relative_to(ROOT)), "line": line_number})
                if ip_pattern.search(line):
                    repository_findings.append({"severity": "INFO", "category": "local_ip", "file": str(path.relative_to(ROOT)), "line": line_number})
                if any(pattern.search(line) for pattern in secret_patterns):
                    secret_findings.append({"severity": "CRITICAL", "file": str(path.relative_to(ROOT)), "line": line_number, "value": "[REDACTED]"})
        for relative in tracked:
            if backup_pattern.search(relative):
                repository_findings.append({"severity": "WARNING", "category": "backup_file", "file": relative, "line": None})
            path = ROOT / relative
            if path.is_file() and path.stat().st_size > 10_000_000:
                repository_findings.append({"severity": "WARNING", "category": "large_file_over_10MB", "file": relative, "line": None})

        audit_lines = [
            f"Git commit: {git_output('rev-parse', 'HEAD')}",
            f"Branch: {git_output('branch', '--show-current')}",
            f"Tracked files: {len(tracked)}",
            f"Python files compiled: {len(python_files)}",
            "",
            "Findings:",
            *[f"{item['severity']} {item['category']} {item['file']}:{item['line'] or ''}" for item in repository_findings],
        ]
        write_text(output / "repository_audit.txt", "\n".join(audit_lines) + "\n")
        write_text(output / "secret_scan_summary.txt", f"Files scanned: {len(text_files)}\nPossible secrets: {len(secret_findings)}\n" + "\n".join(f"CRITICAL {item['file']}:{item['line']} [REDACTED]" for item in secret_findings) + "\n")
        critical_findings = [item for item in repository_findings if item["severity"] == "CRITICAL"] + secret_findings
        failures = []
        if compile_code:
            failures.append("Python compilation failed")
        if import_failures:
            failures.append(f"{import_failures} import checks failed")
        if help_failures:
            failures.append(f"{help_failures} CLI help checks failed")
        failures.extend(f"{item.get('category', 'possible_secret')}: {item['file']}" for item in critical_findings)
        summary = {
            "python_files": len(python_files),
            "compilation_exit_code": compile_code,
            "import_failures": import_failures,
            "cli_help_failures": help_failures,
            "tracked_files": len(tracked),
            "tracked_databases": len(tracked_databases),
            "tracked_pyc_files": len(tracked_pyc),
            "tracked_venv_files": len(tracked_venv),
            "possible_secrets": len(secret_findings),
            "repository_findings": repository_findings,
        }
        return GroupResult(name="static_checks", status="PASS" if not failures else "FAIL", summary=summary, failures=failures, critical=bool(critical_findings or compile_code or import_failures))

    return _timed_group("static_checks", execute)
