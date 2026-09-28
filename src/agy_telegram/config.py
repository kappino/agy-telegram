"""
Configuration management for agy-telegram using pydantic and toml/env support.
Fully decoupled and platform-agnostic.
"""

import os
from pathlib import Path
from typing import List, Optional
import tomllib
from pydantic import BaseModel, Field


class TelegramConfig(BaseModel):
    bot_token: str
    allowed_users: List[int] = Field(default_factory=list)


class AgentConfig(BaseModel):
    executable: str = "agy"
    default_workspace: str = "."
    default_model: str = "gemini-3.8-flash"
    default_effort: str = "high"
    timeout_seconds: int = 600
    skip_permissions: bool = False
    status_command: Optional[str] = None


class SentinelConfig(BaseModel):
    enabled: bool = True
    socket_path: str = "/tmp/agy-sentinel.sock"


class MirrorConfig(BaseModel):
    enabled: bool = True
    mode: str = "tmux"  # "tmux" (live bidirectional bridge) or "log"
    target_session: str = "main:0.0"
    log_file: str = "/tmp/agy-telegram-chat.log"
    check_interval_seconds: float = 1.0


class AppConfig(BaseModel):
    telegram: TelegramConfig
    agent: AgentConfig = Field(default_factory=AgentConfig)
    sentinel: SentinelConfig = Field(default_factory=SentinelConfig)
    mirror: MirrorConfig = Field(default_factory=MirrorConfig)


def load_config(config_path: Optional[str] = None) -> AppConfig:
    """Loads configuration from standard paths, .env files, or environment variables."""
    candidate_paths = []
    if config_path:
        candidate_paths.append(Path(config_path))

    env_config_override = os.getenv("AGY_TELEGRAM_CONFIG")
    if env_config_override:
        candidate_paths.append(Path(env_config_override))

    candidate_paths.extend([
        Path("/etc/agy-telegram/config.toml"),
        Path.home() / ".config/agy-telegram/config.toml",
        Path.home() / ".agy-telegram.toml",
        Path.cwd() / "config.toml",
        Path.cwd() / ".config.toml",
    ])

    for p in candidate_paths:
        if p.is_file():
            with open(p, "rb") as f:
                data = tomllib.load(f)
                return AppConfig(**data)

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    allowed_str = os.getenv("TELEGRAM_ALLOWED_USER_ID", "")
    allowed_users = []
    if allowed_str:
        allowed_users = [int(u.strip()) for u in allowed_str.split(",") if u.strip().isdigit()]

    if not bot_token:
        # Standard .env candidate locations
        env_candidates = [
            Path.cwd() / ".env",
            Path.cwd() / ".env.telegram",
            Path.home() / ".config/agy-telegram/.env",
            Path.home() / ".env.telegram",
            Path.home() / ".agy-telegram.env",
            Path("/etc/agy-telegram/.env"),
        ]
        for env_file in env_candidates:
            if env_file.is_file():
                from dotenv import dotenv_values
                env_vals = dotenv_values(env_file)
                bot_token = env_vals.get("TELEGRAM_BOT_TOKEN", "")
                raw_id = env_vals.get("TELEGRAM_ALLOWED_USER_ID", "")
                if raw_id and raw_id.isdigit():
                    allowed_users = [int(raw_id)]
                if bot_token:
                    break

    if not bot_token:
        raise ValueError(
            "No Telegram bot token found. Set TELEGRAM_BOT_TOKEN or provide config.toml."
        )

    target_session = os.getenv("AGY_TMUX_SESSION", "main:0.0")

    return AppConfig(
        telegram=TelegramConfig(bot_token=bot_token, allowed_users=allowed_users),
        agent=AgentConfig(),
        sentinel=SentinelConfig(),
        mirror=MirrorConfig(mode="tmux", target_session=target_session),
    )
