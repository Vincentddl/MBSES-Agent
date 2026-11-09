from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Kind = Literal["Requirement", "PartType", "Part", "ActionType", "Action", "Port", "Attribute", "Constraint", "UseCase", "Actor"]
RelationKind = Literal["CONTAINS", "DERIVES_FROM", "REFINES", "SATISFIES", "ALLOCATES"]
Nonempty = Annotated[str, Field(min_length=1, max_length=128)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class ElementDraft(StrictModel):
    localId: Annotated[str, Field(min_length=1, max_length=64)]
    kind: Kind
    name: Nonempty
    namespace: Nonempty
    description: str
    properties: dict[str, Any]
    evidence_chunk_ids: list[str]


class RelationDraft(StrictModel):
    relType: RelationKind
    source: str
    target: str


class MetaInfo(StrictModel):
    summary: str


class ModelDraft(StrictModel):
    meta_info: MetaInfo
    model_elements: Annotated[list[ElementDraft], Field(min_length=1, max_length=80)]
    sysml_relationships: Annotated[list[RelationDraft], Field(max_length=300)]
    open_questions: list[str]
    evidence: list[str]


class ProjectCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: Nonempty
    description: str = Field(default="", max_length=2000)


class GenerateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    project_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    input_text: str = Field(min_length=2, max_length=12000)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    bypass_cache: bool = False
    conversation_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    expected_conversation_revision: int | None = Field(default=None, ge=0)


class ConversationCreate(BaseModel):
    project_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class DraftUpdate(BaseModel):
    draft: dict[str, Any]
    expected_revision: int = Field(ge=0)


class ConfirmRequest(BaseModel):
    expected_revision: int = Field(ge=0)
