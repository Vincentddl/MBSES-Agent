import copy
import time

from app.database import SessionLocal
from app.models import GenerationTask, ModelElement, Project
from app.retrieval import retrieve


TEXT = "自动刷牙，需求编号1.1.1。更换刷头，需求编号1.1.2。质量要求，重量不超过200克。防水，可用水冲洗整个牙刷。"


def upload(client, project, text=TEXT):
    response = client.post("/api/knowledge/upload", data={"project_id": project}, files={"file": ("case.txt", text.encode())})
    assert response.status_code == 200, response.text
    return response.json()


def generate(client, project, key, query="提取自动刷牙、更换刷头、质量要求和防水需求"):
    response = client.post("/api/generate", json={"project_id": project, "input_text": query, "idempotency_key": key})
    assert response.status_code == 202
    tid = response.json()["task_id"]
    for _ in range(100):
        task = client.get("/api/tasks/" + tid).json()
        if task["status"] not in {"PENDING", "RUNNING"}:
            return task
        time.sleep(.02)
    raise TimeoutError()


def test_full_review_cache_idempotency_and_atomic_persistence(client, project):
    original = upload(client, project)
    assert upload(client, project)["deduplicated"]
    task = generate(client, project, "one")
    assert task["status"] == "READY_FOR_REVIEW", task
    assert not client.get("/api/elements/" + project).json()["elements"]
    assert generate(client, project, "one")["id"] == task["id"]
    conflict = client.post("/api/generate", json={"project_id": project, "input_text": "不同的输入需求", "idempotency_key": "one"})
    assert conflict.status_code == 409
    cached = generate(client, project, "two")
    assert cached["cache_hit"]
    review = client.post(f"/api/tasks/{task['id']}/validate", json={"draft": task["result_json"], "expected_revision": 1}).json()
    assert review["valid"] and review["revision"] == 2
    assert client.post(f"/api/tasks/{task['id']}/confirm", json={"expected_revision": 1}).status_code == 409
    result = client.post(f"/api/tasks/{task['id']}/confirm", json={"expected_revision": 2}).json()
    assert result["confirmed"] == 5 and result["relations"] == 4
    assert client.post(f"/api/tasks/{task['id']}/confirm", json={"expected_revision": 2}).json()["idempotent"]
    # A different task with conflicting names rolls back every row and state update.
    assert client.post(f"/api/tasks/{cached['id']}/confirm", json={"expected_revision": 1}).status_code == 409
    with SessionLocal() as db:
        assert db.query(ModelElement).filter_by(task_id=cached["id"]).count() == 0
        assert db.get(GenerationTask, cached["id"]).status == "READY_FOR_REVIEW"
    upload(client, project, "新文档：独立参数内容")
    assert not generate(client, project, "three")["cache_hit"]


def test_invalid_edited_draft_blocks_confirmation(client, project):
    upload(client, project)
    task = generate(client, project, "invalid-edit")
    task["result_json"]["sysml_relationships"][0]["target"] = "missing"
    result = client.post(f"/api/tasks/{task['id']}/validate", json={"draft": task["result_json"], "expected_revision": 1}).json()
    assert not result["valid"] and result["status"] == "NEEDS_REVIEW"
    assert client.post(f"/api/tasks/{task['id']}/confirm", json={"expected_revision": 2}).status_code == 422
    assert not client.get("/api/elements/" + project).json()["elements"]


def test_confirmation_revalidates_persisted_draft(client, project):
    upload(client, project)
    task = generate(client, project, "commit-time-check")
    with SessionLocal.begin() as db:
        row = db.get(GenerationTask, task["id"])
        corrupted = copy.deepcopy(row.result_json)
        corrupted["sysml_relationships"][0]["target"] = "missing-endpoint"
        row.result_json = corrupted
    response = client.post(f"/api/tasks/{task['id']}/confirm", json={"expected_revision": task["revision"]})
    assert response.status_code == 422
    assert not client.get("/api/elements/" + project).json()["elements"]


def test_project_retrieval_isolation(client, project):
    upload(client, project)
    other = client.post("/api/projects", json={"name": "隔离项目"}).json()["id"]
    assert retrieve(other, "自动刷牙")[0] == []
    assert retrieve(project, "自动刷牙")[0]


def test_failed_vector_index_can_retry_same_document(client, project, monkeypatch):
    import app.services as module
    original = module.get_embedder
    class Broken:
        def embed(self, texts):
            raise ValueError("模拟向量服务失败")
    monkeypatch.setattr(module, "get_embedder", lambda: Broken())
    response = client.post("/api/knowledge/upload", data={"project_id": project}, files={"file": ("case.txt", TEXT.encode())})
    assert response.status_code == 422
    docs = client.get("/api/knowledge/" + project).json()
    assert len(docs) == 1 and docs[0]["status"] == "FAILED"
    assert retrieve(project, "自动刷牙")[0] == []
    monkeypatch.setattr(module, "get_embedder", original)
    result = upload(client, project)
    assert result["document_id"] == docs[0]["id"] and result["status"] == "INDEXED"


def test_no_document_task_fails_clearly(client, project):
    task = generate(client, project, "no-doc")
    assert task["status"] == "FAILED" and "没有可用" in task["error"]


def test_unavailable_worker_is_explicit_and_same_key_can_retry(client, project, monkeypatch):
    import app.services as module
    upload(client, project)
    query = "提取自动刷牙、更换刷头、质量要求和防水需求"
    with monkeypatch.context() as patch:
        patch.setattr(module, "_executor", None)
        response = client.post("/api/generate", json={"project_id": project, "input_text": query,
            "idempotency_key": "worker-retry"})
        assert response.status_code == 503
    task = generate(client, project, "worker-retry", query)
    assert task["status"] == "READY_FOR_REVIEW"


def test_invalid_file_is_user_error(client, project):
    response = client.post("/api/knowledge/upload", data={"project_id": project}, files={"file": ("broken.docx", b"not a zip")})
    assert response.status_code == 422


def test_invalid_pdf_is_user_error(client, project):
    response = client.post("/api/knowledge/upload", data={"project_id": project}, files={"file": ("broken.pdf", b"%PDF-1.7\ntruncated")})
    assert response.status_code == 422
    assert "损坏" in response.json()["detail"]


def test_missing_docx_parts_is_user_error(client, project):
    from io import BytesIO
    import zipfile
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("word/document.xml", "<document/>")
    response = client.post("/api/knowledge/upload", data={"project_id": project}, files={"file": ("broken.docx", stream.getvalue())})
    assert response.status_code == 422


def test_malformed_docx_xml_is_user_error(client, project):
    from io import BytesIO
    from docx import Document
    import zipfile
    source = BytesIO()
    Document().save(source)
    output = BytesIO()
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(output, "w") as target:
        for info in original.infolist():
            target.writestr(info.filename, b"<broken" if info.filename == "word/document.xml" else original.read(info))
    response = client.post("/api/knowledge/upload", data={"project_id": project}, files={"file": ("broken.docx", output.getvalue())})
    assert response.status_code == 422


def test_status_can_be_restored_via_api(client, project):
    upload(client, project)
    task = generate(client, project, "restore")
    restored = client.get("/api/tasks/" + task["id"]).json()
    assert restored["result_json"] == task["result_json"]
    assert client.get("/").status_code == 200


def test_blank_input_rejected(client, project):
    assert client.post("/api/projects", json={"name": "   "}).status_code == 422
    assert client.post("/api/generate", json={"project_id": project, "input_text": "   "}).status_code == 422


def test_original_docx_case(client, project):
    from pathlib import Path
    import pytest
    from scripts.case_source import default_case_source
    source = default_case_source()
    if not source.exists():
        pytest.skip("Original local case document not available")
    response = client.post("/api/knowledge/upload", data={"project_id": project},
        files={"file": (source.name, source.read_bytes())})
    parsed = response.json()
    assert response.status_code == 200 and parsed["chunk_count"] == 88
    task = generate(client, project, "actual-model", "提取电动牙刷的自动刷牙、更换刷头、质量要求、防水需求，以及刷牙、充电、更换刷头三个用例和模块结构。")
    assert task["status"] == "READY_FOR_REVIEW"
    assert len(task["result_json"]["model_elements"]) == 11
    assert {e["name"] for e in task["result_json"]["model_elements"] if e["kind"] == "UseCase"} == {"刷牙", "充电", "更换刷头"}
    mass = next(e for e in task["result_json"]["model_elements"] if e["name"] == "质量要求")
    assert mass["properties"]["value"] == 200 and mass["properties"]["operator"] == "<="
    reliability = generate(client, project, "actual-reliability", "复算案例可靠性，提取五个子系统MTBF、任务时间与可靠度阈值，保留来源和单位。")
    assert reliability["status"] == "READY_FOR_REVIEW", reliability
    assert reliability["calculation"]["system_reliability"] == pytest.approx(0.9138160420441955)
