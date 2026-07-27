# Testing

## Automated tests

Run from the repository root or the relevant subproject:

```bash
python -m pytest
```

## Compilation

```bash
python -m compileall edge-node server tools
```

## Quick software validation

```powershell
python tools/software_validation/run_all_software_validation.py --quick
```

## Full software validation

```powershell
python tools/software_validation/run_all_software_validation.py `
  --full `
  --duration-seconds 60 `
  --device-counts 1,5,10,20
```

The validation runner uses isolated temporary SQLite databases and deterministic seeds. It separates intentional packet rejection from unexpected request failure.

## RR validation on Raspberry Pi

```bash
cd edge-node
source .venv/bin/activate
python scripts/rr_validation_session.py
python scripts/rr_validation_session.py --summary
```

## One-shot live RR packet

```bash
python scripts/test_live_rr_packet.py --no-post
python scripts/test_live_rr_packet.py
```

## Public evidence

The complete public report is stored in `docs/technical_report/`, and the compact supporting data package is stored in `docs/data/`.
