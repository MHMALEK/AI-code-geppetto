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
SAMPLE_REPO_PATH = Path(os.getenv(
    "SAMPLE_REPO_PATH",
    "/Users/mohammadhosseinmalek/tract-projects/tract-geppetto/sample-repo"
))
CHROMA_PATH = Path(os.getenv("CHROMA_PATH", "./data/chroma"))
SQLITE_PATH = Path(os.getenv("SQLITE_PATH", "./data/tasks.db"))

CHROMA_PATH.mkdir(parents=True, exist_ok=True)
SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
