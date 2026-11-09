"""Real MySQL/Redis/Milvus acceptance test. Qwen calls are deliberately disabled."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.case_source import default_case_source


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=default_case_source())
    args = parser.parse_args()
    os.environ["MBSES_ENV_FILE"] = str(ROOT / ".env.real")
    os.environ.update(LLM_BACKEND="mock", EMBEDDING_BACKEND="mock", ALLOW_PAID_MODEL_CALLS="false")
    from app.config import settings
    from app.database import SessionLocal, engine
    from app.main import app
    from app.models import DocChunk, ModelElement, ModelRelationship
    from app.cache import get_cache
    from app.vector_store import get_vector_store
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    assert engine.dialect.name == "mysql" and settings.CACHE_BACKEND == "redis" and settings.VECTOR_BACKEND == "milvus"
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        def call(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()
        assert call("GET", "/api/readiness")["ready"]
        project = call("POST", "/api/projects", json={"name": "真实中间件验收（模型模拟）", "description": "真实MySQL/Redis/Milvus，模型与向量化为显式模拟，不冒充千问联调。"})
        pid = project["id"]
        upload = call("POST", "/api/knowledge/upload", data={"project_id": pid}, files={"file": (args.source.name, args.source.read_bytes())})
        assert upload["chunk_count"] == 88
        body = {"project_id": pid, "input_text": "提取电动牙刷的自动刷牙、更换刷头、质量要求、防水需求，以及刷牙、充电、更换刷头三个用例和模块结构。", "idempotency_key": "infra-test"}

        def generate(payload):
            task_id = call("POST", "/api/generate", json=payload)["task_id"]
            for _ in range(300):
                task = call("GET", "/api/tasks/" + task_id)
                if task["status"] not in {"PENDING", "RUNNING"}:
                    assert task["status"] == "READY_FOR_REVIEW", task
                    return task
                time.sleep(.1)
            raise TimeoutError("Integration task exceeded 30 seconds")

        task = generate(body)
        confirmed = call("POST", f"/api/tasks/{task['id']}/confirm", json={"expected_revision": task["revision"]})
        cached = generate({**body, "idempotency_key": "infra-cache"})
        assert cached["cache_hit"]
        key = "mbses:acceptance:" + uuid.uuid4().hex
        cache = get_cache()
        cache.set(key, {"verified": True}, 60)
        assert cache.get(key) == {"verified": True}
        ttl = cache.client.ttl(key)
        assert 0 < ttl <= 60
        cache.delete(key)
        store = get_vector_store()
        indexed = store.client.query(collection_name=store.collection,
            filter=f'project_id == "{pid}"', output_fields=["count(*)"], consistency_level="Strong", timeout=10)
        assert indexed[0]["count(*)"] == 88
        with SessionLocal() as db:
            mysql_version = db.execute(text("SELECT VERSION()")).scalar_one()
            sql_chunks = db.query(DocChunk).filter_by(project_id=pid).count()
            sql_elements = db.query(ModelElement).filter_by(project_id=pid).count()
            sql_relations = db.query(ModelRelationship).filter_by(project_id=pid).count()
        assert sql_chunks == 88 and sql_elements == 11 and sql_relations == 4
        result = {"passed": True, "project_id": pid, "task_id": task["id"],
            "mysql": {"version": mysql_version, "chunks": sql_chunks, "elements": sql_elements, "relations": sql_relations},
            "redis": {"ping": cache.client.ping(), "write_read": True, "ttl": ttl, "response_cache_hit": cached["cache_hit"]},
            "milvus": {"version": store.client.get_server_version(), "collection": store.collection, "vectors": 88, "retrieved": len(task["retrieved_chunks"])},
            "qwen": {"called": False, "llm": "mock", "embedding": "mock"}, "confirmation": confirmed}
        output = ROOT / "artifacts" / "real-stack-integration.json"
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
