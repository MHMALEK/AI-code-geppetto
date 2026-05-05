#!/bin/bash
set -e

CHROMA_DIR="${CHROMA_PATH:-/app/data/chroma}"

if [ ! -d "$CHROMA_DIR" ] || [ -z "$(ls -A "$CHROMA_DIR" 2>/dev/null)" ]; then
  echo "First run — indexing sample repo..."
  python -m indexer.index
fi

exec uvicorn api.main:app --host 0.0.0.0 --port 8000
