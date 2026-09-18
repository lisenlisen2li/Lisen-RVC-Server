#!/bin/bash
# RVC Realtime Voice Changer - Web UI
# Bind to 127.0.0.1 instead of 0.0.0.0 if you don't want LAN access.
cd "$(dirname "$0")"
source .venv/bin/activate
export PYTORCH_ENABLE_MPS_FALLBACK=1
python -u -m webui.server --host 0.0.0.0 --port 7866
