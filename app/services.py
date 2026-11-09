import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError

from app.cache import fingerprint, get_cache
from app.chunking import chunk_blocks
from app.config import settings
from app.database import SessionLocal
from app.embedding import get_embedder
from app.llm_client import build_user_prompt, get_llm_client, system_prompt
from app.models import DocChunk, GenerationTask, KnowledgeDocument, ModelElement, ModelRelationship, Project, new_id
from app.parsing import parse_document
from app.reliability import calculation_from_draft
from app.retrieval import retrieve_for_task
from app.validation import check_draft, validate_llm_output
from app.vector_store import get_vector_store


class BusinessError(ValueError):
    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


_ingest_lock = threading.RLock()
_executor = None


def require_model_configuration(*, embedding=False, llm=False):
    required = (embedding and settings.EMBEDDING_BACKEND == "qwen") or (llm and settings.LLM_BACKEND == "qwen")
    if required and not settings.QWEN_API_KEY:
        raise BusinessError("请先在.env.real中配置QWEN_API_KEY，再重启服务；不会回退到模拟模型", 503)
    if required and not settings.ALLOW_PAID_MODEL_CALLS:
        raise BusinessError("付费模型调用尚未授权，请设置ALLOW_PAID_MODEL_CALLS=true并重启服务", 503)


def start_workers():
    global _executor
    _executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mbse-task")
    now = datetime.utcnow()
    with SessionLocal.begin() as db:
        db.query(GenerationTask).filter(GenerationTask.status == "RUNNING",
            or_(GenerationTask.lease_until < now, GenerationTask.lease_until.is_(None))).update(
                {"status": "PENDING", "lease_until": None}, synchronize_session=False)
        pending = [row.id for row in db.query(GenerationTask).filter_by(status="PENDING").all()]
    for task_id in pending:
        submit_generation(task_id)


def stop_workers():
    global _executor
    if _executor is not None:
        _executor.shutdown(wait=True)
        _executor = None


def submit_generation(task_id):
    executor = _executor
    if executor is None:
        raise BusinessError("任务线程池尚未就绪，请稍后使用相同幂等Key重试", 503)
    try:
        executor.submit(run_generation, task_id)
    except RuntimeError as exc:
        # PENDING is durable; startup re-enqueues it. Do not acknowledge a failed handoff.
        raise BusinessError("任务线程池正在关闭，请稍后使用相同幂等Key重试", 503) from exc


def ingest_document(project_id, filename, raw):
    require_model_configuration(embedding=True)
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if len(raw) > settings.MAX_UPLOAD_BYTES:
        raise BusinessError("文件超过30MB限制", 413)
    filename = Path(filename).name[:256]
    blocks, parse_info = parse_document(filename, raw)
    chunks = chunk_blocks(blocks)
    file_hash = fingerprint({"raw_sha256": __import__("hashlib").sha256(raw).hexdigest()})
    parser_profile = f"{settings.PARSER_VERSION}:{settings.CHUNK_TARGET_CHARS}:{settings.CHUNK_OVERLAP_CHARS}:{settings.embedding_profile}"
    with _ingest_lock:
        with SessionLocal.begin() as db:
            if db.get(Project, project_id) is None:
                raise BusinessError("项目不存在", 404)
            doc = db.query(KnowledgeDocument).filter_by(project_id=project_id, content_hash=file_hash,
                                                       parser_version=parser_profile).first()
            if doc and doc.status == "INDEXED":
                return {"document_id": doc.id, "chunk_count": doc.chunk_count, "status": doc.status,
                        "deduplicated": True, "parse_info": doc.parse_info}
            if doc is None:
                doc_id = new_id()
                path = settings.DATA_DIR / "uploads" / project_id / (file_hash + Path(filename).suffix.lower())
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
                doc = KnowledgeDocument(id=doc_id, project_id=project_id, name=filename,
                    content_hash=file_hash, parser_version=parser_profile, storage_path=str(path),
                    source_type=Path(filename).suffix.lstrip("."), parse_info=parse_info,
                    status="INDEXING", chunk_count=len(chunks))
                db.add(doc)
                db.flush()
                for index, chunk in enumerate(chunks):
                    db.add(DocChunk(project_id=project_id, document_id=doc_id, chunk_index=index,
                        section_title=chunk.title[:512], source_locator=chunk.locator[:512],
                        content=chunk.content, content_hash=fingerprint(chunk.content),
                        embedding_profile=settings.embedding_profile, index_status="PENDING"))
            else:
                doc.status, doc.error = "INDEXING", None
            doc_id = doc.id
        # No database transaction is held during model or vector-service requests.
        try:
            with SessionLocal() as db:
                rows = db.query(DocChunk).filter_by(document_id=doc_id).order_by(DocChunk.chunk_index).all()
                texts = [row.section_title + "\n" + row.content for row in rows]
                metadata = [{"chunk_id": row.id, "project_id": project_id,
                    "document_id": doc_id, "embedding_profile": settings.embedding_profile} for row in rows]
            vectors = get_embedder().embed(texts)
            if len(vectors) != len(metadata):
                raise ValueError("向量数量与片段数不匹配")
            records = [{**item, "embedding": vector} for item, vector in zip(metadata, vectors)]
            get_vector_store().upsert(records)
            with SessionLocal.begin() as db:
                doc = db.get(KnowledgeDocument, doc_id)
                db.query(DocChunk).filter_by(document_id=doc_id).update({"index_status": "READY"})
                doc.status = "INDEXED"
                db.execute(update(Project).where(Project.id == project_id).values(kb_version=Project.kb_version + 1))
                result = {"document_id": doc_id, "chunk_count": doc.chunk_count,
                          "status": "INDEXED", "deduplicated": False, "parse_info": doc.parse_info}
            return result
        except Exception as exc:
            with SessionLocal.begin() as db:
                doc = db.get(KnowledgeDocument, doc_id)
                doc.status, doc.error = "FAILED", str(exc)[:1000]
            raise


def create_task(project_id, input_text, idempotency_key=None, bypass_cache=False):
    require_model_configuration(embedding=True, llm=True)
    key = idempotency_key or new_id()
    request_hash = fingerprint({"input": input_text.strip(), "bypass": bypass_cache})
    with SessionLocal() as db:
        old = db.query(GenerationTask).filter_by(project_id=project_id, idempotency_key=key).first()
        if old:
            if old.request_hash != request_hash:
                raise BusinessError("相同幂等Key对应不同输入", 409)
            return old.id, old.status, False
        project = db.get(Project, project_id)
        if project is None:
            raise BusinessError("项目不存在", 404)
        if db.query(GenerationTask).filter(GenerationTask.status.in_(["PENDING", "RUNNING"])).count() >= 100:
            raise BusinessError("任务队列已满，请稍后重试", 429)
        context_hash = fingerprint({"project": project_id, "input": input_text.strip(),
            "kb": project.kb_version, "embedding": settings.embedding_profile,
            "model": settings.QWEN_MODEL, "backend": settings.LLM_BACKEND,
            "prompt": settings.PROMPT_VERSION, "schema": settings.SCHEMA_VERSION,
            "top": settings.TOP_K, "final": settings.FINAL_CHUNKS,
            "context": settings.MAX_CONTEXT_CHARS, "temperature": settings.TEMPERATURE,
            "max_output": settings.LLM_MAX_OUTPUT_TOKENS,
            "output_mode": settings.QWEN_OUTPUT_MODE, "retrieval_plan": "reliability-v1"})
        task = GenerationTask(project_id=project_id, idempotency_key=key, request_hash=request_hash,
            context_hash=context_hash, input_text=input_text.strip(), kb_version=project.kb_version,
            bypass_cache=int(bypass_cache), status="PENDING")
        db.add(task)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            old = db.query(GenerationTask).filter_by(project_id=project_id, idempotency_key=key).one()
            if old.request_hash != request_hash:
                raise BusinessError("幂等Key冲突", 409)
            return old.id, old.status, False
        return task.id, task.status, True


def _evaluate(draft, chunks):
    normalized, errors, warnings = check_draft(draft, [c["chunk_id"] for c in chunks] + ["USER_INPUT"])
    calculation = None
    if normalized is not None and not errors:
        try:
            calculation = calculation_from_draft(normalized)
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(f"PARAMETER_INVALID: {exc}")
    return normalized, errors, warnings, calculation


def run_generation(task_id):
    with SessionLocal.begin() as db:
        claimed = db.query(GenerationTask).filter_by(id=task_id, status="PENDING").update({
            "status": "RUNNING", "lease_until": datetime.utcnow() + timedelta(seconds=settings.TASK_LEASE_SECONDS)})
    if not claimed:
        return
    try:
        with SessionLocal() as db:
            task = db.get(GenerationTask, task_id)
            project_id, query, version = task.project_id, task.input_text, task.kb_version
            context_hash, bypass = task.context_hash, task.bypass_cache
            current = db.get(Project, project_id).kb_version
            if current != version:
                raise ValueError("知识库版本已变化，请重新提交")
        cache = get_cache()
        cached = None if bypass else cache.get("llm:" + context_hash)
        if cached:
            chunks, raw = cached["chunks"], cached["raw"]
            cache_hit = True
        else:
            chunks, _ = retrieve_for_task(project_id, query, version)
            if not chunks:
                raise ValueError("没有可用的知识片段，请先上传文档")
            raw = get_llm_client().chat(system_prompt(), build_user_prompt(query, chunks))
            cache_hit = False
        parsed, parse_errors, _ = validate_llm_output(raw, [c["chunk_id"] for c in chunks] + ["USER_INPUT"])
        if parsed is None:
            raise ValueError("; ".join(parse_errors[:5]))
        draft, errors, warnings, calculation = _evaluate(parsed, chunks)
        with SessionLocal.begin() as db:
            task = db.get(GenerationTask, task_id)
            if task.status != "RUNNING":
                return
            if db.get(Project, project_id).kb_version != version:
                raise ValueError("生成期间知识库版本已变化，请重新提交")
            task.result_json, task.raw_response = draft, raw
            task.retrieved_chunks, task.validation_errors = chunks, errors
            task.warnings, task.calculation = warnings, calculation
            task.cache_hit, task.revision = int(cache_hit), 1
            task.status = "NEEDS_REVIEW" if errors else "NEEDS_INPUT" if draft["open_questions"] else "READY_FOR_REVIEW"
            task.finished_at, task.lease_until = datetime.utcnow(), None
        if not errors and not draft["open_questions"]:
            cache.set("llm:" + context_hash, {"chunks": chunks, "raw": raw}, settings.RESPONSE_CACHE_TTL)
    except Exception as exc:
        with SessionLocal.begin() as db:
            db.query(GenerationTask).filter_by(id=task_id, status="RUNNING").update({
                "status": "FAILED", "error": str(exc)[:2000],
                "finished_at": datetime.utcnow(), "lease_until": None})


def update_draft(task_id, value, revision):
    with SessionLocal() as db:
        task = db.get(GenerationTask, task_id)
        if task is None:
            raise BusinessError("任务不存在", 404)
        if task.status not in {"READY_FOR_REVIEW", "NEEDS_INPUT", "NEEDS_REVIEW"}:
            raise BusinessError("当前状态不能修改草稿", 409)
        chunks = task.retrieved_chunks
    draft, errors, warnings, calculation = _evaluate(value, chunks)
    if draft is None:
        return {"valid": False, "errors": errors, "warnings": warnings,
                "revision": revision, "status": "NEEDS_REVIEW"}
    status = "NEEDS_REVIEW" if errors else "NEEDS_INPUT" if draft["open_questions"] else "READY_FOR_REVIEW"
    with SessionLocal.begin() as db:
        count = db.query(GenerationTask).filter(GenerationTask.id == task_id,
            GenerationTask.revision == revision, GenerationTask.status.in_(["READY_FOR_REVIEW", "NEEDS_INPUT", "NEEDS_REVIEW"])).update({
                "result_json": draft, "validation_errors": errors, "warnings": warnings,
                "calculation": calculation, "revision": revision + 1, "status": status})
        if count != 1:
            raise BusinessError("草稿版本冲突，请刷新", 409)
    return {"valid": not errors and not draft["open_questions"], "errors": errors,
            "warnings": warnings, "revision": revision + 1, "status": status}


def confirm_task(task_id, revision):
    with SessionLocal.begin() as db:
        task = db.get(GenerationTask, task_id)
        if task is None:
            raise BusinessError("任务不存在", 404)
        if task.status == "CONFIRMED":
            return {"confirmed": db.query(ModelElement).filter_by(task_id=task_id).count(),
                    "relations": db.query(ModelRelationship).filter_by(task_id=task_id).count(), "status": "CONFIRMED", "idempotent": True}
        if task.status != "READY_FOR_REVIEW":
            raise BusinessError("需要先完成校验并解决待澄清问题")
        if task.revision != revision:
            raise BusinessError("草稿版本冲突", 409)
        # This endpoint is explicit approval; recheck the saved draft at commit time,
        # rather than trusting a potentially stale browser-side validation flag.
        draft, errors, _, _ = _evaluate(task.result_json, task.retrieved_chunks)
        if errors or draft["open_questions"]:
            raise BusinessError("草稿仍有错误或待澄清问题")
        claimed = db.query(GenerationTask).filter_by(id=task_id, status="READY_FOR_REVIEW", revision=revision).update({"status": "COMMITTING"})
        if claimed != 1:
            raise BusinessError("任务正在被其他请求确认", 409)
        mapping = {}
        for element in draft["model_elements"]:
            row = ModelElement(id=new_id(), project_id=task.project_id, task_id=task_id,
                local_id=element["localId"], kind=element["kind"], name=element["name"],
                namespace=element["namespace"], description=element["description"],
                properties=element["properties"], evidence_chunk_ids=element["evidence_chunk_ids"])
            mapping[element["localId"]] = row.id
            db.add(row)
        try:
            db.flush()
            for relation in draft["sysml_relationships"]:
                db.add(ModelRelationship(project_id=task.project_id, task_id=task_id,
                    rel_type=relation["relType"], source_id=mapping[relation["source"]], target_id=mapping[relation["target"]]))
            task.status = "CONFIRMED"
            db.flush()
        except IntegrityError as exc:
            raise BusinessError("正式模型中已有同作用域同名元素，请修改名称或使用新项目", 409) from exc
        return {"confirmed": len(mapping), "relations": len(draft["sysml_relationships"]), "status": "CONFIRMED", "idempotent": False}
