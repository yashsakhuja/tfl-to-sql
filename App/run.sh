#!/bin/bash
# Activate the virtual environment and launch the Streamlit app.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/.venv/bin/activate"
streamlit run "$SCRIPT_DIR/streamlit_app.py"
