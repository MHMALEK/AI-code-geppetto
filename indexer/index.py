"""
CLI to index repositories into ChromaDB.

Usage:
    python -m indexer.index                     # uses SAMPLE_REPO_PATH (legacy single-repo)
    python -m indexer.index /path/to/repo       # arbitrary path, untagged
    python -m indexer.index --all               # every repo in repos.py
    python -m indexer.index --repo frontend     # one named repo from repos.py
    python -m indexer.index --stats             # show index stats only
    python -m indexer.index --reset             # drop all chunks before indexing
    python -m indexer.index --reset-repo NAME   # drop chunks for one repo before reindex
"""
import argparse
import sys
import time
from collections import Counter


def _index_one(path: str, repo_name: str = "") -> int:
    from indexer.parser import parse_repo
    from indexer.store import add_chunks

    label = repo_name or path
    print(f"\n── {label} ──")
    print(f"   path: {path}")
    t0 = time.time()

    print("   parsing AST...")
    chunks = parse_repo(path, repo_name)
    by_type = Counter(c.chunk_type for c in chunks)
    by_lang = Counter(c.language for c in chunks)
    print(f"   {len(chunks)} chunks in {time.time() - t0:.1f}s "
          f"(by language: {dict(by_lang)})")
    for chunk_type, count in by_type.most_common():
        print(f"     {chunk_type:<12} {count}")

    if not chunks:
        print("   (no chunks — language not supported here?)")
        return 0

    print("   embedding + storing...")
    add_chunks(chunks)
    return len(chunks)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?",
                    help="repo path (legacy single-repo form)")
    ap.add_argument("--all", action="store_true",
                    help="index every repo in repos.py")
    ap.add_argument("--repo", metavar="NAME",
                    help="index a single named repo from repos.py")
    ap.add_argument("--stats", action="store_true",
                    help="print index stats and exit")
    ap.add_argument("--reset", action="store_true",
                    help="delete the entire collection before indexing")
    ap.add_argument("--reset-repo", metavar="NAME",
                    help="delete only chunks tagged with this repo before indexing")
    args = ap.parse_args()

    if args.stats:
        from indexer.store import stats
        s = stats()
        print(f"Total chunks: {s['total_chunks']}")
        if s["by_repo"]:
            print("By repo:")
            for repo, count in sorted(s["by_repo"].items(), key=lambda kv: -kv[1]):
                print(f"  {repo:<24} {count}")
        return 0

    print("Geppetto multi-repo indexer")

    if args.reset:
        from indexer.store import _get_collection
        col = _get_collection()
        existing = col.get(include=[])
        ids = existing.get("ids", [])
        if ids:
            col.delete(ids=ids)
            print(f"  reset: removed {len(ids)} chunks")

    if args.reset_repo:
        from indexer.store import delete_repo
        n = delete_repo(args.reset_repo)
        print(f"  reset-repo {args.reset_repo}: removed {n} chunks")

    t0 = time.time()
    total = 0

    if args.all:
        from repos import REPOS
        missing = [r.name for r in REPOS if not r.path.exists()]
        if missing:
            print(f"  warning: not cloned yet: {missing} "
                  "(run `python -m scripts.clone_repos`)")
        for r in REPOS:
            if r.path.exists():
                total += _index_one(str(r.path), r.name)
    elif args.repo:
        from repos import get_repo
        r = get_repo(args.repo)
        if not r.path.exists():
            print(f"  {r.name} not cloned at {r.path} — "
                  "run `python -m scripts.clone_repos --only {r.name}`")
            return 1
        total += _index_one(str(r.path), r.name)
    else:
        from config import SAMPLE_REPO_PATH
        path = args.path or str(SAMPLE_REPO_PATH)
        total += _index_one(path, "")

    from indexer.store import stats
    s = stats()
    print(f"\nDone in {time.time() - t0:.1f}s — "
          f"indexed {total} chunks this run, {s['total_chunks']} total\n")
    if s["by_repo"]:
        for repo, count in sorted(s["by_repo"].items(), key=lambda kv: -kv[1]):
            print(f"  {repo:<24} {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
