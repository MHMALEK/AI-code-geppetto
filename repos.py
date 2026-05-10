"""
Multi-repo registry.

Hardcoded list of repos the indexer (and later, the agent) operates on.
Edit REPOS to add/remove/rename. Working trees live under config.REPOS_DIR.

URLs default to SSH — change to HTTPS + token if SSH isn't set up against gitlab.com.
QA-automation (Java/Selenium) is intentionally excluded for now.
"""
from dataclasses import dataclass
from pathlib import Path

from config import REPOS_DIR


@dataclass(frozen=True)
class Repo:
    name: str
    url: str
    languages: tuple[str, ...]  # e.g. ("python",), ("typescript",), ("python", "typescript")

    @property
    def path(self) -> Path:
        return REPOS_DIR / self.name


REPOS: list[Repo] = [
    Repo(
        name="api",
        url="git@gitlab.com:tract1/application/api/traceability.git",
        languages=("python",),
    ),
    Repo(
        name="frontend",
        url="git@gitlab.com:tract1/application/frontend.git",
        languages=("typescript",),
    ),
    Repo(
        name="data",
        url="git@gitlab.com:tract1/application/data.git",
        languages=("python",),
    ),
    Repo(
        name="data-cloud-functions",
        url="git@gitlab.com:tract1/application/data-cloud-functions.git",
        languages=("python",),
    ),
]


def get_repo(name: str) -> Repo:
    for r in REPOS:
        if r.name == name:
            return r
    raise KeyError(f"unknown repo: {name!r} (known: {[r.name for r in REPOS]})")


def sourcebot_canonical_name(url: str) -> str:
    """Convert a clone URL to Sourcebot's canonical repo identifier.

    Sourcebot stores repos as `<host>/<path>` (no protocol, no `.git`):
        git@gitlab.com:tract1/application/api/traceability.git
        → gitlab.com/tract1/application/api/traceability
        https://gitlab.com/tract1/application/frontend.git
        → gitlab.com/tract1/application/frontend
    """
    s = url
    if s.startswith("git@"):
        s = s[4:].replace(":", "/", 1)            # git@host:path → host/path
    else:
        s = s.split("://", 1)[-1]                 # strip http(s)://
    if s.endswith(".git"):
        s = s[:-4]
    return s


def all_sourcebot_repo_names() -> list[str]:
    """Every known repo's canonical Sourcebot name. Used to scope multi-repo
    /api/chat/blocking calls so the agent sees the full universe."""
    return [sourcebot_canonical_name(r.url) for r in REPOS]
