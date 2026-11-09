from unittest.mock import Mock
import pytest


def test_paid_call_requires_explicit_consent(monkeypatch):
    import app.http_client as module
    monkeypatch.setattr(module.settings, "QWEN_API_KEY", "test-not-a-real-key")
    monkeypatch.setattr(module.settings, "ALLOW_PAID_MODEL_CALLS", False)
    post = Mock()
    monkeypatch.setattr(module.requests, "post", post)
    with pytest.raises(RuntimeError, match="ALLOW_PAID"):
        module.post_json("/embeddings", {})
    post.assert_not_called()


def test_real_profile_does_not_fall_back_when_key_missing(client, project, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "LLM_BACKEND", "qwen")
    monkeypatch.setattr(settings, "EMBEDDING_BACKEND", "qwen")
    monkeypatch.setattr(settings, "QWEN_API_KEY", "")
    response = client.post("/api/generate", json={"project_id": project, "input_text": "生成牙刷需求"})
    assert response.status_code == 503 and "不会回退" in response.json()["detail"]
    upload = client.post("/api/knowledge/upload", data={"project_id": project}, files={"file": ("a.txt", b"text")})
    assert upload.status_code == 503


def test_cloud_billing_disabled_is_explicit(client, project, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "LLM_BACKEND", "qwen")
    monkeypatch.setattr(settings, "QWEN_API_KEY", "test")
    monkeypatch.setattr(settings, "ALLOW_PAID_MODEL_CALLS", False)
    response = client.post("/api/generate", json={"project_id": project, "input_text": "生成牙刷需求"})
    assert response.status_code == 503 and "尚未授权" in response.json()["detail"]


def test_readiness_distinguishes_configuration_from_connectivity(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "LLM_BACKEND", "qwen")
    monkeypatch.setattr(settings, "QWEN_API_KEY", "")
    response = client.get("/api/readiness")
    assert response.status_code == 503
    assert response.json()["services"]["database"]["state"] == "ok"
    assert response.json()["services"]["llm"]["state"] == "missing_api_key"
    assert client.get("/api/health").json()["model_configuration"]["api_key_present"] is False


def test_redis_real_adapter_keeps_json_and_ttl():
    from app.cache import RedisCache
    adapter = RedisCache()
    adapter.client = Mock()
    adapter.set("test", {"value": 5}, 60)
    assert adapter.client.set.call_args.kwargs["ex"] == 60
    adapter.client.get.return_value = '{"value":5}'
    assert adapter.get("test") == {"value": 5}


def test_mysql_large_model_response_uses_longtext():
    from sqlalchemy.dialects.mysql import dialect
    from app.models import GenerationTask
    assert GenerationTask.__table__.c.raw_response.type.compile(dialect=dialect()) == "LONGTEXT"


@pytest.mark.parametrize("hit", [{"id": "abc", "distance": .9}, {"chunk_id": "abc", "distance": .9}])
def test_milvus_primary_key_formats(hit):
    from app.vector_store import MilvusVectorStore
    adapter = object.__new__(MilvusVectorStore)
    adapter.collection = "test"
    adapter.client = Mock()
    adapter.client.search.return_value = [[hit]]
    assert adapter.search("project", [1., 0.], 1) == [{"chunk_id": "abc", "score": .9}]


def test_configuration_preserves_key_and_existing_passwords(tmp_path, monkeypatch):
    import scripts.configure_real as module
    from dotenv import dotenv_values
    monkeypatch.setattr(module, "ROOT", tmp_path)
    path = tmp_path / ".env.real"
    path.write_text("QWEN_API_KEY=private-test-key\nMYSQL_PASSWORD=existing-password\nMYSQL_ROOT_PASSWORD=root-test\nREDIS_PASSWORD=redis-test\nDATABASE_URL=mysql+pymysql://example\nREDIS_URL=redis://example\n", encoding="utf-8")
    module.main()
    values = dotenv_values(path)
    assert values["QWEN_API_KEY"] == "private-test-key"
    assert values["MYSQL_PASSWORD"] == "existing-password"
    assert values["MINIO_ROOT_PASSWORD"] != "minioadmin" and len(values["MINIO_ROOT_PASSWORD"]) == 48
    module.main()
    assert dotenv_values(path) == values


def test_incomplete_existing_profile_does_not_rotate_secrets(tmp_path, monkeypatch):
    import scripts.configure_real as module
    monkeypatch.setattr(module, "ROOT", tmp_path)
    path = tmp_path / ".env.real"
    original = "QWEN_API_KEY=private-test-key\nMYSQL_PASSWORD=keep-me\n"
    path.write_text(original, encoding="utf-8")
    with pytest.raises(SystemExit, match="MYSQL_ROOT_PASSWORD"):
        module.main()
    assert path.read_text(encoding="utf-8") == original


def test_qwen_output_is_bounded_and_non_thinking(monkeypatch):
    import app.llm_client as module
    post = Mock(return_value={"choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}]})
    monkeypatch.setattr(module, "post_json", post)
    assert module.QwenLLMClient().chat("JSON", "JSON") == '{"ok":true}'
    payload = post.call_args.args[1]
    assert payload["max_tokens"] == module.settings.LLM_MAX_OUTPUT_TOKENS
    assert payload["enable_thinking"] is False


def test_nonlocal_clients_cannot_read_projects_or_chunks():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app, client=("203.0.113.10", 50000)) as client:
        assert client.get("/api/projects").status_code == 403
        assert client.get("/api/chunks/any-id").status_code == 403
        assert client.get("/api/health", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 403
