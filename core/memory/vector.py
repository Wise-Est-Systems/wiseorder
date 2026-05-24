from __future__ import annotations

from typing import Any, Iterable

import chromadb
from chromadb.config import Settings as ChromaSettings

from configs.settings import get_settings


class VectorStore:
    """Thin async-safe wrapper around an embedded persistent ChromaDB.

    Uses Chroma's default embedding function (all-MiniLM-L6-v2 downloaded on first use).
    Stays local — no external server, no network beyond first model fetch.
    """

    def __init__(self, path: str | None = None, collection: str = "wiseorder") -> None:
        self.path = path or get_settings().chroma_path
        self._client = chromadb.PersistentClient(
            path=self.path,
            settings=ChromaSettings(anonymized_telemetry=False, allow_reset=False),
        )
        self._collection = self._client.get_or_create_collection(name=collection)

    def add(
        self,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict[str, Any]] | None = None,
    ) -> None:
        self._collection.add(ids=ids, documents=documents, metadatas=metadatas)

    def query(self, text: str, n_results: int = 5) -> list[dict[str, Any]]:
        res = self._collection.query(query_texts=[text], n_results=n_results)
        hits: list[dict[str, Any]] = []
        ids = (res.get("ids") or [[]])[0]
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        for i, doc in enumerate(docs):
            hits.append(
                {
                    "id": ids[i] if i < len(ids) else None,
                    "document": doc,
                    "metadata": metas[i] if i < len(metas) else {},
                    "distance": dists[i] if i < len(dists) else None,
                }
            )
        return hits

    def count(self) -> int:
        return self._collection.count()

    def delete(self, ids: Iterable[str]) -> None:
        self._collection.delete(ids=list(ids))


_store: VectorStore | None = None


def get_vector_store() -> VectorStore:
    global _store
    if _store is None:
        _store = VectorStore()
    return _store
