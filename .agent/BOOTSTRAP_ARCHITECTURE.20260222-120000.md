# Master Agent Bootstrap Architecture

## Goal
Create a backend-first foundation to manage multiple Cursor Agents through APIs, with Telegram as the first control channel and database-backed records for long-term operations.

## Implemented Foundation
- Python service with FastAPI
- Telegram webhook endpoint for command-based management
- Cursor CLI adapter for triggering agent runs
- SQLAlchemy schema for projects, agents, messages, change logs, and log events
- Utility for generating `.agent` documents using `DOC_NAME.YYYYMMDD-hhmmss.md`

## Why this architecture
- API-first design allows adding a full dashboard web app without rewriting core services
- Webhook approach enables near real-time Telegram management without polling overhead
- Database model already supports project-level and agent-level observability
- Event and message records provide operational traceability for audit and debugging

## Suggested next implementation
1. Add authentication and role-based permissions
2. Add queue workers for long-running agent tasks
3. Add per-project Telegram routing and command templates
4. Add dashboard frontend with timeline and logs
5. Add OpenTelemetry and metrics
