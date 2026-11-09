import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import LONGTEXT

from app.database import Base


def new_id():
    return uuid.uuid4().hex


class Project(Base):
    __tablename__ = "project"
    id = Column(String(32), primary_key=True, default=new_id)
    name = Column(String(128), nullable=False)
    description = Column(Text, default="")
    kb_version = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_document"
    __table_args__ = (UniqueConstraint("project_id", "content_hash", "parser_version"),)
    id = Column(String(32), primary_key=True, default=new_id)
    project_id = Column(String(32), ForeignKey("project.id"), nullable=False, index=True)
    name = Column(String(256), nullable=False)
    source_type = Column(String(16), nullable=False)
    content_hash = Column(String(64), nullable=False)
    parser_version = Column(String(128), nullable=False)
    storage_path = Column(Text, nullable=False)
    parse_info = Column(JSON, default=dict)
    status = Column(String(24), default="INDEXING", nullable=False)
    chunk_count = Column(Integer, default=0)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class DocChunk(Base):
    __tablename__ = "doc_chunk"
    __table_args__ = (UniqueConstraint("document_id", "chunk_index"),)
    id = Column(String(32), primary_key=True, default=new_id)
    project_id = Column(String(32), ForeignKey("project.id"), nullable=False, index=True)
    document_id = Column(String(32), ForeignKey("knowledge_document.id"), nullable=False, index=True)
    chunk_index = Column(Integer, nullable=False)
    section_title = Column(String(512), default="")
    source_locator = Column(String(512), default="")
    content = Column(Text, nullable=False)
    content_hash = Column(String(64), nullable=False)
    embedding_profile = Column(String(192), nullable=False)
    embedding = Column(JSON, nullable=True)
    index_status = Column(String(24), default="PENDING")


class GenerationTask(Base):
    __tablename__ = "generation_task"
    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_key"),
        Index("ix_task_project_state", "project_id", "status"),
    )
    id = Column(String(32), primary_key=True, default=new_id)
    project_id = Column(String(32), ForeignKey("project.id"), nullable=False)
    idempotency_key = Column(String(128), nullable=False)
    request_hash = Column(String(64), nullable=False)
    context_hash = Column(String(64), nullable=False)
    input_text = Column(Text, nullable=False)
    status = Column(String(32), default="PENDING", nullable=False)
    kb_version = Column(Integer, nullable=False)
    bypass_cache = Column(Integer, default=0)
    revision = Column(Integer, default=0, nullable=False)
    cache_hit = Column(Integer, default=0)
    result_json = Column(JSON, nullable=True)
    raw_response = Column(Text().with_variant(LONGTEXT, "mysql"), nullable=True)
    retrieved_chunks = Column(JSON, default=list)
    validation_errors = Column(JSON, default=list)
    warnings = Column(JSON, default=list)
    calculation = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    lease_until = Column(DateTime, nullable=True)
    lease_token = Column(String(32), nullable=True)
    execution_deadline_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)


class ModelElement(Base):
    __tablename__ = "model_element"
    __table_args__ = (
        UniqueConstraint("task_id", "local_id"),
        UniqueConstraint("project_id", "namespace", "kind", "name"),
    )
    id = Column(String(32), primary_key=True, default=new_id)
    project_id = Column(String(32), ForeignKey("project.id"), nullable=False, index=True)
    task_id = Column(String(32), ForeignKey("generation_task.id"), nullable=False, index=True)
    local_id = Column(String(64), nullable=False)
    kind = Column(String(32), nullable=False)
    name = Column(String(128), nullable=False)
    namespace = Column(String(128), nullable=False)
    description = Column(Text, default="")
    properties = Column(JSON, default=dict)
    evidence_chunk_ids = Column(JSON, default=list)
    status = Column(String(24), default="CONFIRMED", nullable=False)


class ModelRelationship(Base):
    __tablename__ = "model_relationship"
    __table_args__ = (UniqueConstraint("task_id", "rel_type", "source_id", "target_id"),)
    id = Column(String(32), primary_key=True, default=new_id)
    project_id = Column(String(32), ForeignKey("project.id"), nullable=False, index=True)
    task_id = Column(String(32), ForeignKey("generation_task.id"), nullable=False, index=True)
    rel_type = Column(String(32), nullable=False)
    source_id = Column(String(32), ForeignKey("model_element.id"), nullable=False)
    target_id = Column(String(32), ForeignKey("model_element.id"), nullable=False)


class TaskUsage(Base):
    """应用级用量台账：每任务每阶段一行，token与成本仅真实模型调用可填。"""
    __tablename__ = "task_usage"
    __table_args__ = (Index("ix_usage_task_stage", "task_id", "stage"),)
    id = Column(String(32), primary_key=True, default=new_id)
    task_id = Column(String(32), ForeignKey("generation_task.id"), nullable=False, index=True)
    stage = Column(String(32), nullable=False)
    backend = Column(String(32), nullable=False)
    http_status = Column(Integer, nullable=True)
    prompt_tokens = Column(Integer, nullable=True)
    completion_tokens = Column(Integer, nullable=True)
    total_tokens = Column(Integer, nullable=True)
    latency_ms = Column(Integer, nullable=False, default=0)
    cost_estimate = Column(Float, nullable=True)
    detail = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Conversation(Base):
    __tablename__ = "conversation"
    id = Column(String(32), primary_key=True, default=new_id)
    project_id = Column(String(32), ForeignKey("project.id"), nullable=False, index=True)
    revision = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class ConversationTurn(Base):
    __tablename__ = "conversation_turn"
    __table_args__ = (UniqueConstraint("conversation_id", "turn_index"),)
    task_id = Column(String(32), ForeignKey("generation_task.id"), primary_key=True)
    conversation_id = Column(String(32), ForeignKey("conversation.id"), nullable=False, index=True)
    turn_index = Column(Integer, nullable=False)
    context = Column(JSON, nullable=False, default=dict)
