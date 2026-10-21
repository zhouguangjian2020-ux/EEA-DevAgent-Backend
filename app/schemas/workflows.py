"""Contracts for reviewed FR/allocation candidates and private project conversations."""
from typing import Annotated, Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, UUID4, model_validator
from app.core.workflow_limits import MAX_UC_BATCH_ITEMS, MAX_FR_BATCH_ITEMS
from app.schemas.manual_work import FrContent, PrdContent, UcContent, UcSource

Text = Annotated[str, StringConstraints(strict=True, max_length=20000)]
class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')

class GenerationRequest(Input):
    client_request_id: UUID4
    thread_id: UUID | None = None

class FrCandidate(FrContent):
    model_config = ConfigDict(extra='forbid')
    description: Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=20000)]
    sources: list[UcSource] = Field(min_length=1, max_length=100)

class AllocationTarget(Input):
    subsystem_id: UUID
    rationale: Text = ''

class AllocationCandidate(Input):
    fr_id: UUID
    fr_revision_id: UUID
    targets: list[AllocationTarget] = Field(max_length=100)
    note: Text = ''
    unmatched_reason: Text = ''
    @model_validator(mode='after')
    def unique_targets(self):
        if len({x.subsystem_id for x in self.targets}) != len(self.targets):
            raise ValueError('Duplicate subsystem')
        if not self.targets and not self.unmatched_reason.strip():
            raise ValueError('Unmatched FR must include a reason')
        return self

class AllocationSave(Input):
    source_version: Annotated[str, StringConstraints(pattern='^[0-9a-f]{64}$')] | None = None
    mutation_id: UUID4
    allocation_id: UUID4
    base_revision_id: UUID | None = None
    fr_id: UUID
    fr_revision_id: UUID
    design_version_id: UUID
    targets: list[AllocationTarget] = Field(max_length=100)
    note: Text = ''
    @model_validator(mode='after')
    def unique_targets(self):
        if len({x.subsystem_id for x in self.targets}) != len(self.targets):
            raise ValueError('Duplicate subsystem')
        return self

class Attachment(Input):
    artifact_id: UUID
    revision_id: UUID

class ThreadCreate(Input):
    client_request_id: UUID4
    title: Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=120)] = '新会话'

class ChatRequest(Input):
    # Opt-in single-turn answer/proposal protocol; legacy clients remain valid.
    unified_conversation: bool = False
    auto_intent: bool = False
    retry: bool = False
    client_request_id: UUID4
    message: Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=20000)]
    mode: Literal['question', 'modify'] = 'question'
    attachments: list[Attachment] = Field(default_factory=list, max_length=20)
    @model_validator(mode='after')
    def check_attachments(self):
        if len({x.artifact_id for x in self.attachments}) != len(self.attachments):
            raise ValueError('Duplicate attachment')
        if self.mode == 'modify' and not self.attachments:
            raise ValueError('Modification requires explicit attachments')
        return self

class Modification(Input):
    artifact_id: UUID
    base_revision_id: UUID
    content: Annotated[PrdContent | UcContent | FrContent, Field(discriminator='kind')]

class FrAdopt(Input):
    creation_id: UUID4
    source_version: Annotated[str, StringConstraints(pattern='^[0-9a-f]{64}$')]
    content: FrCandidate

from datetime import datetime

class SourceSnapshot(BaseModel):
    artifact_id: UUID
    function_id: UUID
    revision_id: UUID
    head_revision_id: UUID
    accepted_revision_id: UUID | None
    number: str
    status: Literal['accepted','pending']
    content: Annotated[PrdContent | UcContent | FrContent, Field(discriminator='kind')]

class GenerationUcContent(UcContent):
    # Read-only generation snapshots contain ALL persisted scenario items.
    # Manual editing and chat modification retain their existing 4/5-slot contract.
    branches: list[Text] = Field(default_factory=list)
    exceptions: list[Text] = Field(default_factory=list)

class GenerationSourceSnapshot(SourceSnapshot):
    content: Annotated[PrdContent | GenerationUcContent | FrContent, Field(discriminator='kind')]

class SubsystemSnapshot(BaseModel):
    subsystem_id: UUID
    name: str
    description: str
    design_version_id: UUID

class GenerationSource(BaseModel):
    project_id: UUID
    function_id: UUID
    sources: list[GenerationSourceSnapshot]
    subsystems: list[SubsystemSnapshot]

class GeneratedFr(FrCandidate):
    candidate_id: UUID4

class GeneratedAllocation(AllocationCandidate):
    candidate_id: UUID4

class FrGenerationResult(BaseModel):
    generation_id: UUID4
    scene: Literal['FROM_UC_CREATE_FR']
    source_version: str
    source: GenerationSource
    candidates: list[GeneratedFr]

class AllocationGenerationResult(BaseModel):
    generation_id: UUID4
    scene: Literal['FROM_FR_CREATE_SS_ASSIGNMENT']
    source_version: str
    source: GenerationSource
    candidates: list[GeneratedAllocation]

class ThreadRead(BaseModel):
    id: UUID
    title: str
    created_at: datetime
    updated_at: datetime

class ThreadPage(BaseModel):
    threads: list[ThreadRead]
    next_offset: int | None

class TurnRead(BaseModel):
    duration_ms: Annotated[int | None, Field(strict=True, ge=0, le=9_007_199_254_740_991)] = None
    client_request_id: UUID
    prompt: str
    body: str
    status: Literal['running','completed','failed','interrupted']
    attachments: list[SourceSnapshot]
    created_at: datetime
    updated_at: datetime

class ThreadHistory(BaseModel):
    thread: ThreadRead
    turns: list[TurnRead]


class TaskResultRef(BaseModel):
    artifact_id: UUID
    revision_id: UUID


class ChatTaskRead(BaseModel):
    duration_ms: Annotated[int | None, Field(strict=True, ge=0, le=9_007_199_254_740_991)] = None
    request_message: str | None = None
    request_function_name: str | None = None
    request_error_code: str | None = None
    id: UUID
    client_request_id: UUID
    task_kind: Literal['PRD', 'UC', 'FR', 'FR_ALLOCATION', 'UC_DIAGRAM']
    stage: Literal['starting', 'planning', 'awaiting_choice', 'creating_function', 'generating', 'saving', 'review', 'completed']
    status: Literal['running', 'waiting_choice', 'preview', 'succeeded', 'empty', 'failed', 'needs_verification']
    function_id: UUID | None
    result_count: int | None
    result_refs: list[TaskResultRef]
    result_available: bool
    unchanged: bool = False
    created_at: datetime
    updated_at: datetime


class ChatTaskPage(BaseModel):
    tasks: list[ChatTaskRead]
    next_offset: int | None


class UcAppendRequest(GenerationRequest):
    prd_id: UUID
    prd_revision_id: UUID

class GeneratedBatchItem(Input):
    artifact_id: UUID
    revision_id: UUID
    function_id: UUID
    created: bool

class GeneratedBatchResult(Input):
    generation_id: UUID
    kind: Literal['UC', 'FR']
    status: Literal['succeeded']
    project_id: UUID
    function_id: UUID
    source_version: str
    replayed: bool
    trace_id: str | None = None
    items: list[GeneratedBatchItem] = Field(min_length=1, max_length=MAX_FR_BATCH_ITEMS, description="UC: at most 100 items; FR: at most 300 items.")

    @model_validator(mode="after")
    def bound_uc_items(self):
        if self.kind == "UC" and len(self.items) > MAX_UC_BATCH_ITEMS:
            raise ValueError(f"UC generation supports at most {MAX_UC_BATCH_ITEMS} items")
        return self
