# 交接文件

2026-09-18 一整天的工作紀錄。寫給接手的人（可能是未來的你，或另一個 session）。

---

## 一、這個專案在幹嘛

**原始目標**：w-okada 的 VCClient 在這台 M4 Pro 上延遲約 300ms，因為它只能用 CPU。
想改用 RVC-MacOS（能用 MPS）當後端，但保留 w-okada 的介面。

**實際做出來的東西**：從 RVC-MacOS fork 出來，自己寫了一整套 Web 介面與網路層，
再加上一個跑在另一台機器上的原生客戶端。

**延遲的演進**：

| 階段 | 延遲 |
|---|---|
| w-okada（CPU） | ~300ms |
| RVC-MacOS 桌面 GUI（MPS） | ~150ms |
| 自己的 Web 介面，瀏覽器收放音 | ~370ms |
| 自己的原生客戶端（抖動緩衝 30ms） | ~185ms |
| **現況**（抖動緩衝 150ms，為了避開掉包） | **~305ms** |

最後那一列是**暫時的權宜**。回程 FEC 已經做進去了（第六節），目的就是把它
調回 30ms、拿回那 120ms——但**還沒在真的 WiFi 上驗收過**，那是下一步。

---

## 二、現況：能跑什麼

### 伺服器（Mac mini，`/Users/lisen/rvc-macos-test`）

```bash
./start_webui.command              # 綁 0.0.0.0，需要 token
./start_webui_behind_proxy.command # 前面有 nginx 時用（--no-token --trust-lan）
./start_gui.command                # 上游的桌面 GUI
```

### 客戶端（另一台機器，`rvc-client/`，獨立 repo）

```bash
./make_app.command    # 產出 RVC Client.app，雙擊即可
```

### GitHub

- 伺服器 https://github.com/lisenlisen2li/Lisen-RVC-Server
- 客戶端 https://github.com/lisenlisen2li/Lisen-RVC-Client

兩個都是 private、AGPL-3.0。伺服器的 `upstream` remote 指向 chaye7417/RVC-MacOS，
保留完整衍生歷史。**客戶端更新方式就是 `git pull` + 重跑 `make_app.command`。**

### 目前的建議設定

| | | |
|---|---|---|
| chunkSec | 0.10 | |
| crossfadeSec | 0.04 | **超過無效**，後端鎖在 `min(crossfade_frame, 4*zc)` |
| extraFrameSec | 2.5 | 不影響延遲，只影響每塊運算量 |
| 音高演算法 | **fcpe** | **rmvpe 在這個分支是壞的**，見第五節 |
| RMS Mix | 1.00 | 低於 1 會多兩次 GPU 同步 |
| 防止 GPU 降頻 | **開** | 見第四節，這是最重要的一個開關 |
| 回程 FEC | **開**（客戶端預設） | XOR 同位，掉一片當場補回來，見第六節 |
| 抖動緩衝 | 150ms → **試 30ms** | FEC 做完後應該能調回去，**待實測**，見第六節 |

---

## 三、架構

```
Mac mini（模型在這）                      客戶端機器
┌────────────────────────┐              ┌──────────────────────┐
│ webui/server.py        │              │ rvc_client.py        │
│   FastAPI              │◄── REST ────►│   音訊引擎           │
│ webui/engine.py        │              │   sounddevice        │
│   推論核心 + GpuWarmer │◄── UDP ─────►│   環形緩衝           │
│ webui/audio_router.py  │              │ client_ui.py         │
│   四種音訊路由         │              │   本機介面伺服器     │
│ webui/static/          │──轉發介面───►│   pywebview 視窗     │
└────────────────────────┘              └──────────────────────┘
```

**關鍵設計**：客戶端的瀏覽器視窗**只畫介面，不碰音訊**。音訊走
sounddevice ←→ UDP，所以沒有瀏覽器那條路徑的抖動。

### 音訊路由：輸入與輸出各自選邊

`webui/audio_router.py` 的 `AudioRouter` 處理四種組合：

| source | sink | 做法 |
|---|---|---|
| server | server | 單一 duplex `sd.Stream`。同一時脈、延遲最低 |
| client | client | UDP 收 → 推論 → UDP 送 |
| server | client | 本機 InputStream → 推論 → UDP 送 |
| client | server | UDP 收 → 推論 → 環形緩衝 → 本機 OutputStream |

瀏覽器直連時只有 local/remote 兩種（走 WebSocket），per-direction 是客戶端才有。

### UDP 協定

定義在兩個地方（刻意重複，讓客戶端資料夾能獨立複製）：
`webui/udp_audio.py` 與 `rvc-client/udp_protocol.py`。**改協定時兩邊都要改**，
`./check.sh` 會比對。

一個區塊切成 8 個分片（`CHUNK_SAMPLES = 600`），因為 macOS 的
`net.inet.udp.maxdgram` 只有 9216 bytes，而乙太網路 MTU 是 1500。

**回程多一片 XOR 同位**（`flags` 的 bit1 = `FLAG_PARITY`，`idx` 等於 `cnt`）。
`cnt` 仍然只算資料片，所以「湊齊了沒」的判斷完全沒變。客戶端在 `/api/start`
帶 `"fec": true` 要求，伺服器預設不送——沒更新的客戶端行為完全不變。

---

## 四、最重要的發現：GPU 降頻

**Apple Silicon 的 GPU 在工作斷斷續續時會降頻。** 這是 RTF 規律性衝到 0.9、
音訊斷斷續續的真正原因——跟瀏覽器、WebSocket、網路**全都無關**。

零網路、零瀏覽器的對照實驗（block 0.10，150 塊）：

| | 中位數 | p95 | 尖峰 |
|---|---|---|---|
| 緊密迴圈（背靠背跑） | 34.1ms | 35.3ms | 1/150 |
| 即時速率（等 0.1 秒） | 62.1ms | 87.5ms | 19/150 |

**光是「按即時速率跑」就慢 79%、尖峰率 19 倍。**

解法是 `engine.GpuWarmer`：在等輸入的空檔做 512×512 matmul 讓 GPU 維持頻率。
WebSocket 路徑實測尖峰 13/150 → **0/150**、最慢 100 → 77ms。

**兩個實作限制**（都踩過）：
- 保溫必須與推論**同一條執行緒**。MPS 的 command buffer 不能跨執行緒同時提交，
  會直接 assert failure 掛掉。
- 用鎖序列化**不行**——保溫迴圈放開鎖立刻重取，Python 的鎖不保證公平，
  處理執行緒會被餓死（實測整個卡住）。
- 正解是把等輸入的逾時縮到 2ms，逾時就 tick 一次。
- 因此**保溫只在 `source == "client"` 時運作**；`source == "server"` 時推論跑在
  CoreAudio 回呼裡，沒有空檔可塞。

---

## 五、這個 fork 的品質問題

上游 `chaye7417/RVC-MacOS` 只有 13 個 commit，全是文件與 UI，**沒有任何效能或
Apple Silicon 相關改動**。README 宣稱的「針對 Apple Silicon 優化」不存在——
MPS 支援是上游 RVC 本來就有的。

**`rmvpe` 是壞的，而且沒修。** `rvc/f0/models.py` 和 `rvc/f0/rmvpe.py` 兩處都把
RMVPE 建成五層玩具 CNN，但真正的 checkpoint 是 741 鍵的 U-Net+GRU。上游 RVC
根本沒有 `rvc/f0/models.py` 這個檔案，是這個 fork 自己加的佔位程式碼。
**只能用 fcpe。**

`install.sh` 原本連不到 huggingface.co 時會退回 `hf-mirror.com`（中國境內鏡像站），
已移除。

---

## 六、回程封包遺失：已加 FEC，等實機驗收

**程式已經寫好也測過了，但還沒在真的 WiFi 上跑過。接手的第一件事是驗收。**

### 症狀（原始問題）

抖動緩衝設 30ms 時，`underruns` 穩定往上爬（約每 1.5 秒一次），音訊斷斷續續。
調到 150ms 就順暢，但**延遲從 185ms 變成 305ms**，幾乎抵消了寫客戶端的全部收益。

### 診斷結論

介面上的「遺失區塊」顯示 **`27 / 1`**（客戶端收到的序號缺口 / 伺服器湊不齊分片）。

**絕大多數是回程（伺服器 → 客戶端）掉包，去程幾乎沒事。** 客戶端走 WiFi，
而伺服器是有線——掉包集中在基地台發給無線端那一段，跟這個不對稱吻合。

算式：每 1.5 秒吃掉 30ms 緩衝 = 每秒短缺 20ms = **2%**。時脈漂移做不到這個量級
（消費級裝置通常是百萬分之 10~100），這是「整塊沒到」的量級。而一個區塊被切成
8 個分片、**少一片就整塊丟棄**，所以 0.25% 的封包遺失會被放大成 2% 的區塊遺失。

伺服器端的推論本身沒問題：本機 60 秒測試 p50 54.5ms、p99 66.5ms、
600 塊只有 1 塊超過七成預算、**零 underrun**。

### 已經做了什麼

選項 1（回程 XOR 同位）已實作：

- `webui/audio_router.py` 的 `_emit()` 在 8 片資料後多送一片 XOR 同位。
- `rvc-client/rvc_client.py` 的 `rx_loop()` 在「剛好缺一片」時當場算回來。
- 頻寬多 12.5%，**延遲一點都沒加**——沒掉包時 8 片就湊齊直接播，同位片隨後
  到達直接丟掉；掉一片才用得上它。
- 客戶端預設開啟，`--no-fec` 可關掉（就是拿來做 A/B 的）。
- 伺服器**預設關閉**，要客戶端在 `/api/start` 帶 `"fec": true` 才送，所以舊的
  客戶端完全不受影響。

**救不了的情況**：一個區塊掉兩片以上。XOR 同位只能救一片。

### 驗收方式（下一個人要做的）

1. 客戶端抖動緩衝設 **30ms**，正常用幾分鐘。
2. 看介面上的 **FEC 救回**（命令列是 `fec-fixed`）和 **underruns**：
   - 救回在爬、underruns 不爬 → **成功**，延遲應該回到 ~185ms。
   - 兩個都在爬 → 掉包是**成串**的（一次掉兩片以上），XOR 救不了。
     這時候做選項 2（封包遺失隱藏），或先把 WiFi 換成有線確認上限。
   - 救回一直是 0、underruns 也不爬 → 那天網路剛好很乾淨，多測幾次。
3. 想量 FEC 到底救了多少，用 `--no-fec` 跑同樣的時間對照。

### 如果 FEC 不夠用，剩下的選項

1. **封包遺失隱藏**。偵測到序號缺口時用前一塊的尾端補上（加淡出），而不是讓
   緩衝見底。聽感會比直接靜音好很多，而且不增加頻寬。跟 FEC 可以疊加。
2. **換有線**。最省力，也能直接判斷「WiFi 是不是唯一元凶」。
3. **送出時打散一點**。現在一個區塊的 9 個封包是背靠背送出去的，基地台的佇列
   可能就是在這種突發下丟包——這也能解釋為什麼掉包是成串的。沒試過。
4. 加大 `CHUNK_SAMPLES` 減少分片數。但會撞上 MTU 與 `udp.maxdgram`，除非確定
   網路支援 jumbo frame，否則不建議。

### 診斷工具

`/api/status` 回傳的 `timing` 有 p50/p90/p99/max 與超過預算的塊數（600 塊滑動視窗）。
`audio` 裡有 `lost_blocks`、`incomplete`、`bad`，以及 `fec`、`parity_sent`。
客戶端的 `/client/status` 有 `lost`、`recovered`、`underruns`、`drift_dropped`、
`buffer_ms`。

**`lost` 的語意變了**：救回來的區塊會正常播出，不再算進 `lost`。所以現在
`lost` 是「真的沒救回來的」，`recovered` 是「FEC 救回來的」，兩個要一起看。

---

## 七、踩過的坑（都已修，但別再踩一次）

### 會讓行程直接掛掉的

**CoreAudio 即時執行緒的堆疊只有約 512KB。** librosa 的 numba JIT、f0 估計器的
惰性載入（`rvc/f0/gen.py` 的 `if not hasattr`）若在那裡首次執行會堆疊溢位，
以 **SIGBUS** 讓整個行程閃退，而且 Python 層看不到任何例外。
→ `gui.py` 與 `engine.py` 都在開串流前跑一次完整的 `warmup()`。

**`threading.stack_size()`**：macOS 的 pthread 預設堆疊也只有 512KB，所以所有
處理執行緒都要用 `engine.spawn_worker()` 建立（會設成 16MB）。

### 會讓整台電腦當機的

**coreaudiod 空轉。** PortAudio 在串流還活著時被 `sd._terminate()`，或行程沒關
串流就死掉，已註冊的 IOProc 不會解除註冊，coreaudiod 會抱著沒有消費者的裝置
持續空轉，每跑一次疊一個孤兒，最後整台當機。

**反覆 kill coreaudiod 只會惡化。** 正確順序是先殺客戶端
（`pkill -f "gui.py|webui.server"`），等 10 秒，真的還在轉才 `sudo killall coreaudiod`
**一次**。

已修三處：`list_audio_devices()` 只在閒置時重新初始化 PortAudio；
`update_devices()` 先 `stop_stream()` 再 `_terminate()`；`stop_stream()` 改為
無條件且可重複呼叫（原本是 `if flag_vc:` 才關，而 flag_vc 為 False 但串流還開著
正是洩漏狀態）。兩處都加了 `atexit`。

### 音訊裝置

**輸入與輸出取樣率不同 → macOS 建立聚合裝置 → 兩個時脈漂移 → 規律性卡頓。**
藍牙輸出常見 44100Hz，配 48000Hz 麥克風是典型踩雷組合。介面會偵測並警告。
（這是最早期卡頓的真正原因，查了很久。）

**CoreAudio -10863 `cannot do in current context`**：同一個裝置要同時開
InputStream 和 OutputStream 時，必須**先全部建立、再一起 start**。建一個就 start
一個的話第二個會失敗，症狀是完全沒有音訊但沒有 Python 例外。

### 其他

**torchfcpe 會在即時回呼裡 `print`**。重採樣過衝讓峰值略超 ±1 就會觸發，而在
音訊回呼裡做 stdout I/O 會造成斷續。→ 推論前 `torch.clamp_(input_wav_res, -1, 1)`。
訊息裡的 "error" 是誤導，它只是 print，不影響計算。

**遠端音訊一定要 HTTPS**。`navigator.mediaDevices` 只存在於安全情境
（https、localhost、127.0.0.1），用區網 IP 走 http 時它是 `undefined`，
錯誤訊息是 `Cannot read properties of undefined (reading 'getUserMedia')`。

**UDP 走不過反向代理**。nginx 是 HTTP 代理，不轉發 UDP。`--server` 必須直接指向
跑模型那台機器。客戶端已加偵測（送出 20 塊仍零接收就印診斷）。

**nginx 反向代理務必設 `proxy_buffering off` + `tcp_nodelay on`**，預設的緩衝會
直接疊延遲。WebSocket 還要 `proxy_read_timeout 3600s` 和
`map $http_upgrade $connection_upgrade`。範例在 `webui/nginx-rvc.conf.example`。

---

## 八、我自己犯過的錯（供警惕）

**量測方法錯導致診斷全錯。** 早期只送 10~15 個區塊就讀一次 `infer_time_ms`，
那是單點取樣加上暖機期，嚴重高估。因此誤判成「WebSocket 路徑讓推論從 43ms
膨脹到 76ms」並據此寫了一整條 UDP 通道——結果嚴謹 A/B 後發現 UDP 和 WebSocket
完全一樣（58ms vs 58ms），**傳輸層從來就不是瓶頸**。
正確做法：跑 40 塊以上、過程中週期取樣、取中位數與百分位數。

**`.gitignore` 不支援行內註解。** `/rvc-client/  # 註解` 整行被當成樣式，完全失效。

**`//` 註解吃掉了 `) {`。** 在 `for (const [a,b] of [...])` 後面加行內註解，
導致整個 app.js 語法錯誤、前端完全不動。之後寫了 `./check.sh`（用 macOS 內建的
JavaScriptCore 檢查 JS），每次改完都先跑。

**先去尾端斜線再判協定**，導致 `http://` 變成 `http://http:/`。shell 和 Python
兩個驗證器都寫錯同一個地方。順序必須是先判協定、再動斜線。

**串流建立順序**：重構時把「先建立全部、再一起 start」改成「建一個就 start 一個」，
撞上 CoreAudio -10863。

**淺層 clone 推不上 GitHub**：`git clone --depth 1` 建的目錄推送時會
`did not receive expected object`，要先 `git fetch --unshallow upstream`。

---

## 九、開發工具

```bash
./check.sh     # Python/JS 語法、三語 i18n 鍵、DOM id、UDP 協定兩份定義是否同步
./bench.py     # 無頭延遲基準測試，不需要音訊裝置
./bench_all.command  # 掃不同 block_time 的可行性
```

**改完一定先跑 `./check.sh`。** 它是踩過前面那些坑之後長出來的。

FEC 的還原邏輯有寫過端到端測試（真的開 UDP socket、真的用兩邊的正式程式碼，
刻意丟分片再比對是否位元級一致），涵蓋：不掉包、掉第一片／中間／最後一片、
最後一片不滿 600 樣本的區塊、掉兩片救不了、同位片自己掉了。測試沒留在 repo 裡，
要改協定的話值得重寫一份。

---

## 十、沒做但討論過的

- **自動更新**：本來要做「檢查版本 + 下載 + 驗證雜湊 + 原子性換檔」，改用 GitHub
  之後 `git pull` 就夠了，不做。若想要「開啟時提示有新版」，建議只提示、
  不自動拉，避免在使用者不知情時改變執行中的程式碼。
- **瀏覽器路徑的 per-direction**：瀏覽器直連時仍是舊的 local/remote 二選一。
  要支援得把 WebSocket 也接進 `AudioRouter`。
- **監聽輸出未實機驗證**：邏輯寫好了（兩條獨立的 OutputStream 配各自的環形緩衝，
  刻意不綁在一起以避開聚合裝置漂移），但測試機湊不出雙輸出情境。
- **Windows 的 `start_client.bat`**：照慣例寫的，沒有 Windows 可測。
