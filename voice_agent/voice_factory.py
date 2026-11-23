"""STT/TTS selection, isolated behind one factory.

Local dev runs Groq Whisper + a locally hosted Kokoro; deployed runs Deepgram, because
Render's free tier has no GPU. Keeping both behind this module means swapping providers
is an env var, and adding a local Parakeet plugin later is a new branch here rather than
a rewrite of agent.py.

IMPORTANT: the plugin imports below are at module level on purpose. LiveKit registers
plugins as a side effect of import and requires that to happen on the main thread -
importing them inside build_stt()/build_tts() instead raises
"RuntimeError: Plugins must be registered on the main thread", because those run inside
the job entrypoint's asyncio task.

The dashboard, the tests and the --simulate path still avoid the audio dependency tree,
because this *module* is imported lazily from build_server(), which runs on the main
thread. Keep it that way: do not import voice_factory at the top of agent.py.
"""

from __future__ import annotations

import logging

from livekit.plugins import deepgram, groq, openai, silero

from shared.config import Settings

logger = logging.getLogger(__name__)


def build_stt(settings: Settings):
    """Speech-to-text.

    local -> Groq whisper-large-v3-turbo. Non-streaming, so AgentSession wraps it with a
             VAD-driven StreamAdapter internally.
    cloud -> Deepgram Nova (natively streaming).

    A local Parakeet TDT plugin belongs here as a third branch. It scores ~1.5-2 points
    better on English WER than whisper-turbo and runs offline, but needs a custom
    stt.STT subclass since LiveKit ships no official plugin for it.
    """
    if settings.voice_provider == "cloud":
        if not settings.deepgram_api_key:
            raise RuntimeError("VOICE_PROVIDER=cloud requires DEEPGRAM_API_KEY")
        logger.info("STT: deepgram nova-3")
        # Keys are passed explicitly rather than left to the plugins' os.environ lookup.
        # Settings comes from pydantic-settings, which parses .env without touching
        # os.environ - so relying on the implicit path makes this module silently
        # dependent on someone else having called load_dotenv() first.
        return deepgram.STT(
            model="nova-3", language="en-IN", api_key=settings.deepgram_api_key
        )

    if not settings.groq_api_key:
        raise RuntimeError("VOICE_PROVIDER=local requires GROQ_API_KEY for Whisper STT")
    logger.info("STT: groq %s", settings.groq_stt_model)
    return groq.STT(
        model=settings.groq_stt_model, language="en", api_key=settings.groq_api_key
    )


def build_tts(settings: Settings):
    """Text-to-speech.

    local -> Kokoro via its OpenAI-compatible endpoint. Start it first:
                 .\\start-cpu.ps1        (from a Kokoro-FastAPI checkout)
             CPU is the safe default here: the GPU image wants ~3.1GB and an RTX 2050
             has only 4GB total, which does not leave room for anything else.
    cloud -> Deepgram Aura-2.
    """
    if settings.voice_provider == "cloud":
        if not settings.deepgram_api_key:
            raise RuntimeError("VOICE_PROVIDER=cloud requires DEEPGRAM_API_KEY")
        logger.info("TTS: deepgram aura-2")
        return deepgram.TTS(
            model="aura-2-thalia-en", api_key=settings.deepgram_api_key
        )

    logger.info("TTS: kokoro at %s (voice=%s)", settings.kokoro_base_url, settings.kokoro_voice)
    return openai.TTS(
        model="kokoro",
        voice=settings.kokoro_voice,
        base_url=settings.kokoro_base_url,
        api_key="not-needed",
        response_format="wav",
    )


def build_llm(settings: Settings):
    """Dialogue LLM for the live voice path.

    Note this is the LiveKit plugin, not shared.llm_client - AgentSession needs a
    streaming LLM object it controls. The Groq->Cerebras failover chain still governs
    the judge and the outcome extractor, which are the parts where a rate-limit stall
    would corrupt a result rather than just drop one call.
    """
    if not settings.groq_api_key:
        raise RuntimeError("GROQ_API_KEY is required for the voice agent's LLM")
    logger.info("LLM: groq %s (reasoning_effort=low)", settings.groq_model_large)
    return groq.LLM(
        model=settings.groq_model_large,
        api_key=settings.groq_api_key,
        # gpt-oss is a reasoning model and reasoning is billed against the same token
        # budget as the reply. On a phone call that budget is also dead air before the
        # agent starts speaking, and an over-thought turn can be truncated mid-sentence.
        reasoning_effort="low",
    )


def dialogue_provider_label(settings: Settings) -> str:
    """Which provider/model build_llm() puts on the call, for the call log.

    Kept next to build_llm so the two change together. The call log's `provider` column
    used to be a hardcoded "groq" at the call site, which would have gone on reporting
    Groq after build_llm was pointed anywhere else. Same format as the provider names
    LLMClient records in simulate mode, so voice and simulated calls group together.
    """
    return "groq"


def build_vad():
    """Silero VAD - local, CPU, no key."""
    return silero.VAD.load()
