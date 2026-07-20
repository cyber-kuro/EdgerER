from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent

SERVER_URL = "http://192.168.0.15:8000/api/vitals"
PATIENT_ID = "P001"
DEVICE_ID = "PI-001"
DEVICE_TYPE = "RASPBERRY_PI"
FIRMWARE_VERSION = "0.2.0"

# The MAX30102 is configured for 100 samples/s with four-sample FIFO
# averaging, resulting in a nominal effective stream rate of 25 Hz.
PPG_SAMPLE_INTERVAL_SEC = 0.04
PPG_RING_BUFFER_SECONDS = 120.0
PPG_RECORD_SECONDS = 60.0

LOG_DIR = BASE_DIR / "logs"
