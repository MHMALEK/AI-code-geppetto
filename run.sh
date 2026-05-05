#!/bin/bash
set -e

cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "No .env found — copying from template"
  cp .env.template .env
  echo "Fill in your API keys in .env then re-run."
  exit 1
fi

if [ ! -d .venv ]; then
  echo "Creating virtualenv..."
  python3 -m venv .venv
fi

source .venv/bin/activate

echo "Installing dependencies..."
pip install -q -r requirements.txt

# Index the repo if ChromaDB is empty
CHROMA_DIR=$(python3 -c "from config import CHROMA_PATH; print(CHROMA_PATH)")
if [ ! -d "$CHROMA_DIR" ] || [ -z "$(ls -A "$CHROMA_DIR" 2>/dev/null)" ]; then
  echo ""
  echo "Indexing sample repo (first run only)..."
  python3 -m indexer.index
fi

echo ""
echo "  Geppetto running at http://localhost:8000"
echo ""
uvicorn api.main:app --reload --port 8000
