"""Application settings, loaded from environment variables / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"

try:
    from dotenv import load_dotenv

    # Always read the project's .env, no matter which folder `streamlit run` is started from.
    load_dotenv(ENV_FILE, override=False)
except ImportError:  # python-dotenv is optional
    pass
DATA_DIR = BASE_DIR / "data"
DATASETS_DIR = DATA_DIR / "datasets"   # downloaded public datasets
UPLOADS_DIR = DATA_DIR / "uploads"     # CSV/Excel uploads converted to SQLite
SESSIONS_DIR = BASE_DIR / "sessions"
CATALOG_FILE = BASE_DIR / "datasets.json"        # dataset catalog (shipped, extensible)
CONNECTIONS_FILE = BASE_DIR / "connections.json"  # user's saved connections (git-ignored)

# Known OpenAI-compatible providers. Any other endpoint works via provider "custom" / LLM_BASE_URL.
PROVIDERS = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "openai/gpt-oss-120b",
        "models": ["openai/gpt-oss-120b", "openai/gpt-oss-20b"],
        "key_env": "GROQ_API_KEY",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4.1-mini",
        "models": ["gpt-4.1-mini", "gpt-4.1", "gpt-4o-mini"],
        "key_env": "OPENAI_API_KEY",
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "model": "gpt-oss:20b",
        "models": ["gpt-oss:20b", "qwen2.5-coder:7b", "llama3.1:8b"],
        "key_env": "",
    },
}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


@dataclass
class Settings:
    provider: str = os.getenv("LLM_PROVIDER", "groq").lower()
    model: str = os.getenv("LLM_MODEL", "")
    base_url: str = os.getenv("LLM_BASE_URL", "")
    api_key: str = os.getenv("LLM_API_KEY", "")
    temperature: float = float(os.getenv("LLM_TEMPERATURE", "0.1"))
    reasoning_effort: str = os.getenv("LLM_REASONING_EFFORT", "medium").lower()  # low | medium | high (gpt-oss)

    database_url: str = os.getenv("DATABASE_URL", "")          # takes priority if set
    default_dataset: str = os.getenv("DEFAULT_DATASET", "chinook")
    row_limit: int = _int("ROW_LIMIT", 500)
    max_retries: int = _int("MAX_SQL_RETRIES", 3)
    history_turns: int = _int("HISTORY_TURNS", 8)
    max_steps: int = _int("MAX_REASONING_STEPS", 5)
    query_timeout_s: int = _int("QUERY_TIMEOUT_S", 30)
    read_only: bool = os.getenv("READ_ONLY", "true").lower() != "false"

    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        preset = PROVIDERS.get(self.provider, {})
        self.model = self.model or preset.get("model", "")
        self.base_url = self.base_url or preset.get("base_url", "")
        if not self.api_key and preset.get("key_env"):
            self.api_key = os.getenv(preset["key_env"], "")
        if self.provider == "ollama" and not self.api_key:
            self.api_key = "ollama"  # Ollama ignores the key but the client requires one


def key_setup_hint() -> str:
    """Explain why no API key was found."""
    if not ENV_FILE.exists():
        return ("No `.env` file found. Create a file named `.env` in the project folder containing "
                "`GROQ_API_KEY=your_key`, then restart the app — or paste the key in the sidebar under "
                "🤖 Language model. Free Groq keys: console.groq.com/keys")
    return ("`.env` was found but has no API key for this provider. Set `GROQ_API_KEY=...` (provider groq) and restart, "
            "or paste the key in the sidebar under 🤖 Language model.")
