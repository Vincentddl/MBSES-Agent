"""Reproducible offline acceptance flow using the original DOCX and public API."""
import argparse
import json
import os
import sys
import time
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.case_source import default_case_source


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=default_case_source())
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts")
    args = parser.parse_args()
    # This command is explicitly offline, even when a user's .env enables cloud APIs.
    for key, value in {"LLM_BACKEND": "mock", "EMBEDDING_BACKEND": "mock",
                       "VECTOR_BACKEND": "numpy", "CACHE_BACKEND": "memory"}.items():
        os.environ[key] = value
    from fastapi.testclient import TestClient
    from app.main import app
    from app.config import settings
    if not settings.DATABASE_URL.startswith("sqlite"):
        raise SystemExit("Offline demo requires SQLite; unset DATABASE_URL or select a SQLite file.")
    started = time.perf_counter()
    args.output.mkdir(parents=True, exist_ok=True)

    def save(name, value):
        (args.output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        def request(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()

        project = request("POST", "/api/projects", json={"name": "电动牙刷案例完整演示", "description": "原文可追溯的离线流程验证；不是实际LLM效果评测。"})
        pid = project["id"]
        upload = request("POST", "/api/knowledge/upload", data={"project_id": pid},
            files={"file": (args.source.name, args.source.read_bytes(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
        repeated = request("POST", "/api/knowledge/upload", data={"project_id": pid},
            files={"file": (args.source.name, args.source.read_bytes())})
        assert repeated["deduplicated"] and repeated["document_id"] == upload["document_id"]

        def generate(text, key):
            response = request("POST", "/api/generate", json={"project_id": pid, "input_text": text, "idempotency_key": key})
            for _ in range(400):
                task = request("GET", "/api/tasks/" + response["task_id"])
                if task["status"] not in {"PENDING", "RUNNING"}:
                    return task
                time.sleep(0.05)
            raise TimeoutError("Offline task exceeded 20 seconds")

        query = "提取电动牙刷的自动刷牙、更换刷头、质量要求、防水需求，以及刷牙、充电、更换刷头三个用例和模块结构。"
        task = generate(query, "case-model-v2")
        assert task["status"] == "READY_FOR_REVIEW", task
        duplicate = request("POST", "/api/generate", json={"project_id": pid, "input_text": query, "idempotency_key": "case-model-v2"})
        assert duplicate["task_id"] == task["id"] and duplicate["deduplicated"]
        # Explicit review gate: revalidate first, then confirm this unchanged draft.
        review = request("POST", f"/api/tasks/{task['id']}/validate", json={"draft": task["result_json"], "expected_revision": task["revision"]})
        assert review["valid"]
        confirmed = request("POST", f"/api/tasks/{task['id']}/confirm", json={"expected_revision": review["revision"]})
        second_confirm = request("POST", f"/api/tasks/{task['id']}/confirm", json={"expected_revision": review["revision"]})
        assert second_confirm["idempotent"]
        cached = generate(query, "case-model-cache-v2")
        assert cached["cache_hit"]

        reliability = generate("复算案例可靠性，提取五个子系统MTBF、任务时间与可靠度阈值，保留来源和单位。", "case-reliability-v2")
        assert reliability["status"] == "READY_FOR_REVIEW", reliability
        calc = reliability["calculation"]
        assert calc and abs(calc["system_reliability"] - 0.913816042044195) < 1e-12
        assert calc["passed"]
        reliability_confirm = request("POST", f"/api/tasks/{reliability['id']}/confirm", json={"expected_revision": reliability["revision"]})
        formal = request("GET", "/api/elements/" + pid)
        counts = dict(Counter(e["kind"] for e in formal["elements"]))
        report = {"mode": "offline-rule-simulation", "source": str(args.source.resolve()),
            "project_id": pid, "model_task_id": task["id"], "reliability_task_id": reliability["id"],
            "parse": upload, "model_confirmation": confirmed, "reliability_confirmation": reliability_confirm,
            "confirmed_element_counts": counts, "confirmed_relations": len(formal["relations"]),
            "checks": {"duplicate_upload": True, "request_idempotency": True, "confirmation_idempotency": True,
                       "response_cache_hit": bool(cached["cache_hit"]), "draft_validation": review["valid"]},
            "reliability": calc, "elapsed_seconds": round(time.perf_counter() - started, 3),
            "boundaries": ["规则提取和哈希向量是离线替身，不是实际千问效果", "未做图片OCR，未恢复完整模型图",
                "输出为内部JSON草稿，未通过SysMLv2解析器或MBSES导入验证", "数据校验不等于工程语义正确，正式使用需要人工审核",
                "18需求、25模块、44视图、230关系等既往规模数字未在本次程序中重建，不列为成果"]}
        save("run-report.json", report)
        save("model-draft.json", task["result_json"])
        save("confirmed-model.json", formal)
        save("retrieval-trace.json", {"model": task["retrieved_chunks"], "reliability": reliability["retrieved_chunks"]})
        save("reliability.json", calc)
        summary = f"""# 电动牙刷案例运行报告

本报告由 scripts/run_demo.py 实际运行生成。模式：离线规则模拟。

## 实际流程
Word原文 → 结构切分 → 哈希向量 → 本地余弦检索 → 规则模拟JSON生成 → 校验 → 模拟审核确认 → SQLite持久化。

## 本次输出
- 原文段落：{upload['parse_info']['paragraphs']}；有效文字块：{upload['parse_info']['text_blocks']}；入库片段：{upload['chunk_count']}。
- 第一批模型：{confirmed['confirmed']}个元素、{confirmed['relations']}条关系。
- 可靠性参数批次：{reliability_confirm['confirmed']}个元素。
- 已确认类型统计：{json.dumps(counts, ensure_ascii=False)}。
- 重复上传、幂等提交、重复确认、响应缓存、草稿校验：均通过本次断言。
- 项目ID：`{pid}`。

## 独立可靠性复算
任务时间：{calc['task_hours']}小时。MTBF依次为：{[c['mtbf_hours'] for c in calc['components']]}小时。
失效率=1/MTBF；子系统R=exp(-失效率×时间)；串联系统R=各子系统R之积。
系统可靠度：**{calc['system_reliability']:.15f}**，满足严格大于{calc['threshold']}。
前提：指数失效、独立串联，按案例忽略刷柄和刷头结构件。不是实物试验结果。

## 验证边界
""" + "\n".join("- " + x for x in report["boundaries"]) + "\n"
        (args.output / "运行报告.md").write_text(summary, encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
