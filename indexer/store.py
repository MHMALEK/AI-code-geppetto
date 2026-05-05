"""
ChromaDB vector store wrapper.

Two-tier retrieval:
  1. Semantic search  — embed query, find similar chunks (broad understanding)
  2. Symbol lookup    — direct name/file match in metadata (precise targeting)
"""
import chromadb
from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction
from config import CHROMA_PATH, OPENAI_API_KEY, EMBED_MODEL
from indexer.parser import CodeChunk

COLLECTION = "codebase"


def _get_collection():
    client = chromadb.PersistentClient(path=str(CHROMA_PATH))
    ef = OpenAIEmbeddingFunction(api_key=OPENAI_API_KEY, model_name=EMBED_MODEL)
    return client.get_or_create_collection(
        name=COLLECTION,
        embedding_function=ef,
        metadata={"hnsw:space": "cosine"},
    )


def add_chunks(chunks: list[CodeChunk], batch_size: int = 100):
    collection = _get_collection()
    total = len(chunks)

    for i in range(0, total, batch_size):
        batch = chunks[i : i + batch_size]

        # Deduplicate within batch (same id can appear from re-indexing)
        seen: set[str] = set()
        ids, docs, metas = [], [], []
        for c in batch:
            uid = f"{c.id}:{c.start_line}"
            if uid not in seen:
                seen.add(uid)
                ids.append(uid)
                docs.append(c.to_document())   # enriched text → better embeddings
                metas.append(c.to_metadata())

        if ids:
            collection.upsert(ids=ids, documents=docs, metadatas=metas)

        print(f"  indexed {min(i + batch_size, total)}/{total}")


def search(query: str, n_results: int = 8, chunk_type: str = None) -> list[dict]:
    """Semantic similarity search."""
    collection = _get_collection()
    where = {"chunk_type": chunk_type} if chunk_type else None

    results = collection.query(
        query_texts=[query],
        n_results=min(n_results, collection.count() or 1),
        where=where,
        include=["documents", "metadatas", "distances"],
    )

    hits = []
    for doc, meta, dist in zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
    ):
        hits.append({
            "content": doc,
            "metadata": meta,
            "score": round(1 - dist, 3),
        })
    return hits


def lookup_symbol(name: str) -> list[dict]:
    """Exact symbol name lookup — used when the task mentions a specific component/function."""
    collection = _get_collection()
    results = collection.get(
        where={"name": name},
        include=["documents", "metadatas"],
    )
    return [
        {"content": doc, "metadata": meta, "score": 1.0}
        for doc, meta in zip(results["documents"], results["metadatas"])
    ]


def stats() -> dict:
    return {"total_chunks": _get_collection().count()}
