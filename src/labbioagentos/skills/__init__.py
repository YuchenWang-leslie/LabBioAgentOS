"""User-approved immutable Gold Skill infrastructure."""

from .curator import SkillCuratorPort
from .models import (
    GoldSkill,
    SkillExecutionRef,
    SkillInstructionRef,
    SkillProcedure,
    SkillProposal,
    SkillScope,
    SkillSearchContext,
    SkillSourceBundle,
    SkillStatus,
    SkillTraceRef,
    SkillUsageOutcome,
    SkillUsageRecord,
    SkillUseAuthorization,
    SkillUseMode,
    SkillUseProposal,
    SkillUserDecision,
)
from .service import GoldSkillService
from .source import SkillSourceProjectionError, SkillSourceProjector
from .store import (
    InMemorySkillStore,
    SkillApprovalRequiredError,
    SkillDecisionError,
    SkillNotFoundError,
    SkillStoreError,
    SkillVersionConflictError,
)

__all__ = [
    "GoldSkill",
    "GoldSkillService",
    "InMemorySkillStore",
    "SkillApprovalRequiredError",
    "SkillCuratorPort",
    "SkillDecisionError",
    "SkillExecutionRef",
    "SkillInstructionRef",
    "SkillNotFoundError",
    "SkillProcedure",
    "SkillProposal",
    "SkillScope",
    "SkillSearchContext",
    "SkillSourceBundle",
    "SkillSourceProjectionError",
    "SkillSourceProjector",
    "SkillStatus",
    "SkillStoreError",
    "SkillTraceRef",
    "SkillUsageOutcome",
    "SkillUsageRecord",
    "SkillUseAuthorization",
    "SkillUseMode",
    "SkillUseProposal",
    "SkillUserDecision",
    "SkillVersionConflictError",
]
