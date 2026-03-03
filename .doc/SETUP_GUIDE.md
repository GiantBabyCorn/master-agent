# Master Agent Detailed Setup Guide

This guide provides a complete setup flow for local development and Docker deployment.

## 1) Prerequisites

- Python `3.11+`
- `pip`
- Docker Desktop (for Compose mode)
- PostgreSQL client tools (optional, for manual DB checks)
- Telegram account (if using Telegram integration)
- Optional CLIs:
  - Cursor CLI (`agent` command)
  - Anthropic Claude CLI (`claude` command)
  - OpenAI Codex CLI (`codex` command)

## 2) Configure environment variables

1. Copy sample file:
   - `cp .env.example .env`
2. Open `.env` and fill required values.

### Minimum required values

- `DATABASE_URL`
- `POSTGRES_USER`
- `POSTGRES_PASSWORD`
- `POSTGRES_DB`
- `COMPOSE_DATABASE_URL`
- `TELEGRAM_MODE` (`webhook` / `polling` / `disabled`)
- `ORCHESTRATION_MODE` (`rules` / `agentic`)

### Required for Telegram webhook mode

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_WEBHOOK_SECRET`
- `TELEGRAM_WEBHOOK_URL`

### Required for provider integrations

- Cursor Cloud:
  - `CURSOR_CLOUD_API_KEY`
- Anthropic:
  - `ANTHROPIC_API_KEY` (or valid local auth method)
- Codex:
  - `CODEX_API_KEY` only if API path is enabled in future (`CODEX_ENABLE_API=true`)

Use `.env.example` comments as the source of truth for key format and how to obtain each credential.

## 3) Local Python setup

1. Create virtual environment:
   - Windows PowerShell:
     - `python -m venv .venv`
     - `.\\.venv\\Scripts\\Activate.ps1`
   - Bash:
     - `python -m venv .venv`
     - `source .venv/bin/activate`
2. Install dependencies:
   - `pip install -e .`
3. Start API:
   - `uvicorn app.main:app --reload --host 0.0.0.0 --port 3000`
4. Verify:
   - `GET http://localhost:3000/healthz`
   - `GET http://localhost:3000/readyz`

## 4) Docker Compose setup (Postgres + API)

1. Copy env:
   - `cp .env.example .env`
2. Start stack:
   - `docker compose up --build`
3. Verify:
   - `GET http://localhost:3000/healthz`
   - `GET http://localhost:3000/readyz`
4. DB access policy:
   - Postgres is internal-only by default (no host port exposure).
   - DB interactions must go through application APIs.
   - For emergency debugging only, temporarily expose DB port in local compose override and remove it immediately after use.

### Development workflow — rebuild vs restart vs nothing

The API container bind-mounts the repo (`./:/app`) and runs uvicorn with `--reload`, so most changes are picked up automatically:

| Change | Action needed |
|---|---|
| Python source file (`.py`) | **Nothing** — uvicorn reloads automatically |
| `.env` file | `docker compose restart master_agent_api` |
| `requirements.txt` / `pyproject.toml` (new package) | `docker compose up --build` |
| `Dockerfile` changed | `docker compose up --build` |
| Build args (`DOCKER_INSTALL_NODEJS`, CLI install commands) | `docker compose up --build` |
| DB schema change (new migration) | `docker compose restart master_agent_api` (auto-migration runs on startup) |

### Data safety notes

- Postgres data is stored in named volume `postgres_data`.
- Do not run `docker compose down -v` unless you intentionally want to remove all DB data.
- Create regular backups:
  - `bash scripts/backup_db.sh`
- Do not keep weak default DB password.
  - Update `POSTGRES_PASSWORD` in `.env` before sharing environment with others.

## 5) Telegram setup

## A. Create bot token

1. In Telegram, open `@BotFather`.
2. Run `/newbot`.
3. Save the returned token and put it into `TELEGRAM_BOT_TOKEN`.

## B. Webhook mode (recommended for production)

1. Set:
   - `TELEGRAM_MODE=webhook`
   - `TELEGRAM_WEBHOOK_URL=https://<public-domain>`
   - `TELEGRAM_WEBHOOK_SECRET=<random-secret>`
2. Start API.
3. Register webhook:
   - `POST /api/telegram/set-webhook`
   - body example: `{ "dropPendingUpdates": true }`

## C. Polling mode (useful for local/dev)

1. Set:
   - `TELEGRAM_MODE=polling`
2. Start API.
3. Polling worker starts automatically.

## 6) Provider verification checklist

Provider behavior in this project:

- No fallback order is used.
- Each task uses only the provider you explicitly request.
- If provider is unavailable, task is rejected with a clear reason.
- Startup logs provider verification results to terminal logs.
- Client side can read provider availability from:
  - `GET /api/v1/providers/health`
- Orchestration mode status:
  - `GET /api/v1/orchestration/modes`

### A. Cursor Cloud

- Required:
  - `CURSOR_CLOUD_API_KEY`
- Verification:
  - API preflight checks `/v0/me`
- Common unavailable reasons:
  - key missing
  - API auth failed
  - network/connectivity issue

### B. Cursor CLI

- Required:
  - `CURSOR_CLI_COMMAND` available in container PATH
- Verification:
  - command existence check + version probe
- Common unavailable reasons:
  - CLI command not installed
  - CLI exists but not authenticated

### C. Anthropic CLI

- Required:
  - `ANTHROPIC_CLI_COMMAND` available in container PATH
- Optional:
  - `ANTHROPIC_API_KEY`
- Verification:
  - command existence check + version probe

### D. Codex CLI

- Required:
  - `CODEX_CLI_COMMAND` available in container PATH
- Verification:
  - command existence check + version probe

## 7) Container CLI installation and auth flow

### A. Auto-install CLIs during Docker build

This project supports optional build-time install commands in `.env`:

- `DOCKER_INSTALL_NODEJS=true`
- `DOCKER_CURSOR_CLI_INSTALL_CMD="curl https://cursor.com/install -fsS | bash"`
- `DOCKER_ANTHROPIC_CLI_INSTALL_CMD="curl -fsSL https://claude.ai/install.sh | bash -s stable"`
- `DOCKER_CODEX_CLI_INSTALL_CMD="npm install -g @openai/codex"`

Then rebuild:

- `docker compose build --no-cache api`
- `docker compose up`

Note:

- `pyproject.toml` installs Python dependencies only.
- External CLIs are not installed by `pip install -e .` unless you explicitly add install commands.

Version update sources:

- Cursor CLI:
  - docs: `https://cursor.com/docs/cli/overview`
  - changelog: `https://cursor.com/changelog`
- Claude CLI:
  - setup docs: `https://docs.anthropic.com/en/docs/claude-code/setup`
  - update command: `claude update`
- Codex CLI:
  - docs: `https://developers.openai.com/codex/cli/`
  - releases: `https://github.com/openai/codex/releases`
  - npm latest version: `npm view @openai/codex version`

### B. Authenticate inside container (if CLI requires login)

1. Open shell in API container:
   - `docker compose exec master_agent_api bash`
2. Run each CLI login flow (provider-specific).
3. Confirm command works:
   - `agent --version`
   - `claude --version`
   - `codex --version`

Check if container version is out of date:

1. Check installed versions:
   - `docker compose exec master_agent_api bash -lc "agent --version"`
   - `docker compose exec master_agent_api bash -lc "claude --version"`
   - `docker compose exec master_agent_api bash -lc "codex --version"`
   - or run one command:
     - `bash scripts/check_cli_versions.sh`
2. Compare against latest docs/release pages above.
3. Rebuild image after updating install commands:
   - `docker compose build --no-cache api`
   - `docker compose up -d`

Recommended for repeatable deployment:

- Prefer environment-variable based auth where available.
- Store keys in `.env` and inject via Docker Compose instead of manual interactive login.

## 8) Core API smoke tests

- `GET /healthz`
- `GET /readyz`
- `GET /api/projects`
- `POST /api/projects`
- `GET /api/v1/providers/capabilities`
- `GET /api/v1/providers/health`
- `GET /api/v1/orchestration/modes`
- `GET /api/v1/tasks`
- `GET /api/v1/approvals`
- `POST /api/v1/agents`
- `GET /api/v1/agents`
- `POST /api/v1/providers/{provider}/sync`

## 9) Master Agent approval flow test

1. Submit a low-risk task (read-only style prompt) via `POST /api/agents/run`.
2. Submit a medium/high-risk prompt to trigger approval flow.
3. Check approval inbox:
   - `GET /api/v1/approvals`
4. Approve or reject:
   - `POST /api/v1/approvals/{approval_id}/approve`
   - `POST /api/v1/approvals/{approval_id}/reject`

## 10) Documents generated by agents

- Build-time or workflow markdown artifacts should be stored in `.agent`.
- Naming format:
  - `DOC_NAME.YYYYMMDD-hhmmss.md`
- Current doc generation command:
  - `python -m app.cli.new_doc ARCHITECTURE_NOTE`
