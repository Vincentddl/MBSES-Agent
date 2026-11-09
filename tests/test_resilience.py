"""缺口修复回归：租约恢复、任务总时限、关键事实对照、用量台账。"""
import copy
import time
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest

from app.database import SessionLocal
from app.models import GenerationTask, TaskUsage
from app.services import _LeaseHeartbeat, create_task, requeue_expired_tasks, run_generation
from app.validation import check_draft
from test_workflow import generate, upload


def running_task(project, key, lease_seconds=600):
    task_id, _, _ = create_task(project, "提取自动刷牙、更换刷头、质量要求和防水需求", key)
    with SessionLocal.begin() as db:
        db.query(GenerationTask).filter_by(id=task_id).update({
            "status": "RUNNING", "lease_token": "test-token",
            "lease_until": datetime.utcnow() + timedelta(seconds=lease_seconds)})
    return task_id


def test_reaper_requeues_expired_running_task(client, project):
    upload(client, project)
    task_id = running_task(project, "reap-expired", lease_seconds=-1)
    assert task_id in requeue_expired_tasks()
    with SessionLocal() as db:
        row = db.get(GenerationTask, task_id)
        assert row.status == "PENDING" and row.lease_until is None and row.lease_token is None


def test_reaper_spares_live_lease(client, project):
    upload(client, project)
    task_id = running_task(project, "reap-live", lease_seconds=600)
    assert task_id not in requeue_expired_tasks()
    with SessionLocal() as db:
        assert db.get(GenerationTask, task_id).status == "RUNNING"


def test_reclaimed_task_reruns_and_completes(client, project):
    upload(client, project)
    task_id = running_task(project, "reap-rerun", lease_seconds=-1)
    requeue_expired_tasks()
    run_generation(task_id)
    with SessionLocal() as db:
        row = db.get(GenerationTask, task_id)
        assert row.status == "READY_FOR_REVIEW"
        assert row.lease_token is None and row.lease_until is None


def test_heartbeat_renews_lease_only_for_token_owner(client, project, monkeypatch):
    from app.config import settings
    task_id = running_task(project, "heartbeat", lease_seconds=-10)
    monkeypatch.setattr(settings, "HEARTBEAT_INTERVAL_SECONDS", 0.05)
    heartbeat = _LeaseHeartbeat(task_id, "test-token")
    time.sleep(0.25)
    heartbeat.stop()
    with SessionLocal() as db:
        renewed = db.get(GenerationTask, task_id).lease_until
        assert renewed > datetime.utcnow()
    # token不匹配的续约不会生效：旧worker崩溃后新持有者不受旧心跳干扰。
    stale = _LeaseHeartbeat(task_id, "wrong-token")
    time.sleep(0.25)
    stale.stop()
    with SessionLocal() as db:
        assert db.get(GenerationTask, task_id).lease_until == renewed


def test_task_deadline_fails_fast_with_marker(client, project, monkeypatch):
    from app.config import settings
    upload(client, project)
    monkeypatch.setattr(settings, "TASK_DEADLINE_SECONDS", 0.0)
    task_id, _, _ = create_task(project, "提取自动刷牙、更换刷头、质量要求和防水需求", "deadline-1")
    run_generation(task_id)
    with SessionLocal() as db:
        row = db.get(GenerationTask, task_id)
        assert row.status == "FAILED" and "TASK_DEADLINE_EXCEEDED" in row.error


def test_http_client_aborts_past_deadline(monkeypatch):
    import app.http_client as module
    monkeypatch.setattr(module.settings, "QWEN_API_KEY", "test")
    monkeypatch.setattr(module.settings, "ALLOW_PAID_MODEL_CALLS", True)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    post = Mock(return_value=Mock(status_code=503, headers={}))
    monkeypatch.setattr(module.requests, "post", post)
    with pytest.raises(module.DeadlineExceeded):
        module.post_json("/chat/completions", {}, deadline=time.monotonic() - 1)
    post.assert_not_called()


def test_http_client_deadline_caps_read_timeout_and_retries(monkeypatch):
    import app.http_client as module
    monkeypatch.setattr(module.settings, "QWEN_API_KEY", "test")
    monkeypatch.setattr(module.settings, "ALLOW_PAID_MODEL_CALLS", True)
    sleeps = []
    monkeypatch.setattr(module.time, "sleep", lambda s: sleeps.append(s))
    post = Mock(return_value=Mock(status_code=503, headers={}))
    monkeypatch.setattr(module.requests, "post", post)
    deadline = time.monotonic() + 0.05
    with pytest.raises(RuntimeError):
        module.post_json("/chat/completions", {}, deadline=deadline)
    assert post.call_count >= 1
    assert all(s < module.settings.LLM_TIMEOUT_SECONDS for s in sleeps)


def test_fact_check_blocks_value_absent_from_evidence(draft):
    texts = {"c1": "质量要求：整机重量不超过200克。"}
    d = copy.deepcopy(draft)
    d["model_elements"][1]["evidence_chunk_ids"] = ["c1"]
    d["evidence"] = ["c1", "USER_INPUT"]
    value, errors, _ = check_draft(d, ["c1", "USER_INPUT"], evidence_texts=texts)
    assert value is not None and not errors
    d["model_elements"][1]["properties"]["value"] = 999
    _, errors, _ = check_draft(d, ["c1", "USER_INPUT"], evidence_texts=texts)
    assert any("FACT_UNSUPPORTED" in e for e in errors)


def test_fact_check_unit_missing_is_warning_only(draft):
    texts = {"c1": "质量要求：整机重量不超过200克。"}
    d = copy.deepcopy(draft)
    d["model_elements"][1]["evidence_chunk_ids"] = ["c1"]
    d["evidence"] = ["c1", "USER_INPUT"]
    d["model_elements"][1]["properties"]["unit"] = "kg"
    _, errors, warnings = check_draft(d, ["c1", "USER_INPUT"], evidence_texts=texts)
    assert not errors
    assert any("UNIT_NOT_FOUND" in w for w in warnings)


def test_fact_check_normalizes_fullwidth_and_thousands(draft):
    texts = {"c2": "重量不超过１，０００克"}
    d = copy.deepcopy(draft)
    d["model_elements"][1]["evidence_chunk_ids"] = ["c2"]
    d["evidence"] = ["c2"]
    d["model_elements"][1]["properties"] = {"value": 1000, "unit": "g", "operator": "<="}
    _, errors, _ = check_draft(d, ["c2"], evidence_texts=texts)
    assert not any("FACT_UNSUPPORTED" in e for e in errors)


def test_fact_check_user_input_can_support_value(draft):
    d = copy.deepcopy(draft)
    d["model_elements"][1]["evidence_chunk_ids"] = ["USER_INPUT"]
    d["model_elements"][1]["properties"]["value"] = 150
    texts = {"USER_INPUT": "重量不超过150克"}
    _, errors, _ = check_draft(d, ["USER_INPUT"], evidence_texts=texts)
    assert not errors


def test_fact_check_skipped_without_texts(draft):
    # 不传原文时保持旧语义（只校验证据ID合法性），兼容历史调用。
    value, errors, _ = check_draft(draft, ["USER_INPUT"])
    assert value is not None and not errors


def test_usage_ledger_written_for_pipeline(client, project):
    upload(client, project)
    task = generate(client, project, "usage-ledger")
    assert task["status"] == "READY_FOR_REVIEW", task
    rows = client.get(f"/api/tasks/{task['id']}/usage").json()
    stages = {r["stage"] for r in rows}
    assert "llm" in stages
    llm = next(r for r in rows if r["stage"] == "llm")
    assert llm["backend"] == "mock" and llm["cost_estimate"] is None
    assert llm["latency_ms"] >= 0


def test_usage_ledger_records_deadline_failure(client, project, monkeypatch):
    from app.config import settings
    upload(client, project)
    monkeypatch.setattr(settings, "TASK_DEADLINE_SECONDS", 0.0)
    task_id, _, _ = create_task(project, "提取自动刷牙、更换刷头、质量要求和防水需求", "usage-deadline")
    run_generation(task_id)
    rows = client.get(f"/api/tasks/{task_id}/usage").json()
    assert any(r["stage"] == "task_deadline" for r in rows)
