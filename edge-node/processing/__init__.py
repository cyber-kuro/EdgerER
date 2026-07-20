"""Hardware-independent signal processing for live and recorded EdgeER PPG."""

from processing.ppg_quality import (
    PPGData,
    PPGQualityResult,
    analyze_ppg_quality,
    load_ppg_csv,
)
from processing.hr_spo2 import estimate_hr_spo2_from_samples
from processing.ppg_pipeline import SharedPPGPipeline
from processing.respiratory_rate import (
    RespiratoryRateAnalysis,
    RespiratoryRateDiagnostics,
    RespiratoryRateResult,
    estimate_respiratory_rate,
    load_ppg_csv_for_rr,
)


__all__ = [
    "PPGData",
    "PPGQualityResult",
    "analyze_ppg_quality",
    "load_ppg_csv",
    "RespiratoryRateAnalysis",
    "RespiratoryRateDiagnostics",
    "RespiratoryRateResult",
    "SharedPPGPipeline",
    "estimate_hr_spo2_from_samples",
    "estimate_respiratory_rate",
    "load_ppg_csv_for_rr",
]
