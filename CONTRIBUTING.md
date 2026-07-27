# Contributing

EdgeER is currently maintained as a portfolio engineering prototype. Contributions should preserve its safety boundaries and avoid implying clinical validation.

## Development principles

- Keep the server authoritative for workflow and device state.
- Preserve one-reader shared-buffer access to the MAX30102.
- Do not convert low-confidence candidate measurements into accepted physiological values.
- Add regression tests for bug fixes.
- Use temporary databases for destructive tests.
- Do not commit runtime databases, raw logs, virtual environments, secrets, or private data.
- Document behavioral changes and limitations.

## Before submitting changes

```bash
python -m pytest
python -m compileall .
```

For repository-wide engineering validation:

```bash
python tools/software_validation/run_all_software_validation.py --quick
```
