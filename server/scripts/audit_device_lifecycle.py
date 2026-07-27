"""Audit or explicitly restore EdgeER device lifecycle records.

Audit mode opens SQLite read-only. Restoration requires one or more explicit
``--restore DEVICE_ID`` arguments; combining them with ``--dry-run`` previews
the requested changes without writing.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any


SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import database
import services


LIFECYCLE_COLUMNS = {"is_active", "retired_at", "retired_reason"}


def open_read_only(database_path: Path) -> sqlite3.Connection:
    """Open an existing SQLite database without permitting writes."""
    connection = sqlite3.connect(
        f"file:{database_path.resolve().as_posix()}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    return connection


def audit_database(database_path: Path) -> dict[str, Any]:
    """Return lifecycle records and inconsistencies without changing data."""
    with closing(open_read_only(database_path)) as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "devices" not in tables:
            return {
                "database": str(database_path),
                "active_devices": [],
                "retired_devices": [],
                "inconsistencies": ["devices table is missing"],
            }

        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(devices)")
        }
        if not LIFECYCLE_COLUMNS <= columns:
            missing = sorted(LIFECYCLE_COLUMNS - columns)
            return {
                "database": str(database_path),
                "active_devices": [],
                "retired_devices": [],
                "inconsistencies": [
                    "lifecycle migration has not run; missing columns: "
                    + ", ".join(missing)
                ],
            }

        devices = [
            dict(row)
            for row in connection.execute(
                """
                SELECT device_id, status, is_active, retired_at, retired_reason,
                       assigned_patient_id, assigned_encounter_id, last_seen
                FROM devices
                ORDER BY device_id
                """
            )
        ]
        active_references: dict[str, list[dict[str, str]]] = {}
        if "encounters" in tables:
            for row in connection.execute(
                """
                SELECT encounter_id, patient_id, assigned_device_id
                FROM encounters
                WHERE active = 1 AND assigned_device_id <> ''
                """
            ):
                active_references.setdefault(row["assigned_device_id"], []).append(
                    {
                        "encounter_id": row["encounter_id"],
                        "patient_id": row["patient_id"],
                    }
                )

    inconsistencies: list[dict[str, Any]] = []
    for device in devices:
        device_id = device["device_id"]
        is_active = bool(device["is_active"])
        references = active_references.get(device_id, [])
        if not is_active and (
            device["assigned_patient_id"]
            or device["assigned_encounter_id"]
            or references
        ):
            inconsistencies.append(
                {
                    "device_id": device_id,
                    "code": "RETIRED_WITH_ACTIVE_ASSIGNMENT",
                    "active_encounters": references,
                }
            )
        if is_active and (device["retired_at"] or device["retired_reason"]):
            inconsistencies.append(
                {"device_id": device_id, "code": "ACTIVE_WITH_RETIREMENT_METADATA"}
            )
        if not is_active and not device["retired_at"]:
            inconsistencies.append(
                {"device_id": device_id, "code": "RETIRED_WITHOUT_TIMESTAMP"}
            )

    return {
        "database": str(database_path),
        "active_devices": [d["device_id"] for d in devices if d["is_active"]],
        "retired_devices": [d["device_id"] for d in devices if not d["is_active"]],
        "inconsistencies": inconsistencies,
        "devices": devices,
    }


def restore_devices(database_path: Path, device_ids: list[str]) -> list[dict[str, Any]]:
    """Restore only the explicitly named devices using application rules."""
    database.DATABASE_PATH = database_path.resolve()
    database.initialize_database()
    database.load_state()
    results: list[dict[str, Any]] = []
    for device_id in dict.fromkeys(device_ids):
        result = services.restore_device(device_id)
        results.append(
            {
                "device_id": device_id,
                "ok": result.get("ok", False),
                "restored": result.get("restored", False),
                "message": result.get("message") or result.get("error"),
            }
        )
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        default=database.DATABASE_PATH,
        help="SQLite database path (defaults to EDGEER_DATABASE_PATH/server database)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Audit and preview requested restores without modifying SQLite",
    )
    parser.add_argument(
        "--restore",
        action="append",
        default=[],
        metavar="DEVICE_ID",
        help="Explicit device ID to restore; repeat for multiple devices",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    database_path = args.database.resolve()
    if not database_path.exists():
        print(json.dumps({"error": f"database not found: {database_path}"}, indent=2))
        return 2

    before = audit_database(database_path)
    output: dict[str, Any] = {"before": before}
    if args.restore:
        if args.dry_run:
            output["restore_preview"] = list(dict.fromkeys(args.restore))
            output["modified"] = False
        else:
            output["restore_results"] = restore_devices(database_path, args.restore)
            output["after"] = audit_database(database_path)
            output["modified"] = True
    else:
        output["modified"] = False

    print(json.dumps(output, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
