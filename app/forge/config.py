"""Configuration management for FORGE using Pydantic Settings."""

import os
import re
from pathlib import Path
from typing import List, Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret|token|password|auth|bearer)\s*[:=]\s*['\"]?([a-zA-Z0-9_\-\.]{8,})['\"]?"),
    re.compile(r"ghp_[a-zA-Z0-9]{36}"),
    re.compile(r"github_pat_[a-zA-Z0-9_]{82}"),
    re.compile(r"sk-[a-zA-Z0-9]{20,48}"),
    re.compile(r"xox[baprs]-[a-zA-Z0-9]{10,48}"),
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
    llm_provider: str = Field(default="fake", description="LLM provider: fake, openai, anthropic, or gemini")
    llm_api_key: Optional[str] = Field(default=None, description="API key for LLM provider")
    llm_model: str = Field(default="gpt-4o", description="Model identifier")
    llm_api_base: Optional[str] = Field(default=None, description="Custom API endpoint base URL")

    # Safety & Resource Limits
    max_steps: int = Field(default=10, description="Maximum agent execution turns per investigation")
    step_timeout_seconds: int = Field(default=60, description="Timeout in seconds per tool execution")
    max_output_bytes: int = Field(default=50_000, description="Maximum allowed output bytes per tool execution")
    max_context_tokens: int = Field(default=8_000, description="Token budget cap for prompt context")

    # Docker sandbox options
    docker_image: str = Field(default="python:3.11-slim", description="Base container image for sandbox execution")
    docker_network_disabled: bool = Field(default=True, description="Enforce no-network execution in containers")

    def get_sanitized_dict(self) -> dict:
        """Return configuration as dictionary with credentials redacted."""
        raw = self.model_dump()
        if raw.get("llm_api_key"):
            raw["llm_api_key"] = "[REDACTED]"
        return raw


def get_settings() -> ForgeSettings:
    """Factory to retrieve loaded settings instance."""
    return ForgeSettings()
