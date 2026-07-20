# PPG-derived respiratory-rate research

This folder is an isolated research workspace for estimating respiratory rate
(RR) from the photoplethysmogram produced by a MAX30102 sensor. It is not wired
into the EdgeER server, dashboard, packet validation, database, or node code.
The baseline is research software, not a validated clinical measurement.

## Intended EdgeER workflow

RR should ultimately be derived from the PPG/MAX30102 waveform rather than
primarily entered by a person. A manually counted RR may be retained as an
optional reference or backup, especially while the algorithm is being
validated, but it is not the intended primary input.

The evaluation sequence is:

1. Evaluate the baseline on the public BIDMC PPG and Respiration Dataset.
2. Record failure modes, confidence rejection behavior, and performance by
   subject/recording.
3. Collect synchronized PPG and reference respiration from the EdgeER
   MAX30102 Raspberry Pi hardware.
4. Re-test the same algorithm before considering node or server integration.

The BIDMC dataset is deliberately **not** downloaded by these scripts. Download
and prepare it separately under a local `data/` folder. See
`sample_format.md` for the expected CSV layout and column mapping options.

## Baseline algorithm

`rr_algorithm.py`:

- removes non-finite samples;
- linearly detrends the PPG to remove DC and slow drift;
- applies a fourth-order Butterworth bandpass from approximately 0.08 to
  0.70 Hz (4.8 to 42 breaths/min);
- processes overlapping windows, 32 seconds by default;
- estimates the dominant respiratory modulation frequency with a Welch power
  spectral density;
- derives confidence from peak-band dominance, peak-to-median contrast, and
  spectral concentration;
- rejects estimates below a configurable confidence threshold; and
- returns `resp_rate_bpm`, `rr_confidence`, `rr_quality`, and detailed debug
  values.

The baseline assumes respiratory modulation is visible in the supplied PPG.
Motion, poor sensor contact, harmonics, irregular respiration, vasoconstriction,
and weak modulation can produce rejection or incorrect peak selection. The
confidence score is a starting heuristic and must itself be calibrated.

## Evaluation metrics

Evaluation reports:

- mean absolute error (MAE), in breaths/min;
- root mean squared error (RMSE), in breaths/min;
- signed bias (`predicted - reference`), in breaths/min;
- percentage of accepted estimates within ±2 breaths/min; and
- percentage of reference windows rejected because the algorithm could not
  produce a sufficiently confident estimate.

Metrics are calculated over windows with a usable reference. Accuracy metrics
use accepted predictions only; the rejection percentage is reported separately
so accuracy cannot be improved invisibly by rejecting difficult windows.

## Quick algorithm check

```powershell
cd C:\Users\hassa\Documents\edgeer_server\research\rr_ppg
python -c "import numpy as np; from rr_algorithm import estimate_respiratory_rate; fs=125; t=np.arange(60*fs)/fs; ppg=np.sin(2*np.pi*1.2*t)+0.25*np.sin(2*np.pi*0.25*t); print(estimate_respiratory_rate(ppg, fs))"
```

The synthetic modulation is 0.25 Hz, so the expected RR is approximately
15 breaths/min.

## Running BIDMC evaluation

The scientific packages used are NumPy, SciPy, pandas, and Matplotlib.

1. Create `research/rr_ppg/data/`.
2. Place locally prepared BIDMC CSV files in it.
3. Make sure each evaluated CSV contains a PPG column and either a reference RR
   column or a reference respiration waveform.
4. Run:

```powershell
cd C:\Users\hassa\Documents\edgeer_server\research\rr_ppg
python evaluate_bidmc_rr.py --data-dir data --sampling-rate 125
```

For explicit column names:

```powershell
python evaluate_bidmc_rr.py `
  --data-dir data `
  --ppg-column PLETH `
  --reference-column reference_rr_bpm `
  --reference-kind rate `
  --sampling-rate 125
```

For a reference respiration waveform:

```powershell
python evaluate_bidmc_rr.py `
  --data-dir data `
  --ppg-column PLETH `
  --reference-column RESP `
  --reference-kind waveform `
  --sampling-rate 125
```

Use `--sampling-rate 0 --time-column "Time [s]"` to infer the sampling rate
from timestamps. Run `python evaluate_bidmc_rr.py --help` for all options.

Generated artifacts:

- `results/bidmc_rr_results.csv`: one row per analysis window;
- `results/bidmc_rr_metrics.csv`: aggregate metrics;
- `plots/*_diagnostic.png`: per-recording RR and confidence traces;
- `plots/bidmc_rr_scatter.png`: predicted-versus-reference agreement; and
- `plots/bidmc_rr_bland_altman.png`: bias/agreement diagnostic.
