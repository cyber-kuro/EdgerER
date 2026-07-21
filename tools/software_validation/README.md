# EdgeER software validation tooling

This tooling exercises EdgeER with temporary SQLite databases and deterministic
synthetic packets. It does not access GPIO, I2C, physical sensors, or the normal
`server/edgeer.db` database.

Quick run:

```powershell
python tools/software_validation/run_all_software_validation.py --quick
```

Bounded full run:

```powershell
python tools/software_validation/run_all_software_validation.py --full
```

Five-minute scalability runs for each requested device count:

```powershell
python tools/software_validation/run_all_software_validation.py --full --duration-seconds 300 --device-counts 1,5,10,20
```

Useful options:

- `--output-dir PATH`
- `--seed INTEGER`
- `--keep-temp-data`
- `--skip-browser`
- `--skip-scalability`
- `--device-counts 1,5,10,20`
- `--duration-seconds SECONDS`

The runner continues after non-critical group failures, records `PASS`, `FAIL`,
`BLOCKED`, or `SKIPPED`, and returns nonzero only for a critical failure. Browser
automation uses standalone Playwright when available. When it is unavailable,
API/template checks still run and the report records the exact enablement
command instead of inventing browser results.
