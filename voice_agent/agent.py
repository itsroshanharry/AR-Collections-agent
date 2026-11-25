"""Collections voice agent - LiveKit Agents 1.6 worker, plus a text-only simulate mode.

Two ways to run it:

    # Live voice, fully local, no LiveKit server and no cloud minutes.
    # Start Kokoro first (.\\start-cpu.ps1 in a Kokoro-FastAPI checkout).
    uv run python -m voice_agent.agent console

    # Text-only sweep over the eval set - deterministic, CI-safe, no audio stack.
    uv run python -m voice_agent.agent --simulate

Simulate mode exercises the same prompts, the same outcome extractor and the same judge
as the voice path; only the transport differs. That is what makes the A/B dashboard's
numbers statements about this agent rather than about a separate toy.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

from shared.config import REPO_ROOT, get_settings
from shared.models import Invoice, PromptVersion, RiskTier
from shared.prompt_store import load_all
from voice_agent.dunning_logic import build_call_queue, classify_risk, tone_for_tier

# pydantic-settings parses .env straight into a Settings object and never touches
# os.environ. The LiveKit worker reads LIVEKIT_URL/API_KEY/API_SECRET from the real
# process environment, so without this it starts with no credentials even though
# get_settings() shows them loaded correctly.
load_dotenv(REPO_ROOT / ".env")
load_dotenv(REPO_ROOT / ".env.local", override=True)

logger = logging.getLogger("collections-agent")

# Reference date for resolving spoken dates in simulate mode; matches the eval set.
SIMULATE_REFERENCE_DATE = date(2026, 8, 1)


def select_prompt(tier: RiskTier, prompts: list[PromptVersion] | None = None) -> PromptVersion:
    """Pick the prompt version whose declared tier coverage matches this call.

    Falls back to the highest version that declares no tiers, then to the highest
    version overall - a missing mapping should degrade to *some* valid prompt rather
    than drop the call.
    """
    prompts = prompts or load_all()
    if not prompts:
        raise RuntimeError("No prompt versions found in voice_agent/prompts/")

    matching = [p for p in prompts if tier in p.applies_to_tier]
    if matching:
        return max(matching, key=lambda p: p.version)

    logger.warning("No prompt declares tier %s; falling back", tier.value)
    unscoped = [p for p in prompts if not p.applies_to_tier]
    return max(unscoped or prompts, key=lambda p: p.version)


class NoCallDue(RuntimeError):
    """The workflow has invoices, but none may be called right now."""


async def pick_call_target(now: datetime | None = None) -> tuple[Invoice, RiskTier]:
    """The invoice this call is about, and the risk tier to call it at.

    In order:
      1. CALL_INVOICE_ID pins one (demos). Taken from the workflow if synced there - so
         the call uses the aged balance and any broken-promise tier bump - else the CSV.
      2. The dunning workflow's call queue: the highest-priority invoice whose cadence
         wants a call that the contact rules allow right now.
      3. Only if the workflow is empty (never synced): the static CSV queue, as before.

    Falling back to the CSV when the workflow simply has nothing due would call
    customers the contact rules say to leave alone, so that raises NoCallDue instead.
    """
    from data.generate_dataset import load_invoices
    from workflow import service

    wanted = os.getenv("CALL_INVOICE_ID")
    if wanted:
        try:
            detail = await service.invoice_detail(wanted, now=now)
            return detail.invoice, RiskTier(detail.row.tier)
        except service.NotFoundError:
            pass
        for inv in load_invoices():
            if inv.invoice_id == wanted:
                return inv, classify_risk(inv)
        raise SystemExit(f"CALL_INVOICE_ID={wanted!r} not found in the workflow or dataset")

    due = await service.due_calls(now=now, limit=1)
    if due:
        return due[0].invoice, due[0].tier
    if await service.has_invoices():
        raise NoCallDue(
            "No call is due: every invoice is waiting on its cadence, a contact rule, or a "
            "person. POST /dunning/tick to advance the schedule, or see GET /dunning/queue."
        )

    logger.warning(
        "Dunning workflow is empty; calling from the static CSV queue. "
        "POST /dunning/sync to manage calls through the workflow."
    )
    queue = build_call_queue(load_invoices(), limit=1)
    if not queue:
        raise SystemExit("No overdue invoices in the dataset.")
    return queue[0].invoice, queue[0].tier


# --------------------------------------------------------------------------------------
# Live voice path
# --------------------------------------------------------------------------------------


def build_server():
    """Construct the LiveKit AgentServer.

    Built lazily inside a function so that --simulate, the dashboard and the test suite
    never import the audio dependency tree.
    """
    from livekit import agents
    from livekit.agents import Agent, AgentServer, AgentSession, function_tool, inference

    from voice_agent.voice_factory import (
        build_llm,
        build_stt,
        build_tts,
        build_vad,
        dialogue_provider_label,
    )

    settings = get_settings()

    class CollectionsAgent(Agent):
        """One dunning call. Tool calls capture the outcome as it happens, rather than
        relying solely on a post-call extraction pass over the transcript."""

        def __init__(self, invoice: Invoice, prompt: PromptVersion) -> None:
            # Everything the tools capture is reconciled against the post-call
            # extraction by the dunning workflow; disagreement opens a QA task.
            super().__init__(instructions=prompt.render(invoice))
            self.invoice = invoice
            self.prompt = prompt
            self.captured: dict[str, object] = {}

        @function_tool
        async def record_promise_to_pay(self, payment_date: str, amount: str = "") -> str:
            """Record a payment date the customer has explicitly committed to.

            Call this only after the customer states a date themselves and you have read
            it back to them for confirmation.

            Args:
                payment_date: The committed date in YYYY-MM-DD form.
                amount: The amount they committed to, if they named one.
            """
            self.captured = {
                "outcome": "promise_to_pay",
                "promise_to_pay_date": payment_date,
                "amount": amount,
            }
            logger.info("Captured promise-to-pay: %s", payment_date)
            return f"Recorded a payment commitment for {payment_date}."

        @function_tool
        async def record_dispute(self, reason: str) -> str:
            """Record that the customer is disputing this invoice.

            Args:
                reason: The customer's stated reason, in their own words.
            """
            self.captured = {"outcome": "dispute_raised", "dispute_reason": reason}
            logger.info("Captured dispute: %s", reason)
            return "Recorded the dispute. It will be routed for review."

        @function_tool
        async def record_already_paid(self, payment_reference: str = "") -> str:
            """Record that the customer says this invoice is already settled.

            Args:
                payment_reference: Their payment reference or UTR, if given.
            """
            self.captured = {
                "outcome": "already_paid",
                "payment_reference": payment_reference,
            }
            logger.info("Captured already-paid claim: %s", payment_reference)
            return "Recorded that the invoice is reported as paid. It will be verified."

    server = AgentServer()

    @server.rtc_session(agent_name="ar-collections-agent")
    async def entrypoint(ctx: agents.JobContext) -> None:
        from ab_dashboard.db import init_db

        await init_db()
        try:
            invoice, tier = await pick_call_target()
        except NoCallDue as exc:
            logger.warning("%s", exc)
            ctx.shutdown(reason="no call due")
            return
        prompt = select_prompt(tier)
        logger.info(
            "Calling about %s (%s %s, %d days overdue) tier=%s tone=%s prompt=%s",
            invoice.invoice_id,
            invoice.amount,
            invoice.currency,
            invoice.days_overdue,
            tier.value,
            tone_for_tier(tier).value,
            prompt.label,
        )

        agent = CollectionsAgent(invoice, prompt)
        # Recorded on the call log so a voice call's scores can be attributed to the model
        # that actually spoke, the same way simulate mode records it per turn.
        dialogue_provider = dialogue_provider_label(settings)
        session = AgentSession(
            vad=build_vad(),
            stt=build_stt(settings),
            llm=build_llm(settings),
            tts=build_tts(settings),
            # v1-mini runs locally on CPU and is free in any context; v1 would call
            # LiveKit Inference. Pinned explicitly so a hosted deploy doesn't silently
            # start billing for turn detection.
            turn_detection=inference.TurnDetector(version="v1-mini"),
        )

        await session.start(agent=agent, room=ctx.room)
        await session.generate_reply(
            instructions=(
                "Greet the customer briefly and professionally, then move to the invoice."
            )
        )

        # Score and persist once the call ends.
        async def _finalise() -> None:
            await _score_and_log_call(
                invoice,
                prompt,
                session,
                agent,
                mode="voice",
                provider=dialogue_provider,
                tier=tier,
            )

        ctx.add_shutdown_callback(_finalise)

    return server


async def _score_and_log_call(
    invoice, prompt, session, agent, *, mode: str, provider: str, tier: RiskTier
) -> None:
    """Extract the outcome, judge the transcript, write it to call_logs, and feed the
    outcome into the dunning workflow (next state + follow-up tasks)."""
    from ab_dashboard.db import init_db
    from shared.eval.judge import judge_transcript
    from shared.llm_client import LLMClient
    from voice_agent.call_logger import log_call, prompt_health
    from voice_agent.outcome_extractor import extract_outcome
    from workflow import service as workflow

    try:
        items = session.history.items
    except Exception:  # noqa: BLE001 - history shape varies; never fail a call on it
        items = []

    lines: list[str] = []
    for item in items:
        role = getattr(item, "role", None)
        text = getattr(item, "text_content", None) or getattr(item, "content", None)
        if not role or not text:
            continue
        speaker = {"assistant": "agent", "user": "customer"}.get(str(role))
        if speaker:
            lines.append(f"{speaker}: {text}")
    transcript = "\n".join(lines)

    if not transcript.strip():
        logger.warning("Empty transcript; nothing to score.")
        return

    client = LLMClient()
    try:
        await init_db()
        outcome, _ = await extract_outcome(client, invoice, transcript)
        score, _ = await judge_transcript(
            client,
            invoice,
            transcript,
            assigned_tone=tone_for_tier(tier).value,
        )
        call_log_id = await log_call(
            invoice,
            prompt.id,
            transcript,
            outcome,
            score,
            mode=mode,
            provider=provider,
        )
        health = await prompt_health(prompt.id)
        logger.info("Prompt %s health: %s", prompt.id, health.reason)

        report = await workflow.record_call_outcome(
            invoice.invoice_id,
            outcome,
            call_log_id=call_log_id,
            captured=agent.captured,
            invoice=invoice,
        )
        logger.info(
            "Workflow: %s is now %s (tier %s), %d task(s) opened, next %s at %s",
            report.invoice_id,
            report.state,
            report.tier,
            len(report.tasks_opened),
            report.next_action_channel,
            report.next_action_at,
        )
        if report.discrepancy:
            logger.warning("Capture/extraction disagreement: %s", report.discrepancy)
    finally:
        await client.aclose()


# --------------------------------------------------------------------------------------
# Simulate path
# --------------------------------------------------------------------------------------


async def run_simulation(
    limit: int | None,
    prompt_id: str | None,
    log_to_db: bool,
    only: list[str] | None = None,
    *,
    min_accuracy: float | None = None,
    require_injections_resisted: bool = False,
    max_hallucination_rate: float | None = None,
    report_path: str | None = None,
) -> int:
    """Drive the eval set text-only and report extraction accuracy against ground truth.

    With any gate threshold set, exits 1 when the sweep misses it, so CI can block a
    prompt change that regresses the agent. `report_path` writes the full summary as
    JSON for a CI artifact.
    """
    from ab_dashboard.db import init_db
    from shared.eval.eval_set import load_eval_set
    from shared.eval.judge import judge_transcript
    from shared.eval.metrics import injection_held, promise_date_correct
    from shared.eval.report import ScenarioRun, apply_gate, summarise
    from shared.llm_client import LLMClient
    from voice_agent.call_logger import log_call
    from voice_agent.outcome_extractor import extract_outcome
    from voice_agent.simulator import simulate_call

    settings = get_settings()
    if not settings.has_any_llm_provider:
        logger.error("No GROQ_API_KEY or CEREBRAS_API_KEY set. Copy .env.example to .env.")
        return 2

    scenarios = load_eval_set()
    if only:
        # Substring match so "S07" or "injection" both work.
        wanted = [s.lower() for s in only]
        scenarios = [s for s in scenarios if any(w in s.scenario_id.lower() for w in wanted)]
        if not scenarios:
            logger.error("No scenario matched %s", only)
            return 2
    if limit:
        scenarios = scenarios[:limit]
    prompts = load_all()

    if log_to_db:
        await init_db()

    client = LLMClient(settings)
    runs: list[ScenarioRun] = []

    try:
        for scenario in scenarios:
            tier = classify_risk(scenario.invoice)
            prompt = (
                next(p for p in prompts if p.id == prompt_id)
                if prompt_id
                else select_prompt(tier, prompts)
            )

            call = await simulate_call(client, prompt, scenario)
            outcome, _ = await extract_outcome(
                client,
                scenario.invoice,
                call.transcript,
                reference_date=SIMULATE_REFERENCE_DATE,
            )
            score, _ = await judge_transcript(
                client,
                scenario.invoice,
                call.transcript,
                assigned_tone=tone_for_tier(tier).value,
                expected_outcome=scenario.expected_outcome,
            )

            match = outcome.outcome == scenario.expected_outcome
            date_ok = promise_date_correct(
                outcome.promise_to_pay_date, scenario.expected_promise_date
            )
            held = (
                injection_held(
                    errored=False,
                    hallucinated=score.hallucinated,
                    extracted_outcome=outcome.outcome,
                    compliance_violations=score.compliance_violations,
                )
                if scenario.is_adversarial
                else None
            )
            runs.append(
                ScenarioRun(
                    scenario_id=scenario.scenario_id,
                    tags=tuple(scenario.tags),
                    expected=scenario.expected_outcome.value,
                    got=outcome.outcome.value,
                    outcome_correct=match,
                    date_correct=date_ok,
                    is_adversarial=scenario.is_adversarial,
                    injection_held=held,
                    composite=score.composite,
                    hallucinated=score.hallucinated,
                )
            )

            flag = "OK " if match else "MISS"
            extra = ""
            if outcome.promise_to_pay_date:
                expected = scenario.expected_promise_date
                mark = "=" if outcome.promise_to_pay_date == expected else "!="
                extra = f" date={outcome.promise_to_pay_date}{mark}{expected}"
            if score.hallucinated:
                extra += f" HALLUCINATED={score.hallucinated_figures}"
            if score.compliance_violations:
                extra += f" VIOLATION={score.compliance_violations}"

            print(
                f"{flag} {scenario.scenario_id:32s} {prompt.id:24s} "
                f"expected={scenario.expected_outcome.value:16s} "
                f"got={outcome.outcome.value:16s} composite={score.composite:.2f}{extra}"
            )

            if log_to_db:
                await log_call(
                    scenario.invoice,
                    prompt.id,
                    call.transcript,
                    outcome,
                    score,
                    mode="simulate",
                    duration_ms=call.total_latency_ms,
                    provider=call.providers[0] if call.providers else "",
                )
    finally:
        await client.aclose()

    report = apply_gate(
        summarise(runs),
        min_accuracy=min_accuracy,
        require_all_injections_resisted=require_injections_resisted,
        max_hallucination_rate=max_hallucination_rate,
    )
    lo, hi = report.outcome_accuracy_ci
    print(
        f"\nOutcome accuracy:      {report.outcome_accuracy:.0%} of {report.scenarios} "
        f"(95% CI {lo:.0%}-{hi:.0%})"
    )
    if report.promise_date_accuracy is not None:
        print(f"Promise-date accuracy: {report.promise_date_accuracy:.0%}")
    if report.adversarial:
        print(f"Injections resisted:   {report.injections_resisted}/{report.adversarial}")
    print(f"Hallucination rate:    {report.hallucination_rate:.0%}")
    weak = {t: v for t, v in report.by_tag.items() if v["accuracy"] < 1.0}
    if weak:
        print(
            "Weak tags:             "
            + ", ".join(f"{t} {v['accuracy']:.0%}" for t, v in weak.items())
        )

    if report_path:
        Path(report_path).write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        print(f"Report written to {report_path}")
    if report.failures:
        print("\nGATE FAILED:\n  - " + "\n  - ".join(report.failures))
        return 1
    return 0


def main() -> int:
    from shared.observability.logging import configure_logging
    from shared.observability.tracing import setup_tracing

    settings = get_settings()
    configure_logging(settings.log_format, settings.log_level)
    setup_tracing("ar-voice-agent")

    # --simulate is handled here, before LiveKit's CLI takes over argv entirely.
    if "--simulate" in sys.argv:
        parser = argparse.ArgumentParser(prog="voice_agent.agent")
        parser.add_argument("--simulate", action="store_true")
        parser.add_argument("--limit", type=int, default=None)
        parser.add_argument(
            "--prompt-id", default=None, help="Force one prompt instead of tier-matching."
        )
        parser.add_argument(
            "--log-to-db", action="store_true", help="Write each call to call_logs."
        )
        parser.add_argument(
            "--only",
            nargs="+",
            default=None,
            metavar="ID",
            help=(
                "Run only scenarios whose id contains one of these (e.g. --only S07 S17). "
                "A full sweep costs ~50K tokens of a 200K/day per-model budget; use this "
                "to re-verify a change without spending a quarter of the day's quota."
            ),
        )
        gate = parser.add_argument_group(
            "quality gate", "Exit 1 if the sweep misses any threshold given (for CI)."
        )
        gate.add_argument("--min-accuracy", type=float, default=None, metavar="0-1")
        gate.add_argument(
            "--require-injections-resisted",
            action="store_true",
            help="Fail if any adversarial scenario is not resisted.",
        )
        gate.add_argument("--max-hallucination-rate", type=float, default=None, metavar="0-1")
        gate.add_argument("--report", default=None, metavar="PATH", help="Write JSON summary.")
        args = parser.parse_args()
        return asyncio.run(
            run_simulation(
                args.limit,
                args.prompt_id,
                args.log_to_db,
                args.only,
                min_accuracy=args.min_accuracy,
                require_injections_resisted=args.require_injections_resisted,
                max_hallucination_rate=args.max_hallucination_rate,
                report_path=args.report,
            )
        )

    # `console` runs entirely locally; every other subcommand needs a LiveKit project.
    if not any(a in sys.argv for a in ("console", "download-files")):
        settings = get_settings()
        missing = [
            name
            for name, value in (
                ("LIVEKIT_URL", settings.livekit_url),
                ("LIVEKIT_API_KEY", settings.livekit_api_key),
                ("LIVEKIT_API_SECRET", settings.livekit_api_secret),
            )
            if not value
        ]
        if missing:
            logger.error(
                "Missing %s in .env. Get them from https://cloud.livekit.io -> your "
                "project -> Settings -> Keys. (`console` mode needs none of these.)",
                ", ".join(missing),
            )
            return 2

    _check_voice_stack()

    from livekit import agents

    agents.cli.run_app(build_server())
    return 0


def _check_voice_stack() -> None:
    """Warn about a missing voice dependency before a call starts, not mid-sentence."""
    settings = get_settings()
    if settings.voice_provider == "cloud":
        if not settings.deepgram_api_key:
            logger.error("VOICE_PROVIDER=cloud but DEEPGRAM_API_KEY is not set.")
    else:
        import socket
        from urllib.parse import urlparse

        parsed = urlparse(settings.kokoro_base_url)
        with socket.socket() as sock:
            sock.settimeout(1.5)
            if sock.connect_ex((parsed.hostname or "localhost", parsed.port or 8880)) != 0:
                logger.error(
                    "VOICE_PROVIDER=local but nothing is listening on %s. Start Kokoro "
                    "(docker compose -f infra/docker-compose.kokoro.yml up) or switch to "
                    "VOICE_PROVIDER=cloud with a DEEPGRAM_API_KEY.",
                    settings.kokoro_base_url,
                )


if __name__ == "__main__":
    sys.exit(main())
