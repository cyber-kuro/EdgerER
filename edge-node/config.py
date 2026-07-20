from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent

SERVER_URL = "http://192.168.0.15:8000/api/vitals"
PATIENT_ID = "P001"
DEVICE_ID = "PI-001"
DEVICE_TYPE = "RASPBERRY_PI"
FIRMWARE_VERSION = "1.0.0"

# The MAX30102 is configured for 100 samples/s with four-sample FIFO
# averaging, resulting in a nominal effective stream rate of 25 Hz.
PPG_SAMPLE_INTERVAL_SEC = 0.04
PPG_RING_BUFFER_SECONDS = 120.0
PPG_RECORD_SECONDS = 60.0

# Shared PPG processing windows, refresh cadence, and cache freshness.
# All durations are seconds; sample counts assume the nominal 25 Hz stream.
HR_SPO2_WINDOW_SEC = 12.0
HR_SPO2_MIN_DURATION_SEC = 10.0
HR_SPO2_MIN_SAMPLES = 200
HR_SPO2_REFRESH_INTERVAL_SEC = 6.0
HR_SPO2_STALE_AFTER_SEC = 20.0

PPG_QUALITY_WINDOW_SEC = 60.0
PPG_QUALITY_REFRESH_INTERVAL_SEC = 10.0
PPG_QUALITY_STALE_AFTER_SEC = 30.0

RR_WINDOW_SEC = PPG_RECORD_SECONDS
RR_MIN_DURATION_SEC = 45.0
RR_MIN_SAMPLES = 1_000
RR_REFRESH_INTERVAL_SEC = 300.0
RR_STALE_AFTER_SEC = 360.0

# Before a cache has its first result, retry this often without running an
# estimator until the required buffered duration/sample count is available.
PPG_WARMUP_RETRY_INTERVAL_SEC = 5.0
PPG_HEALTH_LOG_INTERVAL_SEC = 60.0

LOG_DIR = BASE_DIR / "logs"
