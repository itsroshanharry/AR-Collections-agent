# Setup & Run Guide

Every command needed to go from a fresh clone to a live voice call. Windows/PowerShell throughout;
`uv run` works identically on macOS/Linux.

---

## 0. Prerequisites

| Tool | Version used | Install |
|---|---|---|
| Python | **3.12** (not 3.13 — torch/onnxruntime wheels lag) | `winget install Python.Python.3.12` |
| uv | 0.10+ | `winget install astral-sh.uv` |
| git | any | `winget install Git.Git` |
| Docker | only for local Kokoro TTS | optional |

---

## 1. Install

```powershell
git clone <your-repo> ar-collections-agent
cd ar-collections-agent

# Dashboard + data tooling only (no audio stack, ~30s)
uv sync --extra dashboard --extra data

# Add the voice stack when you need it (pulls torch, ~3-5 min)
uv sync --extra dashboard --extra data --extra voice
```

> The repo is a set of top-level packages, not an installed distribution. Run modules with `-m` from
> the repo root (`uv run python -m data.generate_dataset`), or set `PYTHONPATH=.`

---

## 2. Keys

```powershell
Copy-Item .env.example .env
notepad .env
```

**Only `GROQ_API_KEY` is required** to run the dashboard, the eval sweep, and terminal-based voice.

| Variable | Where to get it | Needed for | Cost |
|---|---|---|---|
| `GROQ_API_KEY` | [console.groq.com/keys](https://console.groq.com/keys) | **Everything** | Free, no card |
| `CEREBRAS_API_KEY` | [cloud.cerebras.ai](https://cloud.cerebras.ai) | Failover chain (optional) | Free tier |
| `DEEPGRAM_API_KEY` | [console.deepgram.com](https://console.deepgram.com) → API Keys | Voice with `VOICE_PROVIDER=cloud` | $200 credit, no card |
| `LIVEKIT_URL` / `_API_KEY` / `_API_SECRET` | [cloud.livekit.io](https://cloud.livekit.io) → project → Settings → Keys | Browser voice only | Free, no card |
| `DATABASE_URL` | [neon.tech](https://neon.tech) | Deployment only | Free, always-on |
| `OPERATOR_API_KEY` / `ADMIN_API_KEY` | Any long random string you make up | Locking a deployed demo (open locally without them) | — |

**Do not put real keys in `.env.example`** — it is committed. Only `.env` is gitignored.

Check that every key you set actually works:

```bash
uv run python -m shared.check_keys
```

Each service gets the smallest real request that proves its key (Groq and Cerebras: list
models, confirm the configured ones exist, run a one-line completion; Deepgram: list projects;
LiveKit: list rooms with a signed token; plus Langfuse, the database, and local Ollama/Kokoro if
running). Unset keys are skipped. It exits non-zero if a configured service fails, and never
prints key values.

### Groq quota — plan around this

Free tier is **8,000 tokens/min and 200,000 tokens/day, per model**. One full 18-scenario sweep costs
~50K tokens, so roughly **4 sweeps per day**. The client paces itself to avoid 429s, but the daily
cap is a hard wall. `gpt-oss-120b` and `gpt-oss-20b` have **separate** pools.

---

## 3. Dataset

```powershell
# Optional but recommended: real amount/ageing distributions from Kaggle.
# Kaggle now issues a token string, not a kaggle.json file.
New-Item -ItemType Directory -Force "$env:USERPROFILE\.kaggle" | Out-Null
Set-Content "$env:USERPROFILE\.kaggle\access_token" -Value "KGAT_your_token" -NoNewline -Encoding ascii

uv pip install kaggle
uv run kaggle datasets download -d pradumn203/payment-date-prediction-for-invoices-dataset -p data/raw/
Expand-Archive data\raw\*.zip -DestinationPath data\raw -Force

# Generate. Works without the Kaggle file too (warns, uses built-in distributions).
uv run python -m data.generate_dataset --rows 500 --seed 42
```

Deterministic for a given `--seed` — the same seed always produces a byte-identical CSV.

---

## 4. Run the A/B dashboard

```powershell
uv run uvicorn ab_dashboard.main:app --reload
```

Open <http://127.0.0.1:8000/docs> and drive it from there:

| Step | Call | Body |
|---|---|---|
| 1 | `POST /prompts/sync` | — imports the agent's own prompt YAMLs |
| 2 | `POST /experiments` | `{"variant_a_id": "collections_formal_v1", "variant_b_id": "collections_friendly_v2", "limit": 3}` |
| 3 | `GET /experiments/{id}` | poll until `status` is `completed` |
| 4 | `GET /experiments/{id}/results` | win-rate, latency, hallucination-rate, per-scenario |

Drop `"limit": 3` for the full 18 scenarios (~14 min, ~100K tokens for both variants).

---

## 5. Run the eval sweep

```powershell
# Full sweep - 18 scenarios, ~14 min, ~50K tokens
uv run python -m voice_agent.agent --simulate

# Quick check - first 3 scenarios, ~90s
uv run python -m voice_agent.agent --simulate --limit 3

# Specific scenarios only (cheap re-verification after a change)
uv run python -m voice_agent.agent --simulate --only S07 S17

# Persist every call to call_logs
uv run python -m voice_agent.agent --simulate --log-to-db

# Force one prompt instead of tier-matching
uv run python -m voice_agent.agent --simulate --prompt-id collections_friendly_v2
```

Reports outcome accuracy, promise-date accuracy, and injections resisted.

---

## 6. Voice

### 6a. Terminal — no LiveKit, no cloud minutes

```powershell
uv run python -m voice_agent.agent console
```

Uses your mic and speakers directly. Needs a TTS backend (below).

### 6b. Browser — via LiveKit Cloud

```powershell
uv run python -m voice_agent.agent dev
```

Then: [cloud.livekit.io](https://cloud.livekit.io) → your project → **Agent Console** → select
`ar-collections-agent` → connect. The agent opens the call itself, using the highest-priority invoice
from the dunning queue.

### TTS backend — pick one

**Deepgram** (used for the verified live call):
```
VOICE_PROVIDER=cloud
DEEPGRAM_API_KEY=<your key>
```

**Kokoro, local and free:**
```powershell
docker compose -f infra/docker-compose.kokoro.yml up -d
```
```
VOICE_PROVIDER=local
```
Needs ~2GB disk for the image. Check free space first — a full disk fails mid-pull with confusing
`input/output error` messages.

### If you restart the agent

Kill the old process first, or it holds the log file and the restart fails silently:

```powershell
Get-Process python | Stop-Process -Force
```

---

## 7. Tests

```powershell
uv run pytest -q          # ~490 tests, fully mocked, no network
uv run ruff check .
```

CI also runs the whole suite against a real Postgres. To do that yourself, set
`TEST_POSTGRES_URL` to a **throwaway** database: every test drops and recreates its `public`
schema, so never point it at the Neon database the deployed demo uses.

---

## 8. Inspect results

```powershell
uv run python -c @"
import sqlite3, json
c = sqlite3.connect('local.db')
for r in c.execute('select invoice_id, mode, outcome, promise_to_pay_date, composite from call_logs order by created_at desc limit 10'):
    print(r)
"@
```

---

## 9. Deploy (optional)

```powershell
docker build -f infra/Dockerfile.dashboard -t ar-dashboard .
docker run -p 8000:8000 --env-file .env ar-dashboard
```

Render: point a Blueprint at [`infra/render.yaml`](infra/render.yaml). Set `DATABASE_URL` to a **Neon**
connection string — Render's own free Postgres is deleted 30 days after creation. Free web services
also spin down after ~15 min idle with a 30–60s cold start, so warm it before a live demo.

### Neon

1. Sign up at [neon.tech](https://neon.tech) (free plan, no card) and create a project. Pick
   region **AWS US West 2 (Oregon)**, next to Render's default region.
2. **Connect** → switch **Connection pooling** off → copy the `postgresql://...` string. Use it
   as given; the app switches it to the async driver itself, and `sslmode=require` stays.
3. Optional check from your machine, without saving it anywhere (`Read-Host` input stays out of
   your shell history):

   ```powershell
   $env:DATABASE_URL = Read-Host "Neon connection string"
   uv run python -m shared.check_keys      # Database should read OK
   Remove-Item Env:DATABASE_URL
   ```

The tables are created by the migrations on the app's first start; there is nothing to run by hand.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `No GROQ_API_KEY set` | `.env` missing or key in `.env.example` | Keys go in `.env`; `.env.example` is a template |
| `AllProvidersFailedError ... tokens per day (TPD)` | Daily 200K quota exhausted | Wait, switch `GROQ_MODEL_LARGE` to the 20B (separate pool), or use another key |
| `ws_url is required` | LiveKit vars missing | Add all three; `console` mode needs none |
| `Plugins must be registered on the main thread` | `livekit.plugins` imported inside a function | Must stay at module level in `voice_factory.py` (there's a test for this) |
| `input/output error` on docker pull | Disk full | Free space; `docker builder prune -a` is the safest large reclaim |
| Agent restart fails silently | Old process holds the log file | `Get-Process python \| Stop-Process -Force` |
| Agent joins but says nothing | Job crashed after accepting | Check `agent_dev.log` for the traceback |
| Eval sweep hangs on 429s | Concurrency too high | `max_concurrent_llm_calls` is 1 by default; raise only on a paid tier |
