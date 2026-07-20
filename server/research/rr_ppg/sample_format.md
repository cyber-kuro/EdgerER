# Expected local BIDMC CSV format

The evaluator reads CSV files recursively from a local data directory. It does
not download or modify the BIDMC dataset. Each evaluated CSV must contain PPG
and a synchronized respiration reference in the same row/sample timeline.

## Recommended format: reference RR values

```csv
time_seconds,ppg,reference_rr_bpm
0.000,42891,16
0.008,43022,16
0.016,43108,16
```

- `time_seconds`: monotonically increasing sample time; optional when
  `--sampling-rate` is supplied.
- `ppg`: raw or consistently preprocessed PPG/PLETH samples.
- `reference_rr_bpm`: synchronized reference rate in breaths/min. A slower
  numeric reference may repeat across many PPG samples.

Example:

```powershell
python evaluate_bidmc_rr.py --data-dir data --sampling-rate 125
```

## Alternative format: reference respiration waveform

```csv
Time [s],PLETH,RESP
0.000,42891,-0.031
0.008,43022,-0.028
0.016,43108,-0.021
```

Tell the evaluator explicitly that `RESP` is a waveform:

```powershell
python evaluate_bidmc_rr.py `
  --data-dir data `
  --ppg-column PLETH `
  --reference-column RESP `
  --reference-kind waveform `
  --sampling-rate 125
```

For waveform references, the starter evaluator estimates the dominant
respiratory frequency from the reference waveform in each analysis window. For
publication-grade evaluation, confirm this reference-extraction method against
the dataset documentation and annotations.

## BIDMC column adaptation

The public BIDMC distribution may expose signals and numerics in separate files
or use names such as `PLETH`, `RESP`, and `Time [s]`. Prepare synchronized local
CSV files before running this script. Do not align streams merely by row number
if their timestamps or sampling rates differ; resample or join them by time.

Automatic column matching is case-insensitive and ignores spaces/punctuation.
Recognized examples include:

- PPG: `ppg`, `pleth`, `photoplethysmogram`, `ppg_signal`
- reference: `reference_rr_bpm`, `resp_rate_bpm`, `respiratory_rate`,
  `resp_rate`, `rr`, `resp`
- time: `time_seconds`, `time_s`, `time`, `timestamp`

Override any automatic choice:

```powershell
python evaluate_bidmc_rr.py `
  --data-dir C:\data\bidmc_prepared `
  --pattern "*.csv" `
  --ppg-column "PLETH" `
  --reference-column "RESP" `
  --time-column "Time [s]" `
  --reference-kind waveform `
  --sampling-rate 0
```

`--reference-kind auto` treats a stable 4–60 numeric series as RR values and
other series as a waveform. Because some waveform amplitudes can resemble RR
values numerically, explicitly selecting `rate` or `waveform` is safer.

## Data-quality expectations

- Sampling intervals should be regular, or the signal should be resampled to a
  regular rate first.
- PPG and reference must be synchronized.
- Units and preprocessing should be consistent across recordings.
- NaNs are tolerated, but windows with too few valid samples or low spectral
  confidence will be rejected.
- At least one complete analysis window is required (32 seconds by default).

