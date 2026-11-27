# Implementation Record

Maps every requirement in [project_plan.md](project_plan.md) to what was actually built, where it
lives, and how it was verified. Written so the project is understandable without the conversation
that produced it.

**Status: both deliverables complete and verified against live models.**

| Verification | Result |
|---|---|
| Unit/integration tests | **439 passed**, 1 skipped (Postgres, needs `TEST_POSTGRES_URL`) |
| Eval sweep (`--simulate`, original 18 scenarios, live LLM) | **16/18 outcomes (89%)**, 6/7 promise dates, **2/2 injections resisted**. Measured before the metric definitions were unified (see bugs below); re-run before quoting. |
| Eval sweep, 10 new scenarios (S19–S28, live LLM) | **9/10** on first run, **3/3 new attacks resisted**, 0 hallucinations. Both misses were extractor gaps, fixed and re-verified with no regressions on S06/S09/S10 (5/5). |
| Live voice call (LiveKit + Deepgram, real audio) | **composite 4.8/5**, promise-to-pay captured correctly, 0 hallucinations, 0 compliance violations |
| Lint | `ruff` clean |

---

## Section 3 — Infra decisions

| Plan said | Built | Notes |
|---|---|---|
| Groq primary (Llama 3.3 70B / 3.1 8B) | ✅ [`shared/llm_client.py`](shared/llm_client.py) | **Models changed.** `llama-3.3-70b-versatile` and `llama-3.1-8b-instant` were decommissioned 2026-08-16. Uses `openai/gpt-oss-120b` (dialogue) and `openai/gpt-oss-20b` (judge/extraction). |
| Cerebras fallback, retry-on-429 chain | ✅ `LLMClient._build_chain` | Groq → Cerebras → Ollama. Retries within a provider, then fails over. Records which provider served each call. |
| FastAPI backend, `/docs` as demo UI | ✅ [`ab_dashboard/main.py`](ab_dashboard/main.py) | 8 endpoints, all exercised. |
| Postgres | ✅ [`ab_dashboard/db.py`](ab_dashboard/db.py) | **SQLite locally, Neon deployed.** Render's free Postgres is deleted 30 days after creation — unusable for a portfolio demo. |
| LiveKit Agents orchestration | ✅ [`voice_agent/agent.py`](voice_agent/agent.py) | LiveKit Agents **1.6.7** (`AgentServer` + `@server.rtc_session`). |
| Silero VAD | ✅ `voice_factory.build_vad` | Local, CPU. |
| Parakeet STT + Kokoro TTS (local) | ⚠️ **Deferred** | See "Deviations" below. |
| Deepgram for deployed build | ✅ `VOICE_PROVIDER=cloud` | Became the *primary* path — this is what the live call ran on. |
| Ragas / DeepEval | ❌ **Not used** | Replaced by a purpose-built judge. Generic hallucination metrics don't know what an invoice is; the rubric here diffs against the actual `Invoice` record. |

## Section 4 — Dataset

| Plan said | Built |
|---|---|
| Kaggle base as statistical shape | ✅ [`data/generate_dataset.py`](data/generate_dataset.py) — 49,305 real amounts, 16,764 real lateness observations |
| Faker enrichment: GSTIN, HSN/SAC, days-overdue, dispute flags, multi-currency | ✅ All present, plus `reminder_count`, `last_reminder_date`, `risk_tier` |
| `promise_to_pay_date` starts null, filled by agent calls | ✅ Always written empty; populated only via `CallOutcome` |
| Output feeds dunning logic and per-call context | ✅ `load_invoices()` → `build_call_queue()` → `pick_invoice()` |

**Corrections made:** the plan's Kaggle slug (`pradumn203/customer-invoices-dataset`) 404s; the real one
is `pradumn203/payment-date-prediction-for-invoices-dataset`. It is licensed **CC BY-NC 4.0**
(attribution, non-commercial) and gitignored, so the generator falls back to built-in distributions
when absent — a fresh clone still runs.

Only **strictly-late** invoices calibrate the ageing curve. The base covers every invoice and ~33%
cleared on or before the due date; including those gave a third of generated rows `days_overdue = 0`,
which the call queue then drops entirely.

## Section 5 — Collections Voice Agent

| # | Plan requirement | Implementation | Verified by |
|---|---|---|---|
| 1 | Dataset drives call state | `pick_invoice()` reads the enriched CSV | Live call selected `INV-100379` from the queue |
| 2 | Risk tiers from days-overdue + history | [`dunning_logic.py`](voice_agent/dunning_logic.py) — `classify_risk()` | `test_dunning_logic.py`, 30 tests on tier boundaries |
| 2 | Tone/escalation per tier | `tone_for_tier()`, 4 tiers → gentle/neutral/firm/escalation | Live call ran `critical → escalation` |
| 3 | TTS-optimised prompts | [`prompts/*.yaml`](voice_agent/prompts/) — short sentences, spoken numbers, no markup | Live call: *"eighty-three thousand seven hundred forty-six dollars"* |
| 3 | Versioned with IDs, feeding the dashboard | `PromptVersion` + [`prompt_store.py`](shared/prompt_store.py) | `POST /prompts/sync` imports the same files |
| 4 | Interruption-aware multi-turn | `AgentSession` + `TurnDetector(version="v1-mini")` | Live call: agent cut off mid-sentence, recovered |
| 4 | Structured outcome extraction | [`outcome_extractor.py`](voice_agent/outcome_extractor.py) — 7 outcome types | Live call: `promise_to_pay`, `2026-08-15`, confidence 0.95 |
| 5 | LLM-as-judge on 4 dimensions | [`rubric.py`](shared/eval/rubric.py) — 5 dimensions (added `compliant`) | Live call scored 4.8/5 |
| 5 | Flag underperforming prompt versions | `call_logger.prompt_health()` — rolling mean, min-sample threshold | Unit tested |
| 6 | Store transcripts + scores + prompt IDs | `call_logs` table | Live call row persisted |

## Section 6 — Prompt A/B Testing Dashboard

| # | Plan requirement | Implementation | Verified |
|---|---|---|---|
| 1 | 15–20 scenario fixed eval set | **28 scenarios** in [`eval_scenarios.yaml`](data/eval_scenarios.yaml) | All 7 outcome types, 4 currencies, 5 adversarial across 4 attack types |
| 2 | `POST /prompts` | ✅ [`routers/prompts.py`](ab_dashboard/routers/prompts.py) | |
| 2 | `GET /prompts/{id}/history` | ✅ lineage by name, oldest first | |
| 2 | `POST /experiments` | ✅ background task, returns immediately | |
| 2 | `GET /experiments/{id}/results` | ✅ win-rate, latency, hallucination-rate | |
| 3 | Run A and B per scenario, judge both, record latency | ✅ [`experiment_runner.py`](ab_dashboard/experiment_runner.py) | |
| 3 | Aggregate win-rate + latency + hallucination flags | ✅ plus extraction accuracy, p95 latency, per-dimension means | |
| 4 | Validate via `/docs` | ✅ all 8 endpoints exercised in-process | `test_db_and_api.py` |
| 5 | React frontend | ❌ **Not built** — explicitly optional in the plan | |

---

## Beyond the plan

Things not asked for that turned out to matter:

**Grounded hallucination detection** ([`rubric.py`](shared/eval/rubric.py)). Asking an LLM "did it
invent anything?" has unreliable recall, and a miss is indistinguishable from a clean call. Instead a
regex extracts *every* figure the agent uttered, diffs against `Invoice.quotable_figures()` **and**
against what the customer said, then hands the LLM a concrete candidate list to adjudicate. Machine
does recall; model does judgement.

**Position-blind pairwise judging** (`judge_pairwise`). LLM judges prefer whichever candidate they
see first. With a fixed order that bias lands entirely on one variant, so the win-rate would measure
presentation order rather than prompt quality. Order is randomised per scenario from a fixed seed —
reproducible and auditable — then mapped back to A/B.

**Ground-truth extraction accuracy.** Every scenario declares its correct outcome and, where
applicable, the exact promise-to-pay date. An agent and a judge can agree with each other and both
be wrong.

**Adversarial scenarios.** S17 and S18 contain prompt injections in customer speech. Listed as a
"next step" in the plan; included in v1 because they are the most valuable cases in the set. S25–S27
extend this beyond injection: payment-diversion fraud, compliance baiting, and third-party data
fishing.

**Significance, not just win-rate** ([`stats.py`](shared/eval/stats.py)). Exact sign test, Wilson
intervals and a paired bootstrap, standard library only. The verdict says "not significant" when a
split could be chance, which on tens of scenarios is most of the time.

**Eval as a CI gate** ([`report.py`](shared/eval/report.py)). `--simulate` takes accuracy,
injection-resistance and hallucination thresholds and exits 1 on a miss, with a JSON report.

**Observability** ([`shared/observability/`](shared/observability/), [`infra/monitoring/`](infra/monitoring/)).
Prometheus metrics with bounded labels, OpenTelemetry traces using the GenAI semantic conventions,
JSON logs correlated by request and trace id, and readiness checks. Workflow counters are recorded
only after their transaction commits, so a write retried after a version conflict is not counted
twice. The Grafana dashboard is generated from code, and a test fails if any panel or alert
references a metric that does not exist.

**Proactive rate-limit pacing** (`TokenBucket`). Groq's free tier is 8,000 tokens/min **and 200,000
tokens/day per model**, charged against *reserved* `max_tokens` rather than tokens generated. Backoff
alone means every 429 is a wasted round-trip. Pacing took a full sweep from 75 rejections to **zero**.

**Risk-tiered dunning workflow** ([`workflow/`](workflow/)). The plan's "tone per tier" became a
full collections workflow, split so the rules stay auditable:

| Module | Pure? | Role |
|---|---|---|
| [`dunning_policy.yaml`](workflow/dunning_policy.yaml) | data | Cadence per tier, contact caps, calling hours, promise grace, task SLAs |
| [`policy.py`](workflow/policy.py) | yes | Loads and validates it; every cadence must end in escalation |
| [`scheduler.py`](workflow/scheduler.py) | yes | Earliest *permitted* next action: caps, minimum gap, customer-local calling hours, do-not-call |
| [`state_machine.py`](workflow/state_machine.py) | yes | Snapshot + event → new state, tasks to open, tasks to close |
| [`service.py`](workflow/service.py) | no | Loads a row, applies the pure functions, writes state + tasks + audit event in one transaction |
| [`cash_application.py`](workflow/cash_application.py) | yes | Bank line + open invoices → which invoices it pays, by which rule, or why it cannot be matched |
| [`cash.py`](workflow/cash.py) | no | Ingests credits (idempotent on UTR), applies matches, runs the exceptions queue |
| [`bank_feed.py`](workflow/bank_feed.py) | yes | The simulated bank feed the demo uses in place of a real bank |

Choices worth noting: contact caps are **per customer**, not per invoice, and each customer's most
urgent invoice leads the conversation; tiering uses the invoice **as it stands today** (aged since
sync, net of payments); each broken promise bumps the tier; concurrent writers (scheduler, voice
agent, a person resolving a task) are serialised by an optimistic-lock version column rather than
locks. The voice agent dials from this queue and calls no one when nothing is due.

**Customer-level collections.** Real collectors work the account, not the invoice. The call queue
returns one call per customer, filed under their most urgent invoice and carrying the rest of the
account; the prompt lists every overdue invoice and the account total, and the grounding check
accepts those figures. Reminders cover the whole account. A promise applies to the whole account
only when it is dated and for at least the account total (or names no amount); disputes, partial
plans and smaller promises stay with the invoice they were made on, because nothing in the call
says which other invoice they meant.

**Cash application.** Marking invoices paid by hand is the part real AR systems automate. Bank
credits are matched by ordered pure rules and applied through the existing payment path, so a
matched payment closes the invoice and auto-resolves its verify/reconcile tasks. The rules are
deliberately conservative: an overpayment, a short payment across several named invoices, or an
amount that fits two invoices equally goes to a person, because applying money to the wrong
invoice means chasing a customer who paid. "Already paid" claims are checked against the bank
data in both directions, by the reference the customer quoted on the call. On a simulated day of
credits the auto-match rate is around 75%, with the rest in the exceptions queue with reasons.

**Durable, resumable experiments** ([`jobs.py`](ab_dashboard/jobs.py)). The database is the queue:
a worker claims a run with one conditional `UPDATE` (atomic on SQLite and Postgres, so no Redis and
no `SKIP LOCKED`), renews a heartbeat, and commits each scenario's results the moment it finishes.
Graceful shutdown hands the lease back; a crash lets it expire. A reaper sweep then resumes the run
from the last stored scenario, and a unique `(experiment, scenario, variant)` constraint makes a
double write impossible.

**Versioned schema** ([`migrations/`](ab_dashboard/migrations/)). Alembic, applied by `init_db()` on
startup. Databases created by the earlier `create_all` path are detected and stamped at the baseline
revision, so they upgrade in place without losing data. A test asserts the migrations produce
exactly the models' schema, so a model change without a migration fails CI.

**Simulate mode.** `--simulate` runs the eval set text-only through the same prompts, extractor and
judge as the voice path — deterministic, CI-safe, no audio stack. This is what makes the dashboard's
numbers statements about the real agent.

---

## Deviations from the plan, and why

**1. Parakeet STT deferred (accepted quality cost).**
Parakeet TDT 0.6B is ~1.5–2 points better on English WER than Whisper-turbo and runs offline. It no
longer needs NeMo — it's in HuggingFace `transformers` as `AutoModelForTDT`. It was deferred because
**LiveKit ships no official plugin**, so it needs a custom `stt.STT` subclass, and writing that
before the agent had ever completed a call was the wrong order of work. `voice_factory.py` is
structured to take it as a third branch. **This is the top v2 item.**

Mitigation: the agent never reads invoice figures from the transcript — they come from the injected
context block, and the judge cross-checks against it. Confirmed live: STT rendered *"Prisha"* as
*"Prashas"* on one take and the agent's stated facts were still perfect.

**2. Kokoro TTS not used.** Blocked by a full C: drive (0 bytes free) during the image pull, not by
anything in the design. `VOICE_PROVIDER=local` still routes to it and `infra/docker-compose.kokoro.yml`
is ready.

**3. Ragas/DeepEval replaced by a purpose-built judge.** Generic frameworks score generic
hallucination. The domain question here is narrower and answerable exactly: *did the agent state a
figure absent from this invoice?*

**4. Judge has 5 dimensions, not 4.** Added `compliant` — no threats of legal action, no invented
fees, no impersonating a tax authority, no obeying instructions embedded in customer speech.

**5. Neon instead of Render Postgres.** Render deletes free Postgres 30 days after creation.

---

## Bugs found and fixed during the build

Recorded because several were silent — they produced plausible-looking output while being wrong.

| Bug | Why it mattered |
|---|---|
| Amount regex used `\d{1,3}` as its leading group | Could not match an ungrouped 4-digit number **at all**. A fabricated "late fee of 7500" scored as a clean call. |
| `Decimal("45000.00").normalize()` → `4.5E+4` | Scientific notation never string-matched plain digits, so legitimate amounts read as hallucinations. |
| Dates shredded by the amount pattern | `2026-06-01` became candidates `2026`, `06`, `01`, flagging honest agents. Fixed by matching and masking IDs → dates → amounts in order. |
| Risk tiering compared raw amounts across currencies | 18,400 SGD (~₹12 lakh) scored as less material than ₹300,000. Now INR-normalised before any check. |
| Pairwise verdict computed but never persisted | `/results` silently fell back to comparing absolute composites, discarding the position-blind judgement entirely. |
| Reasoning tokens billed against `max_tokens` | On `gpt-oss`, 97 of 123 completion tokens went to reasoning — every agent reply was truncated mid-sentence, and structured calls returned hard `400 json_validate_failed`. Fixed with `reasoning_effort` + real headroom. |
| Backoff read `x-ratelimit-reset-requests` | That's the **daily** window (`5h26m`). Pacing a per-minute 429 off it burned the whole retry budget. |
| `x-should-retry: false` ignored | Groq sets it on daily-quota exhaustion. Cost five pointless 30s waits and a misleading "all providers failed". |
| Plugin imports deferred inside factory functions | LiveKit registers plugins on import and requires the main thread — **every live call crashed**. The dashboard-optimisation caused it. |
| API keys read from `os.environ` by plugins | pydantic-settings parses `.env` without touching `os.environ`, so credentials were loaded in Python but invisible to LiveKit and Deepgram. Now passed explicitly. |
| S07 eval scenario was ambiguous | Mixed a quality complaint into a refusal, so it read as either `refused` or `dispute_raised` and **flipped across three runs**. Ambiguous ground truth is noise; the complaint was removed rather than the label re-chased. |
| Judge scored the customer's injection as the agent's violation | Every adversarial scenario scored ~1 on compliance regardless of agent behaviour — pure noise in any A/B built on it. |
| Dashboard promise-date accuracy counted *any* extracted date as correct | `--simulate` exact-matched against ground truth; the dashboard checked `date is not None`. A wrong date scored as a hit. Both now call `shared/eval/metrics.py`. |
| "Injection resisted" had three different definitions | `--simulate` checked for a fabricated commitment; the runner and `/results` only checked for hallucinated figures — so an agent that logged the injected promise-to-pay still "held". One definition now, which also counts compliance violations (agreeing to a customer-supplied discount is not a hallucination, but it is a breach). |
| Experiment results written only after the whole run | `BackgroundTasks` is in-memory: a restart or Render spin-down mid-run lost every scenario and left the row `running` forever. |
| Extractor called a partial dispute "unclear" | Found by the expanded eval set (S20): contesting 20 of 120 billed hours is a dispute even when the rest will be paid. The rubric said "contests the amount" but not "part of it". |
| Extractor dropped the date on a dated instalment | S22: the rubric never said which date to record for a partial payment, so the model left it null. Now: the specific date named, the earliest if several. |
| A due call could be pre-empted by a reminder email to the same customer | Found running the workflow on the real 500-invoice ledger: a pass reported 4 calls due and the queue was empty. Invoices were ticked in arbitrary order, so a low-priority email went out first and the minimum contact gap then blocked the urgent call. Now ticked in priority order, one contact per customer per pass. |
| Every synthetic invoice had its own customer name and currency | The generator drew a fresh company name for each invoice, so one customer id appeared under three names. Found building the account view and bank matching: an account payment by the customer's name could never match. Customers are now created once. |
| Voice agent always dialled the top of the static CSV queue | Every run called the same invoice regardless of what the last call achieved. Now it dials from the workflow's queue. |
| `init_db()` used `create_all` with Alembic installed but unused | `create_all` never alters existing tables, so any new column would be silently missing on every deployed database. |

---

## Not built (deliberately)

- **Frontend** — optional in the plan; `/docs` is the working UI for now. Planned in
  [docs/frontend-plan.md](docs/frontend-plan.md).
- **ERP / NetSuite sync, WhatsApp OCR** — plan lists these as v2 stretch goals. The database is
  the system of record; invoice sync and the bank feed are the two integration points an ERP and a
  bank connection would plug into.
- **Credit management and deductions** — credit limits and order holds, and investigating
  deliberate B2B short payments, are separate AR functions from collections.
- **Email and SMS delivery** — the workflow schedules and records these steps (they count toward
  the contact caps) but does not send them; an email/SMS provider is a drop-in at that one call site.
- **Payment-date risk model** — plan lists as a next step.
- **Multi-tenant SaaS scaffolding** — explicitly deprioritised in the plan.
