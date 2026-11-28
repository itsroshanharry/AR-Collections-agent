"""Single source of truth for keys, model IDs and endpoints.

Everything reads settings from here rather than os.environ directly, so provider
swaps (Groq -> Cerebras, local -> cloud voice) are one config change.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
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

    # --- Observability ---
    # text for terminals, json for log pipelines (Loki, CloudWatch, Datadog...).
    log_format: Literal["text", "json"] = "text"
    log_level: str = "INFO"

    # --- Access control (ab_dashboard/auth.py) ---
    # Sent as X-API-Key. Operators work the book; admins can also write debts off, send
    # them to legal, start experiments (which spend LLM budget) and reset the demo.
    operator_api_key: str | None = None
    admin_api_key: str | None = None
    # The role of a request without a key. Unset: admin while no keys are configured (a
    # local checkout just works), viewer once any key is. Set it to "viewer" when
    # deploying, so a deploy with a missing key fails closed rather than open.
    public_role: Literal["viewer", "operator", "admin"] | None = None
    # Origins allowed to call the API from a browser, comma-separated. The console is
    # served from the API's own origin and needs none.
    cors_allow_origins: str = ""

    # --- Dunning workflow ---
    # Seconds between scheduler passes in the dashboard process. 0 disables the timer;
    # POST /dunning/tick still runs a pass on demand.
    dunning_tick_seconds: float = 300

    @field_validator("operator_api_key", "admin_api_key", "public_role", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        # `ADMIN_API_KEY=` in an env file means "not set", not "the empty key".
        return None if isinstance(value, str) and not value.strip() else value

    @property
    def has_any_llm_provider(self) -> bool:
        return bool(self.groq_api_key or self.cerebras_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
