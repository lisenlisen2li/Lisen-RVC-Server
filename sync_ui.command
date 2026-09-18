#!/bin/bash
# 把 webui/static/ 複製到 rvc-client/static/。
#
# 客戶端自己服務介面檔（這樣伺服器連不上時還畫得出頁面，見 client_ui.py），
# 代價是同一份介面存在兩個地方。伺服器這份是唯一的真相來源，改完介面就跑這個
# 腳本同步過去；./check.sh 會比對兩邊，不同步就報錯。
set -e
cd "$(dirname "$0")"
mkdir -p rvc-client/static
rsync -a --delete webui/static/ rvc-client/static/
echo "  已同步 webui/static/ → rvc-client/static/"
echo "  記得在 rvc-client/ 裡也 commit 一次，那是獨立的 repo。"
