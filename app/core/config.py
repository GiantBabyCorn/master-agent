from functools import lru_cache
import sys
from typing import Set

from pydantic import ValidationError
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
    # Set to true ONLY if you intentionally want any Telegram user to be able
    # to use this bot.  Ignored when telegram_allowed_user_ids is non-empty.
    telegram_allow_all_users: bool = False
    telegram_mode: str = "webhook"
    telegram_poll_interval_sec: float = 3
    telegram_polling_timeout_sec: int = 30
    telegram_auto_revoke_webhook: bool = True

    cursor_cli_command: str = "agent"
    cursor_cli_timeout_ms: int = 120000
    cursor_cli_force_approve: bool = False
    cursor_cli_login_timeout_sec: int = 300
    cursor_cli_url_capture_timeout_sec: int = 30

    cursor_cloud_api_key: str = ""
    cursor_cloud_base_url: str = "https://api.cursor.com"
    cursor_cloud_default_repo: str = ""
    cursor_cloud_default_ref: str = ""
    cursor_cloud_auto_pr: bool = True
    cursor_cloud_branch_prefix: str = "agent/"
    cursor_cloud_default_model: str = ""
    cursor_cloud_poll_interval_sec: float = 30.0

    anthropic_api_key: str = ""
    anthropic_cli_command: str = "claude"
    anthropic_api_model: str = "claude-opus-4-6"
    # claude_cli_command is an alias for anthropic_cli_command for clarity
    claude_cli_command: str = ""  # if empty, falls back to anthropic_cli_command
    claude_cli_login_timeout_sec: int = 300
    claude_cli_url_capture_timeout_sec: int = 30
    # Claude CLI OAuth overrides (mirrors what the claude binary itself supports)
    claude_code_oauth_client_id: str = ""  # overrides default client ID; empty = use built-in
    claude_auth_use_platform: bool = False  # true = use platform.claude.com/oauth/authorize instead of claude.ai
    # Login method: "auth_login" (PTY `claude auth login`) or "pkce_platform" (API PKCE via platform.claude.com)
    claude_login_method: str = "auth_login"

    codex_cli_command: str = "codex"
    codex_enable_api: bool = False
    codex_api_key: str = ""

    approval_policy_default: str = "balanced"
    orchestration_mode: str = "rules"
    master_agent_provider: str = "cursor_cli"
    startup_require_master_provider_available: bool = True
    approval_auto_approve_read_only: bool = True
    approval_medium_requires_user: bool = True

    request_timeout_sec: int = 30
    claude_cli_task_timeout_sec: int = 3600  # claude -p can run for many minutes; separate from API timeout
    retry_max_attempts: int = 3
    retry_base_delay_ms: int = 300
    circuit_breaker_fail_threshold: int = 5
    circuit_breaker_recovery_sec: int = 30

    db_auto_create_tables: bool = True
    db_enable_startup_migration_gate: bool = True

    # GitHub webhook integration
    github_webhook_secret: str = ""
    github_default_provider: str = "claude_cli"
    github_notify_chat_id: int = 0  # Telegram chat_id to notify on triggered tasks (0 = disabled)
    github_auto_run_on_push: bool = False
    github_auto_run_on_pr: bool = False
    github_mention_trigger: str = "@claude"  # keyword in issue comments that triggers a task

    # Scheduled tasks — JSON array of {name, provider, prompt, interval_sec, notify_chat_id}
    # Example: '[{"name":"daily","provider":"claude_cli","prompt":"Review code","interval_sec":86400}]'
    scheduled_tasks: str = ""

    def allowed_telegram_user_ids(self) -> Set[str]:
        return {user_id.strip() for user_id in self.telegram_allowed_user_ids.split(",") if user_id.strip()}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        settings = Settings()
    except ValidationError as exc:
        color_enabled = bool(getattr(sys.stderr, "isatty", lambda: False)())

        def color(text: str, code: str) -> str:
            if not color_enabled:
                return text
            return f"{code}{text}\033[0m"

        missing_fields = []
        invalid_fields: list[str] = []
        for err in exc.errors():
            if err.get("type") == "missing":
                location = err.get("loc", ())
                if location:
                    missing_fields.append(str(location[0]).upper())
            else:
                location = err.get("loc", ())
                if location:
                    invalid_fields.append(str(location[0]).upper())

        examples_by_field = {
            "DATABASE_URL": 'DATABASE_URL="postgresql+psycopg://<user>:<password>@<host>:<port>/<db>"',
            "TELEGRAM_MODE": "TELEGRAM_MODE=webhook",
            "ORCHESTRATION_MODE": "ORCHESTRATION_MODE=rules",
            "POSTGRES_PASSWORD": "POSTGRES_PASSWORD=<strong_random_password>",
        }

        print(color("Configuration check failed.", "\033[91m"), file=sys.stderr)
        if missing_fields:
            print(
                f"{color('Missing required environment variables:', '\033[93m')} {', '.join(sorted(set(missing_fields)))}",
                file=sys.stderr,
            )
        if invalid_fields:
            print(
                f"{color('Invalid environment variable values:', '\033[93m')} {', '.join(sorted(set(invalid_fields)))}",
                file=sys.stderr,
            )

        print(color("How to fix:", "\033[96m"), file=sys.stderr)
        print("1) Copy .env.example to .env if not already done", file=sys.stderr)
        print("2) Fill required values in .env", file=sys.stderr)
        print("3) Re-run the app", file=sys.stderr)
        suggestion_fields = sorted(set(missing_fields + invalid_fields))
        if suggestion_fields:
            print("", file=sys.stderr)
            print("Examples for missing/invalid fields:", file=sys.stderr)
            for field in suggestion_fields:
                if field in examples_by_field:
                    print(f"- {examples_by_field[field]}", file=sys.stderr)
            if "DATABASE_URL" in suggestion_fields:
                print('- Docker compose runtime example: COMPOSE_DATABASE_URL="postgresql+psycopg://<user>:<password>@master_agent_db:5432/<db>"', file=sys.stderr)

        raise SystemExit(2) from None

    validation_issues: list[tuple[str, str]] = []

    if settings.telegram_mode not in {"webhook", "polling", "disabled"}:
        validation_issues.append(("TELEGRAM_MODE", "must be one of: webhook, polling, disabled"))

    if settings.telegram_mode in {"webhook", "polling"} and not settings.telegram_bot_token.strip():
        validation_issues.append(("TELEGRAM_BOT_TOKEN", "is required when TELEGRAM_MODE is webhook or polling"))

    if settings.telegram_mode == "webhook":
        if not settings.telegram_webhook_url.strip():
            validation_issues.append(("TELEGRAM_WEBHOOK_URL", "is required when TELEGRAM_MODE=webhook"))
        if not settings.telegram_webhook_secret.strip():
            validation_issues.append(("TELEGRAM_WEBHOOK_SECRET", "is required when TELEGRAM_MODE=webhook"))

    if settings.orchestration_mode not in {"rules", "agentic"}:
        validation_issues.append(("ORCHESTRATION_MODE", "must be one of: rules, agentic"))

    if settings.master_agent_provider not in {"cursor_cli", "cursor_cloud", "anthropic", "claude_cli", "anthropic_api", "codex"}:
        validation_issues.append(
            ("MASTER_AGENT_PROVIDER", "must be one of: cursor_cli, cursor_cloud, claude_cli, anthropic_api, codex")
        )

    if settings.orchestration_mode == "agentic":
        if settings.master_agent_provider == "cursor_cloud" and not settings.cursor_cloud_api_key.strip():
            validation_issues.append(
                ("CURSOR_CLOUD_API_KEY", "is required when ORCHESTRATION_MODE=agentic and MASTER_AGENT_PROVIDER=cursor_cloud")
            )
        if settings.master_agent_provider == "cursor_cli" and not settings.cursor_cli_command.strip():
            validation_issues.append(
                ("CURSOR_CLI_COMMAND", "is required when ORCHESTRATION_MODE=agentic and MASTER_AGENT_PROVIDER=cursor_cli")
            )
        if settings.master_agent_provider in {"anthropic", "claude_cli"} and not (settings.claude_cli_command or settings.anthropic_cli_command).strip():
            validation_issues.append(
                ("ANTHROPIC_CLI_COMMAND", "is required when ORCHESTRATION_MODE=agentic and MASTER_AGENT_PROVIDER=claude_cli")
            )
        if settings.master_agent_provider == "codex" and not settings.codex_cli_command.strip():
            validation_issues.append(
                ("CODEX_CLI_COMMAND", "is required when ORCHESTRATION_MODE=agentic and MASTER_AGENT_PROVIDER=codex")
            )

    if validation_issues:
        color_enabled = bool(getattr(sys.stderr, "isatty", lambda: False)())

        def color(text: str, code: str) -> str:
            if not color_enabled:
                return text
            return f"{code}{text}\033[0m"

        print(color("Configuration check failed.", "\033[91m"), file=sys.stderr)
        print(color("Missing or invalid runtime requirements:", "\033[93m"), file=sys.stderr)
        for key, reason in validation_issues:
            print(f"- {key}: {reason}", file=sys.stderr)

        print("", file=sys.stderr)
        print(color("How to fix:", "\033[96m"), file=sys.stderr)
        print("1) Open .env", file=sys.stderr)
        print("2) Update the keys listed above", file=sys.stderr)
        print("3) Re-run the app", file=sys.stderr)
        print("", file=sys.stderr)
        print("Common examples:", file=sys.stderr)
        print("TELEGRAM_MODE=webhook", file=sys.stderr)
        print('TELEGRAM_BOT_TOKEN="123456789:AAExampleTokenValue"', file=sys.stderr)
        print('TELEGRAM_WEBHOOK_URL="https://your-public-domain.example.com"', file=sys.stderr)
        print('TELEGRAM_WEBHOOK_SECRET="<random_secret>"', file=sys.stderr)
        print("ORCHESTRATION_MODE=agentic", file=sys.stderr)
        print("MASTER_AGENT_PROVIDER=cursor_cli", file=sys.stderr)
        print('CURSOR_CLI_COMMAND="agent"', file=sys.stderr)
        print('CURSOR_CLOUD_API_KEY="key_xxx"', file=sys.stderr)

        raise SystemExit(2)

    return settings
