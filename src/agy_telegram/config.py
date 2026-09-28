"""
Configuration management for agy-telegram using pydantic and toml/env support.
Fully decoupled and platform-agnostic.
"""

import logging
import os
from pathlib import Path
from typing import List, Optional, Any

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore

from pydantic import BaseModel, Field

logger = logging.getLogger("agy_telegram.config")


def get_antigravity_home() -> Path:
    """Returns the base data directory for Antigravity, respecting ANTIGRAVITY_HOME and GEMINI_CLI_HOME."""
    env_home = os.getenv("ANTIGRAVITY_HOME") or os.getenv("GEMINI_CLI_HOME")
    if env_home:
        return Path(env_home).expanduser()
    return Path.home() / ".gemini/antigravity-cli"


def _parse_allowed_users(raw: Any) -> List[int]:
    """Parses a single int, list of ints, or comma-separated string of user IDs."""
    if not raw:
        return []
    if isinstance(raw, list):
        return [int(x) for x in raw if str(x).strip().isdigit()]
    return [int(u.strip()) for u in str(raw).split(",") if u.strip().isdigit()]


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
    socket_path: str = "/run/agy-telegram/sentinel.sock" if Path("/run/agy-telegram").is_dir() else "/tmp/agy-sentinel.sock"


class MirrorConfig(BaseModel):
    enabled: bool = True
    mode: str = "tmux"  # "tmux" (live bidirectional bridge) or "log"
    target_session: str = "main:0.0"
    log_file: str = "/tmp/agy-telegram-chat.log"
    check_interval_seconds: float = 1.0


class ApprovalConfig(BaseModel):
    timeout_seconds: int = 180
    fallback_action: str = "reject"  # "reject" | "abort"
    auto_approve_patterns: List[str] = Field(default_factory=lambda: [
        r"^git\s+(status|diff|log|branch|show)",
        r"^(cat|head|tail|grep|find|ls|pwd|which|echo)\b",
        r"^pytest(\s+.*)?$",
        r"^npm\s+test(\s+.*)?$"
    ])
    hard_deny_patterns: List[str] = Field(default_factory=lambda: [
        r"^rm\s+(-rf|-fr|--recursive)\s+/",
        r":\(\)\{.*\}\;:",
        r">\s*/dev/sd[a-z]"
    ])


class MediaConfig(BaseModel):
    upload_dir: str = ".agy/incoming"
    max_image_size_mb: int = 10


class AppConfig(BaseModel):
    telegram: TelegramConfig
    agent: AgentConfig = Field(default_factory=AgentConfig)
    sentinel: SentinelConfig = Field(default_factory=SentinelConfig)
    mirror: MirrorConfig = Field(default_factory=MirrorConfig)
    approval: ApprovalConfig = Field(default_factory=ApprovalConfig)
    media: MediaConfig = Field(default_factory=MediaConfig)



def load_config(config_path: Optional[str] = None) -> AppConfig:
    """Loads configuration from standard paths, .env files, or environment variables."""
    # 1. Explicit CLI argument: must exist, no fallback
    if config_path:
        p = Path(config_path)
        if not p.is_file():
            raise FileNotFoundError(f"Configuration file not found: {config_path}")
        with open(p, "rb") as f:
            data = tomllib.load(f)
            config = AppConfig(**data)
            _validate_loaded_config(config)
            return config

    # 2. Environment variable override: must exist, no fallback
    env_config_override = os.getenv("AGY_TELEGRAM_CONFIG")
    if env_config_override:
        p = Path(env_config_override)
        if not p.is_file():
            raise FileNotFoundError(
                f"Configuration file specified by AGY_TELEGRAM_CONFIG not found: {env_config_override}"
            )
        with open(p, "rb") as f:
            data = tomllib.load(f)
            config = AppConfig(**data)
            _validate_loaded_config(config)
            return config

    # 3. Standard candidate paths
    candidate_paths = [
        Path("/etc/agy-telegram/config.toml"),
        Path.home() / ".config/agy-telegram/config.toml",
        Path.home() / ".agy-telegram.toml",
        Path.cwd() / "config.toml",
        Path.cwd() / ".config.toml",
    ]

    for p in candidate_paths:
        if p.is_file():
            with open(p, "rb") as f:
                data = tomllib.load(f)
                config = AppConfig(**data)
                _validate_loaded_config(config)
                return config

    # 4. Environment variables & .env candidates
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    allowed_str = os.getenv("TELEGRAM_ALLOWED_USER_ID", "")
    allowed_users = _parse_allowed_users(allowed_str)

    if not bot_token:
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
                if raw_id:
                    allowed_users = _parse_allowed_users(raw_id)
                if bot_token:
                    break

    if not bot_token:
        raise ValueError(
            "No Telegram bot token found. Set TELEGRAM_BOT_TOKEN or provide config.toml."
        )

    target_session = os.getenv("AGY_TMUX_SESSION", "main:0.0")

    config = AppConfig(
        telegram=TelegramConfig(bot_token=bot_token, allowed_users=allowed_users),
        agent=AgentConfig(),
        sentinel=SentinelConfig(),
        mirror=MirrorConfig(mode="tmux", target_session=target_session),
    )
    _validate_loaded_config(config)
    return config


def _validate_loaded_config(config: AppConfig) -> None:
    """Validates security constraints on the loaded configuration."""
    if not config.telegram.allowed_users:
        logger.warning(
            "BLOCKING SECURITY WARNING: 'allowed_users' is empty! No users will be authorized to access the bot."
        )
        raise ValueError(
            "Security validation failed: 'allowed_users' must contain at least one valid Telegram user ID."
        )
