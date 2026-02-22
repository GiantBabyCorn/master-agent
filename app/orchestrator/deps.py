from functools import lru_cache

from app.core.config import get_settings
from app.orchestrator.agentic_service import AgenticOrchestrator
from app.orchestrator.service import MasterOrchestrator


@lru_cache(maxsize=1)
def get_orchestrator() -> MasterOrchestrator:
    mode = get_settings().orchestration_mode.strip().lower()
    if mode == "agentic":
        return AgenticOrchestrator()
    return MasterOrchestrator()
