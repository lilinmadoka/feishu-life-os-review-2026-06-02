# Handoff Package Contents

Updated: 2026-05-29

The generated zip package is intended for technical review and project takeover. It includes:

- `app/`: FastAPI service, Agent runtime, Feishu adapters, services, workers.
- `docs/`: project specifications, architecture notes, Feishu setup notes, current handoff notes.
- `scripts/`: local gateway, reminder worker, validation, seed, and schema helper scripts.
- `tests/`: pytest test suite.
- `validation/`: previous validation results and manual checklists.
- `README.md`, `pyproject.toml`, `Makefile`, `railway.json`, `.env.example`.
- `HANDOFF_ARTIFACTS/`: generated snapshot files such as file manifest, database schema, runtime status, test results, and redacted configuration.

The package intentionally excludes:

- `.env`: contains private credentials.
- `.data/`: contains local SQLite database, logs, process IDs, and potentially personal data.
- `.venv/`: local virtual environment.
- `.pytest_cache/`, `.ruff_cache/`, `__pycache__/`, `*.pyc`.
- `feishu_life_os.egg-info/`: generated packaging metadata.

If the receiving programmer needs a real database sample, export a small sanitized fixture instead of sending `.data/lifeos.sqlite3` directly.

