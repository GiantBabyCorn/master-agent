from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class ProjectStatus(enum.Enum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    ARCHIVED = "ARCHIVED"


class AgentStatus(enum.Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    ERROR = "ERROR"
    DISABLED = "DISABLED"


class MessageSource(enum.Enum):
    TELEGRAM = "TELEGRAM"
    DASHBOARD = "DASHBOARD"
    SYSTEM = "SYSTEM"
    AGENT = "AGENT"


class MessageDirection(enum.Enum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"


class LogLevel(enum.Enum):
    TRACE = "TRACE"
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"
    FATAL = "FATAL"


class TaskStatus(enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class ApprovalStatus(enum.Enum):
    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class RiskLevel(enum.Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ProviderKind(enum.Enum):
    CURSOR_CLOUD = "CURSOR_CLOUD"
    CURSOR_CLI = "CURSOR_CLI"
    ANTHROPIC = "ANTHROPIC"
    CODEX = "CODEX"


class OrchestrationMode(enum.Enum):
    RULES = "RULES"
    AGENTIC = "AGENTIC"


class SyncJobStatus(enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class SyncDiffType(enum.Enum):
    ADDED = "ADDED"
    UPDATED = "UPDATED"
    ARCHIVED = "ARCHIVED"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=datetime.utcnow, onupdate=datetime.utcnow
    )


class Project(Base, TimestampMixin):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    repo_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    status: Mapped[ProjectStatus] = mapped_column(Enum(ProjectStatus), default=ProjectStatus.ACTIVE, index=True)
    metadata_json: Mapped[Optional[dict]] = mapped_column("metadata", JSON, nullable=True)

    agents: Mapped[list["Agent"]] = relationship(back_populates="project")
    messages: Mapped[list["Message"]] = relationship(back_populates="project")
    change_logs: Mapped[list["ChangeLog"]] = relationship(back_populates="project")


class Agent(Base, TimestampMixin):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    role: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    status: Mapped[AgentStatus] = mapped_column(Enum(AgentStatus), default=AgentStatus.IDLE, index=True)
    command: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=False), nullable=True)
    project_id: Mapped[Optional[str]] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), nullable=True)

    project: Mapped[Optional[Project]] = relationship(back_populates="agents")
    messages: Mapped[list["Message"]] = relationship(back_populates="agent")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    source: Mapped[MessageSource] = mapped_column(Enum(MessageSource))
    direction: Mapped[MessageDirection] = mapped_column(Enum(MessageDirection))
    text: Mapped[str] = mapped_column(Text)
    chat_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    external_user_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    metadata_json: Mapped[Optional[dict]] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow, index=True)
    project_id: Mapped[Optional[str]] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), nullable=True)
    agent_id: Mapped[Optional[str]] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"), nullable=True)

    project: Mapped[Optional[Project]] = relationship(back_populates="messages")
    agent: Mapped[Optional[Agent]] = relationship(back_populates="messages")


class AgentTask(Base, TimestampMixin):
    __tablename__ = "agent_tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    provider: Mapped[ProviderKind] = mapped_column(Enum(ProviderKind), index=True)
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus), default=TaskStatus.PENDING, index=True)
    approval_status: Mapped[ApprovalStatus] = mapped_column(
        Enum(ApprovalStatus), default=ApprovalStatus.NOT_REQUIRED, index=True
    )
    risk_level: Mapped[RiskLevel] = mapped_column(Enum(RiskLevel), default=RiskLevel.LOW, index=True)
    prompt: Mapped[str] = mapped_column(Text)
    result_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    requested_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, unique=True)
    project_id: Mapped[Optional[str]] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), nullable=True)
    agent_id: Mapped[Optional[str]] = mapped_column(ForeignKey("provider_agents.id", ondelete="SET NULL"), nullable=True)


class TaskStep(Base):
    __tablename__ = "task_steps"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_tasks.id", ondelete="CASCADE"), index=True)
    step_index: Mapped[int] = mapped_column(Integer)
    step_type: Mapped[str] = mapped_column(String(100))
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus), default=TaskStatus.PENDING)
    input_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    output_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_tasks.id", ondelete="CASCADE"), index=True)
    status: Mapped[ApprovalStatus] = mapped_column(Enum(ApprovalStatus), default=ApprovalStatus.PENDING, index=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    suggested_safe_alternative: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=False), nullable=True)
    decided_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class ProviderRun(Base):
    __tablename__ = "provider_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_tasks.id", ondelete="CASCADE"), index=True)
    provider: Mapped[ProviderKind] = mapped_column(Enum(ProviderKind), index=True)
    external_run_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus), default=TaskStatus.PENDING)
    request_payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    response_payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=False), nullable=True)


class ChannelSession(Base):
    __tablename__ = "channel_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    channel: Mapped[str] = mapped_column(String(100), index=True)
    external_chat_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    external_user_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    metadata_json: Mapped[Optional[dict]] = mapped_column("metadata", JSON, nullable=True)


class PolicyDecision(Base):
    __tablename__ = "policy_decisions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_tasks.id", ondelete="CASCADE"), index=True)
    policy_name: Mapped[str] = mapped_column(String(100))
    risk_level: Mapped[RiskLevel] = mapped_column(Enum(RiskLevel), index=True)
    allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow, index=True)


class EventRecord(Base):
    __tablename__ = "event_records"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    aggregate_type: Mapped[str] = mapped_column(String(100), index=True)
    aggregate_id: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow, index=True)


class ProviderAgent(Base, TimestampMixin):
    __tablename__ = "provider_agents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    provider: Mapped[ProviderKind] = mapped_column(Enum(ProviderKind), index=True)
    external_agent_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus), default=TaskStatus.PENDING, index=True)
    mode_default: Mapped[OrchestrationMode] = mapped_column(
        Enum(OrchestrationMode), default=OrchestrationMode.RULES, index=True
    )
    project_id: Mapped[Optional[str]] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), nullable=True)
    metadata_json: Mapped[Optional[dict]] = mapped_column("metadata", JSON, nullable=True)


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    provider_agent_id: Mapped[str] = mapped_column(ForeignKey("provider_agents.id", ondelete="CASCADE"), index=True)
    provider_run_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    mode: Mapped[OrchestrationMode] = mapped_column(Enum(OrchestrationMode), default=OrchestrationMode.RULES, index=True)
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus), default=TaskStatus.PENDING, index=True)
    risk_level: Mapped[RiskLevel] = mapped_column(Enum(RiskLevel), default=RiskLevel.LOW, index=True)
    requested_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    output: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow, index=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=False), nullable=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=False), nullable=True)


class AgentMessage(Base):
    __tablename__ = "agent_messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    provider_agent_id: Mapped[str] = mapped_column(ForeignKey("provider_agents.id", ondelete="CASCADE"), index=True)
    agent_run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    role: Mapped[str] = mapped_column(String(32), index=True)
    content: Mapped[str] = mapped_column(Text)
    external_message_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow, index=True)


class AgentLog(Base):
    __tablename__ = "agent_logs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    provider_agent_id: Mapped[str] = mapped_column(ForeignKey("provider_agents.id", ondelete="CASCADE"), index=True)
    agent_run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    level: Mapped[LogLevel] = mapped_column(Enum(LogLevel), index=True)
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow, index=True)


class AgentFileChange(Base):
    __tablename__ = "agent_file_changes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    provider_agent_id: Mapped[str] = mapped_column(ForeignKey("provider_agents.id", ondelete="CASCADE"), index=True)
    agent_run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    path: Mapped[str] = mapped_column(String(1024))
    change_type: Mapped[str] = mapped_column(String(32), index=True)
    before_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    after_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow, index=True)


class AgentArtifact(Base):
    __tablename__ = "agent_artifacts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    provider_agent_id: Mapped[str] = mapped_column(ForeignKey("provider_agents.id", ondelete="CASCADE"), index=True)
    agent_run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    artifact_type: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(255))
    uri: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    metadata_json: Mapped[Optional[dict]] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow, index=True)


class SyncJob(Base):
    __tablename__ = "sync_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    provider: Mapped[ProviderKind] = mapped_column(Enum(ProviderKind), index=True)
    status: Mapped[SyncJobStatus] = mapped_column(Enum(SyncJobStatus), default=SyncJobStatus.PENDING, index=True)
    triggered_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    summary: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow, index=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=False), nullable=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=False), nullable=True)


class SyncDiff(Base):
    __tablename__ = "sync_diffs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    sync_job_id: Mapped[str] = mapped_column(ForeignKey("sync_jobs.id", ondelete="CASCADE"), index=True)
    diff_type: Mapped[SyncDiffType] = mapped_column(Enum(SyncDiffType), index=True)
    entity_type: Mapped[str] = mapped_column(String(64), index=True)
    entity_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    details: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow, index=True)


class ChangeLog(Base):
    __tablename__ = "change_logs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(255), index=True)
    entity_id: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(255))
    actor: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    before: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    after: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    metadata_json: Mapped[Optional[dict]] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow, index=True)
    project_id: Mapped[Optional[str]] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), nullable=True)

    project: Mapped[Optional[Project]] = relationship(back_populates="change_logs")


class LogEvent(Base):
    __tablename__ = "log_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    level: Mapped[LogLevel] = mapped_column(Enum(LogLevel), index=True)
    context: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow, index=True)
