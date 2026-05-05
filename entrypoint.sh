#!/bin/bash
set -e

REPO_DIR="/app/data/repo"
CHROMA_DIR="${CHROMA_PATH:-/app/data/chroma}"

# ── Google / Vertex AI credentials (optional — only if using vertex_ai/ models)
# Recommended: use gemini/ models instead (just GEMINI_API_KEY, no JSON needed).
# If you do need Vertex AI, store the service account JSON as GOOGLE_CREDENTIALS_JSON.
if [ -n "$GOOGLE_CREDENTIALS_JSON" ]; then
  echo "$GOOGLE_CREDENTIALS_JSON" > /app/data/gcp-credentials.json
  export GOOGLE_APPLICATION_CREDENTIALS=/app/data/gcp-credentials.json
fi

# ── Git identity ──────────────────────────────────────────────────────────────
git config --global user.email "${GIT_USER_EMAIL:-geppetto@geppetto.ai}"
git config --global user.name  "${GIT_USER_NAME:-Geppetto}"
git config --global init.defaultBranch main

# ── Clone target repo if not already present ─────────────────────────────────
# Note: gh CLI picks up GITHUB_TOKEN from env automatically — no explicit login needed.
if [ -n "$TARGET_REPO_URL" ] && [ ! -d "$REPO_DIR/.git" ]; then
  echo "Cloning $TARGET_REPO_URL → $REPO_DIR ..."
  mkdir -p "$REPO_DIR"

  if [ -n "$GITHUB_TOKEN" ]; then
    # Embed token so git push works without interactive auth
    AUTH_URL=$(echo "$TARGET_REPO_URL" | sed "s|https://github.com|https://x-access-token:${GITHUB_TOKEN}@github.com|")
    git clone "$AUTH_URL" "$REPO_DIR"
    git -C "$REPO_DIR" remote set-url origin "$AUTH_URL"
  else
    git clone "$TARGET_REPO_URL" "$REPO_DIR"
  fi

  echo "Repo ready."
fi

# Point SAMPLE_REPO_PATH at the volume-backed clone when TARGET_REPO_URL is set
if [ -n "$TARGET_REPO_URL" ]; then
  export SAMPLE_REPO_PATH="$REPO_DIR"
fi

# ── Index on first run ────────────────────────────────────────────────────────
if [ ! -d "$CHROMA_DIR" ] || [ -z "$(ls -A "$CHROMA_DIR" 2>/dev/null)" ]; then
  echo "First run — indexing repo..."
  python -m indexer.index
fi

exec uvicorn api.main:app --host 0.0.0.0 --port 8000
