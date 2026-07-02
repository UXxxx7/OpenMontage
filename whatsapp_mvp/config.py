# WhatsApp MVP - Configuration
# Loads environment variables and provides typed config.

from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv

# Project root is two levels up from this file
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")


@dataclass
class Config:
    # WhatsApp Cloud API
    whatsapp_verify_token: str = field(
        default_factory=lambda: os.getenv("WHATSAPP_VERIFY_TOKEN", "")
    )
    whatsapp_access_token: str = field(
        default_factory=lambda: os.getenv("WHATSAPP_ACCESS_TOKEN", "")
    )
    whatsapp_phone_number_id: str = field(
        default_factory=lambda: os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
    )
    whatsapp_app_secret: str = field(
        default_factory=lambda: os.getenv("WHATSAPP_APP_SECRET", "")
    )

    # Redis / Queue
    redis_url: str = field(
        default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379/0")
    )

    # Database
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL", f"sqlite:///{_PROJECT_ROOT / 'openmontage_whatsapp.db'}"
        )
    )

    # Storage
    storage_root: Path = field(
        default_factory=lambda: Path(
            os.getenv("STORAGE_ROOT", str(_PROJECT_ROOT / "storage"))
        )
    )

    # Public base URL (for ngrok / tunnel)
    public_base_url: str = field(
        default_factory=lambda: os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")
    )

    # LLM 规划器配置
    # provider 可选: deepseek | openai | claude | custom（中转站/OpenAI兼容）
    llm_provider: str = field(
        default_factory=lambda: os.getenv("LLM_PROVIDER", "deepseek")
    )
    llm_api_key: str = field(
        default_factory=lambda: os.getenv(
            "LLM_API_KEY",
            os.getenv("DEEPSEEK_API_KEY", os.getenv("OPENAI_API_KEY", "")),
        )
    )
    llm_base_url: str = field(
        default_factory=lambda: os.getenv("LLM_BASE_URL", "")
    )
    llm_model: str = field(
        default_factory=lambda: os.getenv("LLM_MODEL", "deepseek-chat")
    )
    # 保留兼容旧配置
    deepseek_api_key: str = field(
        default_factory=lambda: os.getenv("DEEPSEEK_API_KEY", "")
    )
    openai_api_key: str = field(
        default_factory=lambda: os.getenv("OPENAI_API_KEY", "")
    )

    # Transcription
    transcribe_provider: str = field(
        default_factory=lambda: os.getenv("TRANSCRIBE_PROVIDER", "faster_whisper")
    )
    faster_whisper_model: str = field(
        default_factory=lambda: os.getenv("FASTER_WHISPER_MODEL", "small")
    )

    # OpenMontage
    openmontage_root: Path = field(
        default_factory=lambda: Path(
            os.getenv("OPENMONTAGE_ROOT", str(_PROJECT_ROOT))
        )
    )
    remotion_preview: bool = field(
        default_factory=lambda: os.getenv("REMOTION_PREVIEW", "true").lower() == "true"
    )

    @property
    def jobs_dir(self) -> Path:
        return self.storage_root / "jobs"

    def ensure_dirs(self) -> None:
        self.jobs_dir.mkdir(parents=True, exist_ok=True)


_config: Optional[Config] = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
        _config.ensure_dirs()
    return _config

