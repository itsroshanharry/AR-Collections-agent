# Project Plan: AI Collections Voice Agent + Prompt A/B Testing Dashboard

## 1. Why We're Building This

I'm an AI Engineer (2 years at Accenture, multi-agent systems / MCP / RAG / production LLM infra)
targeting roles at fintech-adjacent AI startups, specifically ones building agentic AI for finance
back-office automation (AR/AP) — e.g. Peakflo. Their engineering JDs (FDE, AI Automation Engineer)
explicitly call out:

- Voice AI + prompt engineering for financial use cases (collections calls, policy servicing)
- Continuous refinement via LLM self-reflection / LLM-as-judge scoring
- Prompt versioning and A/B testing pipelines
- AR/AP domain fluency (dunning workflows, invoice reconciliation, GST/multi-region compliance)

This project is designed to directly demonstrate all of the above, using real domain framing
(modeled loosely on how companies like Ninja Van and InMobi use AR automation) rather than a
generic chatbot demo. Two deliverables:

1. **AI Collections Voice Agent** — calls (simulated) customers about overdue invoices, handles
   multi-turn/interruption-aware dialogue, extracts promise-to-pay commitments.
2. **Prompt A/B Testing Dashboard** — standalone reusable tool: takes two prompt versions, runs
   them against a fixed eval set, reports win-rate / latency / hallucination-rate. Usable on its
   own as a portfolio artifact regardless of which company I'm applying to.

Both are built to run fully free (open-source models locally for dev, free-tier cloud APIs as a
fallback/deployment option) since budget is $0.

---

## 2. Workspace / Folder Structure

Single monorepo, multiple sub-project folders. This works fine — keeps shared code (data models,
eval logic) reusable across both the voice agent and the dashboard without duplicating anything.

```
ar-collections-agent/
├── README.md
├── .env.example                    # API keys template (Groq, Cerebras, Deepgram, etc.)
├── pyproject.toml / requirements.txt
│
├── shared/                         # code used by both sub-projects
│   ├── models.py                   # Invoice, Customer, CallLog, PromptVersion schemas (Pydantic)
│   ├── llm_client.py                # Groq primary + Cerebras fallback wrapper
│   └── eval/
│       ├── judge.py                 # LLM-as-judge scoring logic
│       └── eval_set.py              # fixed eval scenarios (loaded from data/)
│
├── data/
│   ├── raw/                         # Kaggle datasets (payment-date-prediction, invoice datasets)
│   ├── synthetic/                   # Faker-generated AR dataset w/ GST fields (GSTIN, HSN/SAC,
│   │                                 #   days-overdue, dispute flags, multi-currency)
│   └── generate_dataset.py
│
├── voice_agent/                    # Project 1: Collections Voice Agent
│   ├── agent.py                     # LiveKit Agents worker entrypoint
│   ├── dunning_logic.py             # risk-tier classification, call prioritization
│   ├── prompts/
│   │   ├── v1_formal.yaml
│   │   ├── v2_friendly.yaml
│   │   └── ...                      # versioned prompt templates
│   ├── stt_tts_local.py             # Parakeet STT + Kokoro TTS (local dev)
│   ├── stt_tts_cloud.py             # Deepgram STT+TTS (deployed fallback)
│   └── call_logger.py               # writes transcripts + judge scores to DB
│
├── ab_dashboard/                   # Project 2: Prompt A/B Testing Dashboard
│   ├── main.py                      # FastAPI app
│   ├── routers/
│   │   ├── prompts.py                # POST /prompts, GET /prompts/{id}/history
│   │   └── experiments.py            # POST /experiments, GET /experiments/{id}/results
│   ├── experiment_runner.py         # runs prompt A vs B against eval set
│   └── db.py                        # Postgres models (prompt_versions, experiments, results)
│
├── frontend/                       # optional, built after API is validated via /docs
│   └── (React + Recharts, calls ab_dashboard API)
│
└── infra/
    ├── Dockerfile.agent
    ├── Dockerfile.dashboard
    └── render.yaml                  # Render deployment config
```

---

## 3. Infra Decisions (Recap)

**LLM**
- Primary: **Groq** — Llama 3.3 70B for reasoning/dialogue, Llama 3.1 8B for lightweight
  classification tasks. Free tier: 30K tokens/min, 14,400 req/day, no card needed. Fast enough
  for voice latency requirements (~320-390 tok/s).
- Fallback: **Cerebras** — same open models (Llama 3.3 70B, Qwen3, GPT-OSS 120B), free tier 30
  RPM / 1M tokens/day. Build a simple retry-on-429 fallback chain — Groq → Cerebras → (optionally
  local Ollama) — this is itself a good engineering talking point (no single point of failure).
- Optional multimodal/long-context: Google AI Studio (Gemini 2.5 Flash), free 1,500 req/day.

**Voice stack — local dev (fully free, needs a GPU)**
- VAD: Silero VAD
- STT: Parakeet TDT 0.6B V2 (NVIDIA, near-top of HF Open ASR leaderboard, ~3000x real-time)
- TTS: Kokoro (local, open weights)
- Orchestration: **LiveKit Agents** (same framework mentioned in Peakflo's own job posting —
  deliberate choice)

**Voice stack — deployed version (since free hosts have no GPU/limited RAM)**
- Swap STT+TTS to **Deepgram** — $200 free credit, no card, no expiry, covers ~435 hrs of
  transcription, includes Aura-2 TTS. Plenty for a portfolio project's lifetime.
- Alternative/zero-cost fallback: browser-native Web Speech API (lower quality, but literally free
  forever, no backend dependency) — could be a lightweight demo mode.

**Backend**
- **FastAPI** for both the voice agent's supporting API and the A/B dashboard — Python-native
  (matches LiveKit Agents), async-friendly (needed for concurrent STT/LLM/TTS streaming), and
  gives free auto-generated interactive docs at `/docs` (doubles as an instant demo UI before any
  frontend exists).

**Database**
- Postgres — via Render's free tier, alongside the web service, so everything lives on one
  platform without juggling separate signups.

**Deployment**
- **Render free Hobby plan** — genuinely free, no card, deploys web services (512MB RAM) +
  Postgres. Caveat: spins down after ~15 min inactivity, 30-50s cold start — fine for a portfolio
  demo, just warm it up before a live interview call.
- Frontend (if built): Vercel or Cloudflare Pages — free, no catches, for static/React apps.
- Avoid Railway/Fly.io — both dropped their real free tiers in 2024-2025.

**Eval**
- Ragas / DeepEval for hallucination detection and structured output validation (already familiar
  from my Accenture work + SaaS Admin Safety Benchmark project).

---

## 4. Dataset — Sources & Enrichment

**Base data (real-shaped, from Kaggle):**
- *"Customer Invoices Dataset - Payment Date Prediction on Open Invoices"* (Kaggle, pradumn203)
- *"Customer Invoice Dataset - Order Amount Prediction"* (Kaggle, nikhil1479)
- Optional fallback/supplement: *"Mock Invoices Dataset"* (Kaggle, cankatsrc) — already
  Faker-generated, structured similarly, useful if the two real-shaped datasets are too small.

**Download steps:**
```bash
pip install kaggle --break-system-packages
# Kaggle account -> Account settings -> "Create New API Token" -> downloads kaggle.json
mkdir -p ~/.kaggle && mv kaggle.json ~/.kaggle/

kaggle datasets download -d pradumn203/customer-invoices-dataset -p data/raw/
unzip data/raw/customer-invoices-dataset.zip -d data/raw/
```

**Why a synthetic layer is needed on top:** public invoice datasets give realistic
amount/date distributions but never include AR-collections-specific fields — GSTIN, HSN/SAC
codes, dispute flags, reminder/call history, promise-to-pay outcomes. No public dataset models
"AR collections calling" scenarios, so this layer has to be constructed. Approach: use the
Kaggle data's statistical shape as a base, then enrich each row synthetically with Faker.

**`data/generate_dataset.py`:**
```python
import pandas as pd
from faker import Faker
import random

fake = Faker('en_IN')  # Indian locale for GST-relevant fake data

# 1. Load real Kaggle data as the statistical base
base_df = pd.read_csv("data/raw/customer_invoices.csv")

# 2. Enrich each row with synthetic AR/GST fields
def enrich_row(row):
    return {
        **row,
        "gstin": fake.bothify(text="##???####?#Z#"),  # GSTIN-shaped fake ID
        "hsn_sac_code": random.choice(["998314", "998313", "996511"]),
        "days_overdue": max(0, (pd.Timestamp.now() - pd.to_datetime(row["due_date"])).days),
        "dispute_flag": random.random() < 0.08,       # ~8% of invoices disputed
        "promise_to_pay_date": None,                  # filled in after agent calls run
        "reminder_count": random.randint(0, 4),
        "currency": random.choice(["INR", "USD", "SGD"]),
    }

enriched = base_df.apply(enrich_row, axis=1, result_type="expand")
enriched.to_csv("data/synthetic/ar_dataset_enriched.csv", index=False)
```

`promise_to_pay_date` starts `None` and gets populated only once the voice agent's simulated
calls actually extract a commitment from a "customer" — this is the field the continuous-
refinement/judge-scoring loop cares most about (did the agent correctly extract this?).

Output of this script (`data/synthetic/ar_dataset_enriched.csv`) is what feeds the dunning
logic and the voice agent's per-call context — not the raw Kaggle files directly.

---

## 5. Implementation Steps — Collections Voice Agent

1. **Dataset**
   - Use `data/synthetic/ar_dataset_enriched.csv` (see Section 4) as the source of truth for
     invoice/customer state driving every simulated call.

2. **Dunning / prioritization logic**
   - Classify invoices into risk tiers (e.g. low/medium/high overdue risk) based on days-overdue
     + payment history.
   - Decide call tone/escalation level per tier (gentle reminder vs firm escalation).

3. **Voice-optimized prompts**
   - Write prompt templates per tone/scenario, optimized for TTS: short sentences, no ambiguous
     abbreviations, natural pause points.
   - Version every prompt (v1_formal, v2_friendly, etc.) with a version ID stored alongside the
     template — this feeds directly into the A/B dashboard later.

4. **Multi-turn, interruption-aware dialogue**
   - Handle mid-call interjections ("I already paid," "can I get an extension") via LiveKit's
     adaptive interruption handling.
   - Extract structured outcomes per call: promise-to-pay date, dispute raised, no answer, etc.

5. **Continuous refinement loop**
   - After each (simulated) call, run the transcript through an LLM-as-judge: did it stay on
     topic, was tone appropriate, did it correctly extract a promise-to-pay date, any hallucinated
     invoice details?
   - Log judge scores against the prompt version used — flags underperforming prompt variants
     automatically.

6. **Logging/observability**
   - Store call transcripts, judge scores, and prompt version IDs in Postgres — same pattern as my
     Agent Swarm project's S3 execution-log approach, adapted here.

---

## 6. Implementation Steps — Prompt A/B Testing Dashboard

1. **Fixed eval set**
   - Build a small set of representative scenarios (e.g. 15-20 simulated customer calls/inputs
     covering different overdue tiers, dispute cases, payment-promise cases) — this is the
     constant benchmark every prompt version gets tested against.

2. **Core FastAPI endpoints**
   - `POST /prompts` — register a new prompt version (template + metadata).
   - `GET /prompts/{id}/history` — view version lineage.
   - `POST /experiments` — takes two prompt version IDs, runs both against the fixed eval set.
   - `GET /experiments/{id}/results` — returns win-rate, latency, hallucination-rate comparison.

3. **Experiment runner logic**
   - For each eval scenario: run prompt A and prompt B through the LLM, score both via
     LLM-as-judge, record latency per call.
   - Aggregate into a win-rate metric (which variant scored higher more often) + average
     latency + hallucination flags per variant.

4. **Validate via `/docs`**
   - Use FastAPI's auto-generated Swagger UI to trigger experiments and confirm the comparison
     logic works — this alone is a demoable MVP, no frontend required yet.

5. **(Optional) Frontend**
   - Once the API is validated, build a small React + Recharts dashboard calling the same
     endpoints — bar charts for win-rate/latency/hallucination-rate per prompt version.

---

## 7. Potential Next Steps (context for later, not required for v1)

**After a successful local run:**
- Expand the eval set with adversarial/edge-case scenarios (drawing on my SaaS Admin Safety
  Benchmark project's adversarial-attack-vector approach — e.g. prompt injection via a fake
  "customer" transcript).
- Add a lightweight risk-scoring model (using the Kaggle payment-date-prediction dataset) that
  feeds into the dunning logic's prioritization — ties in existing RAG/eval skills to a genuinely
  useful upstream signal.

**If deploying beyond a local/demo-video version:**
- Swap local STT/TTS (Parakeet/Kokoro) for Deepgram in the deployed build (infra section above)
  since Render's free tier has no GPU.
- Deploy `ab_dashboard` and `voice_agent` as separate Render services (agent worker vs API) to
  avoid async event-loop contention.
- Add a provider-agnostic fallback chain in code (Groq → Cerebras → local Ollama) as a resilience
  feature worth calling out explicitly in the README/resume bullet.

**Stretch goals (v2, only if time remains):**
- Mock a NetSuite-style ERP integration (2-way sync pattern, à la the Ninja Van case study) against
  a free sandbox account — bigger lift, higher realism payoff.
- WhatsApp OCR-based invoice ingestion (à la Hai Sia case study) — different technical skill
  (vision/OCR), treat as separate side-experiment rather than a core v1 feature.

**Not planned for this project (explicitly deprioritized):**
- Full production-grade multi-tenant SaaS scaffolding — this is a portfolio/demo project, not a
  product. Keep scope tight around the two deliverables above.
