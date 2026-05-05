# 🪄 Geppetto

> An AI coding agent that reads a task, searches your codebase with RAG, writes the code, and opens a PR — while you watch it think in real time.

---

## What it does

1. You POST a task (title + description + optional Jira ID) via `curl` or the dashboard
2. The agent **searches the indexed codebase** to understand existing patterns
3. It **reads relevant files**, creates a branch, makes precise edits, and commits
4. A **PR is opened** with a clear description
5. Every step streams live to the **dashboard**

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         Geppetto                                │
│                                                                 │
│  curl / Dashboard                                               │
│       │                                                         │
│       ▼                                                         │
│  ┌─────────────┐     ┌──────────────────────────────────────┐  │
│  │  FastAPI    │────▶│           Agent Loop                 │  │
│  │  + SQLite   │     │  (LiteLLM → Gemini / Claude / Ollama)│  │
│  └─────────────┘     └────────────────┬─────────────────────┘  │
│       │                               │                         │
│       │ SSE stream                    │ tools                   │
│       ▼                               ▼                         │
│  ┌─────────────┐     ┌──────────────────────────────────────┐  │
│  │  Dashboard  │     │  search_code  read_file  edit_file   │  │
│  │  (live feed)│     │  create_branch  commit  push_pr      │  │
│  └─────────────┘     └────────────┬─────────────────────────┘  │
│                                   │                             │
│                     ┌─────────────▼────────────┐               │
│                     │       RAG Index           │               │
│                     │  tree-sitter AST parser   │               │
│                     │  + ChromaDB + OpenAI emb  │               │
│                     └──────────────────────────┘               │
│                                                                 │
│  Langfuse  ◀──── every LLM call traced automatically           │
└─────────────────────────────────────────────────────────────────┘
```

---

## Tech Stack

| Layer | Technology | Why |
|---|---|---|
| **LLM** | [LiteLLM](https://github.com/BerriAI/litellm) | unified interface — swap Gemini ↔ Claude ↔ Ollama via `.env` |
| **Default model** | Gemini 2.5 Pro (Vertex AI) | top-tier code quality, GCP billing |
| **Code parsing** | [tree-sitter](https://tree-sitter.github.io/) | AST-based chunking at function/class boundaries |
| **Embeddings** | OpenAI `text-embedding-3-small` | fast, cheap, high quality |
| **Vector DB** | [ChromaDB](https://www.trychroma.com/) | embedded, zero infra, persistent |
| **Observability** | [Langfuse](https://langfuse.com/) | full LLM traces, token costs, latency |
| **API** | FastAPI + SSE | real-time streaming to dashboard |
| **Task store** | SQLite | simple, no infra |
| **Dashboard** | Vanilla JS + Tailwind | zero build step, ships as static HTML |
| **Git ops** | subprocess + gh CLI | branch, commit, push, PR |

---

## RAG: How Code Indexing Works

Most RAG systems chunk code by character count — this produces useless fragments. Geppetto does it properly:

### 1. AST-based chunking (tree-sitter)
Each `.ts` / `.tsx` file is parsed into an Abstract Syntax Tree. Chunks are extracted at **semantic boundaries**:
- React components (`const MyComponent = () => ...`)
- Custom hooks (`useSupplierData`, `useAuth`)
- Functions, classes, methods
- TypeScript interfaces and type aliases

### 2. Metadata-enriched embeddings
Each chunk is embedded with its context prepended:
```
// File: src/components/SupplierTable.tsx | Type: component | Name: SupplierTable
// Imports: import { useQuery } from '@tanstack/react-query'; ...

const SupplierTable = ({ filters }: Props) => {
  ...
}
```
This makes semantic search dramatically more accurate — the model knows *what kind* of thing it's reading.

### 3. Two-tier retrieval
When the agent calls `search_code`:
- **Semantic search** — find conceptually related code via vector similarity
- **Exact symbol lookup** — if the task mentions `SupplierTable`, fetch it directly by name

---

## Supported LLM Backends

Change `LLM_MODEL` in `.env` — no code changes needed:

```bash
LLM_MODEL=vertex_ai/gemini-2.5-pro       # GCP Vertex AI (default)
LLM_MODEL=vertex_ai/gemini-2.0-flash     # cheaper/faster
LLM_MODEL=anthropic/claude-sonnet-4-6    # Anthropic API
LLM_MODEL=ollama/qwen2.5-coder:32b       # local Ollama
```

---

## Setup

### 1. Clone and configure

```bash
cd ~/tract-projects/tract-geppetto
cp .env.template .env
# Edit .env with your keys
```

### 2. Authenticate GCP (for Vertex AI)

```bash
gcloud auth application-default login
gcloud config set project YOUR_PROJECT_ID
```

### 3. Run

```bash
./run.sh
```

On first run this will:
- Create a Python virtualenv
- Install dependencies
- Index the sample repo (tree-sitter parse → embed → store in ChromaDB)
- Start the API at `http://localhost:8000`

---

## Usage

### Dashboard
Open `http://localhost:8000` — click **New Task** or use the curl command below.

### curl (simulates a Jira webhook)

```bash
curl -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "jira_id": "DEV-1234",
    "title": "Add loading spinner to SupplierTable",
    "description": "When the table is fetching data, show a loading spinner instead of an empty table. Use the existing Spinner component if available."
  }'
```

### Watch progress
```bash
# Stream events in terminal
curl http://localhost:8000/tasks/{task_id}/stream

# Or just open the dashboard
open http://localhost:8000
```

### Re-index after code changes
```bash
python -m indexer.index

# Check index stats
python -m indexer.index --stats
```

---

## Observability with Langfuse

Sign up free at [cloud.langfuse.com](https://cloud.langfuse.com), add keys to `.env`:

```bash
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
```

Every task automatically becomes a Langfuse trace with:
- Full tool call history
- Token usage per step
- Latency breakdown
- Cost per task

---

## Project Structure

```
tract-geppetto/
├── indexer/
│   ├── parser.py       # tree-sitter AST parser (TS/TSX/JS)
│   ├── store.py        # ChromaDB vector store + two-tier retrieval
│   └── index.py        # CLI: python -m indexer.index
├── agent/
│   ├── tools.py        # 11 tools: search, read, edit, git, PR
│   └── runner.py       # LiteLLM agent loop with Langfuse tracing
├── api/
│   ├── models.py       # SQLite task store + Pydantic models
│   └── main.py         # FastAPI + SSE streaming
├── dashboard/
│   └── index.html      # real-time task dashboard
├── sample-repo/        # target codebase (tract frontend)
├── config.py
├── requirements.txt
└── run.sh
```

---

## Agent Tools

| Tool | Description |
|---|---|
| `search_code` | Semantic + symbol search across the RAG index |
| `read_file` | Read file with line numbers |
| `list_files` | Glob files by pattern |
| `grep_code` | Literal/regex search |
| `edit_file` | Exact-string replacement (safe, precise) |
| `create_file` | Create new file |
| `git_status` | Show modified files |
| `git_diff` | Show current diff |
| `create_branch` | Create and checkout branch |
| `commit_changes` | Stage all + commit |
| `push_and_create_pr` | Push + open GitHub PR |
