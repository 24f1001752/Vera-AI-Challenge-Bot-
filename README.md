# Vera Challenge Bot (Deterministic v1+)

This repo now includes a working bot implementation for the magicpin AI challenge with all required endpoints:

- `POST /v1/context`
- `POST /v1/tick`
- `POST /v1/reply`
- `GET /v1/healthz`
- `GET /v1/metadata`

It is deterministic, stateful (in-memory), and validates against the local contract smoke test.

## 1) Project layout (added app code)

- `app/main.py` - FastAPI routes and endpoint wiring
- `app/engine.py` - deterministic trigger selection + message composition logic
- `app/store.py` - in-memory state store with versioning/idempotency
- `app/schemas.py` - request/response models
- `app/config.py` - metadata/config values
- `smoke_contract.py` - local contract validator (no LLM key needed)
- `requirements.txt` - dependencies
- `run_local.ps1` - convenience script

## 2) Setup

From project root:

```powershell
python -m pip install -r requirements.txt
```

## 3) Run the API

### Recommended (works even if PowerShell script execution is restricted)

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

### Optional script

```powershell
.\run_local.ps1
```

If script execution is blocked on your machine, use the recommended command above.

## 4) Check the API quickly

Open in browser:

- `http://127.0.0.1:8080/v1/healthz`
- `http://127.0.0.1:8080/v1/metadata`
- `http://127.0.0.1:8080/docs`

## 5) Validate contract behavior (recommended before submission)

Keep API running, then in another terminal:

```powershell
python smoke_contract.py
```

Expected:

```text
[PASS] Contract smoke test complete.
```

## 6) Run official local judge

Edit `judge_simulator.py`:

- set `BOT_URL = "http://127.0.0.1:8080"`
- set `LLM_PROVIDER`
- set `LLM_API_KEY`
- optional `LLM_MODEL`

Then run:

```powershell
python judge_simulator.py
```

## 6.1) Deploy for final submission (Render - fastest)

1. Push this project to a GitHub repo.
2. In Render, choose **New +** -> **Blueprint**.
3. Select your repo; Render auto-detects `render.yaml`.
4. Deploy.
5. After deploy, open:
   - `https://<your-service>.onrender.com/v1/healthz`
   - `https://<your-service>.onrender.com/v1/metadata`
6. Use `https://<your-service>.onrender.com` as the submission base URL.

If you do not want Blueprint, create a Web Service manually with:

- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Health check path: `/v1/healthz`

## 6.2) Alternative deploy (Railway)

1. Create new project from GitHub repo.
2. Railway detects Python automatically.
3. Set start command:
   - `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
4. Deploy and verify `/v1/healthz` and `/v1/metadata`.
5. Submit Railway public URL as base URL.

## 7) Restart / stop operations

### Stop current server (if needed)

Use Ctrl+C in the terminal where uvicorn is running.

### If port is stuck

```powershell
$conn = Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue
if ($conn) { Stop-Process -Id $conn.OwningProcess -Force }
```

Then start uvicorn again.

## 8) Current behavior notes

- Context writes are versioned per `(scope, context_id)`.
- Same/lower version is rejected as `stale_version` (HTTP 409).
- Tick output is capped to max 20 actions.
- Suppression keys are respected once used.
- Reply flow handles stop/defer/accept and auto-reply-like messages deterministically.

## 9) Production caveat

Current state store is in-memory. Restarting the process clears loaded context and conversation state. For deployment, move store to persistent backing (e.g. SQLite/Redis/Postgres) while keeping the same endpoint contract.

