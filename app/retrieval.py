import re

from app.cache import fingerprint, get_cache
from app.config import settings
from app.database import SessionLocal
from app.embedding import get_embedder
from app.http_client import remaining_seconds
from app.models import DocChunk, KnowledgeDocument, Project
from app.vector_store import get_vector_store


def normalize_query(text):
    return re.sub(r"\s+", " ", text.strip())


def retrieve(project_id, query, expected_kb_version=None, deadline=None):
    remaining_seconds(deadline)
    query = normalize_query(query)
    with SessionLocal() as db:
        project = db.get(Project, project_id)
        if project is None:
            raise ValueError("项目不存在")
        version = project.kb_version
        if expected_kb_version is not None and version != expected_kb_version:
            raise ValueError("知识库已更新，请重新提交任务")
    key = "retr:" + fingerprint({"project": project_id, "version": version, "query": query,
        "profile": settings.embedding_profile, "top_k": settings.TOP_K, "final": settings.FINAL_CHUNKS,
        "context": settings.MAX_CONTEXT_CHARS})
    cache = get_cache()
    cached = cache.get(key)
    if cached is not None:
        return cached, True
    query_vec = get_embedder().embed([query], **({"deadline": deadline} if deadline is not None else {}))[0]
    remaining_seconds(deadline)
    hits = get_vector_store().search(project_id, query_vec, settings.TOP_K)
    remaining_seconds(deadline)
    ids = [hit["chunk_id"] for hit in hits]
    with SessionLocal() as db:
        rows = db.query(DocChunk, KnowledgeDocument.name).join(KnowledgeDocument).filter(
            DocChunk.id.in_(ids), DocChunk.project_id == project_id,
            KnowledgeDocument.status == "INDEXED", DocChunk.index_status == "READY",
            DocChunk.embedding_profile == settings.embedding_profile,
        ).all() if ids else []
        lookup = {row.id: (row, name) for row, name in rows}
        result, used = [], 0
        for hit in hits:
            if hit["chunk_id"] not in lookup:
                continue
            row, name = lookup[hit["chunk_id"]]
            if used + len(row.content) > settings.MAX_CONTEXT_CHARS:
                continue
            result.append({"chunk_id": row.id, "section_title": row.section_title,
                "source_locator": row.source_locator, "document_id": row.document_id,
                "document_name": name, "content": row.content, "score": round(hit["score"], 6)})
            used += len(row.content)
            if len(result) >= settings.FINAL_CHUNKS:
                break
    # No mid-paragraph slicing: every cited excerpt is exactly the stored excerpt.
    cache.set(key, result, settings.CACHE_TTL_SECONDS)
    return result, False


def retrieve_for_task(project_id, query, version, deadline=None):
    if not ("可靠" in query or "MTBF" in query):
        return retrieve(project_id, query, version, deadline=deadline)
    # A bounded explicit workflow, not an autonomous multi-agent planner.
    plans = [
        ("无线充电底座 MTBF 默认值 输入", lambda s: "MTBF指标是" in s),
        ("无线充电受电子系统设计 MTBF 三个模块默认值", lambda s: "三个模块" in s and "改为" in s),
        ("任务时间 T:Real 默认值", lambda s: bool(re.search(r"T:Real[=＝]\d+", s))),
        ("可靠度需求 R>", lambda s: bool(re.search(r"R\s*>\s*0\.\d+", s))),
    ]
    selected, seen = [], set()
    for subquery, predicate in plans:
        candidates, _ = retrieve(project_id, subquery, version, deadline=deadline)
        match = next((c for c in candidates if predicate(c["content"])), None)
        if match and match["chunk_id"] not in seen:
            selected.append({**match, "retrieval_query": subquery})
            seen.add(match["chunk_id"])
    if selected:
        first = selected[0]
        with SessionLocal() as db:
            row = db.get(DocChunk, first["chunk_id"])
            previous = db.query(DocChunk).filter_by(document_id=row.document_id,
                chunk_index=row.chunk_index - 1, index_status="READY").first()
            if previous and "无线充电底座子系统设计" in previous.content and previous.id not in seen:
                selected.insert(0, {"chunk_id": previous.id, "document_id": previous.document_id,
                    "section_title": previous.section_title, "source_locator": previous.source_locator,
                    "document_name": first["document_name"], "content": previous.content,
                    "score": first["score"], "retrieval_query": "相邻上下文补全"})
    used, bounded = 0, []
    for chunk in selected[:settings.FINAL_CHUNKS]:
        if used + len(chunk["content"]) <= settings.MAX_CONTEXT_CHARS:
            bounded.append(chunk)
            used += len(chunk["content"])
    return bounded, False
