# Draft: Agent-Centric API and Database Plan

## Scope

This draft defines:

- API surfaces for multi-provider, multi-agent control
- Database entities for lifecycle, history, and synchronization
- Pagination and sync behavior requirements

## API Draft (v1)

## Agents

- `POST /api/v1/agents`
  - create/enable a new provider agent
- `GET /api/v1/agents`
  - filter: provider, status, mode, projectId
  - pagination required
- `GET /api/v1/agents/{agentId}`
- `POST /api/v1/agents/{agentId}/start`
- `POST /api/v1/agents/{agentId}/stop`
- `POST /api/v1/agents/{agentId}/followup`

## Agent History

- `GET /api/v1/agents/{agentId}/messages`
- `GET /api/v1/agents/{agentId}/logs`
- `GET /api/v1/agents/{agentId}/file-changes`
- `GET /api/v1/agents/{agentId}/artifacts`

All history endpoints require cursor pagination:

- `cursor`
- `limit`
- `nextCursor`

## Tasks and Approvals

- `POST /api/v1/tasks`
  - supports `mode=rules|agentic`
- `GET /api/v1/tasks`
- `GET /api/v1/approvals`
- `POST /api/v1/approvals/{approvalId}/approve`
- `POST /api/v1/approvals/{approvalId}/reject`

## Provider Operations

- `GET /api/v1/providers/capabilities`
- `GET /api/v1/providers/health`
- `POST /api/v1/providers/{provider}/sync`
- `GET /api/v1/providers/{provider}/sync-jobs`
- `GET /api/v1/providers/{provider}/sync-jobs/{jobId}`

## Database Draft

## New Core Tables

- `providers`
- `provider_agents`
- `agent_runs`
- `agent_sessions`
- `agent_messages`
- `agent_logs`
- `agent_file_changes`
- `agent_artifacts`
- `sync_jobs`
- `sync_diffs`

## Existing Tables to Reuse

- `policy_decisions`
- `approvals`
- `event_records`
- `log_events`
- `change_logs`

## Key Fields (recommended)

## provider_agents

- `id`
- `provider`
- `external_agent_id`
- `name`
- `status`
- `mode_default`
- `project_id`
- `metadata`
- `created_at`
- `updated_at`

## agent_runs

- `id`
- `agent_id`
- `provider_run_id`
- `mode`
- `status`
- `risk_level`
- `requested_by`
- `started_at`
- `ended_at`

## agent_messages

- `id`
- `agent_id`
- `run_id`
- `role` (user/assistant/system/tool)
- `content`
- `external_message_id`
- `created_at`

## agent_file_changes

- `id`
- `agent_id`
- `run_id`
- `path`
- `change_type` (add/update/delete/rename)
- `before_hash`
- `after_hash`
- `summary`
- `created_at`

## sync_jobs

- `id`
- `provider`
- `status`
- `triggered_by`
- `started_at`
- `ended_at`
- `summary`

## sync_diffs

- `id`
- `sync_job_id`
- `diff_type` (added/updated/archived)
- `entity_type`
- `entity_id`
- `details`

## Sync Behavior

`POST /sync` should:

1. Fetch current provider-side agents.
2. Compare against DB state.
3. Insert missing agents.
4. Update changed records.
5. Mark missing provider-side agents as archived.
6. Persist job summary and detailed diff rows.

This operation must be user-triggerable and auditable.

## Startup Guarantees

On app startup:

- verify DB connectivity
- verify required tables
- create missing tables in bootstrap mode
- fail fast when migration gate is enabled and schema baseline is broken

## Telegram Mapping (current and future)

Suggested command set:

- `/agent create ...`
- `/agent list ...`
- `/agent start ...`
- `/agent stop ...`
- `/agent followup ...`
- `/agent history ...`
- `/sync <provider>`
- `/mode <rules|agentic>`
- `/approvals`

## Non-Goals (for this phase)

- automatic cross-provider fallback
- autonomous execution without policy enforcement
- provider-specific ad hoc endpoints without contract alignment
