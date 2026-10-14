"""Bounded explicit reviews; partial business failures are returned per object."""
from typing import Literal
from uuid import UUID
from pydantic import Field, UUID4, model_validator
from app.core.workflow_limits import MAX_UC_BATCH_ITEMS, MAX_FR_BATCH_ITEMS
from app.schemas.manual_work import Input, AcceptRevision
from app.schemas.workflows import AllocationSave

class ArtifactReviewItem(AcceptRevision):
    artifact_id: UUID

class AllocationReviewItem(Input):
    allocation_id: UUID4
    mutation_id: UUID4
    revision_id: UUID | None = None
    save: AllocationSave | None = None

    @model_validator(mode="after")
    def exact_target(self):
        if (self.save is None) == (self.revision_id is None):
            raise ValueError("Provide a saved revision OR an explicit draft, not both")
        if self.save is not None and self.save.allocation_id != self.allocation_id:
            raise ValueError("Allocation identity mismatch")
        return self

class ArtifactReviewBatch(Input):
    kind: Literal["UC", "FR"]
    items: list[ArtifactReviewItem] = Field(min_length=1, max_length=MAX_FR_BATCH_ITEMS, description="UC: at most 100 items; FR: at most 300 items.")

    @model_validator(mode="after")
    def unique_items(self):
        if self.kind == "UC" and len(self.items) > MAX_UC_BATCH_ITEMS:
            raise ValueError(f"UC review supports at most {MAX_UC_BATCH_ITEMS} items")
        _unique(self.items, "artifact_id")
        return self

class AllocationReviewBatch(Input):
    items: list[AllocationReviewItem] = Field(min_length=1, max_length=MAX_FR_BATCH_ITEMS)

    @model_validator(mode="after")
    def unique_items(self):
        _unique(self.items, "allocation_id")
        if len({x.save.mutation_id for x in self.items if x.save}) != sum(x.save is not None for x in self.items):
            raise ValueError("Duplicate save identity")
        return self

def _unique(items, field):
    if len({getattr(x, field) for x in items}) != len(items) or len({x.mutation_id for x in items}) != len(items):
        raise ValueError("Duplicate review identity")

class ReviewItemResult(Input):
    artifact_id: UUID
    revision_id: UUID | None = None
    accepted: bool
    saved: bool = False
    error_code: str | None = None
    error_message: str | None = None

class BatchReviewResult(Input):
    function_id: UUID
    items: list[ReviewItemResult]
