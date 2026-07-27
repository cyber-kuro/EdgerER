from contextlib import asynccontextmanager
import json
from pathlib import Path
import time

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn

import database
import services


BASE_DIR = Path(__file__).resolve().parent


CONFLICT_CODES = {
    "DEVICE_ASSIGNED",
    "DEVICE_NEEDS_CLEANING",
    "DEVICE_NOT_RETIRED",
    "DEVICE_SENSOR_FAULT",
    "DEVICE_OFFLINE",
    "DEVICE_RETIRED",
    "DEVICE_UNAVAILABLE",
    "DUPLICATE_PACKET",
    "ENCOUNTER_DEVICE_ASSIGNED",
    "INACTIVE_ENCOUNTER",
}


def api_result(
    result: dict,
    *,
    success_status: int = 200,
    not_found_codes: set[str] | None = None,
):
    if result.get("ok") is False or result.get("accepted") is False:
        if result.get("code") in (not_found_codes or set()):
            status_code = 404
        else:
            status_code = 409 if result.get("code") in CONFLICT_CODES else 400
        return JSONResponse(content=result, status_code=status_code)
    if success_status != 200:
        return JSONResponse(content=result, status_code=success_status)
    return result


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.initialize_database()
    database.load_state()
    yield


app = FastAPI(lifespan=lifespan)
app.mount(
    "/static",
    StaticFiles(directory=BASE_DIR / "static"),
    name="static",
)
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.post("/api/vitals")
async def receive_vitals(request: Request):
    started_at = time.perf_counter()
    received_at = services.packet_received_at_string()
    raw_body = await request.body()
    raw_packet_json = raw_body.decode("utf-8", errors="replace")

    try:
        packet = json.loads(raw_packet_json)
    except json.JSONDecodeError as error:
        result = services.reject_vitals_packet(
            raw_packet_json=raw_packet_json,
            error=f"malformed JSON: {error.msg}",
            received_at=received_at,
            started_at=started_at,
            code="MALFORMED_JSON",
        )
        return JSONResponse(content=result, status_code=400)

    result = services.receive_vitals(
        packet,
        raw_packet_json=raw_packet_json,
        received_at=received_at,
        started_at=started_at,
    )

    return api_result(result)


@app.get("/api/latest")
def get_latest():
    return services.get_latest()


@app.get("/api/history")
def get_history():
    return services.get_history()


@app.get("/api/packet_logs")
def get_packet_logs(limit: int = 50):
    return services.get_packet_logs(limit)


@app.get("/api/devices")
def get_devices(include_retired: bool = False):
    return services.get_devices(include_retired=include_retired)


@app.post("/api/register_device")
def register_device(device_profile: dict):
    return api_result(services.register_device(device_profile))


@app.post("/api/assign_device")
def assign_device(assignment: dict):
    return api_result(services.assign_device(assignment))


@app.post("/api/devices/mark_clean")
def mark_device_clean(request: dict):
    return api_result(services.mark_device_clean(request))


@app.delete("/api/devices/{device_id}")
def remove_device(device_id: str, reason: str | None = None):
    return api_result(
        services.remove_device(device_id, reason),
        not_found_codes={"UNKNOWN_DEVICE"},
    )


@app.post("/api/devices/{device_id}/restore")
def restore_device(device_id: str):
    return api_result(
        services.restore_device(device_id),
        not_found_codes={"UNKNOWN_DEVICE"},
    )


@app.get("/api/patients")
def get_patients():
    return services.get_patients()


@app.post("/api/action")
def add_action(action: dict):
    return api_result(services.add_action(action))


@app.get("/api/events/{patient_id}")
def get_patient_events(patient_id: str):
    return services.get_patient_events(patient_id)


@app.get("/api/encounters/{patient_id}")
def get_encounters(patient_id: str):
    return services.get_encounters(patient_id)


@app.get("/api/handoff/{patient_id}")
def get_handoff(patient_id: str, encounter_id: str | None = None):
    return services.get_handoff(patient_id, encounter_id)


@app.post("/api/register_patient")
def register_patient(profile: dict):
    return api_result(services.register_patient(profile))


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
