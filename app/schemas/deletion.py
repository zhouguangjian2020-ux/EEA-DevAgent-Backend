"""Single-object and explicit-selection batch deletion; no restore or physical purge."""
from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, UUID4, field_validator, model_validator

DeleteKind = Literal["project", "function", "PRD", "UC", "FR", "FR_ALLOCATION"]


class DeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mutation_id: UUID4
    base_lock_version: str = Field(strict=True, pattern=r"^(0|[1-9][0-9]{0,18})$")

    @field_validator("base_lock_version")
    @classmethod
    def bigint_version(cls, value):
        if int(value) > 9223372036854775807:
            raise ValueError("版本超出范围")
        return value


class DeleteTarget(BaseModel):
    id: UUID
    project_id: UUID
    function_id: UUID | None = None
    kind: DeleteKind
    code: str
    name: str
    lock_version: str
    head_revision_id: UUID | None = None
    accepted_revision_id: UUID | None = None


class DeleteBlocker(BaseModel):
    id: UUID
    kind: str
    code: str
    reason: str
    reference_versions: list[str] = Field(default_factory=list)


class AllocationDeleteTarget(BaseModel):
    subsystem_id: UUID
    subsystem_code: str
    subsystem_name: str
    reference_versions: list[str]


class DeletionImpact(BaseModel):
    target: DeleteTarget
    can_delete: bool
    blocked: bool
    blocker_count: int
    blockers: list[DeleteBlocker]
    blockers_truncated: bool
    allocation_fr_id: UUID | None = None
    allocation_targets: list[AllocationDeleteTarget] = Field(default_factory=list)
    history_preserved: Literal[True] = True
    checked_versions: list[str] = Field(default_factory=lambda: ["head", "accepted", "stable_identity"])


class DeleteResult(BaseModel):
    mutation_id: UUID
    project_id: UUID
    function_id: UUID | None = None
    id: UUID
    kind: DeleteKind
    deleted: Literal[True] = True
    archived_at: datetime
    lock_version: str
    history_preserved: Literal[True] = True


# Project deletion stays a separate empty-project operation; never in a batch.
BulkDeleteKind = Literal["function", "PRD", "UC", "FR", "FR_ALLOCATION"]


class BulkDeleteIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: UUID
    kind: BulkDeleteKind


class BulkDeleteItem(BulkDeleteIdentity):
    base_lock_version: str = Field(strict=True, pattern=r"^(0|[1-9][0-9]{0,18})$")
    @field_validator("base_lock_version")
    @classmethod
    def bigint_version(cls, value):
        return DeleteRequest.bigint_version(value)


class BulkDeleteSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    items: list[BulkDeleteIdentity] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def unique_targets(self):
        if len({item.id for item in self.items}) != len(self.items):
            raise ValueError("同一业务对象只能选择一次")
        return self


class BulkDeleteRequest(BulkDeleteSelection):
    mutation_id: UUID4
    items: list[BulkDeleteItem] = Field(min_length=1, max_length=200)


class BulkDeletionImpact(BaseModel):
    items: list[DeletionImpact]
    total: int
    can_delete: bool
    blocked: bool


class BulkDeleteResult(BaseModel):
    mutation_id: UUID
    project_id: UUID
    items: list[DeleteResult]
    deleted: Literal[True] = True
    history_preserved: Literal[True] = True
