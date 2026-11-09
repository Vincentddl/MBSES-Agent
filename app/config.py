"""Offline-first configuration; external services are explicitly opt-in."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(Path(os.getenv("MBSES_ENV_FILE", str(ROOT / ".env"))))


class Settings:
    DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT / ".data"))).resolve()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{(DATA_DIR / 'mbses.db').as_posix()}")
    CACHE_BACKEND = os.getenv("CACHE_BACKEND", "memory")
    REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    VECTOR_BACKEND = os.getenv("VECTOR_BACKEND", "numpy")
    MILVUS_URI = os.getenv("MILVUS_URI", "http://127.0.0.1:19530")
    MILVUS_COLLECTION = os.getenv("MILVUS_COLLECTION", "mbses_kb_chunk_v2")
    LLM_BACKEND = os.getenv("LLM_BACKEND", "mock")
    EMBEDDING_BACKEND = os.getenv("EMBEDDING_BACKEND", "mock")
    QWEN_API_KEY = os.getenv("QWEN_API_KEY", "")
    ALLOW_PAID_MODEL_CALLS = os.getenv("ALLOW_PAID_MODEL_CALLS", "false").lower() == "true"
    QWEN_BASE_URL = os.getenv("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    QWEN_MODEL = os.getenv("QWEN_MODEL", "qwen-plus")
    QWEN_EMBED_MODEL = os.getenv("QWEN_EMBED_MODEL", "text-embedding-v4")
    QWEN_OUTPUT_MODE = os.getenv("QWEN_OUTPUT_MODE", "json_object")
    EMBED_DIM = int(os.getenv("EMBED_DIM", "1024"))
    EMBED_BATCH_SIZE = int(os.getenv("EMBED_BATCH_SIZE", "10"))
    TEMPERATURE = float(os.getenv("TEMPERATURE", "0.1"))
    CONNECT_TIMEOUT = float(os.getenv("CONNECT_TIMEOUT", "3"))
    LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "60"))
    LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "2"))
    LLM_MAX_OUTPUT_TOKENS = int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "8192"))
    TOP_K = int(os.getenv("TOP_K", "12"))
    FINAL_CHUNKS = int(os.getenv("FINAL_CHUNKS", "6"))
    CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "600"))
    RESPONSE_CACHE_TTL = int(os.getenv("RESPONSE_CACHE_TTL", "1800"))
    CHUNK_TARGET_CHARS = int(os.getenv("CHUNK_TARGET_CHARS", "600"))
    CHUNK_OVERLAP_CHARS = int(os.getenv("CHUNK_OVERLAP_CHARS", "80"))
    MAX_CONTEXT_CHARS = int(os.getenv("MAX_CONTEXT_CHARS", "12000"))
    MAX_UPLOAD_BYTES = 30 * 1024 * 1024
    MAX_ELEMENTS = 80
    PARSER_VERSION = "docx-order-v2"
    PROMPT_VERSION = "mbse-draft-v2"
    SCHEMA_VERSION = "2.0"
    TASK_LEASE_SECONDS = 600

    def __init__(self):
        for key, choices in {
            "CACHE_BACKEND": {"memory", "redis"}, "VECTOR_BACKEND": {"numpy", "milvus"},
            "LLM_BACKEND": {"mock", "qwen"}, "EMBEDDING_BACKEND": {"mock", "qwen"},
            "QWEN_OUTPUT_MODE": {"json_object", "json_schema"},
        }.items():
            if getattr(self, key) not in choices:
                raise ValueError(f"Unsupported {key}: {getattr(self, key)}")
        if not 0 <= self.CHUNK_OVERLAP_CHARS < self.CHUNK_TARGET_CHARS:
            raise ValueError("Chunk overlap must be smaller than target length")
        if not 1 <= self.FINAL_CHUNKS <= self.TOP_K or self.EMBED_DIM <= 0:
            raise ValueError("Invalid retrieval or vector dimensions")
        if self.EMBED_BATCH_SIZE < 1 or self.MAX_CONTEXT_CHARS < 1:
            raise ValueError("Batch and context limits must be positive")
        if self.CONNECT_TIMEOUT <= 0 or self.LLM_TIMEOUT_SECONDS <= 0 or not 0 <= self.LLM_MAX_RETRIES <= 5:
            raise ValueError("Invalid timeout or retry limit")
        if not 64 <= self.LLM_MAX_OUTPUT_TOKENS <= 32768:
            raise ValueError("Invalid output token limit")

    @property
    def embedding_profile(self):
        model = self.QWEN_EMBED_MODEL if self.EMBEDDING_BACKEND == "qwen" else "lexical-hash-v2"
        return f"{self.EMBEDDING_BACKEND}:{model}:{self.EMBED_DIM}"


settings = Settings()
