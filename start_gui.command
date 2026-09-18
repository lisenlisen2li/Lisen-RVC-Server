#!/bin/bash
# RVC-MacOS 即時變聲器啟動腳本
cd "$(dirname "$0")"
source .venv/bin/activate
export PYTORCH_ENABLE_MPS_FALLBACK=1
python gui.py
