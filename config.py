import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── LLM backend ───────────────────────────────────────────────────────────────
# Switch by changing LLM_MODEL in .env — no code changes needed.
# Examples:
#   vertex_ai/gemini-2.5-pro          ← GCP Vertex AI (default)
#   vertex_ai/gemini-2.0-flash        ← cheaper/faster Gemini
#   anthropic/claude-sonnet-4-6       ← Anthropic API
#   ollama/qwen2.5-coder:32b          ← local Ollama
LLM_MODEL = os.getenv("LLM_MODEL", "vertex_ai/gemini-2.5-pro")

# ── GCP / Vertex AI ───────────────────────────────────────────────────────────
VERTEXAI_PROJECT = os.getenv("VERTEXAI_PROJECT", "")
VERTEXAI_LOCATION = os.getenv("VERTEXAI_LOCATION", "us-central1")

# ── Other API keys (only needed for chosen backend) ───────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# ── Embeddings ────────────────────────────────────────────────────────────────
EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-small")

# ── Langfuse observability (optional — leave blank to disable) ────────────────
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")
LANGFUSE_HOST = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")

# ── Paths ─────────────────────────────────────────────────────────────────────
# ── Jira ──────────────────────────────────────────────────────────────────────
JIRA_BASE_URL    = os.getenv("JIRA_BASE_URL", "")
JIRA_EMAIL       = os.getenv("JIRA_EMAIL", "")
JIRA_API_TOKEN   = os.getenv("JIRA_API_TOKEN", "")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY", "SCRUM")

# Transition IDs (fetched from the board — update if your board differs)
JIRA_TRANSITION_IN_PROGRESS = "21"
JIRA_TRANSITION_IN_REVIEW   = "31"
JIRA_TRANSITION_DONE        = "41"

# ── Paths ─────────────────────────────────────────────────────────────────────
SAMPLE_REPO_PATH = Path(os.getenv(
    "SAMPLE_REPO_PATH",
    "/Users/mohammadhosseinmalek/tract-projects/code-geppetto/sample-geppetto-repo"
))
CHROMA_PATH = Path(os.getenv("CHROMA_PATH", "./data/chroma"))
SQLITE_PATH = Path(os.getenv("SQLITE_PATH", "./data/tasks.db"))

CHROMA_PATH.mkdir(parents=True, exist_ok=True)
SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
