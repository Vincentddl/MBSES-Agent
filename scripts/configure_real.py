"""Create a private real-stack profile without overwriting existing credentials."""
import secrets
from pathlib import Path
from dotenv import dotenv_values, set_key

ROOT = Path(__file__).resolve().parents[1]


def main():
    path = ROOT / ".env.real"
    if path.exists():
        existing = dotenv_values(path)
        required = ["MYSQL_PASSWORD", "MYSQL_ROOT_PASSWORD", "REDIS_PASSWORD", "DATABASE_URL", "REDIS_URL"]
        missing = [key for key in required if not existing.get(key)]
        if missing:
            raise SystemExit("Incomplete .env.real; missing: " + ", ".join(missing) +
                             ". Existing credentials were not changed. Restore the matching profile; do not regenerate passwords for existing data volumes.")
        added = False
        for key, value in {"MINIO_ROOT_USER": "mbses" + secrets.token_hex(6),
                           "MINIO_ROOT_PASSWORD": secrets.token_hex(24),
                           "LLM_TIMEOUT_SECONDS": "180", "LLM_MAX_RETRIES": "0",
                           "LLM_MAX_OUTPUT_TOKENS": "4096"}.items():
            if not existing.get(key):
                set_key(path, key, value)
                added = True
        print("Existing credentials preserved; missing storage credentials added." if added else
              ".env.real already exists; credentials were not changed.")
        return
    mysql, root, redis = (secrets.token_hex(24) for _ in range(3))
    values = {
        "STACK_PROFILE": "real", "DATA_DIR": (ROOT / ".data" / "real").as_posix(),
        "MYSQL_PASSWORD": mysql, "MYSQL_ROOT_PASSWORD": root, "REDIS_PASSWORD": redis,
        "MINIO_ROOT_USER": "mbses" + secrets.token_hex(6), "MINIO_ROOT_PASSWORD": secrets.token_hex(24),
        "DATABASE_URL": f"mysql+pymysql://mbses:{mysql}@127.0.0.1:3307/mbses?charset=utf8mb4",
        "CACHE_BACKEND": "redis", "REDIS_URL": f"redis://:{redis}@127.0.0.1:16379/0",
        "VECTOR_BACKEND": "milvus", "MILVUS_URI": "http://127.0.0.1:19531",
        "MILVUS_COLLECTION": "mbses_kb_chunk_real_v1", "LLM_BACKEND": "qwen",
        "EMBEDDING_BACKEND": "qwen", "QWEN_API_KEY": "", "ALLOW_PAID_MODEL_CALLS": "false",
        "QWEN_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "QWEN_MODEL": "qwen-plus", "QWEN_EMBED_MODEL": "text-embedding-v4",
        "QWEN_OUTPUT_MODE": "json_object", "EMBED_DIM": "1024",
        "LLM_TIMEOUT_SECONDS": "180", "LLM_MAX_RETRIES": "0", "LLM_MAX_OUTPUT_TOKENS": "4096",
        "TOP_K": "12", "FINAL_CHUNKS": "6", "TEMPERATURE": "0.1",
    }
    with path.open("x", encoding="utf-8") as output:
        output.write("# Local-only credentials. Never commit or share this file.\n")
        output.write("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
    print("Created .env.real with random local passwords. API key remains empty; paid calls disabled.")


if __name__ == "__main__":
    main()
