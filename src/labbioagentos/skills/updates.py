"""Scoped replacements in the authored content of an exact Skill draft."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import BoundedText, ShortText, SkillAdaptationPoint, SkillCuratorDraft


class _Edit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _NameEdit(_Edit):
    field: Literal["proposed_name"]
    value: ShortText


class _TextEdit(_Edit):
    field: Literal["description", "applicability"]
    value: BoundedText


class _StepsEdit(_Edit):
    field: Literal["workflow_outline"]
    value: tuple[BoundedText, ...] = Field(min_length=1, max_length=100)


class _ListEdit(_Edit):
    field: Literal[
        "agent_collaboration_guidance", "execution_guidance", "parameter_guidance",
        "validation_expectations", "known_failure_modes", "debug_lessons", "known_limitations",
    ]
    value: tuple[BoundedText, ...] = Field(max_length=100)


class _PrinciplesEdit(_Edit):
    field: Literal["reusable_principles"]
    value: tuple[BoundedText, ...] = Field(max_length=32)


class _AdaptationEdit(_Edit):
    field: Literal["adaptation_points"]
    value: tuple[SkillAdaptationPoint, ...] = Field(max_length=32)


class _CatalogEdit(_Edit):
    field: Literal["tags", "artifact_types"]
    value: frozenset[ShortText] = Field(max_length=100)


class _ContractsEdit(_Edit):
    field: Literal["input_contract_ids", "output_contract_ids"]
    value: tuple[ShortText, ...] = Field(max_length=100)


class SkillUpdatePatch(_Edit):
    """Only named fields change. Empty collections explicitly clear those sections."""

    edits: tuple[_NameEdit | _TextEdit | _StepsEdit | _ListEdit | _PrinciplesEdit |
                 _AdaptationEdit | _CatalogEdit | _ContractsEdit, ...]

    @model_validator(mode="after")
    def unique_fields(self):
        if len({edit.field for edit in self.edits}) != len(self.edits):
            raise ValueError("Each edited Skill field must be named only once")
        return self

    def apply(self, base: SkillCuratorDraft) -> SkillCuratorDraft:
        draft = base.model_dump()
        for edit in self.edits:
            target = draft if edit.field in {"proposed_name", "description"} else draft["procedure"]
            target[edit.field] = edit.value
        return SkillCuratorDraft.model_validate(draft)
