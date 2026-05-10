"""
Clone (or pull) every repo in repos.REPOS into config.REPOS_DIR.

Usage:
    python -m scripts.clone_repos              # clone missing, pull existing
    python -m scripts.clone_repos --no-pull    # only clone, skip pull on existing
    python -m scripts.clone_repos --only api   # operate on a single repo

Auth notes:
    - Default URLs in repos.py are SSH (git@gitlab.com:...). Make sure your SSH key
      is added to GitLab, or rewrite the URLs to HTTPS in repos.py and rely on a
      credential helper / GITLAB_TOKEN.
    - This script does NOT embed tokens in URLs.
"""
import argparse
import subprocess
import sys
from pathlib import Path

from config import REPOS_DIR
from repos import REPOS, get_repo


def _run(cmd: list[str], cwd: Path | None = None) -> int:
    print(f"  $ {' '.join(cmd)}" + (f"   (cwd={cwd})" if cwd else ""))
    return subprocess.call(cmd, cwd=str(cwd) if cwd else None)


def clone_or_pull(name: str, url: str, *, pull: bool) -> None:
    target = REPOS_DIR / name
    if target.exists():
        if not (target / ".git").exists():
            print(f"[{name}] {target} exists but is not a git repo — skipping")
            return
        if pull:
            print(f"[{name}] pulling…")
            _run(["git", "fetch", "--all", "--prune"], cwd=target)
            _run(["git", "pull", "--ff-only"], cwd=target)
        else:
            print(f"[{name}] already cloned, --no-pull set, skipping")
        return

    print(f"[{name}] cloning {url} → {target}")
    rc = _run(["git", "clone", url, str(target)])
    if rc != 0:
        print(
            f"[{name}] clone failed (rc={rc}). "
            "Check SSH key / network / repo URL in repos.py."
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-pull", action="store_true",
                    help="don't pull repos that already exist")
    ap.add_argument("--only", metavar="NAME",
                    help="only operate on this repo (must match a name in repos.py)")
    args = ap.parse_args()

    targets = [get_repo(args.only)] if args.only else REPOS
    print(f"REPOS_DIR = {REPOS_DIR.resolve()}")
    print(f"{len(targets)} repo(s): {[r.name for r in targets]}\n")

    for r in targets:
        clone_or_pull(r.name, r.url, pull=not args.no_pull)
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
