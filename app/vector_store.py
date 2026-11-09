from functools import lru_cache

import numpy as np

from app.config import settings
from app.database import SessionLocal
from app.models import DocChunk, KnowledgeDocument


class NumpyVectorStore:
    def upsert(self, records):
        with SessionLocal.begin() as db:
            for item in records:
                chunk = db.get(DocChunk, item["chunk_id"])
                if chunk is None:
                    raise ValueError("Chunk必须先持久化")
                chunk.embedding = item["embedding"]

    def search(self, project_id, query_vec, top_k):
        with SessionLocal() as db:
            rows = db.query(DocChunk).join(KnowledgeDocument).filter(
                DocChunk.project_id == project_id, KnowledgeDocument.status == "INDEXED",
                DocChunk.index_status == "READY", DocChunk.embedding_profile == settings.embedding_profile,
            ).all()
            ids, vectors = [], []
            for row in rows:
                if row.embedding is not None and len(row.embedding) == len(query_vec):
                    ids.append(row.id)
                    vectors.append(row.embedding)
        if not ids:
            return []
        matrix = np.array(vectors, dtype=np.float64)
        q = np.array(query_vec, dtype=np.float64)
        denom = np.linalg.norm(matrix, axis=1) * np.linalg.norm(q)
        scores = np.divide(matrix @ q, denom, out=np.zeros(len(ids)), where=denom > 0)
        order = np.argsort(-scores, kind="stable")[:top_k]
        return [{"chunk_id": ids[i], "score": float(scores[i])} for i in order]


class MilvusVectorStore:
    def __init__(self):
        from pymilvus import DataType, MilvusClient
        self.client = MilvusClient(uri=settings.MILVUS_URI, timeout=10)
        self.collection = settings.MILVUS_COLLECTION
        if self.client.has_collection(self.collection):
            fields = self.client.describe_collection(self.collection)["fields"]
            vector = next(f for f in fields if f["name"] == "embedding")
            if int(vector["params"]["dim"]) != settings.EMBED_DIM:
                raise ValueError("Milvus维度与配置不一致，请创建新Collection")
        else:
            schema = self.client.create_schema(auto_id=False, enable_dynamic_field=False)
            schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=64)
            schema.add_field("project_id", DataType.VARCHAR, max_length=64)
            schema.add_field("document_id", DataType.VARCHAR, max_length=64)
            schema.add_field("embedding_profile", DataType.VARCHAR, max_length=192)
            schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=settings.EMBED_DIM)
            indexes = self.client.prepare_index_params()
            indexes.add_index(field_name="embedding", index_type="FLAT", metric_type="COSINE")
            self.client.create_collection(self.collection, schema=schema, index_params=indexes)

    def upsert(self, records):
        self.client.upsert(collection_name=self.collection, data=records)

    def search(self, project_id, query_vec, top_k):
        import json
        rows = self.client.search(collection_name=self.collection, data=[query_vec], limit=top_k,
            filter=f"project_id == {json.dumps(project_id)} and embedding_profile == {json.dumps(settings.embedding_profile)}",
            consistency_level="Strong", search_params={"metric_type": "COSINE"})[0]
        result = []
        for row in rows:
            chunk_id = row.get("chunk_id", row.get("id"))
            if not chunk_id:
                raise ValueError("Milvus检索结果缺少主键")
            result.append({"chunk_id": chunk_id, "score": float(row["distance"])})
        return result


@lru_cache(maxsize=1)
def get_vector_store():
    return MilvusVectorStore() if settings.VECTOR_BACKEND == "milvus" else NumpyVectorStore()
