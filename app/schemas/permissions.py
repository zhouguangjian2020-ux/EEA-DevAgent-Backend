from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, UUID4, field_validator


class AccountRead(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    login_name: str
    display_name: str
    department: str
    state: str


class AccountListResponse(BaseModel):
    items: list[AccountRead]


class AccountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    login_name: str = Field(min_length=1, max_length=100)
    display_name: str = Field(min_length=1, max_length=100)
    department: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=8, max_length=200)

    @field_validator("login_name", "display_name", "department", mode="before")
    @classmethod
    def normalize_text(cls, value):
        if not isinstance(value, str):
            return value
        value = " ".join(value.strip().split())
        if "\x00" in value:
            raise ValueError("不能包含空字符")
        return value


class AccountPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    department: str | None = Field(default=None, min_length=1, max_length=200)
    state: str | None = Field(default=None, pattern="^(active|disabled)$")

    @field_validator("display_name", "department", mode="before")
    @classmethod
    def normalize_optional_text(cls, value):
        if value is None or not isinstance(value, str):
            return value
        value = " ".join(value.strip().split())
        if "\x00" in value:
            raise ValueError("不能包含空字符")
        return value


class ProjectPermissionRole(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    name: str
    description: str | None = None


class ProjectMemberRead(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: UUID
    login_name: str
    display_name: str
    department: str
    state: str
    project_state: str
    role_code: str
    role_name: str


class FunctionPermissionRead(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: UUID
    login_name: str
    display_name: str
    department: str
    role_code: str
    role_name: str


class PermissionFunctionRead(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    code: str
    name: str
    permissions: list[FunctionPermissionRead] = Field(default_factory=list)


class PermissionSettingsResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    project_id: UUID
    project_name: str
    can_manage: bool
    project_roles: list[ProjectPermissionRole]
    function_roles: list[ProjectPermissionRole]
    members: list[ProjectMemberRead]
    functions: list[PermissionFunctionRead]


class ProjectMemberUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: UUID4
    role_code: str = Field(pattern="^(project_api_author|project_api_reader)$")


class FunctionPermissionUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: UUID4
    role_code: str = Field(pattern="^(function_viewer|function_editor)$")


class PermissionMutationResponse(BaseModel):
    ok: bool = True
