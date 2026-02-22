from __future__ import annotations

from dataclasses import dataclass

from app.core.config import get_settings
from app.db.models import RiskLevel


@dataclass
class PolicyInput:
    prompt: str
    provider: str


@dataclass
class PolicyDecisionResult:
    risk_level: RiskLevel
    allowed: bool
    requires_approval: bool
    reason: str
    suggestion: str | None = None


def classify_risk(prompt: str) -> RiskLevel:
    normalized = prompt.lower()
    high_markers = ["rm -rf", "delete", "drop table", "force push", "credential", "secret", "token", "production"]
    medium_markers = ["install", "upgrade", "migrate", "edit", "refactor", "write", "deploy"]

    if any(marker in normalized for marker in high_markers):
        return RiskLevel.HIGH
    if any(marker in normalized for marker in medium_markers):
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def evaluate_policy(input_data: PolicyInput) -> PolicyDecisionResult:
    settings = get_settings()
    risk = classify_risk(input_data.prompt)

    if settings.approval_policy_default == "strict":
        return PolicyDecisionResult(
            risk_level=risk,
            allowed=risk == RiskLevel.LOW,
            requires_approval=risk != RiskLevel.LOW,
            reason="Strict mode requires approval for medium and high risk actions",
            suggestion="Try a read-only analysis command first.",
        )

    if settings.approval_policy_default == "aggressive":
        return PolicyDecisionResult(
            risk_level=risk,
            allowed=True,
            requires_approval=risk == RiskLevel.HIGH,
            reason="Aggressive mode auto-approves most actions",
            suggestion="Consider narrowing the command scope before execution.",
        )

    # Balanced mode
    if risk == RiskLevel.HIGH:
        return PolicyDecisionResult(
            risk_level=risk,
            allowed=False,
            requires_approval=True,
            reason="High-risk action requires manual approval in balanced mode",
            suggestion="Split into read-only verification first, then execute.",
        )

    if risk == RiskLevel.MEDIUM and settings.approval_medium_requires_user:
        return PolicyDecisionResult(
            risk_level=risk,
            allowed=False,
            requires_approval=True,
            reason="Medium-risk action requires manual approval in balanced mode",
            suggestion="Provide a narrower scope or dry-run command.",
        )

    return PolicyDecisionResult(
        risk_level=risk,
        allowed=True,
        requires_approval=False,
        reason="Action is allowed by balanced mode",
    )
