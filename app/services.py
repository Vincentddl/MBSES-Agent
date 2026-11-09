import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func, or_, update
from sqlalchemy.exc import IntegrityError

from app.cache import fingerprint, get_cache
from app.chunking import chunk_blocks
from app.config import settings
from app.database import SessionLocal
from app.embedding import get_embedder
from app.http_client import DeadlineExceeded
from app.llm_client import build_user_prompt, get_llm_client, system_prompt
from app.memory import load_context, merge_chunks, reserve_turn
from app.models import DocChunk, GenerationTask, KnowledgeDocument, ModelElement, ModelRelationship, Project, TaskUsage, new_id
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
_reaper = None
_reaper_stop = None
_submitted = set()
_submission_lock = threading.RLock()


def require_model_configuration(*, embedding=False, llm=False):
    required = (embedding and settings.EMBEDDING_BACKEND == "qwen") or (llm and settings.LLM_BACKEND == "qwen")
    if required and not settings.QWEN_API_KEY:
        raise BusinessError("请先在.env.real中配置QWEN_API_KEY，再重启服务；不会回退到模拟模型", 503)
    if required and not settings.ALLOW_PAID_MODEL_CALLS:
        raise BusinessError("付费模型调用尚未授权，请设置ALLOW_PAID_MODEL_CALLS=true并重启服务", 503)


def record_usage(task_id, stage, backend, latency_ms, http_status=None, prompt_tokens=None,
                 completion_tokens=None, total_tokens=None, detail=None):
    """应用级用量台账：台账写失败只丢记录，绝不影响主流程。"""
    cost = None
    if prompt_tokens is not None and completion_tokens is not None:
        cost = round(prompt_tokens / 1000 * settings.PRICE_INPUT_PER_1K +
                     completion_tokens / 1000 * settings.PRICE_OUTPUT_PER_1K, 6)
    if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
        total_tokens = prompt_tokens + completion_tokens
    try:
        with SessionLocal.begin() as db:
            db.add(TaskUsage(task_id=task_id, stage=stage, backend=backend, http_status=http_status,
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, total_tokens=total_tokens,
                latency_ms=max(int(latency_ms), 0), cost_estimate=cost, detail=detail))
    except Exception:
        pass


class _LeaseHeartbeat:
    """租约心跳：RUNNING任务必须持续续约；worker崩溃后停止续约，由回收线程接管。"""
    def __init__(self, task_id, token):
        self.task_id, self.token = task_id, token
        self.stop_event = threading.Event()
        interval = settings.HEARTBEAT_INTERVAL_SECONDS or settings.TASK_LEASE_SECONDS / 3
        self.thread = threading.Thread(target=self._loop, args=(interval,), name="mbse-lease", daemon=True)
        self.thread.start()

    def _loop(self, interval):
        while not self.stop_event.wait(interval):
            try:
                # 以lease_token做围栏：续约只作用于仍持有租约的本次执行。
                with SessionLocal.begin() as db:
                    db.query(GenerationTask).filter_by(id=self.task_id, status="RUNNING",
                        lease_token=self.token).update(
                        {"lease_until": datetime.utcnow() + timedelta(seconds=settings.TASK_LEASE_SECONDS)})
            except Exception:
                pass

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=5)


def requeue_expired_tasks():
    """把租约过期（或丢失）的RUNNING任务重新置为PENDING；返回被回收的任务ID。"""
    now = datetime.utcnow()
    with SessionLocal.begin() as db:
        rows = db.query(GenerationTask.id, GenerationTask.lease_token, GenerationTask.lease_until,
                        GenerationTask.execution_deadline_at).filter(GenerationTask.status == "RUNNING",
            or_(GenerationTask.lease_until < now, GenerationTask.lease_until.is_(None))).all()
        ids = []
        for row in rows:
            exhausted = row.execution_deadline_at is not None and row.execution_deadline_at <= now
            values = {"status": "FAILED" if exhausted else "PENDING", "lease_until": None, "lease_token": None}
            if exhausted:
                values.update(error="TASK_DEADLINE_EXCEEDED: recovery budget exhausted", finished_at=now)
            changed = db.query(GenerationTask).filter(GenerationTask.id == row.id,
                GenerationTask.status == "RUNNING", GenerationTask.lease_token == row.lease_token,
                GenerationTask.lease_until == row.lease_until,
                or_(GenerationTask.lease_until < now, GenerationTask.lease_until.is_(None))).update(
                values, synchronize_session=False)
            if changed and not exhausted:
                ids.append(row.id)
    return ids


def _reap_loop():
    while not _reaper_stop.wait(settings.TASK_REAP_INTERVAL_SECONDS):
        try:
            recover_pending_tasks()
        except Exception:
            pass


def recover_pending_tasks():
    requeue_expired_tasks()
    with SessionLocal() as db:
        pending = [row.id for row in db.query(GenerationTask.id).filter_by(status="PENDING").limit(100).all()]
    for task_id in pending:
        try:
            submit_generation(task_id)
        except BusinessError:
            # Still durable PENDING: the next scan retries this handoff too.
            continue


def start_workers():
    global _executor, _reaper, _reaper_stop
    _executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mbse-task")
    recover_pending_tasks()
    _reaper_stop = threading.Event()
    _reaper = threading.Thread(target=_reap_loop, name="mbse-reaper", daemon=True)
    _reaper.start()


def stop_workers():
    global _executor, _reaper, _reaper_stop
    if _reaper_stop is not None:
        _reaper_stop.set()
    if _reaper is not None:
        _reaper.join(timeout=5)
        _reaper = None
    if _executor is not None:
        _executor.shutdown(wait=True)
        _executor = None


def submit_generation(task_id):
    executor = _executor
    if executor is None:
        raise BusinessError("任务线程池尚未就绪，请稍后使用相同幂等Key重试", 503)
    with _submission_lock:
        if task_id in _submitted:
            return
        _submitted.add(task_id)

    def release(_future=None):
        with _submission_lock:
            _submitted.discard(task_id)

    try:
        future = executor.submit(run_generation, task_id)
    except RuntimeError as exc:
        release()
        # PENDING is durable; startup re-enqueues it. Do not acknowledge a failed handoff.
        raise BusinessError("任务线程池正在关闭，请稍后使用相同幂等Key重试", 503) from exc
    future.add_done_callback(release)


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


def create_task(project_id, input_text, idempotency_key=None, bypass_cache=False,
                conversation_id=None, expected_conversation_revision=None):
    require_model_configuration(embedding=True, llm=True)
    key = idempotency_key or new_id()
    request_hash = fingerprint({"input": input_text.strip(), "bypass": bypass_cache,
        "conversation_id": conversation_id})
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
            "output_mode": settings.QWEN_OUTPUT_MODE, "retrieval_plan": "reliability-v1", "tool_protocol": "bounded-v1"})
        task = GenerationTask(project_id=project_id, idempotency_key=key, request_hash=request_hash,
            context_hash=context_hash, input_text=input_text.strip(), kb_version=project.kb_version,
            bypass_cache=int(bypass_cache), status="PENDING")
        db.add(task)
        try:
            db.flush()
            if conversation_id:
                memory = reserve_turn(db, project_id, conversation_id, expected_conversation_revision,
                                      task.id, input_text.strip())
                task.context_hash = fingerprint({"base": context_hash, "memory": memory, "tools": "bounded-v1"})
            db.commit()
        except IntegrityError:
            db.rollback()
            old = db.query(GenerationTask).filter_by(project_id=project_id, idempotency_key=key).one()
            if old.request_hash != request_hash:
                raise BusinessError("幂等Key冲突", 409)
            return old.id, old.status, False
        return task.id, task.status, True


def _evaluate(draft, chunks, user_input=None):
    # 事实对照需要片段原文：引用ID合法只是第一层，数值必须能在原文中找到。
    texts = {c["chunk_id"]: c["content"] for c in chunks}
    if user_input:
        texts["USER_INPUT"] = user_input
    normalized, errors, warnings = check_draft(draft, [c["chunk_id"] for c in chunks] + ["USER_INPUT"],
                                               evidence_texts=texts if settings.FACT_CHECK_ENABLED else None)
    calculation = None
    if normalized is not None and not errors:
        try:
            calculation = calculation_from_draft(normalized)
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(f"PARAMETER_INVALID: {exc}")
    return normalized, errors, warnings, calculation


def run_generation(task_id):
    token = new_id()
    started_total = time.monotonic()
    with SessionLocal.begin() as db:
        claimed = db.query(GenerationTask).filter_by(id=task_id, status="PENDING").update({
            "status": "RUNNING", "lease_until": datetime.utcnow() + timedelta(seconds=settings.TASK_LEASE_SECONDS),
            "lease_token": token,
            "execution_deadline_at": func.coalesce(GenerationTask.execution_deadline_at,
                datetime.utcnow() + timedelta(seconds=settings.TASK_DEADLINE_SECONDS))})
        if claimed:
            expires = db.get(GenerationTask, task_id).execution_deadline_at
    if not claimed:
        return
    deadline = time.monotonic() + max((expires - datetime.utcnow()).total_seconds(), 0)
    heartbeat = _LeaseHeartbeat(task_id, token)

    def fail(message):
        with SessionLocal.begin() as db:
            db.query(GenerationTask).filter_by(id=task_id, status="RUNNING", lease_token=token).update({
                "status": "FAILED", "error": str(message)[:2000],
                "finished_at": datetime.utcnow(), "lease_until": None, "lease_token": None})

    def check_deadline(stage):
        if time.monotonic() >= deadline:
            raise DeadlineExceeded(
                f"TASK_DEADLINE_EXCEEDED: 任务总时限{int(settings.TASK_DEADLINE_SECONDS)}s在{stage}阶段前已耗尽")

    try:
        check_deadline("初始化")
        with SessionLocal() as db:
            task = db.get(GenerationTask, task_id)
            project_id, query, version = task.project_id, task.input_text, task.kb_version
            context_hash, bypass = task.context_hash, task.bypass_cache
            memory = load_context(db, task_id)
            source_input = memory.get("source_input", query)
            current = db.get(Project, project_id).kb_version
            if current != version:
                raise ValueError("知识库版本已变化，请重新提交")
        cache = get_cache()
        cached = None if bypass else cache.get("llm:" + context_hash)
        if cached:
            chunks, raw = cached["chunks"], cached["raw"]
            cache_hit = True
        else:
            check_deadline("检索前")
            started = time.monotonic()
            chunks, _ = retrieve_for_task(project_id, query, version, deadline=deadline)
            chunks = merge_chunks(memory.get("previous_chunks", []), chunks, settings.MAX_CONTEXT_CHARS)
            if not chunks:
                raise ValueError("没有可用的知识片段，请先上传文档")
            record_usage(task_id, "retrieval_embedding", settings.EMBEDDING_BACKEND,
                         (time.monotonic() - started) * 1000,
                         detail={"chunk_count": len(chunks)})
            check_deadline("生成前")
            started = time.monotonic()
            from app.agent import run_tools
            client = get_llm_client()
            chunks, tool_context = run_tools(client, project_id, query, version, chunks, memory, deadline,
                lambda stage, usage, detail: record_usage(task_id, stage, settings.LLM_BACKEND,
                    usage.get("latency_ms", 0), prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"), total_tokens=usage.get("total_tokens"), detail=detail))
            raw, llm_usage = client.chat_with_usage(system_prompt(), build_user_prompt(query, chunks, memory, tool_context),
                                                              deadline=deadline)
            record_usage(task_id, "llm", settings.LLM_BACKEND,
                         llm_usage.get("latency_ms", (time.monotonic() - started) * 1000),
                         http_status=llm_usage.get("http_status"), prompt_tokens=llm_usage.get("prompt_tokens"),
                         completion_tokens=llm_usage.get("completion_tokens"), total_tokens=llm_usage.get("total_tokens"),
                         detail={"model": llm_usage.get("model")})
            cache_hit = False
        check_deadline("校验前")
        parsed, parse_errors, _ = validate_llm_output(raw, [c["chunk_id"] for c in chunks] + ["USER_INPUT"])
        if parsed is None:
            raise ValueError("; ".join(parse_errors[:5]))
        draft, errors, warnings, calculation = _evaluate(parsed, chunks, source_input)
        with SessionLocal.begin() as db:
            if db.get(Project, project_id).kb_version != version:
                raise ValueError("生成期间知识库版本已变化，请重新提交")
            # lease_token围栏：租约被回收后，过期worker的结果不允许覆盖新执行。
            count = db.query(GenerationTask).filter_by(id=task_id, status="RUNNING", lease_token=token).update({
                "result_json": draft, "raw_response": raw, "retrieved_chunks": chunks,
                "validation_errors": errors, "warnings": warnings, "calculation": calculation,
                "cache_hit": int(cache_hit), "revision": 1,
                "status": "NEEDS_REVIEW" if errors else "NEEDS_INPUT" if draft["open_questions"] else "READY_FOR_REVIEW",
                "finished_at": datetime.utcnow(), "lease_until": None, "lease_token": None})
        if count != 1:
            return
        if not errors and not draft["open_questions"]:
            cache.set("llm:" + context_hash, {"chunks": chunks, "raw": raw}, settings.RESPONSE_CACHE_TTL)
    except Exception as exc:
        fail(str(exc))
        record_usage(task_id, "task_deadline" if isinstance(exc, DeadlineExceeded) else "task_failed",
                     "pipeline", (time.monotonic() - started_total) * 1000, detail={"error": str(exc)[:200]})
    finally:
        heartbeat.stop()


def update_draft(task_id, value, revision):
    with SessionLocal() as db:
        task = db.get(GenerationTask, task_id)
        if task is None:
            raise BusinessError("任务不存在", 404)
        if task.status not in {"READY_FOR_REVIEW", "NEEDS_INPUT", "NEEDS_REVIEW"}:
            raise BusinessError("当前状态不能修改草稿", 409)
        chunks = task.retrieved_chunks
        query = load_context(db, task_id).get("source_input", task.input_text)
    draft, errors, warnings, calculation = _evaluate(value, chunks, query)
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
        draft, errors, _, _ = _evaluate(task.result_json, task.retrieved_chunks,
            load_context(db, task_id).get("source_input", task.input_text))
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
