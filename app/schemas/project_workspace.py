"""Read-only response models for a project's display workspace."""
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.projects import ProjectRead


class WorkspaceRevision(BaseModel):
    model_config = ConfigDict(frozen=True)

    revision_id: UUID
    revision_no: str
    status: str
    type_version_id: UUID
    created_via: str
    created_at: datetime
    accepted_revision_id: UUID | None = None


class WorkspacePrd(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    lock_version: str = "0"
    can_delete: bool = False
    code: str
    revision: WorkspaceRevision
    markdown_body: str
    source: str = "database"


class WorkspaceUc(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    lock_version: str = "0"
    can_delete: bool = False
    code: str
    revision: WorkspaceRevision
    prd_revision_id: UUID
    name: str
    description: str
    actors: str
    preconditions: str
    main_flow: str
    branches: list[str] = Field(min_length=4, max_length=4)
    exceptions: list[str] = Field(min_length=5, max_length=5)
    postconditions: str
    extensions: str
    hmi: str
    regulations: str
    performance: str
    nonfunctional: str


class WorkspaceFrSource(BaseModel):
    model_config = ConfigDict(frozen=True)

    uc_id: UUID
    uc_code: str
    uc_name: str
    uc_revision_id: UUID
    rationale: str | None = None


class WorkspaceFr(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    lock_version: str = "0"
    can_delete: bool = False
    code: str
    revision: WorkspaceRevision
    description: str
    sources: list[WorkspaceFrSource]
    acceptance_criteria: str = ""


class WorkspaceSubsystem(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    code: str
    name: str
    description: str
    responsibility_in: str | None = None
    responsibility_out: str | None = None
    domain_group: str | None = None
    is_allocatable: bool
    sort_order: int
    design_version_id: UUID
    design_version_no: int


class WorkspaceAllocationTarget(BaseModel):
    model_config = ConfigDict(frozen=True)

    subsystem_id: UUID
    subsystem_name: str
    subsystem_code: str
    domain_group: str | None = None
    rationale: str | None = None


class WorkspaceAllocation(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    lock_version: str = "0"
    can_delete: bool = False
    fr_id: UUID
    fr_code: str
    revision: WorkspaceRevision
    fr_revision_id: UUID
    design_version_id: UUID
    design_version_no: int
    note: str | None = None
    targets: list[WorkspaceAllocationTarget]


class WorkspaceFunction(BaseModel):
    workflow_capabilities: list[str] = Field(default_factory=list)
    model_config = ConfigDict(frozen=True)

    lock_version: str
    can_update: bool = False
    can_delete: bool = False

    id: UUID
    code: str
    name: str
    description: str | None = None
    prd: WorkspacePrd | None = None
    can_create_prd: bool = False
    can_create_uc: bool = False
    can_create_fr: bool = False
    accepted_use_cases: list[WorkspaceFrSource] = Field(default_factory=list)
    use_cases: list[WorkspaceUc]
    functional_requirements: list[WorkspaceFr]
    subsystems: list[WorkspaceSubsystem]
    fr_allocations: list[WorkspaceAllocation]


class ProjectWorkspaceResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    project: dict
    functions: list[WorkspaceFunction]
    can_create_function: bool = False
    capabilities: list[str] = Field(default_factory=list)
    read_only: bool = True
    source: str = "database"

