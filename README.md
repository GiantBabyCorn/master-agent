# Master Agent (Cursor CLI Orchestrator)

This project is a Python backend for managing multi-provider coding agents from one place, with policy-based safety and approval workflow.

## What is included now

- FastAPI server with modular architecture:
  - `channels/` for integration entry points
  - `providers/` for agent backends
  - `orchestrator/` for master task flow
  - `policy/` for risk and approvals
  - `persistence/` for event records
- Telegram integration with configurable mode:
  - webhook
  - polling
  - disabled
- Provider adapters:
  - Cursor Cloud API
  - Cursor CLI
  - Anthropic CLI (SDK-ready architecture)
  - Codex CLI (API-ready feature-flag architecture)
- Provider preflight verification:
  - startup checks provider availability
  - unavailable providers include explicit reason in logs
  - client-side health endpoint marks `enabled/status/reason`
- Master orchestrator with:
  - risk classification (low, medium, high)
  - balanced default policy
  - approval queue for risky actions
  - event records and provider run records
- SQLAlchemy data model for projects, agents, messages, change logs, logs, tasks, task steps, approvals, provider runs, channel sessions, policy decisions, and immutable event records
- `.agent` document pipeline with fixed naming format:
  - `DOC_NAME.YYYYMMDD-hhmmss.md`
- API surfaces for:
  - health/readiness
  - projects and agents
  - provider capabilities and provider health
  - task timeline
  - approval inbox and approve/reject actions
  - Telegram webhook management
- Docker Compose stack with persistent Postgres volume and backup script

## Quick start (local Python)

Detailed guide:

- `.doc/SETUP_GUIDE.md`

1. Create and activate virtual environment
2. Install dependencies:
   - `pip install -e .`
3. Copy env file:
   - `cp .env.example .env`
4. Update `.env` values, especially:
   - `POSTGRES_USER`
   - `POSTGRES_PASSWORD`
   - `POSTGRES_DB`
   - `DATABASE_URL`
     - Note: in Docker Compose mode, `master_agent_api` service overrides this value to use `master_agent_db` service host.
   - `COMPOSE_DATABASE_URL`
   - `TELEGRAM_MODE`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_WEBHOOK_SECRET`
   - `TELEGRAM_WEBHOOK_URL`
5. Start the API:
   - `uvicorn app.main:app --reload --host 0.0.0.0 --port 3000`

## Quick start (Docker Compose)

Detailed guide:

- `.doc/SETUP_GUIDE.md`

1. Copy env:
   - `cp .env.example .env`
2. Start stack:
   - `docker compose up --build`
3. Data persistence:
   - Postgres data is in named volume `postgres_data`
4. DB port exposure:
   - Default policy: Postgres is internal-only and has no host port mapping.
   - Only API container can access DB through compose network.
   - If emergency debugging needs direct DB access, temporarily add port mapping locally and remove it after debugging.
5. Backup:
   - `bash scripts/backup_db.sh`

Important:

- Avoid `docker compose down -v` unless you intentionally want to delete all DB data.

## Telegram modes

- `TELEGRAM_MODE=webhook`
  - Use `POST /api/telegram/set-webhook`
- `TELEGRAM_MODE=polling`
  - API starts polling worker automatically
- `TELEGRAM_MODE=disabled`
  - Telegram routes remain available but no automatic transport worker starts

## Provider availability behavior

- No fallback order is used.
- Each request runs on the single provider you specify.
- If provider is unavailable (not configured or verify failed), request returns failure with reason.
- Check provider status from:
  - `GET /api/v1/providers/health`
- Orchestration modes:
  - `GET /api/v1/orchestration/modes`
  - set `ORCHESTRATION_MODE=rules|agentic` in env

## Agent-centric endpoints (MVP)

- `POST /api/v1/agents`
- `GET /api/v1/agents`
- `GET /api/v1/agents/{agentId}`
- `POST /api/v1/agents/{agentId}/start`
- `POST /api/v1/agents/{agentId}/stop`
- `POST /api/v1/agents/{agentId}/followup`
- `GET /api/v1/agents/{agentId}/messages?cursor=&limit=`
- `GET /api/v1/agents/{agentId}/logs?cursor=&limit=`
- `GET /api/v1/agents/{agentId}/file-changes?cursor=&limit=`
- `GET /api/v1/agents/{agentId}/artifacts?cursor=&limit=`
- `POST /api/v1/providers/{provider}/sync`
- `GET /api/v1/providers/{provider}/sync-jobs`
- `GET /api/v1/providers/{provider}/sync-jobs/{jobId}`

## API contract artifacts

- OpenAPI file:
  - `docs/api/openapi.yaml`
- Agent-centric draft:
  - `docs/api/openapi-agent-centric-draft.yaml`
- Example payloads:
  - `docs/api/examples/tasks-list.json`
  - `docs/api/examples/approvals-list.json`

## Document generation

Use:

- `python -m app.cli.new_doc ARCHITECTURE_NOTE`

Output file example:

- `.agent/ARCHITECTURE_NOTE.20260222-113000.md`

## Operations docs

- Reliability:
  - `docs/operations/reliability.md`
- Secret rotation:
  - `docs/operations/secret-rotation.md`
- CLI version check helper:
  - `bash scripts/check_cli_versions.sh`
