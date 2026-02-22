from pydantic import BaseModel, Field


class CreateProjectRequest(BaseModel):
    name: str = Field(min_length=1)
    description: str | None = None
    repoPath: str | None = None


class RunAgentRequest(BaseModel):
    provider: str = Field(default="cursor_cli", min_length=1)
    agentName: str = Field(default="default", min_length=1)
    prompt: str = Field(min_length=1)
    projectId: str | None = None
    projectPath: str | None = None
    metadata: dict | None = None
    idempotencyKey: str | None = None


class SetWebhookRequest(BaseModel):
    dropPendingUpdates: bool = True
