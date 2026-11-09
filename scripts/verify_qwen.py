"""Two small, explicitly authorized API calls. Never print credentials."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-paid", action="store_true", help="Authorize two small metered requests")
    args = parser.parse_args()
    if not args.allow_paid:
        raise SystemExit("No API calls made. Pass --allow-paid only after authorizing provider charges.")
    os.environ["MBSES_ENV_FILE"] = str(ROOT / ".env.real")
    os.environ["ALLOW_PAID_MODEL_CALLS"] = "true"
    from app.config import settings
    from app.embedding import QwenEmbedder
    from app.http_client import post_json
    settings.LLM_MAX_RETRIES = 0
    if not settings.QWEN_API_KEY:
        raise SystemExit("QWEN_API_KEY is missing; no requests made.")
    vectors = QwenEmbedder().embed(["电动牙刷自动刷牙需求", "电动牙刷防水要求"])
    response = post_json("/chat/completions", {"model": settings.QWEN_MODEL,
        "messages": [{"role": "system", "content": "只输出JSON。"},
                     {"role": "user", "content": '请返回JSON对象：{"status":"ok"}'}],
        "temperature": 0, "enable_thinking": False, "max_tokens": 64,
        "response_format": {"type": "json_object"}})
    content = response["choices"][0]["message"]["content"]
    if json.loads(content).get("status") != "ok":
        raise RuntimeError("Generation returned an unexpected JSON result")
    result = {"passed": True, "scope": "connectivity-only", "embedding_model": settings.QWEN_EMBED_MODEL,
        "vector_count": len(vectors), "dimensions": len(vectors[0]), "llm_model": settings.QWEN_MODEL,
        "llm_json_valid": True, "usage": response.get("usage"),
        "note": "Two short texts and one short generation; not an MBSE accuracy evaluation. Retries disabled."}
    output = ROOT / "artifacts" / "qwen-connectivity.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
