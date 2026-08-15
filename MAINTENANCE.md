# Maintenance Guide

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set `DEEPSEEK_API_KEY` in `.env`, place Markdown documents in `knowledge_base/`, then build the index:

```bash
PYTHONPATH=src python -m scripts.build_index
```

## Run

```bash
PYTHONPATH=src python -m agents.web_server --port 8787
```

Open `http://127.0.0.1:8787/`. Do not open `scripts/webui.html` directly because file pages cannot call the local API reliably.

## Validate changes

```bash
python -m py_compile src/agents/*.py scripts/*.py tests/*.py
.venv/bin/python tests/test_smoke.py
.venv/bin/python tests/test_web_store.py
```

## Runtime files

The following are local-only and intentionally ignored by Git:

- `.env`: API credentials
- `data/`: indexes, runtime overrides, SQLite sessions, and application logs
- `logs/`: legacy logs
- `benchmark/results.*`: generated benchmark reports

Back up `data/webui.sqlite3` if session history must be preserved during a machine migration.

## Release checklist

1. Run both test files and the Python compile check.
2. Search tracked files for credentials and machine-specific absolute paths.
3. Review `git status` and `git diff --cached` before committing.
4. Never commit `.env`, `data/runtime.json`, databases, logs, indexes, or knowledge-base documents.
5. Tag releases using semantic versions when behavior changes are ready for users.

## Security

If a credential is committed or pasted into a public system, removing it from the latest revision is not sufficient. Revoke or rotate it at the provider, then remove it from Git history before publishing. See `SECURITY.md` for the application threat model.
