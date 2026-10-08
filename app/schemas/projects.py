"""Read models for the existing ee.projects table; no DDL or write models."""
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, UUID4, field_validator


class ProjectRead(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    code: str
    name: str
    description: str | None
    lock_version: str  # PostgreSQL bigint: avoid browser integer precision loss.
    created_at: datetime
    updated_at: datetime
    can_update: bool = False
    can_delete: bool = False
    can_manage_access: bool = False


class ProjectCapabilities(BaseModel):
    can_create: bool = False


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # Generated once per form submission, reused on uncertain retries. Not an authorization token.
    creation_id: UUID4
    name: str = Field(min_length=1, max_length=100, strict=True)
    description: str | None = Field(default=None, max_length=2000, strict=True)

    @field_validator("name", "description", mode="before")
    @classmethod
    def normalize_text(cls, value, info):
        if not isinstance(value, str):
            return value
        value = value.strip()
        if "\x00" in value:
            raise ValueError("不能包含空字符")
        return None if info.field_name == "description" and not value else value


class ProjectListResponse(BaseModel):
    items: list[ProjectRead]
    limit: int
    offset: int
    has_more: bool
    capabilities: ProjectCapabilities = Field(default_factory=ProjectCapabilities)


class ProjectErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str


class ProjectErrorResponse(BaseModel):
    detail: ProjectErrorDetail

