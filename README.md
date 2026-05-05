# Geppetto

An AI coding agent that reads a task, searches your codebase with RAG, edits the code, runs tests, captures screenshots when needed, and opens a pull request — with a live trace in the dashboard.

---

## What it does

1. You create a task (dashboard, `POST /tasks`, generic **`POST /webhook`** (n8n / Jira payload shapes), **Slack** `/geppetto`, or **Telegram**).
2. The agent **searches the indexed codebase** (semantic + symbol lookup).
3. It **reads and edits files**, branches, commits, and runs **`npm test`** in the target repo when appropriate.
4. It can **`take_screenshot`** of the running dev app (Playwright) for before/after review.
5. **`push_and_create_pr`** pushes the branch and opens a GitHub PR (via `gh`; needs a token — see below).
6. Optional **Jira**: transitions and comments when tasks complete; create/list issues via API.
7. Every step streams to the **dashboard** (SSE).

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         Geppetto                                 │
│                                                                  │
│  Dashboard · curl · /webhook · Slack · Telegram · /telegram       │
│       │                                                          │
│       ▼                                                          │
│  ┌─────────────┐     ┌──────────────────────────────────────┐   │
│  │  FastAPI    │────▶│           Agent loop                  │   │
│  │  + SQLite   │     │  (LiteLLM → Gemini / Vertex / Claude)  │   │
│  └─────────────┘     └────────────────┬─────────────────────┘   │
│       │ SSE                            │ tools                     │
│       ▼                                ▼                           │
│  ┌─────────────┐     ┌──────────────────────────────────────┐   │
│  │  Dashboard  │     │  search · read · edit · git · PR      │   │
│  │  + /screenshots      screenshot · npm test                 │   │
│  └─────────────┘     └────────────┬────────────────────────────┘   │
│                                   │                                │
│                     ┌─────────────▼────────────┐                  │
│                     │  RAG index               │                  │
│                     │  tree-sitter + ChromaDB  │                  │
│                     └──────────────────────────┘                  │
│                                                                  │
│  Langfuse (optional) · Jira REST · Telegram Bot API              │
└─────────────────────────────────────────────────────────────────┘
```

---

## Tech stack

| Layer | Technology | Notes |
|-------|------------|--------|
| **LLM** | [LiteLLM](https://github.com/BerriAI/litellm) | Switch models in `.env` (`LLM_MODEL=…`) |
| **Default model** | `gemini/gemini-2.5-pro` (Google AI Studio) | Set `GEMINI_API_KEY`; use `vertex_ai/…` for GCP |
| **Embeddings** | Configurable (`EMBED_MODEL`) | Often OpenAI or Vertex embedding model |
| **Vector DB** | ChromaDB | Persisted under `CHROMA_PATH` |
| **Code chunks** | tree-sitter | TS/TSX/JS chunks at function/component boundaries |
| **API** | FastAPI + SSE | Tasks, Jira helpers, `/ask` RAG Q&A, static dashboard |
| **Tasks** | SQLite | `tasks` + `telegram_processed` (dedupe) |
| **Git / PR** | git + [GitHub CLI](https://cli.github.com/) `gh` | `GITHUB_TOKEN` or `GH_TOKEN` for non-interactive PRs |
| **Screenshots** | Playwright | Headless Chromium; dev app must match `SCREENSHOT_APP_URL` |
| **Dashboard** | Single `index.html` | Served at `/` |

---

## RAG: how indexing works

1. **AST-based chunking** — tree-sitter splits `.ts` / `.tsx` (and friends) at components, hooks, functions, types.
2. **Enriched text** — each chunk is embedded with file path, kind, name, and imports so search is context-aware.
3. **Two-tier retrieval** — semantic similarity plus exact symbol hints when the task names a component.

Re-index after large code changes:

```bash
python -m indexer.index
python -m indexer.index --stats
```

---

## Setup

### 1. Clone and configure

```bash
cd code-geppetto
cp .env.template .env
# Edit .env — at minimum: LLM keys, OPENAI (embeddings), SAMPLE_REPO_PATH, TARGET_REPO_URL for PRs
```

### 2. LLM backends

Examples (see `.env.template`):

```bash
LLM_MODEL=gemini/gemini-2.5-pro
GEMINI_API_KEY=...

# Or Vertex:
# LLM_MODEL=vertex_ai/gemini-2.5-pro
# VERTEXAI_PROJECT=...  + gcloud auth application-default login

# Or Claude / Ollama — set the matching API env vars.
```

### 3. GitHub PRs (`push_and_create_pr`)

The agent runs `gh pr create`. For CI, headless servers, or Cursor agents, set **one** of:

```bash
GITHUB_TOKEN=ghp_...   # recommended; copied to GH_TOKEN inside the tool
# GH_TOKEN=ghp_...     # alternative
```

Do **not** embed tokens in `git remote` URLs. Prefer SSH: `git@github.com:org/repo.git`.

### 4. Run locally

```bash
./run.sh
```

This installs deps, ensures the venv, indexes Chroma if empty, starts the **sample app** with Vite on **port 5173** (`--strictPort` from `run.sh`), and starts **uvicorn** on **port 8000**.

- Dashboard: `http://127.0.0.1:8000`
- Sample UI (for screenshots): must match `SCREENSHOT_APP_URL` (default `http://127.0.0.1:5173`)

If Vite fails with “port in use”, free **5173** or set `SCREENSHOT_APP_URL` to the URL Vite prints.

---

## Configuration highlights

| Variable | Purpose |
|----------|---------|
| `SAMPLE_REPO_PATH` | Repo the agent edits and tests |
| `TARGET_REPO_URL` | Git remote for clone/push (see `.env.template` / Fly) |
| `PUBLIC_BASE_URL` | Browser base for dashboard deep links (e.g. Telegram “watch live”) |
| `SCREENSHOT_APP_URL` | URL Playwright opens for `take_screenshot` |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_POLLING` | Long-poll on localhost; use **one** poller per bot (webhook vs poll — don’t mix active webhook + poll) |
| `JIRA_*` | REST URL, email, API token, project key; transition IDs in `config.py` if your workflow differs |

Copy **`.env.template`** and fill in values; never commit `.env`.

---

## Usage

### Dashboard

Open `PUBLIC_BASE_URL` (default `http://127.0.0.1:8000`). Create tasks, attach optional Jira keys, watch the stream.

### curl

```bash
curl -X POST http://127.0.0.1:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "jira_id": "SCRUM-4",
    "title": "Add button at bottom of page",
    "description": "Match existing button styles; keep layout responsive."
  }'
```

### Stream

```bash
curl http://127.0.0.1:8000/tasks/{task_id}/stream
```

### Generic webhook (`POST /webhook`)

For **n8n**, **Jira webhooks**, or any HTTP caller. Supports a flat body `{"title","description","jira_id"}` or a Jira-style `{"issue":{"key","fields":{…}}}` payload — see docstring in `api/main.py`.

### Slack (optional)

`POST /slack` — slash command integration; configure your Slack app to post form data to this URL (see `api/main.py`).

### Jira helpers

- `GET /jira/issues`, `GET /jira/issues/{key}`, `POST /jira/issues` (create issue only; start the agent via `/tasks` or dashboard with `jira_id`).

### Telegram (optional)

- **Webhook:** `POST /telegram` with the standard Bot API update JSON (HTTPS in production).
- **Local polling:** `TELEGRAM_POLLING=true` — deletes webhook on startup; only **one** process may call `getUpdates` for that bot (second local process skips polling if the flock lock is held; another machine or bot client still causes Telegram conflicts).

Voice notes can be transcribed with Whisper if `OPENAI_API_KEY` is set (see `.env.template`).

### Reset local SQLite + move Jira issues to “To Do”

```bash
python scripts/reset_db_and_jira.py
```

Clears tasks and Telegram dedupe rows; if Jira env is set, walks project issues toward the **new** status category (typical backlog / To Do). Adjust workflow in Jira if some states have no path backward.

---

## Observability (Langfuse)

Optional — add `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` in `.env`. Traces include tool calls, latency, and usage when the agent is wired to emit spans.

---

## Project structure

```
code-geppetto/
├── agent/
│   ├── tools.py        # search, edit, git, PR, screenshot, tests
│   └── runner.py       # agent loop + LiteLLM
├── api/
│   ├── main.py         # FastAPI, SSE, Telegram, Jira, /ask
│   ├── models.py       # SQLite tasks + telegram dedupe
│   └── jira.py         # Jira REST client
├── indexer/            # tree-sitter + Chroma
├── dashboard/
│   └── index.html
├── scripts/
│   └── reset_db_and_jira.py
├── data/               # chroma, tasks.db, screenshots (gitignored pieces)
├── config.py
├── run.sh
├── entrypoint.sh       # container-style uvicorn
└── requirements.txt
```

The demo target app path defaults to **`sample-geppetto-repo`** (configure with `SAMPLE_REPO_PATH`). That folder may be a separate git repo in your workspace.

---

## Agent tools

| Tool | Description |
|------|-------------|
| `search_code` | Semantic + symbol-aware search |
| `read_file` | Read file with line numbers |
| `list_files` | Glob paths |
| `grep_code` | Regex search |
| `edit_file` | Exact-string replacement |
| `create_file` | Add a new file |
| `git_status` / `git_diff` | Working tree |
| `create_branch` | Checkout new branch |
| `commit_changes` | Stage + commit |
| `push_and_create_pr` | Push + `gh pr create` (needs token) |
| `take_screenshot` | Playwright capture of `SCREENSHOT_APP_URL` |
| `run_tests` | `npm test` in the sample repo |

---

## Deployment notes

- Set **`PUBLIC_BASE_URL`** to the public URL of this API (Telegram links, webhooks).
- For Telegram in production, prefer **webhook** over polling; set `TELEGRAM_POLLING=false`.
- Ensure **`GITHUB_TOKEN`** (or `GH_TOKEN`) is available in the environment if the agent should open PRs.

---

## License / upstream

Treat this repo as your integration shell; adjust `SAMPLE_REPO_PATH`, Jira transition IDs, and team conventions to match your project.
