"""First phase of PRD creation: a reviewable plan, never a write receipt."""
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, UUID4, field_validator, model_validator


class FeaturePlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    client_request_id: UUID4
    thread_id: UUID | None = None
    message: Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=4000)]

    @field_validator("message")
    @classmethod
    def reject_nul(cls, value):
        if "\x00" in value:
            raise ValueError("提示词不能包含空字符")
        return value


class FeatureDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=2000)
    description_source: Literal["agent_output"] = "agent_output"


class FeatureCandidate(BaseModel):
    function_id: UUID
    name: str
    description: str
    match: Literal["exact_name", "similar_name", "similar_description"]
    prd_artifact_id: UUID | None = None
    can_reuse_for_prd: bool
    blocked_reason: Literal["prd_exists", "prd_create_forbidden"] | None = None


class FeaturePlanReview(BaseModel):
    client_request_id: UUID
    project_id: UUID
    status: Literal["ready_to_create", "needs_selection", "blocked"]
    definition: FeatureDefinition
    candidates: list[FeatureCandidate]
    candidate_count: int
    has_more: bool
    can_create_new_function: bool
    exact_name_conflict: bool
    # Deliberately not called a generation_id or created result. S2 will own
    # durable intent identity, choice validation, concurrency, and writes.
    phase: Literal["plan_review"] = "plan_review"


class PrdCreationChoice(BaseModel):
    """A choice, never a browser-authored plan or document."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_version: int = Field(strict=True, ge=1)
    action: Literal["auto", "create", "reuse", "continue"]
    function_id: UUID | None = None
    name: Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=100)] | None = None

    @field_validator("name")
    @classmethod
    def safe_name(cls, value):
        import unicodedata
        if value is not None and any(unicodedata.category(c).startswith("C") and not c.isspace() for c in value):
            raise ValueError("功能名称不能含控制字符")
        return " ".join(value.split()) if value is not None else None

    @model_validator(mode="after")
    def choice_shape(self):
        if self.action == "reuse":
            if self.function_id is None or self.name is not None:
                raise ValueError("复用只能指定功能ID")
        elif self.function_id is not None or (self.action in ("auto", "continue") and self.name is not None):
            raise ValueError("创建不能指定功能ID；重试不能替换目标或名称")
        return self


class PrdCreationIntent(BaseModel):
    id: UUID
    project_id: UUID
    client_request_id: UUID
    version: int
    status: Literal["planning", "planned", "generating", "failed", "blocked", "saved", "interrupted"]
    next_phase: Literal["plan", "choose", "generate", "open"]
    review: FeaturePlanReview | None = None
    function_id: UUID | None = None
    artifact_id: UUID | None = None
    revision_id: UUID | None = None
    error_code: str | None = None
    # Never claim accepted: saving an immutable PRD revision does not confirm it.
    saved_as: Literal["pending"] | None = None
