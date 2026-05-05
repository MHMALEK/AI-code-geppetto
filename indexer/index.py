"""
CLI to index a repository into ChromaDB.

Usage:
    python -m indexer.index                     # uses SAMPLE_REPO_PATH from .env
    python -m indexer.index /path/to/repo
    python -m indexer.index --stats             # show index stats only
"""
import sys
import time
from collections import Counter


def main():
    if "--stats" in sys.argv:
        from indexer.store import stats
        s = stats()
        print(f"Index stats: {s}")
        return

    from config import SAMPLE_REPO_PATH
    from indexer.parser import parse_repo
    from indexer.store import add_chunks, stats

    repo_path = sys.argv[1] if len(sys.argv) > 1 else str(SAMPLE_REPO_PATH)

    print(f"\n Geppetto Indexer")
    print(f" Repo: {repo_path}\n")

    t0 = time.time()

    print("Step 1/2  Parsing AST...")
    chunks = parse_repo(repo_path)

    by_type = Counter(c.chunk_type for c in chunks)
    print(f"  Found {len(chunks)} chunks in {time.time() - t0:.1f}s")
    for chunk_type, count in by_type.most_common():
        print(f"    {chunk_type:<12} {count}")

    print("\nStep 2/2  Embedding + storing in ChromaDB...")
    add_chunks(chunks)

    s = stats()
    print(f"\n Done in {time.time() - t0:.1f}s — {s['total_chunks']} total chunks in index\n")


if __name__ == "__main__":
    main()
