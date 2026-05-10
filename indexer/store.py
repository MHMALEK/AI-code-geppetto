"""
ChromaDB vector store wrapper.
Embeddings via LiteLLM — defaults to Vertex AI text-embedding-004 (no OpenAI needed).

Two-tier retrieval:
  1. Semantic search  — embed query, find similar chunks
  2. Symbol lookup    — direct name/file match in metadata

Multi-repo: every chunk carries `repo` in metadata; pass repo= to scope a search.
"""
import chromadb
from chromadb import EmbeddingFunction, Embeddings
from config import CHROMA_PATH, EMBED_MODEL
from indexer.parser import CodeChunk

COLLECTION = "codebase"


class LiteLLMEmbeddingFunction(EmbeddingFunction):
    """Thin ChromaDB adapter over LiteLLM — works with any embedding model."""

    def __call__(self, input: list[str]) -> Embeddings:
        import litellm
        # Batch in groups of 20 to stay within Vertex AI request limits
        all_embeddings = []
        for i in range(0, len(input), 20):
            batch = input[i:i + 20]
            response = litellm.embedding(model=EMBED_MODEL, input=batch)
            all_embeddings.extend([item["embedding"] for item in response.data])
        return all_embeddings


def _get_collection():
    client = chromadb.PersistentClient(path=str(CHROMA_PATH))
    return client.get_or_create_collection(
        name=COLLECTION,
        embedding_function=LiteLLMEmbeddingFunction(),
        metadata={"hnsw:space": "cosine"},
    )


def add_chunks(chunks: list[CodeChunk], batch_size: int = 50):
    collection = _get_collection()
    total = len(chunks)

    for i in range(0, total, batch_size):
        batch = chunks[i:i + batch_size]
        seen: set[str] = set()
        ids, docs, metas = [], [], []
        for c in batch:
            uid = f"{c.id}:{c.start_line}"
            if uid not in seen:
                seen.add(uid)
                ids.append(uid)
                docs.append(c.to_document())
                metas.append(c.to_metadata())
        if ids:
            collection.upsert(ids=ids, documents=docs, metadatas=metas)
        print(f"  indexed {min(i + batch_size, total)}/{total}")


def _build_where(chunk_type: str | None, repo: str | None) -> dict | None:
    """Chroma `where` accepts a single equality dict or {'$and': [...]}.

    Returning None when no filters are set lets Chroma skip filtering entirely.
    """
    clauses: list[dict] = []
    if chunk_type:
        clauses.append({"chunk_type": chunk_type})
    if repo:
        clauses.append({"repo": repo})
    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def search(
    query: str,
    n_results: int = 8,
    chunk_type: str | None = None,
    repo: str | None = None,
) -> list[dict]:
    collection = _get_collection()
    where = _build_where(chunk_type, repo)
    results = collection.query(
        query_texts=[query],
        n_results=min(n_results, collection.count() or 1),
        where=where,
        include=["documents", "metadatas", "distances"],
    )
    return [
        {"content": doc, "metadata": meta, "score": round(1 - dist, 3)}
        for doc, meta, dist in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        )
    ]


def lookup_symbol(name: str, repo: str | None = None) -> list[dict]:
    collection = _get_collection()
    where = {"name": name}
    if repo:
        where = {"$and": [where, {"repo": repo}]}
    results = collection.get(
        where=where,
        include=["documents", "metadatas"],
    )
    return [
        {"content": doc, "metadata": meta, "score": 1.0}
        for doc, meta in zip(results["documents"], results["metadatas"])
    ]


def delete_repo(repo: str) -> int:
    """Remove every chunk tagged with this repo. Returns count deleted."""
    collection = _get_collection()
    existing = collection.get(where={"repo": repo}, include=[])
    ids = existing.get("ids", [])
    if ids:
        collection.delete(ids=ids)
    return len(ids)


def stats() -> dict:
    """Total chunk count plus per-repo breakdown."""
    collection = _get_collection()
    total = collection.count()
    # Pull metadata for everything (cheap — no embeddings, no documents).
    by_repo: dict[str, int] = {}
    if total:
        all_meta = collection.get(include=["metadatas"]).get("metadatas") or []
        for m in all_meta:
            r = (m or {}).get("repo") or "<untagged>"
            by_repo[r] = by_repo.get(r, 0) + 1
    return {"total_chunks": total, "by_repo": by_repo}
