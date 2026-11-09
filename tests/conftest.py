import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_temporary = tempfile.TemporaryDirectory(prefix="mbses-tests-")
os.environ["DATA_DIR"] = _temporary.name
os.environ["DATABASE_URL"] = "sqlite:///" + (Path(_temporary.name) / "tests.db").as_posix()
for key, value in {"LLM_BACKEND": "mock", "EMBEDDING_BACKEND": "mock",
                   "VECTOR_BACKEND": "numpy", "CACHE_BACKEND": "memory"}.items():
    os.environ[key] = value

import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.database import engine


@pytest.fixture(scope="session", autouse=True)
def close_database_before_temp_cleanup():
    yield
    engine.dispose()
    _temporary.cleanup()


@pytest.fixture
def client():
    with TestClient(app, client=("127.0.0.1", 50000)) as value:
        yield value


@pytest.fixture
def project(client):
    return client.post("/api/projects", json={"name": "测试项目"}).json()["id"]


@pytest.fixture
def draft():
    return {"meta_info": {"summary": "测试"}, "model_elements": [
        {"localId": "root", "kind": "Requirement", "name": "系统需求", "namespace": "req",
         "description": "需求", "properties": {}, "evidence_chunk_ids": ["USER_INPUT"]},
        {"localId": "child", "kind": "Requirement", "name": "质量要求", "namespace": "req",
         "description": "重量不超过200克", "properties": {"value": 200, "unit": "g", "operator": "<="},
         "evidence_chunk_ids": ["USER_INPUT"]}],
        "sysml_relationships": [{"source": "root", "relType": "CONTAINS", "target": "child"}],
        "open_questions": [], "evidence": ["USER_INPUT"]}
