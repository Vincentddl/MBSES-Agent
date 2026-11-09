"""Metered case run: real infrastructure, real Qwen, no automatic confirmation."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.case_source import default_case_source


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-paid", action="store_true")
    parser.add_argument("--resume", action="store_true", help="Use the remaining budget from the saved report")
    parser.add_argument("--source", type=Path, default=default_case_source())
    args = parser.parse_args()
    if not args.allow_paid:
        raise SystemExit("No API calls made. Explicit --allow-paid is required.")
    os.environ.update(MBSES_ENV_FILE=str(ROOT / ".env.real"), ALLOW_PAID_MODEL_CALLS="true",
                      LLM_BACKEND="qwen", EMBEDDING_BACKEND="qwen", LLM_MAX_RETRIES="0")
    if args.resume:
        os.environ.update(LLM_TIMEOUT_SECONDS="180", LLM_MAX_OUTPUT_TOKENS="4096")
    from app.config import settings
    from app.database import engine
    from app.main import app
    from fastapi.testclient import TestClient
    import requests
    if not settings.QWEN_API_KEY:
        raise SystemExit("Missing API Key; no calls made.")
    assert engine.dialect.name == "mysql" and settings.CACHE_BACKEND == "redis" and settings.VECTOR_BACKEND == "milvus"
    counters = {"embedding": 0, "generation": 0}
    usage, tasks = [], []
    original_post = requests.post

    def metered_post(url, **kwargs):
        kind = "embedding" if url.endswith("/embeddings") else "generation"
        limit = 16 if kind == "embedding" else 2
        if counters[kind] >= limit:
            raise RuntimeError("Authorized API request budget exhausted")
        counters[kind] += 1
        response = original_post(url, **kwargs)
        entry = {"kind": kind, "http_status": response.status_code}
        if response.ok:
            entry["usage"] = response.json().get("usage", {})
        usage.append(entry)
        return response

    requests.post = metered_post
    report = {"mode": "real-qwen-case", "source": args.source.name, "calls": counters,
        "request_usage": usage, "tasks": tasks, "confirmed_automatically": False,
        "note": "真实模型候选草稿，须人工审核；不承诺案例图像中的全量模型恢复。"}
    if args.resume:
        previous = json.loads((ROOT / "artifacts" / "qwen-case-report.json").read_text(encoding="utf-8"))
        if previous.get("mode") != "real-qwen-case" or previous["calls"]["generation"] != 1:
            raise SystemExit("Resume requires exactly one previously attempted generation. No calls made.")
        counters.update(previous["calls"])
        usage.extend(previous["request_usage"])
        tasks.extend(previous["tasks"])
        report.update(project_id=previous["project_id"], upload=previous["upload"], resumed=True)
    try:
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            def call(method, path, **kwargs):
                response = client.request(method, path, **kwargs)
                if not response.is_success:
                    raise RuntimeError(f"Application HTTP {response.status_code}: {response.json().get('detail', '')}")
                return response.json()
            if args.resume:
                pid = report["project_id"]
            else:
                project = call("POST", "/api/projects", json={"name": "电动牙刷真实千问案例", "description": "MySQL + Redis + Milvus + Qwen，模型输出待人工审核。"})
                pid = report["project_id"] = project["id"]
                report["upload"] = call("POST", "/api/knowledge/upload", data={"project_id": pid},
                    files={"file": (args.source.name, args.source.read_bytes())})
            queries = ["仅依据本项目电动牙刷原文，提取自动刷牙、更换刷头、质量要求、防水需求，以及刷牙、充电、更换刷头三个用例和有依据的结构模块。质量数值保留比较符和单位。不推断原文没有的功能，不超过20个模型元素。",
                "复算电动牙刷案例可靠性。提取五个子系统的MTBF、任务时间和可靠度阈值，保留各自原文依据。MTBF属性properties使用parameter_role=mtbf,component=子系统名称,value=小时数,unit=h；任务时间使用parameter_role=task_hours,value=小时数,unit=h；阈值使用parameter_role=reliability_threshold,operator=>,value=阈值,unit=1。不要把复制前的旧默认值当成受电子系统的最终值。缺少依据写入open_questions，不猜测。"]
            for index, query in enumerate(queries):
                if args.resume and index == 0:
                    continue
                pending = call("POST", "/api/generate", json={"project_id": pid, "input_text": query,
                    "idempotency_key": f"qwen-live-{index}", "bypass_cache": True})
                task_id = pending["task_id"]
                deadline = time.monotonic() + 360
                while True:
                    task = call("GET", "/api/tasks/" + task_id)
                    if task["status"] not in {"PENDING", "RUNNING"}:
                        break
                    if time.monotonic() > deadline:
                        raise TimeoutError("Task polling deadline exceeded; do not repeat automatically")
                    time.sleep(.2)
                tasks.append(task)
                if task["status"] == "FAILED":
                    report["error"] = task["error"]
                    break
            report["completed"] = len(tasks) == 2 and all(t["status"] != "FAILED" for t in tasks)
            report["partial_success"] = any(t["result_json"] and not t["validation_errors"] for t in tasks)
    except Exception as exc:
        report["completed"] = False
        report["error"] = str(exc)
    finally:
        requests.post = original_post
        output = ROOT / "artifacts" / "qwen-case-report.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"completed": report["completed"], "project_id": report.get("project_id"), "calls": counters,
        "tasks": [{"id": t["id"], "status": t["status"], "elements": len((t["result_json"] or {}).get("model_elements", [])),
                   "errors": t["validation_errors"], "calculation": t["calculation"]} for t in tasks],
        "error": report.get("error"), "report": str(output)}, ensure_ascii=False, indent=2))
    if not report["completed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
