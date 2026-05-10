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
