# Architecture Draft: Dual Orchestration Modes

## Purpose

Define a clear architecture that supports both:

- `rules` mode (deterministic orchestrator + policy engine)
- `agentic` mode (master agent + sub-agents)

Both modes must preserve auditable behavior and consistent operational controls.

## Core Design Principles

1. One control plane, two orchestration strategies.
2. Shared policy and approval pipeline across both modes.
3. Provider-agnostic execution model with capability discovery.
4. Event-first persistence for traceability and replayability.
5. Agent lifecycle and history are first-class entities.

## Mode Model

## Rules Mode

Flow:

`request -> policy_check -> approval_gate (optional) -> provider_execution -> persist`

Characteristics:

- deterministic
- easy to audit
- low operational ambiguity

## Agentic Mode

Flow:

`request -> master_planner -> sub_agent_tasks -> policy_gate_per_step -> aggregate -> persist`

Characteristics:

- high flexibility
- better for multi-step decomposition
- requires stronger guardrails and observability

## Shared Components

- `PolicyEngine`
- `ApprovalService`
- `ProviderRouter`
- `EventStore`
- `AuditLogService`
- `CapabilityRegistry`

## Runtime Configuration

Global mode:

- `ORCHESTRATION_MODE=rules|agentic`

Per-task override:

- request payload may include `mode` to override global default

## Suggested Component Boundaries

- `app/orchestrator/rules_orchestrator.py`
- `app/orchestrator/agentic_orchestrator.py`
- `app/orchestrator/master_planner.py`
- `app/policy/engine.py`
- `app/policy/approval_service.py`
- `app/providers/registry.py`
- `app/persistence/event_store.py`

## Provider Interaction Contract

All providers should support a common baseline:

- `create_agent`
- `list_agents`
- `start_agent`
- `stop_agent`
- `followup_agent`
- `get_agent_history`
- `sync_agents`

If a provider lacks a method, expose this through capability metadata.

## Security and Governance

- No implicit provider fallback.
- Every action has a policy decision record.
- Risk classification applies in both modes.
- High-risk actions always require approval.
- All externally visible side effects are persisted as immutable events.

## Observability Requirements

- Correlation ID on every request and task step.
- Structured logs for orchestration transitions.
- Provider status telemetry with explicit unavailable reason.
- Task timeline reconstruction from event records.

## Rollout Strategy

1. Keep `rules` mode as default.
2. Add `agentic` mode behind explicit feature toggle.
3. Enable agentic mode for selected projects first.
4. Compare outcomes and adjust policy thresholds.

## Success Criteria

- Both modes run under the same API contract.
- Approval decisions remain consistent regardless of mode.
- Providers can be added without changing orchestration core.
- History, logs, and file changes are queryable with pagination.
