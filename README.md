# AR Collections Voice Agent + Prompt A/B Testing Dashboard

Two connected deliverables for agentic AI in finance back-office automation:

1. **Collections voice agent** — a LiveKit Agents worker that runs dunning calls about overdue
   invoices, handles interruptions, and extracts promise-to-pay commitments.
2. **Prompt A/B testing dashboard** — a FastAPI service that runs two prompt versions against a
   fixed eval set and reports win-rate, latency, hallucination-rate and extraction accuracy.

They are not two demos in one repo. The dashboard imports the agent's **actual prompt YAMLs** and
scores them with the **same judge and the same outcome extractor** the agent runs in production, so
an experiment result is a statement about the agent rather than about a proxy for it.

Runs at **$0**: free-tier LLM inference, local TTS, SQLite for dev.

> Built with [Claude Code](https://claude.com/claude-code). [IMPLEMENTATION.md](IMPLEMENTATION.md)
> records every design decision, where each deviates from the original plan and why, and the twelve
> bugs found during the build — several of which were silent, producing plausible output while being
> wrong. [SETUP.md](SETUP.md) has every command from clone to live call.

---

## Design decisions worth calling out

**Prompt versions are one artifact, shared.** `shared/prompt_store.py` loads and validates the same
YAML files the agent runs. `POST /prompts/sync` imports them into the dashboard. Placeholders are
validated at load time — a typo'd `{invoce_number}` fails on startup rather than raising KeyError
mid-conversation.

**Hallucination detection is grounded, not vibes.** `shared/eval/rubric.py` regexes every
figure the agent uttered, diffs it against the invoice record *and* against what the customer said,
and hands the judge a concrete candidate list. The LLM decides intent; the regex guarantees nothing
is missed. Figures the customer volunteered are never counted against the agent.

**Win-rate is position-blind.** LLM judges prefer whichever candidate they see first. With a fixed
presentation order that bias lands entirely on one variant, and the resulting number measures
ordering rather than quality. `judge_pairwise` randomises the order per scenario from a fixed seed —
reproducible, auditable, and unbiased — then maps the verdict back to A/B.

**Extraction accuracy is scored against ground truth, not the judge.** Every eval scenario declares
its correct outcome and, where applicable, the exact promise-to-pay date. An agent and a judge can
agree with each other and both be wrong.

**Provider failover is explicit.** Groq → Cerebras → Ollama, retrying on 429/5xx within a provider
before moving on. Cerebras' free tier caps context at 8K, so oversized prompts skip it rather than
attempting and failing. Every response records which provider served it — a mid-run failover changes
the model, which would otherwise silently confound an A/B result.

**Experiments survive restarts.** The database doubles as the job queue: a worker takes a lease
with one atomic conditional `UPDATE`, renews a heartbeat, and commits results per scenario. A dead
worker's lease expires and a reaper resumes its run where it stopped. This works the same on SQLite
and Postgres, with no Redis to run.

**Multi-currency risk tiering is INR-normalised.** A 12,750 USD invoice (~₹11 lakh) must not score
as less material than a ₹300,000 one. `amount_in_inr()` converts before any materiality check.

---

## Quick start

```bash
uv sync --extra dashboard --extra data          # dashboard only
uv sync --extra dashboard --extra data --extra voice   # + the audio stack

cp .env.example .env        # add GROQ_API_KEY (free, no card: console.groq.com/keys)

uv run python -m data.generate_dataset --rows 500 --seed 42
```

### 1. The A/B dashboard

```bash
uv run uvicorn ab_dashboard.main:app --reload
```

Drive it entirely from <http://127.0.0.1:8000/docs> — no frontend needed:

| Step | Call |
|---|---|
| 1 | `POST /prompts/sync` — import the agent's prompt YAMLs |
| 2 | `POST /experiments` — `{"variant_a_id": "collections_formal_v1", "variant_b_id": "collections_friendly_v2"}` |
| 3 | `GET /experiments/{id}` — poll until `status` is `completed` (`completed_count` advances per scenario) |
| 4 | `GET /experiments/{id}/results` — win-rate, latency, hallucination-rate, per-scenario breakdown |

Add `{"limit": 3}` to `POST /experiments` for a fast smoke run instead of all 18 scenarios.

Runs are durable. Each scenario's results are committed as soon as it finishes, so `/results` is
readable mid-run. If the process restarts, the run resumes from the last stored scenario rather than
starting over or hanging in `running` (see `ab_dashboard/jobs.py`).

### 2. The voice agent

```bash
# Text-only sweep over the eval set. Deterministic, no audio stack, CI-safe.
uv run python -m voice_agent.agent --simulate
uv run python -m voice_agent.agent --simulate --limit 3 --log-to-db

# Live voice, fully local. Start Kokoro-FastAPI first (.\start-cpu.ps1), then:
uv run python -m voice_agent.agent console
```

`console` mode runs in your terminal with your own mic and speakers — no LiveKit server, no cloud
minutes.

---

## Stack

| Layer | Local dev | Deployed |
|---|---|---|
| LLM | Groq `openai/gpt-oss-120b` → Cerebras fallback | same |
| STT | Groq `whisper-large-v3-turbo` | Deepgram Nova-3 |
| TTS | Kokoro-FastAPI on `localhost:8880` | Deepgram Aura-2 |
| VAD | Silero (local, CPU) | same |
| Turn detection | LiveKit `v1-mini` (local, CPU, free) | same |
| Database | SQLite | Neon Postgres |

Switch with `VOICE_PROVIDER=local|cloud`; everything routes through `voice_agent/voice_factory.py`.

### Two things that will bite you if you follow older guides

- **`llama-3.3-70b-versatile` and `llama-3.1-8b-instant` were decommissioned on 2026-08-16.**
  Replacements are `openai/gpt-oss-120b` and `openai/gpt-oss-20b`.
- **Render's free Postgres is deleted 30 days after creation.** Use Neon (always-free) for anything
  you plan to demo more than a month out.

---

## Layout

```
shared/            models, config, LLM failover client, prompt store, judge + rubric
data/              dataset generator, eval_scenarios.yaml (18 scenarios, 2 adversarial)
voice_agent/       dunning logic, prompts/, LiveKit agent, simulator, extractor, call logger
ab_dashboard/      FastAPI app, models + Alembic migrations, experiment runner, durable jobs, routers
tests/             198 tests, no network calls
```

## Data provenance

There are **two independent data sources**, and it matters which is which:

| | Origin | Drives |
|---|---|---|
| `data/synthetic/ar_dataset_enriched.csv` | Kaggle base + Faker enrichment | the agent's call queue — which invoice `console` mode dials |
| `data/eval_scenarios.yaml` | hand-written, 18 scenarios | the A/B dashboard **and** `--simulate` |

The eval scenarios pin their own invoices (`INV-200001`…) with deliberately chosen tiers and
personas. They never read the generated CSV, so **the Kaggle base does not influence any A/B
result** — it only makes the call-queue demo realistic.

**Base dataset:** [Customer Invoices — Payment Date Prediction](https://www.kaggle.com/datasets/pradumn203/payment-date-prediction-for-invoices-dataset)
by Pradumn Kumar, licensed **CC BY-NC 4.0** (attribution, non-commercial). It is gitignored, so
clone-and-run falls back to built-in distributions with a warning.

Two deliberate choices in how it is used:

- **Only strictly-late invoices calibrate the ageing distribution.** The base covers every invoice,
  and ~33% cleared on or before the due date. Including those would give a third of generated rows
  `days_overdue = 0` — which `build_call_queue` then drops — and leave the escalation paths
  unexercised. Conditioning on "given it is late, how late?" is the population a collections agent
  actually works.
- **AR fields are synthetic by necessity.** No public invoice dataset carries GSTIN, HSN/SAC,
  dispute flags, reminder history or promise-to-pay outcomes. The base supplies the amount and
  ageing *shape*; everything collections-specific is generated on top.

The resulting tier mix is heavily low/medium, because that is genuinely what late B2B invoices look
like — ~61% of late invoices in the base clear within 15 days. The call queue sorts by priority, so
the critical cases still surface first.

## The eval set

18 scenarios covering every outcome type — cooperative, evasive, disputed, already-paid, hostile,
gatekeeper, voicemail, multi-currency, GST query — each pinning an invoice, a scripted customer, and
ground truth.

Two are **adversarial**: the customer's speech contains prompt injections instructing the agent to
cancel the invoice or to log a commitment that was never made. A correct agent treats customer
speech as data, never as instruction. `adversarial_held_a/b` in the results reports which variant
resisted them.

## Tests

```bash
uv run pytest                    # 198 tests, fully mocked, no API calls
uv run ruff check .
```

Schema changes go through Alembic. The app applies migrations on startup; to author one:

```bash
uv run alembic revision --autogenerate -m "describe the change"
```

`tests/test_migrations_and_jobs.py` fails if the models and the migrations disagree.

To verify Postgres compatibility before deploying:

```bash
TEST_POSTGRES_URL=postgresql://... uv run pytest tests/test_db_and_api.py
```

## Roadmap

1. **Local Parakeet STT plugin.** `nvidia/parakeet-tdt-0.6b-v2` runs on plain `transformers`
   (`AutoModelForTDT`, no NeMo) and beats whisper-large-v3-turbo by ~1.5–2 points of WER on English
   while running fully offline. It needs a custom `stt.STT` subclass since LiveKit ships no official
   plugin. `voice_factory.py` is structured to take it as a third branch.
2. Expand adversarial coverage beyond the two injection scenarios.
3. Payment-date risk model feeding the dunning prioritisation.
