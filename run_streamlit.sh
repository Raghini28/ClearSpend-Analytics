#!/usr/bin/env bash
# Local dev: install deps (use your venv if you have one), then start the app.
set -euo pipefail
cd "$(dirname "$0")"
pip install -r requirements.txt
export STREAMLIT_SERVER_FILE_WATCHER_TYPE="${STREAMLIT_SERVER_FILE_WATCHER_TYPE:-poll}"
exec streamlit run main.py "$@"
