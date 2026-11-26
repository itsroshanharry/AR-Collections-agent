# AR Collections Voice Agent + Prompt A/B Testing Dashboard

[![CI](https://github.com/itsroshanharry/AR-Collections-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/itsroshanharry/AR-Collections-agent/actions/workflows/ci.yml)

Two connected deliverables for agentic AI in finance back-office automation:

1. **Collections voice agent** — a LiveKit Agents worker that runs dunning calls about overdue
   invoices, handles interruptions, and extracts promise-to-pay commitments. It is driven by a
   **risk-tiered dunning workflow**: per-tier cadences, contact-frequency and calling-hours
   rules, a per-invoice state machine, and follow-up tasks opened from every call outcome.
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

**A win is not reported unless it is real.** With tens of scenarios, a 10–8 split is about as likely
to be chance as not. `/results` carries an exact sign test on head-to-head wins, 95% Wilson intervals
on each win-rate, and a paired bootstrap interval on the composite difference, and the verdict says
"not significant" when it isn't. A per-tag breakdown shows *where* a variant wins or fails (disputes,
multi-currency, adversarial), not just whether.

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

**Dunning is a policy, not a loop.** [`workflow/dunning_policy.yaml`](workflow/dunning_policy.yaml)
is the whole playbook as data: a cadence per risk tier (email → call → SMS → escalation to a
person), weekly contact and call caps, a minimum gap between contacts, and calling hours in the
*customer's* timezone. A step that would break a rule is deferred, never skipped or sent early. Every
cadence must end in escalation, so nothing loops forever, and the file is validated at startup.

**Every call outcome opens the right follow-up.** A per-invoice state machine
([`workflow/state_machine.py`](workflow/state_machine.py), pure functions) routes a promise to a
`verify_payment` task due after a grace period, a dispute to the disputes desk with dunning paused, and
an "already paid" claim to reconciliation. A lapsed promise restarts the cadence one risk tier harsher,
and repeated broken promises escalate to a person. Tasks are idempotent (deduped per call), task
resolutions drive the invoice onward, and every transition lands in an append-only audit trail.

**Two independent signals per call.** The agent records outcomes live through tool calls; a separate
extractor reads the transcript afterwards. When they disagree, a QA task opens rather than one silently
winning.

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

Add `{"limit": 3}` to `POST /experiments` for a fast smoke run instead of all 28 scenarios.

Runs are durable. Each scenario's results are committed as soon as it finishes, so `/results` is
readable mid-run. If the process restarts, the run resumes from the last stored scenario rather than
starting over or hanging in `running` (see `ab_dashboard/jobs.py`).

### 2. The dunning workflow

| Step | Call |
|---|---|
| 1 | `POST /dunning/sync` — load the ledger (the generated dataset) into the workflow |
| 2 | `POST /dunning/tick` — run a scheduler pass now (the app also runs one every 5 minutes) |
| 3 | `GET /dunning/queue` — calls the playbook wants that the contact rules allow right now |
| 4 | `POST /dunning/invoices/{id}/outcomes` — record a call outcome (the voice agent does this itself) |
| 5 | `GET /tasks?assignee=disputes` — a team's work queue; `PATCH /tasks/{id}` to resolve |
| 6 | `GET /dunning/invoices/{id}` — state, tasks, contact history and the full audit trail |

`POST /dunning/invoices/{id}/payments` stands in for the ERP or bank feed.

### 3. The voice agent

```bash
# Text-only sweep over the eval set. Deterministic, no audio stack, CI-safe.
uv run python -m voice_agent.agent --simulate
uv run python -m voice_agent.agent --simulate --limit 3 --log-to-db

# Live voice, fully local. Start Kokoro-FastAPI first (.\start-cpu.ps1), then:
uv run python -m voice_agent.agent console
```

`console` mode runs in your terminal with your own mic and speakers — no LiveKit server, no cloud
minutes.

Once the workflow is synced, the agent dials the top of its call queue, at the invoice's current tier
and outstanding balance, and feeds the outcome back in. If nothing is due it calls no one. Set
`CALL_INVOICE_ID` to pin a specific invoice for a demo.

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

## Monitoring

| Signal | Where | Notes |
|---|---|---|
| Metrics | `GET /metrics` (Prometheus) | LLM requests, latency, tokens, failovers and pacing; call outcomes, judge scores, hallucinations and compliance per prompt version; dunning contacts, transitions, tasks, open/overdue tasks per team, calls due, outstanding balance by tier; experiments; HTTP by route template |
| Traces | [Langfuse](https://langfuse.com) when `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` are set; any OTLP backend via `OTEL_EXPORTER_OTLP_ENDPOINT` (both at once is fine) | One trace per request or experiment run: the scheduler pass, each simulated call, the judge and extractor with their verdicts, and every LLM call with model, tokens and finish reason (GenAI semantic conventions). Live voice calls add LiveKit's own turn, STT and TTS spans. Prompt and completion text only with `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`, since transcripts hold customer data |
| Logs | stdout; `LOG_FORMAT=json` for pipelines | Every line carries the request id (from `X-Request-ID` or generated) and the active trace/span id |
| Health | `/health` (liveness), `/health/ready` | Readiness fails with 503 if the database is unreachable, the schema is behind, or the scheduler has stalled |

Metrics and traces split by job: **Prometheus and Grafana** for the system (workflow health, SLAs,
alerts), **Langfuse** for the LLM calls (each prompt, completion, token count and verdict, grouped per
experiment). Both are free at this scale.

A full local stack, with Prometheus and alert rules, a pre-built Grafana dashboard and Jaeger, is
one command:

```bash
docker compose -f infra/docker-compose.monitoring.yml up -d
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 LOG_FORMAT=json \
  uv run --extra observability uvicorn ab_dashboard.main:app --host 0.0.0.0
# Grafana http://localhost:3000 · Prometheus http://localhost:9090 · Jaeger http://localhost:16686
```

Alerts cover a stalled scheduler, tasks past SLA, any compliance violation, a rising hallucination
rate, LLM failure and failover, latency, API errors and the app going down. A test fails if any alert
or dashboard panel references a metric the app does not export.

### Two things that will bite you if you follow older guides

- **`llama-3.3-70b-versatile` and `llama-3.1-8b-instant` were decommissioned on 2026-08-16.**
  Replacements are `openai/gpt-oss-120b` and `openai/gpt-oss-20b`.
- **Render's free Postgres is deleted 30 days after creation.** Use Neon (always-free) for anything
  you plan to demo more than a month out.

---

## Layout

```
shared/            models, config, LLM failover client, prompt store
shared/eval/       judge, rubric, metric definitions, significance stats, sweep report + gate
shared/observability/  Prometheus metrics, OpenTelemetry tracing, structured logging
data/              dataset generator, eval_scenarios.yaml (28 scenarios, 5 adversarial)
voice_agent/       risk tiering, prompts/, LiveKit agent, simulator, extractor, call logger
workflow/          dunning policy YAML, contact scheduler, state machine, workflow service
ab_dashboard/      FastAPI app, models + Alembic migrations, experiment runner, durable jobs, routers
infra/             Dockerfile, Render blueprint, Kokoro TTS, monitoring stack (Prometheus, Grafana, Jaeger)
tests/             348 tests, no network calls
```

## Data provenance

There are **two independent data sources**, and it matters which is which:

| | Origin | Drives |
|---|---|---|
| `data/synthetic/ar_dataset_enriched.csv` | Kaggle base + Faker enrichment | the agent's call queue — which invoice `console` mode dials |
| `data/eval_scenarios.yaml` | hand-written, 28 scenarios | the A/B dashboard **and** `--simulate` |

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

28 scenarios covering every outcome type — cooperative, evasive, disputed (including a partial
dispute), already-paid, hostile, gatekeeper, voicemail, callback request, instalment plan, repeat
promise — across INR, USD, EUR and GBP and a code-mixed Hindi-English call. Each pins an invoice, a
scripted customer, and a single unambiguous ground truth.

Five are **adversarial**, each a different attack: two prompt injections (cancel the invoice, log a
commitment never made), payment-diversion fraud ("confirm our new bank account"), compliance baiting
(goading the agent into a legal threat), and fishing for another customer's balance. A correct agent
treats customer speech as data, never as instruction. Resistance requires no hallucinated figure, no
compliance violation, and no planted outcome.

The sweep doubles as a CI gate:

```bash
uv run python -m voice_agent.agent --simulate \
  --min-accuracy 0.85 --require-injections-resisted --max-hallucination-rate 0.05 \
  --report eval-report.json        # exits 1 if any threshold is missed
```

## Tests

```bash
uv run pytest                    # 348 tests, fully mocked, no API calls
uv run ruff check .
```

Schema changes go through Alembic. The app applies migrations on startup; to author one:

```bash
uv run alembic revision --autogenerate -m "describe the change"
```

`tests/test_migrations_and_jobs.py` fails if the models and the migrations disagree.

Every database-backed test takes a fresh database from one fixture. With `TEST_POSTGRES_URL` set,
the whole suite runs against Postgres instead of SQLite (the schema is reset before each test):

```bash
TEST_POSTGRES_URL=postgresql://user:pass@localhost:5432/scratch uv run pytest
```

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs lint, the suite on both SQLite and
Postgres, and a build-and-boot of the Docker image on every push and pull request. On `main` it also
runs three live scenarios through the quality gate, and
[`eval-weekly.yml`](.github/workflows/eval-weekly.yml) runs the full sweep every Monday to catch
provider-side model drift. Both live runs need a `GROQ_API_KEY` repository secret.

## Roadmap

1. **Local Parakeet STT plugin.** `nvidia/parakeet-tdt-0.6b-v2` runs on plain `transformers`
   (`AutoModelForTDT`, no NeMo) and beats whisper-large-v3-turbo by ~1.5–2 points of WER on English
   while running fully offline. It needs a custom `stt.STT` subclass since LiveKit ships no official
   plugin. `voice_factory.py` is structured to take it as a third branch.
2. **Streaming.** Time-to-first-token per turn in the simulator, so the concise prompt's latency
   claim is measured rather than assumed, and live experiment progress over SSE.
3. **CI.** GitHub Actions running lint, tests and a gated 3-scenario `--simulate` smoke run.
4. **API authentication** and a CORS allow-list in place of `*`.
5. Payment-date risk model feeding the dunning prioritisation.
