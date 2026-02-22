from pydantic import BaseModel, Field


class CreateProviderAgentRequest(BaseModel):
    provider: str = Field(min_length=1)
    name: str = Field(min_length=1)
    projectId: str | None = None
    mode: str = "rules"
    config: dict | None = None


class StartProviderAgentRequest(BaseModel):
    prompt: str = Field(min_length=1)
    mode: str | None = None
    requestedBy: str | None = None
    projectPath: str | None = None
    metadata: dict | None = None


class FollowupProviderAgentRequest(BaseModel):
    text: str = Field(min_length=1)


class SyncProviderRequest(BaseModel):
    triggeredBy: str | None = None
