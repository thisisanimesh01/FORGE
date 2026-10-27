"""Configuration management for FORGE using Pydantic Settings."""

import os
import re
from pathlib import Path
from typing import List, Optional
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret|token|password|auth|bearer)\s*[:=]\s*['\"]?([a-zA-Z0-9_\-\.]{8,})['\"]?"),
    re.compile(r"(?i)(bearer\s+)([a-zA-Z0-9_\-\.]{12,})"),
    re.compile(r"ghp_[a-zA-Z0-9]{36}"),
    re.compile(r"github_pat_[a-zA-Z0-9_]{82}"),
    re.compile(r"sk-[a-zA-Z0-9_\-]{20,64}"),
    re.compile(r"xox[baprs]-[a-zA-Z0-9]{10,48}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    # Groq keys (gsk_...)
    re.compile(r"gsk_[a-zA-Z0-9]{20,}"),
]


def redact_secrets(text: str) -> str:
    """Scrub sensitive keys and tokens from logs and reports."""
    if not text:
        return text
    sanitized = text
    for pattern in SECRET_PATTERNS:
        sanitized = pattern.sub(r"[REDACTED_SECRET]", sanitized)
    return sanitized


class ForgeSettings(BaseSettings):
    """Central configuration for FORGE runtime."""

    model_config = SettingsConfigDict(
        env_prefix="FORGE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: str = Field(default="development", description="Environment: development, test, or production")
    log_level: str = Field(default="INFO", description="Logging verbosity")

    # Storage paths
    db_path: Path = Field(default=Path("forge.sqlite3"), description="Path to SQLite run/event database")
    workspace_dir: Path = Field(
        default=Path("/tmp/forge_workspaces"),
        description="Base temporary directory for acquired repositories",
    )

    # Model & Agent configuration
    llm_provider: str = Field(
        default="fake",
        description="LLM provider: fake, ollama, ollama_cloud, groq, gemini, or deepseek",
    )
    llm_model: str = Field(default="", description="Model identifier (provider default used when empty)")
    llm_api_base: Optional[str] = Field(default=None, description="Custom API endpoint base URL override (for proxy/vLLM)")

    # Per-provider credentials and configuration (supports *_API_KEY and *_API conventions)
    groq_api_key: str = Field(
        default="",
        description="API key for Groq Cloud",
        validation_alias=AliasChoices("GROQ_API_KEY", "GROQ_API", "FORGE_GROQ_API_KEY"),
    )
    gemini_api_key: str = Field(
        default="",
        description="API key for Google Gemini",
        validation_alias=AliasChoices("GEMINI_API_KEY", "GEMINI_API", "FORGE_GEMINI_API_KEY"),
    )
    deepseek_api_key: str = Field(
        default="",
        description="API key for DeepSeek",
        validation_alias=AliasChoices("DEEPSEEK_API_KEY", "DEEPSEEK_API", "FORGE_DEEPSEEK_API_KEY"),
    )
    ollama_api_key: str = Field(
        default="",
        description="API key for Ollama Cloud",
        validation_alias=AliasChoices("OLLAMA_API_KEY", "FORGE_OLLAMA_API_KEY"),
    )
    ollama_base_url: str = Field(
        default="http://localhost:11434",
        description="Base URL for local Ollama server",
        validation_alias=AliasChoices("OLLAMA_BASE_URL", "FORGE_OLLAMA_BASE_URL"),
    )

    # Safety & Resource Limits
    max_steps: int = Field(default=10, description="Maximum agent execution turns per investigation")
    step_timeout_seconds: int = Field(default=60, description="Timeout in seconds per tool execution")
    max_output_bytes: int = Field(default=50_000, description="Maximum allowed output bytes per tool execution")
    max_context_tokens: int = Field(default=8_000, description="Token budget cap for prompt context")

    # Docker sandbox options
    docker_image: str = Field(default="python:3.11-slim", description="Base container image for sandbox execution")
    docker_network_disabled: bool = Field(default=True, description="Enforce no-network execution in containers")

    # Cloud-fallback guard — MUST remain False; no automatic provider switching allowed
    allow_cloud_fallback: bool = Field(
        default=False,
        description="Automatic cloud provider fallback is forbidden by security policy. Must remain False.",
    )

    def get_sanitized_dict(self) -> dict:
        """Return configuration as dictionary with all credentials redacted."""
        raw = self.model_dump()
        for k in ("groq_api_key", "gemini_api_key", "deepseek_api_key", "ollama_api_key"):
            if raw.get(k):
                raw[k] = "[REDACTED]"
            else:
                raw[k] = "(not set)"
        raw["ollama_base_url"] = self.ollama_base_url
        return raw



def get_settings() -> ForgeSettings:
    """Factory to retrieve loaded settings instance."""
    return ForgeSettings()
