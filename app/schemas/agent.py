from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, UUID4, field_validator

from app.core.workflow_limits import MAX_FR_BATCH_ITEMS


class PrdExpandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    feature_name: str = Field(
        min_length=1, max_length=4000,
        description="输入框中待扩写的功能名称或简述；不是下拉框的本地测试功能 ID。",
        examples=["AEB自动紧急制动"],
    )


class PrdExpandResponse(BaseModel):
    text: str
    request_id: str
    trace_id: str | None = None
    response_mode: Literal["buffered"] = "buffered"


class PrdGenerationRequest(BaseModel):
    """Identity-only request for generating a PRD from a stored function definition."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    thread_id: UUID | None = None
    client_request_id: UUID4 = Field(default_factory=uuid4)
    project_id: UUID
    function_id: UUID
    version: str | None = Field(
        default=None,
        min_length=1,
        max_length=50,
        description="可选的目标 PRD 版本号；不传时不由业务后端猜测。",
        examples=["V1.0"],
    )


class AgentErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str


class AgentErrorResponse(BaseModel):
    detail: AgentErrorDetail


class GenerationSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    kind: Literal["PRD"]
    artifact_id: UUID
    revision_id: UUID


class UcGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    scene: Literal["FROM_PRD_CREATE_UC"]
    project_id: UUID
    function_id: UUID
    source: GenerationSource
    client_request_id: UUID4


class UcScenario(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    main_flow: str = Field(min_length=1, max_length=20000)
    branches: list[str] = Field(max_length=100)
    exceptions: list[str] = Field(max_length=100)

    @field_validator("main_flow", "branches", "exceptions")
    @classmethod
    def valid_scenario_text(cls, value):
        texts = value if isinstance(value, list) else [value]
        if any("\x00" in text or len(text) > 20000 for text in texts):
            raise ValueError("场景文本不能包含空字符或超过20000字符")
        if isinstance(value, list) and any(not text.strip() for text in texts):
            raise ValueError("无适用场景时返回空数组，不填写空槽位")
        return value


class UcCandidateContent(BaseModel):
    """11 Agent fields; the 12th field (number) is assigned only on formal save."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=20000)
    actors: str = Field(max_length=20000)
    preconditions: str = Field(max_length=20000)
    scenario: UcScenario
    postconditions: str = Field(max_length=20000)
    extensions: str = Field(max_length=20000)
    hmi: str = Field(max_length=20000)
    regulations: str = Field(max_length=20000)
    performance: str = Field(max_length=20000)
    nonfunctional: str = Field(max_length=20000)

    @field_validator("*", mode="after")
    @classmethod
    def no_nul(cls, value):
        if isinstance(value, str) and "\x00" in value:
            raise ValueError("不能包含空字符")
        return value


class UcGenerationCandidate(UcCandidateContent):
    """A transient candidate; candidate_id is not a persisted artifact ID."""

    candidate_id: UUID4
    ordinal: int = Field(ge=1)


class UcGenerationSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: UUID
    function_id: UUID
    prd_id: UUID
    prd_revision_id: UUID


class UcGenerationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    generation_id: UUID
    scene: Literal["FROM_PRD_CREATE_UC"]
    status: Literal["succeeded"]
    source: UcGenerationSource
    candidates: list[UcGenerationCandidate] = Field(default_factory=list, max_length=100)
    trace_id: str | None = None


class UcDiagramRequest(BaseModel):
    thread_id: UUID | None = None
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    scene: Literal["FROM_UC_CREATE_UC_DIAGRAM"]
    project_id: UUID
    function_id: UUID
    known_uc_source_version: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    client_request_id: UUID4


class UcDiagramSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: UUID
    function_id: UUID
    uc_count: int = Field(ge=0, le=100)


class UcDiagramResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    generation_id: UUID
    scene: Literal["FROM_UC_CREATE_UC_DIAGRAM"]
    status: Literal["empty", "unchanged", "succeeded"]
    source: UcDiagramSource
    source_version: str
    diagram_format: Literal["mermaid"] | None = None
    diagram_code: str | None = None
    trace_id: str | None = None


class UcTemplateField(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    label: str
    value_type: Literal["string", "array", "object"]
    required: bool = False
    generated_by: Literal["agent", "backend"] = "agent"
    description: str = ""
    children: list["UcTemplateField"] = Field(default_factory=list)


class UcTemplateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scene: Literal["FROM_PRD_CREATE_UC"]
    template_id: str
    template_version: str
    fields: list[UcTemplateField]
    candidate_schema: dict[str, object]
    instructions: list[str]


class RequirementDiagramRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scene: Literal["FROM_SS_ASSIGNMENT_CREATE_REQ_DIAGRAM"]
    project_id: UUID
    function_id: UUID
    subsystem_id: UUID
    client_request_id: UUID4


class RequirementDiagramSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: UUID
    function_id: UUID
    subsystem_id: UUID
    subsystem_name: str
    allocation_count: int = Field(ge=0, le=MAX_FR_BATCH_ITEMS)
    fr_count: int = Field(ge=0, le=MAX_FR_BATCH_ITEMS)


class RequirementDiagramResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    generation_id: UUID
    scene: Literal["FROM_SS_ASSIGNMENT_CREATE_REQ_DIAGRAM"]
    status: Literal["empty", "succeeded"]
    source: RequirementDiagramSource
    source_version: str
    markdown: str | None = None
    diagram_format: Literal["mermaid"] | None = None
    diagram_code: str | None = None
    trace_id: str | None = None
