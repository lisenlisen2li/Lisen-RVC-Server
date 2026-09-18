#!/bin/bash
# 掃描不同 block_time 的可行性
cd "$(dirname "$0")"
for bt in 0.05 0.10 0.15 0.20 0.25; do
  .venv/bin/python bench.py "${1:-Frieren_e720_s6480}" $bt 2>/dev/null | grep -E "block_time|mps|cpu" 
  echo
done
