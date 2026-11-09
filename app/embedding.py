import hashlib
import math
from functools import lru_cache

from app.config import settings
from app.http_client import post_json


class MockEmbedder:
    """Deterministic lexical hashing, NOT a semantic embedding quality evaluation."""
    def embed(self, texts):
        result = []
        for text in texts:
            vec = [0.0] * settings.EMBED_DIM
            text = "".join(text.lower().split())
            for old, new in [("水冲洗", "防水"), ("水洗", "防水"), ("清洗", "防水"), ("重量", "质量")]:
                text = text.replace(old, new)
            for size in (1, 2):
                for index in range(len(text) - size + 1):
                    term = text[index:index + size]
                    bucket = int(hashlib.sha256(term.encode()).hexdigest()[:8], 16) % len(vec)
                    vec[bucket] += 0.25 if size == 1 else 1.0
            norm = math.sqrt(sum(x * x for x in vec)) or 1
            result.append([x / norm for x in vec])
        return result


class QwenEmbedder:
    def embed(self, texts):
        result = []
        for start in range(0, len(texts), settings.EMBED_BATCH_SIZE):
            batch = texts[start:start + settings.EMBED_BATCH_SIZE]
            response = post_json("/embeddings", {"model": settings.QWEN_EMBED_MODEL,
                                 "input": batch, "dimensions": settings.EMBED_DIM})
            data = response.get("data", [])
            if sorted(item.get("index", -1) for item in data) != list(range(len(batch))):
                raise RuntimeError("Embedding返回数量或索引不匹配")
            for item in sorted(data, key=lambda item: item["index"]):
                vector = item["embedding"]
                if len(vector) != settings.EMBED_DIM or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in vector):
                    raise RuntimeError("Embedding维度不一致或含非有限值")
                result.append([float(x) for x in vector])
        return result


@lru_cache(maxsize=1)
def get_embedder():
    return QwenEmbedder() if settings.EMBEDDING_BACKEND == "qwen" else MockEmbedder()
