"""Stage 1: mutable project/function metadata, never content revisions."""
from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, UUID4, field_validator, model_validator


class MetadataPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mutation_id: UUID4
    base_lock_version: str = Field(strict=True, pattern=r"^(0|[1-9][0-9]{0,18})$")
    name: str | None = Field(default=None, min_length=1, max_length=100, strict=True)
    description: str | None = Field(default=None, max_length=2000, strict=True)

    @field_validator("name", "description", mode="before")
    @classmethod
    def normalize(cls, value, info):
        if not isinstance(value, str):
            return value
        if "\x00" in value:
            raise ValueError("不能包含空字符")
        value = value.strip()
        return None if info.field_name == "description" and not value else value

    @model_validator(mode="after")
    def valid_patch(self):
        if not self.model_fields_set.intersection({"name", "description"}):
            raise ValueError("至少提供名称或简介")
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("名称不能为空")
        if int(self.base_lock_version) > 9223372036854775807:
            raise ValueError("版本超出范围")
        return self


class FunctionRead(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    project_id: UUID
    code: str
    name: str
    description: str | None
    lock_version: str
    created_at: datetime
    updated_at: datetime
    can_update: bool = False
    can_delete: bool = False
