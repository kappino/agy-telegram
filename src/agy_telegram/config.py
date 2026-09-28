"""
Configuration management for agy-telegram using pydantic and toml/env support.
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

class SentinelConfig(BaseModel):
    enabled: bool = True
    socket_path: str = "/tmp/agy-sentinel.sock"

class MirrorConfig(BaseModel):
    enabled: bool = True
    mode: str = "tmux"  # "tmux" (live bidirectional bridge) or "log"
    target_session: str = "main:0.0"
    log_file: str = "/tmp/agy-telegram-chat.log"

class AppConfig(BaseModel):
    telegram: TelegramConfig
    agent: AgentConfig = Field(default_factory=AgentConfig)
    sentinel: SentinelConfig = Field(default_factory=SentinelConfig)
    mirror: MirrorConfig = Field(default_factory=MirrorConfig)

def load_config(config_path: Optional[str] = None) -> AppConfig:
    """Load configuration from standard candidate paths or environment variables."""
    candidate_paths = []
    if config_path:
        candidate_paths.append(Path(config_path))
    
    candidate_paths.extend([
        Path("/etc/agy-telegram/config.toml"),
        Path.home() / ".config/agy-telegram/config.toml",
        Path("/opt/aegis-agent/config.toml"),
        Path.cwd() / "config.toml",
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
        legacy_env = Path("/opt/aegis-agent/.env.telegram")
        if legacy_env.is_file():
            from dotenv import dotenv_values
            env_vals = dotenv_values(legacy_env)
            bot_token = env_vals.get("TELEGRAM_BOT_TOKEN", "")
            raw_id = env_vals.get("TELEGRAM_ALLOWED_USER_ID", "")
            if raw_id and raw_id.isdigit():
                allowed_users = [int(raw_id)]

    if not bot_token:
        raise ValueError("Nessun token Telegram trovato in config.toml o variabili d'ambiente!")

    return AppConfig(
        telegram=TelegramConfig(bot_token=bot_token, allowed_users=allowed_users),
        agent=AgentConfig(),
        sentinel=SentinelConfig(),
        mirror=MirrorConfig(mode="tmux", target_session="aegis:0.0"),
    )
