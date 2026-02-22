from functools import lru_cache
from typing import Set

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    node_env: str = "development"
    port: int = 3000
    database_url: str
    log_level: str = "info"
    api_prefix: str = "/api"
    api_v1_prefix: str = "/api/v1"

    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""
    telegram_webhook_url: str = ""
    telegram_allowed_user_ids: str = ""
    telegram_mode: str = "webhook"
    telegram_poll_interval_sec: int = 3
    telegram_polling_timeout_sec: int = 30

    cursor_cli_command: str = "agent"
    cursor_cli_timeout_ms: int = 120000

    cursor_cloud_api_key: str = ""
    cursor_cloud_base_url: str = "https://api.cursor.com"

    anthropic_api_key: str = ""
    anthropic_cli_command: str = "claude"

    codex_cli_command: str = "codex"
    codex_enable_api: bool = False
    codex_api_key: str = ""

    approval_policy_default: str = "balanced"
    orchestration_mode: str = "rules"
    approval_auto_approve_read_only: bool = True
    approval_medium_requires_user: bool = True

    request_timeout_sec: int = 30
    retry_max_attempts: int = 3
    retry_base_delay_ms: int = 300
    circuit_breaker_fail_threshold: int = 5
    circuit_breaker_recovery_sec: int = 30

    db_auto_create_tables: bool = True
    db_enable_startup_migration_gate: bool = True

    def allowed_telegram_user_ids(self) -> Set[str]:
        return {user_id.strip() for user_id in self.telegram_allowed_user_ids.split(",") if user_id.strip()}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
