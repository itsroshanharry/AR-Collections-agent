"""Single source of truth for keys, model IDs and endpoints.

Everything reads settings from here rather than os.environ directly, so provider
swaps (Groq -> Cerebras, local -> cloud voice) are one config change.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
SYNTHETIC_DIR = DATA_DIR / "synthetic"
PROMPTS_DIR = REPO_ROOT / "voice_agent" / "prompts"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", REPO_ROOT / ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- LLM providers ---
    groq_api_key: str | None = None
    cerebras_api_key: str | None = None
    ollama_base_url: str = "http://localhost:11434/v1"

    # llama-3.3-70b-versatile / llama-3.1-8b-instant were decommissioned 2026-08-16.
    groq_model_large: str = "openai/gpt-oss-120b"
    groq_model_small: str = "openai/gpt-oss-20b"
    cerebras_model_large: str = "gpt-oss-120b"
    ollama_model: str = "llama3.1:8b"

    groq_base_url: str = "https://api.groq.com/openai/v1"
    cerebras_base_url: str = "https://api.cerebras.ai/v1"

    # Cerebras' free tier caps context at 8K tokens; transcripts are trimmed to fit
    # before we allow a failover onto it.
    cerebras_context_limit_tokens: int = 8000

    # Groq free tier, read from x-ratelimit-limit-tokens. Per model, per minute - and
    # charged against reserved max_tokens, not tokens actually generated. Used to pace
    # requests client-side so 429s stop happening rather than being retried after.
    groq_tokens_per_minute: int = 8000

    # --- Voice stack ---
    voice_provider: Literal["local", "cloud"] = "local"
    kokoro_base_url: str = "http://localhost:8880/v1"
    kokoro_voice: str = "af_alloy"
    deepgram_api_key: str | None = None
    groq_stt_model: str = "whisper-large-v3-turbo"

    # --- LiveKit (unused in `console` mode) ---
    livekit_url: str | None = None
    livekit_api_key: str | None = None
    livekit_api_secret: str | None = None

    # --- Database ---
    # Render's free Postgres is deleted 30 days after creation; use Neon when deployed.
    database_url: str = "sqlite+aiosqlite:///./local.db"

    # --- Experiment runner ---
    # Groq's free tier allows 8,000 tokens/min per model (verified from the
    # x-ratelimit-limit-tokens header - secondary sources claiming 30K are wrong).
    # One simulated call plus judging costs roughly 10K, so concurrency above 1 just
    # generates 429s that the client then has to sleep off. Raise this only on a paid
    # tier, where it is the single biggest speed-up available.
    max_concurrent_llm_calls: int = 1

    @property
    def has_any_llm_provider(self) -> bool:
        return bool(self.groq_api_key or self.cerebras_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
