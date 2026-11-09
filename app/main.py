from contextlib import asynccontextmanager
from pathlib import Path
from zipfile import BadZipFile
import ipaddress

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.database import create_all, get_db
from app.models import DocChunk, GenerationTask, KnowledgeDocument, ModelElement, ModelRelationship, Project
from app.schemas import ConfirmRequest, DraftUpdate, GenerateRequest, ProjectCreate
from app.readiness import inspect_services
from app.services import BusinessError, confirm_task, create_task, ingest_document, start_workers, stop_workers, submit_generation, update_draft


@asynccontextmanager
async def lifespan(app):
    create_all()
    start_workers()
    yield
    stop_workers()


app = FastAPI(title="MBSESAI Python辅助建模", version="0.2.0", lifespan=lifespan)


@app.middleware("http")
async def local_workbench_only(request, call_next):
    # Projects organize one operator's files; they are not tenant/auth boundaries.
    # Enforce the documented loopback-only boundary even if Uvicorn is misbound.
    try:
        local = request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        local = False
    if not local:
        return JSONResponse({"detail": "本工作台仅允许本机访问；多用户部署必须先添加身份认证和项目授权。"}, status_code=403)
    return await call_next(request)


@app.exception_handler(BusinessError)
def business_error(request, exc):
    return JSONResponse(status_code=exc.status, content={"detail": str(exc)})


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/health")
def health():
    missing = not settings.QWEN_API_KEY or not settings.ALLOW_PAID_MODEL_CALLS
    mode = "模型模拟" if settings.LLM_BACKEND == "mock" else "真实技术栈（模型待配置）" if missing else "真实技术栈（API待验证）"
    return {"status": "ok", "mode": mode,
        "backends": {"llm": settings.LLM_BACKEND, "embedding": settings.EMBEDDING_BACKEND,
            "vector_store": settings.VECTOR_BACKEND, "cache": settings.CACHE_BACKEND,
            "database": "sqlite" if settings.DATABASE_URL.startswith("sqlite") else "mysql"},
        "embedding_profile": settings.embedding_profile,
        "model_configuration": {"api_key_present": bool(settings.QWEN_API_KEY), "paid_calls_enabled": settings.ALLOW_PAID_MODEL_CALLS,
            "llm_model": settings.QWEN_MODEL, "embedding_model": settings.QWEN_EMBED_MODEL},
        "note": "后端模式为配置值；真实外部服务未在健康接口中调用。模拟模式不代表LLM质量。"}


@app.get("/api/readiness")
def readiness():
    result = inspect_services()
    return JSONResponse(result, status_code=200 if result["ready"] else 503)


@app.get("/api/projects")
def projects(db: Session = Depends(get_db)):
    return [{"id": p.id, "name": p.name, "description": p.description, "kb_version": p.kb_version}
            for p in db.query(Project).order_by(Project.created_at).all()]


@app.post("/api/projects", status_code=201)
def project_create(req: ProjectCreate, db: Session = Depends(get_db)):
    project = Project(name=req.name, description=req.description)
    db.add(project)
    db.commit()
    return {"id": project.id, "name": project.name}


@app.post("/api/knowledge/upload")
def upload(project_id: str = Form(...), file: UploadFile = File(...)):
    raw = file.file.read(settings.MAX_UPLOAD_BYTES + 1)
    try:
        return ingest_document(project_id, file.filename or "document.txt", raw)
    except BusinessError:
        raise
    except (ValueError, UnicodeError, BadZipFile) as exc:
        raise BusinessError(str(exc)) from exc


@app.get("/api/knowledge/{project_id}")
def knowledge(project_id: str, db: Session = Depends(get_db)):
    return [{"id": d.id, "name": d.name, "status": d.status, "chunk_count": d.chunk_count,
             "parse_info": d.parse_info, "error": d.error}
            for d in db.query(KnowledgeDocument).filter_by(project_id=project_id).all()]


@app.post("/api/generate", status_code=202)
def generate(req: GenerateRequest):
    task_id, status, created = create_task(req.project_id, req.input_text, req.idempotency_key, req.bypass_cache)
    if created or status == "PENDING":
        submit_generation(task_id)
    return {"task_id": task_id, "status": status, "deduplicated": not created}


@app.get("/api/tasks/{task_id}")
def task_get(task_id: str, db: Session = Depends(get_db)):
    task = db.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    return {"id": task.id, "project_id": task.project_id, "status": task.status,
        "input_text": task.input_text, "result_json": task.result_json,
        "validation_errors": task.validation_errors, "warnings": task.warnings,
        "revision": task.revision, "retrieved_chunks": task.retrieved_chunks,
        "calculation": task.calculation, "error": task.error, "cache_hit": bool(task.cache_hit),
        "kb_version": task.kb_version, "created_at": task.created_at, "finished_at": task.finished_at}


@app.post("/api/tasks/{task_id}/validate")
def validate(task_id: str, req: DraftUpdate):
    return update_draft(task_id, req.draft, req.expected_revision)


@app.post("/api/tasks/{task_id}/confirm")
def confirm(task_id: str, req: ConfirmRequest):
    return confirm_task(task_id, req.expected_revision)


@app.get("/api/tasks/{task_id}/export")
def export(task_id: str, db: Session = Depends(get_db)):
    task = db.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    return JSONResponse({"format": "MBSESAI internal draft", "sysml_tool_validated": False,
        "status": task.status, "draft": task.result_json, "sources": task.retrieved_chunks,
        "calculation": task.calculation}, headers={"Content-Disposition": f'attachment; filename="model-{task_id}.json"'})


@app.get("/api/elements/{project_id}")
def elements(project_id: str, db: Session = Depends(get_db)):
    rows = db.query(ModelElement).filter_by(project_id=project_id).all()
    rels = db.query(ModelRelationship).filter_by(project_id=project_id).all()
    return {"elements": [{"id": row.id, "kind": row.kind, "name": row.name,
        "namespace": row.namespace, "description": row.description, "properties": row.properties,
        "evidence_chunk_ids": row.evidence_chunk_ids, "status": row.status, "task_id": row.task_id} for row in rows],
        "relations": [{"id": row.id, "rel_type": row.rel_type, "source_id": row.source_id,
                       "target_id": row.target_id} for row in rels]}


@app.get("/api/chunks/{chunk_id}")
def chunk_get(chunk_id: str, db: Session = Depends(get_db)):
    row = db.get(DocChunk, chunk_id)
    if row is None:
        raise HTTPException(404, "片段不存在")
    return {"chunk_id": row.id, "section_title": row.section_title, "content": row.content,
            "source_locator": row.source_locator, "project_id": row.project_id}
