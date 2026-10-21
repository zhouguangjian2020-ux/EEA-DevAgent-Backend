"""Small, explicit contracts for manual creation and immutable draft revisions."""
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, UUID4, field_validator

Text = Annotated[str, StringConstraints(strict=True, max_length=20000)]
Name = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=100)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("*", mode="after")
    @classmethod
    def no_nul(cls, value):
        if isinstance(value, str) and "\x00" in value:
            raise ValueError("不能包含空字符")
        if isinstance(value, list) and any(isinstance(x, str) and "\x00" in x for x in value):
            raise ValueError("不能包含空字符")
        return value


class FunctionCreate(Input):
    creation_id: UUID4
    name: Name
    description: Annotated[str, StringConstraints(strict=True, strip_whitespace=True, max_length=2000)] = ""


class PrdContent(Input):
    kind: Literal["PRD"]
    markdown_body: Annotated[str, StringConstraints(strict=True, max_length=200000)]


class UcContent(Input):
    kind: Literal["UC"]
    prd_revision_id: UUID
    name: Text = ""
    description: Text = ""
    actors: Text = ""
    preconditions: Text = ""
    main_flow: Text = ""
    branches: list[Text] = Field(default_factory=lambda: [""] * 4, min_length=4, max_length=4)
    exceptions: list[Text] = Field(default_factory=lambda: [""] * 5, min_length=5, max_length=5)
    postconditions: Text = ""
    extensions: Text = ""
    hmi: Text = ""
    regulations: Text = ""
    performance: Text = ""
    nonfunctional: Text = ""


class UcSource(Input):
    uc_id: UUID
    uc_revision_id: UUID


class FrContent(Input):
    kind: Literal["FR"]
    description: Text
    acceptance_criteria: Text = ""
    sources: list[UcSource] = Field(default_factory=list, max_length=100)

    @field_validator("sources")
    @classmethod
    def unique_sources(cls, value):
        if len({s.uc_id for s in value}) != len(value):
            raise ValueError("来源 UC 不得重复")
        return value


class PrdCreate(Input):
    kind: Literal["PRD"]
    creation_id: UUID4
    name: Name


class UcCreate(UcContent):
    creation_id: UUID4
    name: Name


class FrCreate(FrContent):
    creation_id: UUID4
    description: Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=20000)]


CreateArtifact = Annotated[PrdCreate | UcCreate | FrCreate, Field(discriminator="kind")]
Content = Annotated[PrdContent | UcContent | FrContent, Field(discriminator="kind")]


class SaveRevision(Input):
    mutation_id: UUID4
    base_revision_id: UUID
    content: Content


class AcceptRevision(Input):
    mutation_id: UUID4
    revision_id: UUID


class WriteResult(BaseModel):
    function_id: UUID
    artifact_id: UUID | None = None
    revision_id: UUID | None = None
    created: bool = True


class AiReviewReceipt(BaseModel):
    """A missing receipt is not proof that an in-flight write failed."""
    status: Literal["confirmed", "not_found"]
    artifact_id: UUID
    function_id: UUID | None = None
    revision_id: UUID | None = None
