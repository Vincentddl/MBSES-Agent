"""Read-only infrastructure probes: never perform paid model requests."""
from sqlalchemy import text
from app.config import settings
from app.database import engine


def inspect_services():
    result = {}
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        result["database"] = {"state": "ok", "backend": engine.dialect.name}
    except Exception as exc:
        result["database"] = {"state": "unavailable", "error_type": type(exc).__name__}
    if settings.CACHE_BACKEND == "redis":
        try:
            import redis
            client = redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=2, socket_timeout=2)
            try:
                client.ping()
            finally:
                client.close()
            result["cache"] = {"state": "ok", "backend": "redis"}
        except Exception as exc:
            result["cache"] = {"state": "unavailable", "error_type": type(exc).__name__}
    else:
        result["cache"] = {"state": "local", "backend": "memory"}
    if settings.VECTOR_BACKEND == "milvus":
        try:
            from pymilvus import MilvusClient
            client = MilvusClient(uri=settings.MILVUS_URI, timeout=5)
            try:
                client.list_collections(timeout=5)
            finally:
                client.close()
            result["vector_store"] = {"state": "ok", "backend": "milvus"}
        except Exception as exc:
            result["vector_store"] = {"state": "unavailable", "error_type": type(exc).__name__}
    else:
        result["vector_store"] = {"state": "local", "backend": "numpy"}
    for key, backend in [("llm", settings.LLM_BACKEND), ("embedding", settings.EMBEDDING_BACKEND)]:
        state = "simulation" if backend == "mock" else "missing_api_key" if not settings.QWEN_API_KEY else "billing_disabled" if not settings.ALLOW_PAID_MODEL_CALLS else "configured_not_probed"
        result[key] = {"state": state, "backend": backend}
    ready = all(value["state"] in {"ok", "local", "simulation", "configured_not_probed"} for value in result.values())
    return {"ready": ready, "services": result,
            "note": "没有调用千问；configured_not_probed只代表已配置，不代表API联调成功。"}
