# RVC 即時變聲器 — 伺服器

在 Apple Silicon 上跑 RVC 即時變聲，走 MPS。提供網頁介面，以及給原生客戶端用的
UDP 音訊通道。

客戶端在另一個 repo：**Lisen-RVC-Client**（在你手邊那台裝置收放音，不需要模型或 torch）。

> **接手的人請先看 [HANDOVER.md](HANDOVER.md)** — 完整的工作紀錄、關鍵發現、
> 踩過的坑，以及回程封包遺失的處理（已加 XOR 同位，待實機驗收）。

## 為什麼有這個專案

w-okada 的 VCClient 在這台 M4 Pro 上只能用 CPU，延遲約 300ms。這個 fork 能用
MPS，實測推論快 3.8 倍（MPS 43ms vs CPU 166ms），延遲降到約 150ms。

## 啟動

```bash
./start_webui.command              # 綁 0.0.0.0，需要 token
./start_webui_behind_proxy.command # 前面有會驗證身分的 nginx 時用
./start_gui.command                # 原本的桌面 GUI（PySimpleGUI）
```

終端機會印出含 token 的網址。

## 音訊路由

輸入端與輸出端**各自**可以是伺服器或客戶端，四種組合：

| 輸入 | 輸出 | 伺服器做的事 |
|---|---|---|
| server | server | 單一 duplex 串流。同一時脈、延遲最低（約 150ms） |
| client | client | UDP 收 → 推論 → UDP 送（約 185ms） |
| server | client | 本機收音 → 推論 → UDP 送給客戶端 |
| client | server | UDP 收 → 推論 → 本機播放（例如餵 BlackHole） |

瀏覽器直連時只有 local / remote 兩種（音訊走 WebSocket，約 370ms）；per-direction
是原生客戶端才有的。

## 實測數字（M4 Pro、48kHz、block 0.10s）

| | 推論中位數 | p95 | 尖峰率 |
|---|---|---|---|
| 純引擎，緊密迴圈 | 34.1ms | 35.3ms | 1/150 |
| 即時速率，**不防降頻** | 62.1ms | 84.0ms | 19/150 |
| 即時速率，**防降頻** | 33.4ms | 34.5ms | 0/150 |

**Apple Silicon 的 GPU 在工作斷斷續續時會降頻**，這是 RTF 規律性衝到 0.9 的
真正原因——不是瀏覽器、不是 WebSocket、不是網路。介面上的「防止 GPU 降頻」
會在區塊之間插入少量運算讓時脈維持住。細節見 `webui/engine.py` 的 `GpuWarmer`。

## 建議參數

| | | |
|---|---|---|
| chunkSec | 0.10 | 實測穩定；0.05 餘裕只剩 16%，偏緊 |
| crossfadeSec | 0.04 | **超過無效**，後端鎖在 `min(crossfade_frame, 4*zc)` |
| extraFrameSec | 2.5 | 不影響延遲，只影響每塊運算量與連貫度 |
| 音高演算法 | fcpe | **rmvpe 在這個分支是壞的**，見 NOTICE |
| RMS Mix | 1.00 | 低於 1 會多兩次 GPU 同步 |
| 防止 GPU 降頻 | 開 | 代價是持續耗電發熱 |

## 錄製

介面的「錄製」區塊可以把**變聲後的輸出**錄成 WAV 或 MP3（MP3 需要 `ffmpeg`）。

- 檔案寫在**伺服器這台機器**的 `recordings/`，介面上可以下載與刪除。
- 語音平常完全不落地，**錄製是唯一的例外**，而且只有你主動按下才會開始。
- 錄音檔**沒有到期自動刪除，會一直留到你自己刪**。它是個人語音資料，
  `recordings/` 已列入 `.gitignore`。
- 錄的是套用「輸出音量」之前的訊號，所以事後調喇叭不會改變錄好的檔案。

## 音量

輸入、輸出、監聽三段各自可調，單位 dB，滑到最底是靜音。輸入增益套在變聲**之前**
（所以輸入儀表顯示的就是模型實際收到的音量），輸出與監聽只影響放出來的聲音。

## 音訊裝置的坑

**程式啟動後才插上的裝置，要按介面上的「重新掃描裝置」才會出現。** PortAudio
在初始化時就把裝置列表定案，之後不再重掃，所以重新整理網頁沒有用。轉換中不能
重掃（會讓 coreaudiod 抱著沒有消費者的裝置空轉），請先按停止。

**輸入和輸出要用取樣率相同的裝置。** 不同的話 macOS 會建立聚合裝置，兩個時脈
漂移會造成規律性卡頓。藍牙輸出常見 44100Hz，配 48000Hz 的麥克風是典型踩雷組合
（介面偵測到會警告）。

## 開發

```bash
./check.sh           # Python/JS 語法、三語 i18n 鍵、DOM id、兩份重複定義是否同步
./sync_ui.command    # webui/static/ → rvc-client/static/
./bench.py           # 無頭延遲基準測試，不需要音訊裝置
```

有兩處是刻意重複、必須手動保持同步的，`check.sh` 兩個都會比對：

- **UDP 協定**：`webui/udp_audio.py` 與客戶端的 `udp_protocol.py`。
- **網頁介面**：`webui/static/` 與 `rvc-client/static/`。客戶端自己服務介面檔，
  這樣伺服器連不上時畫面還在、位址欄還改得到。改完介面跑 `./sync_ui.command`
  同步，再到客戶端重跑 `make_app.command`。

## 授權

AGPL-3.0。衍生關係與我們改了什麼，見 [NOTICE](NOTICE)。

上游的說明文件保留在 [README_MACOS_FORK.md](README_MACOS_FORK.md)（RVC-MacOS 的，
含 33 個內建音色清單）與 [README_ORIGINAL.md](README_ORIGINAL.md)（RVC 本家的）。
