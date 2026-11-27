# Frontend plan: the operator console

## What it is for

The frontend is a **demo and interview surface**, not a product for daily users. In about three
minutes it has to show the story that `/docs` cannot show well:

1. The book of overdue invoices, tiered by risk, and what the scheduler is doing with it.
2. A call about a customer's **whole account**, and the follow-up work that call opened.
3. A day of **bank credits** arriving: most invoices closing on their own, a few exceptions left
   for a person, with the reason each one did not match.
4. A person working the exceptions and task queues, with every step in the audit trail.
5. The prompt A/B dashboard: two prompt versions, a win-rate, and whether it is significant.

Everything it shows already exists in the API. The frontend adds no business logic.

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Hosting | **Served by FastAPI itself**, from the same Render service | One deploy, one URL, one cold start. Same origin, so there is no CORS to configure and no second free service to keep awake. |
| Framework | **React + TypeScript + Vite** | Widely recognised, fast builds, and the output is static files FastAPI can serve. |
| Data fetching | **TanStack Query** | Caching, refetch after writes, and polling for experiment progress with little code. |
| API client | **Generated from FastAPI's OpenAPI schema** (`openapi-typescript`) | The client's types come from the backend, so a changed response shape fails the frontend build instead of failing in a demo. |
| Styling | **Tailwind CSS** | No design system to maintain; consistent spacing and colour from utility classes. |
| Charts | **None**: proportion bars in CSS | The overview needs a few bars, not a chart library (~100 KB saved). |
| Routes | UI under `/app/...`, `/` redirects there | Keeps client routes clear of the API's `/dunning`, `/cash`, `/tasks` and `/experiments` paths. |
| Build | A Node stage in `infra/Dockerfile.dashboard` builds `frontend/dist`; the Python stage copies it in | The image stays a single service; Node is not in the runtime image. |

Rejected: a separate frontend host (Netlify, Vercel). It adds CORS, a second deploy and a second
URL, and the API still cold-starts on Render, so it would not make the demo faster.

## Pages

| Page | Shows | Actions | API |
|---|---|---|---|
| **Overview** | Outstanding by risk tier, invoices by state, calls due, open tasks per team, tasks past SLA, auto-match rate, unmatched payments | Run a scheduler pass, simulate a bank feed | new `GET /dunning/summary`, `POST /dunning/tick`, `POST /cash/simulate` |
| **Call queue** | One row per customer: lead invoice, the rest of the account, account total, tier, priority | Record an outcome (covering the account or one invoice), later: run a demo call | `GET /dunning/queue`, `POST /dunning/invoices/{id}/outcomes` |
| **Customers** | Searchable list with balance in dunning and open-task count | Open an account | new `GET /dunning/customers` |
| **Customer account** | Every invoice and its state, totals per currency, open tasks, the customer's bank credits | Do-not-call toggle, post a payment by hand | `GET /dunning/customers/{id}`, `GET /cash/transactions?customer_id=` |
| **Invoice** | State, cadence step, next action, contact history, the full audit timeline | — | `GET /dunning/invoices/{id}` |
| **Work queues** | One tab per team (ar-ops, disputes, collections-lead, qa), soonest due first, overdue highlighted | Resolve with one of the task's allowed resolutions | `GET /tasks`, `PATCH /tasks/{id}` |
| **Cash application** | Last ingest result line by line (rule, reason, where the money went); the exceptions queue | Simulate a feed, upload a statement CSV, apply or dismiss an exception | `/cash/*` |
| **Experiments** | Prompt versions; runs with live progress; results with win-rate, confidence intervals, sign test and per-tag breakdown | Sync prompts, start an A/B run | `/prompts/*`, `/experiments/*` |

A status badge in the header polls `/health/ready`, so a cold start reads as "starting up" rather
than as a broken page.

## Backend work the frontend needs

Small additions, each with tests:

1. `GET /dunning/summary`: the overview figures in one call. The same queries `refresh_metrics()`
   runs, returned as JSON rather than scraped from Prometheus.
2. `GET /dunning/customers`: paginated list with filters (tier, state, has open tasks).
3. Static file serving for `frontend/dist`, with a fallback to `index.html` for client routes.
4. `POST /admin/demo/reset`: wipe the workflow and cash tables and re-sync the ledger, so a public
   demo that visitors have clicked through can be put back to a clean state before an interview.

Later, for the strongest part of a live demo:

5. **Demo call with a streamed transcript.** `POST /dunning/invoices/{id}/demo-call` with a chosen
   customer persona (cooperative, disputes, already paid), run through the same simulator, extractor
   and judge as the eval set, with turns streamed over Server-Sent Events. The page then shows the
   conversation appearing, the extracted outcome, and the invoice and tasks changing. This is also
   where time-to-first-token gets measured (the streaming roadmap item).

## Authentication

Planned as its own milestone before the public deploy (README roadmap). The frontend's part:

- **Read-only by default.** Anyone with the link can look at everything.
- **Operator and admin keys** unlock actions. A "Sign in" dialog stores the key in the browser and
  sends it as a header; the action buttons are disabled with a tooltip until then.
- **A role switcher** for demos: viewer / operator / admin, to show that writing off a debt needs
  more authority than resolving a task.
- The acting role is recorded on every audit event, so the timeline shows who did what.

## Deployment

- **Render free web service** from `infra/render.yaml` (Docker), **Neon** free Postgres as
  `DATABASE_URL` (Render's free Postgres is deleted after 30 days).
- `GROQ_API_KEY` as a Render secret for experiments and demo calls.
- After the first deploy: `POST /dunning/sync` once (or the demo reset), then the scheduler runs on
  its own timer.
- **Cold starts:** the free instance sleeps after about 15 minutes idle and takes 30 to 60 seconds
  to wake. Open the URL a minute before an interview. A scheduled ping could keep it awake, but it
  is not needed for occasional demos.
- CI already builds the image and boots it; it will also build the frontend, and a Playwright smoke
  test can load each page against the booted container.

## Order of work

| Phase | Scope | Rough size |
|---|---|---|
| 1 ✅ | Scaffold (Vite, Tailwind, generated client, routing), static serving, `GET /dunning/summary`, `GET /dunning/customers`, Docker build stage. Pages: Overview, Call queue, Customers, Customer account, Invoice, Work queues, Cash application. Recharts was dropped: CSS bars cover the overview, and Recharts 2 is no longer maintained. | done |
| 2 | Experiments pages: prompt list, start a run, live progress, results. | 1 day |
| 3 | Authentication (backend keys and roles, frontend sign-in and role switcher), demo reset, CORS allow-list. | 1 day |
| 4 | Deploy to Render with Neon; Playwright smoke test in CI. | half a day |
| 5 | Demo call with a streamed transcript (SSE) and time-to-first-token. | 1 to 2 days |

Phases 1 to 4 give a public, safe, clickable demo. Phase 5 is the part that makes it memorable.
