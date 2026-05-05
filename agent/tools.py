"""
Agent tool implementations + Claude tool definitions.

Tools mirror what a senior engineer does when investigating a codebase:
search semantically, read files, grep for patterns, edit precisely, then commit.
"""
import subprocess
import time
from pathlib import Path
from pathlib import Path as _Path
from config import SAMPLE_REPO_PATH

SCREENSHOTS_DIR = _Path("./data/screenshots")
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

REPO = Path(SAMPLE_REPO_PATH)


def _git(args: list[str]) -> tuple[bool, str]:
    r = subprocess.run(
        ["git"] + args, cwd=str(REPO), capture_output=True, text=True, timeout=30
    )
    return r.returncode == 0, (r.stdout + r.stderr).strip()


# ── Tool implementations ──────────────────────────────────────────────────────

def search_code(query: str, n_results: int = 8) -> str:
    from indexer.store import search, lookup_symbol

    # Two-tier: semantic search + exact symbol lookup for any capitalised words
    hits = search(query, n_results=n_results)

    # Also try to find exact symbols mentioned in the query
    words = [w.strip(".,;:") for w in query.split() if w[0:1].isupper()]
    for word in words[:2]:
        exact = lookup_symbol(word)
        for h in exact:
            if not any(x["metadata"] == h["metadata"] for x in hits):
                hits.insert(0, h)

    if not hits:
        return "No results found."

    lines = []
    for i, h in enumerate(hits[:10], 1):
        m = h["metadata"]
        lines.append(
            f"[{i}] {m['file_path']}:{m['start_line']}  "
            f"({m['chunk_type']}: {m['name']})  score={h['score']}"
        )
        lines.append(h["content"][:600])
        lines.append("─" * 60)
    return "\n".join(lines)


def read_file(path: str) -> str:
    full = REPO / path.lstrip("/")
    if not full.exists():
        return f"File not found: {path}"
    if not full.resolve().is_relative_to(REPO.resolve()):
        return "Access denied"
    lines = full.read_text(encoding="utf-8", errors="replace").split("\n")
    return "\n".join(f"{i+1:4d} | {l}" for i, l in enumerate(lines))


def list_files(pattern: str = "src/**/*.tsx") -> str:
    import glob
    matches = glob.glob(str(REPO / pattern), recursive=True)
    rel = sorted(str(Path(m).relative_to(REPO)) for m in matches)
    return "\n".join(rel) if rel else "No files found."


def grep_code(pattern: str, path: str = "src") -> str:
    r = subprocess.run(
        ["grep", "-rn", "--include=*.ts", "--include=*.tsx",
         "--include=*.js", "--include=*.jsx", pattern, path],
        cwd=str(REPO), capture_output=True, text=True, timeout=15,
    )
    out = (r.stdout or "No matches found.")[:4000]
    return out


def edit_file(path: str, old_str: str, new_str: str) -> str:
    full = REPO / path.lstrip("/")
    if not full.exists():
        return f"File not found: {path}"
    if not full.resolve().is_relative_to(REPO.resolve()):
        return "Access denied"
    content = full.read_text(encoding="utf-8")
    if old_str not in content:
        return (
            f"String not found in {path}. "
            "Use read_file to check exact content including whitespace."
        )
    full.write_text(content.replace(old_str, new_str, 1), encoding="utf-8")
    return f"Edited {path}"


def create_file(path: str, content: str) -> str:
    full = REPO / path.lstrip("/")
    if not full.resolve().is_relative_to(REPO.resolve()):
        return "Access denied"
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    return f"Created {path}"


def git_status() -> str:
    _, out = _git(["status", "--short"])
    return out or "Working tree clean."


def git_diff(path: str = None) -> str:
    args = ["diff", "HEAD"] + ([path] if path else [])
    _, diff = _git(args)
    return diff[:5000] or "No changes."


def create_branch(branch_name: str) -> str:
    _, out = _git(["checkout", "-b", branch_name])
    return out


def commit_changes(message: str) -> str:
    _git(["add", "-A"])
    ok, out = _git(["commit", "-m", message])
    return out if ok else f"Commit failed: {out}"


def push_and_create_pr(branch_name: str, title: str, body: str) -> str:
    ok, remotes = _git(["remote", "-v"])
    if not remotes:
        return "No remote configured. Changes committed locally — add origin when ready."

    ok, push_out = _git(["push", "-u", "origin", branch_name])
    if not ok:
        return f"Push failed: {push_out}"

    r = subprocess.run(
        ["gh", "pr", "create", "--title", title, "--body", body],
        cwd=str(REPO), capture_output=True, text=True, timeout=30,
    )
    if r.returncode == 0:
        return f"PR created: {r.stdout.strip()}"
    return f"Pushed but PR creation failed: {r.stderr.strip()}"


def take_screenshot(label: str = "screenshot") -> str:
    """Capture the running app at localhost:5173 using Playwright."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        try:
            s.connect(("localhost", 5173))
        except OSError:
            return "Screenshot skipped: no app running at localhost:5173 (not available in cloud deployment)."

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "Screenshot skipped: playwright not installed."

    slug = label.replace(" ", "_").replace("/", "-")[:40]
    filename = f"{slug}_{int(time.time())}.png"
    filepath = SCREENSHOTS_DIR / filename

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto("http://localhost:5173", wait_until="networkidle", timeout=15000)
            page.screenshot(path=str(filepath))
            browser.close()
        return f"screenshot:/screenshots/{filename}"
    except Exception as e:
        return f"Screenshot failed: {e}"


def run_tests() -> str:
    """Run the test suite in the sample repo. Fix any failures before committing."""
    r = subprocess.run(
        ["npm", "test"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = (r.stdout + r.stderr).strip()
    status = "PASSED" if r.returncode == 0 else "FAILED"
    return f"tests:{status}\n\n{output[:3000]}"


# ── Claude tool schemas ───────────────────────────────────────────────────────

TOOL_DEFINITIONS = [
    {
        "name": "search_code",
        "description": (
            "Semantic + exact-symbol search over the codebase. "
            "Use this first to find relevant components, hooks, utilities, or types "
            "before reading any file. Returns ranked snippets with file paths and line numbers."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language description of what you're looking for"},
                "n_results": {"type": "integer", "default": 8},
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a file with line numbers. Use after search_code to inspect exact content before editing.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Relative path from repo root"}},
            "required": ["path"],
        },
    },
    {
        "name": "list_files",
        "description": "List files matching a glob pattern.",
        "input_schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string", "description": "e.g. 'src/components/**/*.tsx'"}},
            "required": ["pattern"],
        },
    },
    {
        "name": "grep_code",
        "description": "Search for a literal string or regex in the codebase.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string", "default": "src", "description": "Directory to search in"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Replace an exact string in a file. old_str must match exactly (whitespace included). "
            "If it fails, use read_file to check the actual content."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_str": {"type": "string", "description": "Exact string to replace"},
                "new_str": {"type": "string", "description": "Replacement"},
            },
            "required": ["path", "old_str", "new_str"],
        },
    },
    {
        "name": "create_file",
        "description": "Create a new file in the repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "git_status",
        "description": "Show which files have been modified.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "git_diff",
        "description": "Show the current diff of all or a specific file.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Optional file path"}},
        },
    },
    {
        "name": "create_branch",
        "description": "Create and checkout a new git branch.",
        "input_schema": {
            "type": "object",
            "properties": {"branch_name": {"type": "string", "description": "e.g. feat/DEV-1234-add-spinner"}},
            "required": ["branch_name"],
        },
    },
    {
        "name": "commit_changes",
        "description": "Stage all changes and commit.",
        "input_schema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
    },
    {
        "name": "push_and_create_pr",
        "description": "Push branch to origin and open a pull request.",
        "input_schema": {
            "type": "object",
            "properties": {
                "branch_name": {"type": "string"},
                "title": {"type": "string"},
                "body": {"type": "string", "description": "PR description in markdown"},
            },
            "required": ["branch_name", "title", "body"],
        },
    },
    {
        "name": "take_screenshot",
        "description": (
            "Take a screenshot of the running app at localhost:5173. "
            "Call once before edits (label='before') and once after (label='after') to show visual diff."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "Short label like 'before' or 'after_tooltip'"},
            },
            "required": ["label"],
        },
    },
    {
        "name": "run_tests",
        "description": (
            "Run the test suite (npm test) in the sample repo. "
            "Always call this after editing and before committing. Only commit if tests pass."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]

TOOL_MAP = {
    "search_code": search_code,
    "read_file": read_file,
    "list_files": list_files,
    "grep_code": grep_code,
    "edit_file": edit_file,
    "create_file": create_file,
    "git_status": git_status,
    "git_diff": git_diff,
    "create_branch": create_branch,
    "commit_changes": commit_changes,
    "push_and_create_pr": push_and_create_pr,
    "take_screenshot": take_screenshot,
    "run_tests": run_tests,
}
