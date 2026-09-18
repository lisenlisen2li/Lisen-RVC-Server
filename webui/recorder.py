"""錄製變聲後的輸出。

音訊平常完全不落地（見 server.py 開頭的說明），**錄製是唯一的例外**，而且
只有使用者在介面上明確按下才會開始。檔案寫在 ROOT/recordings/，留在磁碟上
直到使用者自己刪除；介面提供列表、下載與刪除三件事。隱私權政策那一節也要
跟著寫清楚，不要讓「不寫入磁碟」那句話變成謊話。

**寫檔絕對不能發生在即時路徑上。** engine.process() 在
source == sink == "server" 時是跑在 CoreAudio 的回呼裡，在那裡碰磁碟會直接
造成斷音。所以 feed() 只做一次 put_nowait，真正的寫入交給背景執行緒；佇列
滿了就丟棄並計數，寧可錄壞也不要卡住音訊。

錄的是「變聲後、套用輸出音量之前」的訊號，所以事後調喇叭音量不會改變錄音
內容——輸出音量是監聽用的，不該影響產出。
"""

import queue
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from shutil import which

import numpy as np

# 檔名由本模組產生，這個樣式同時也是刪除／下載時的白名單。
# 不符合的一律拒絕，避免有人用 ../ 跳出目錄。
NAME_RE = re.compile(r"^rvc-\d{8}-\d{6}\.(wav|mp3)$")

# 佇列上限。一塊 0.1 秒，2000 塊約 200 秒的緩衝——磁碟只要不是整整三分鐘
# 完全沒有回應，就不會掉資料。
QUEUE_BLOCKS = 2000

MP3_BITRATE = "192k"


class Recorder:
    """把 engine 產出的區塊寫成 wav 或 mp3。

    生命週期由 /api/record/start 與 /api/record/stop 驅動，feed() 則是由
    engine.process() 每塊呼叫一次。三者可能在不同執行緒，所以 start/stop
    用鎖序列化，feed 只碰一個區域變數複本。
    """

    def __init__(self, root: Path):
        self.dir = Path(root) / "recordings"
        self._lock = threading.Lock()
        self._q: queue.Queue = None
        self._thread: threading.Thread = None
        self._file = None
        self._path: Path = None
        self._wav_path: Path = None
        self.active = False
        self.fmt = "wav"
        self.samplerate = 48000
        self.frames = 0
        self.dropped = 0
        self.started_at = 0.0
        self.last_error = None

    # ---------- 狀態 ----------

    def status(self):
        return {
            "active": self.active,
            "format": self.fmt,
            "seconds": round(self.frames / self.samplerate, 1) if self.samplerate else 0.0,
            "dropped": self.dropped,
            "name": self._path.name if self._path else None,
            "error": self.last_error,
        }

    # ---------- 開始 / 結束 ----------

    def start(self, samplerate: int, fmt: str = "wav"):
        """開始錄製。回傳狀態字典。已在錄製中會丟 RuntimeError。"""
        fmt = (fmt or "wav").lower()
        if fmt not in ("wav", "mp3"):
            raise ValueError("format_not_supported")
        if fmt == "mp3" and not which("ffmpeg"):
            # 早點講。錄完才發現轉不了檔，使用者會以為錄音不見了。
            raise RuntimeError("ffmpeg_not_found")

        import soundfile as sf

        with self._lock:
            if self.active:
                raise RuntimeError("already_recording")

            self.dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            # 同一秒內連按兩次就會撞名，往後找到沒被佔用的那一秒。
            wav = self.dir / f"rvc-{stamp}.wav"
            final = self.dir / f"rvc-{stamp}.{fmt}"
            while wav.exists() or final.exists():
                stamp = datetime.fromtimestamp(
                    datetime.strptime(stamp, "%Y%m%d-%H%M%S").timestamp() + 1
                ).strftime("%Y%m%d-%H%M%S")
                wav = self.dir / f"rvc-{stamp}.wav"
                final = self.dir / f"rvc-{stamp}.{fmt}"

            # 一律先寫 wav，mp3 在 stop() 才轉。邊錄邊寫的好處是行程被砍掉
            # 時已經錄到的部分還留得住，而且記憶體用量固定。
            self._file = sf.SoundFile(
                str(wav), mode="w", samplerate=int(samplerate),
                channels=1, subtype="PCM_16",
            )
            self._path = final
            self._wav_path = wav
            self.samplerate = int(samplerate)
            self.fmt = fmt
            self.frames = 0
            self.dropped = 0
            self.last_error = None
            self.started_at = time.time()
            self._q = queue.Queue(maxsize=QUEUE_BLOCKS)
            self._thread = threading.Thread(
                target=self._writer, args=(self._q, self._file),
                name="rec-writer", daemon=True,
            )
            self._thread.start()
            self.active = True

        return self.status()

    def _writer(self, q: queue.Queue, f):
        """背景寫檔。None 是結束訊號。"""
        while True:
            block = q.get()
            if block is None:
                return
            try:
                f.write(block)
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                return

    def feed(self, block: np.ndarray):
        """收一個區塊。**這個函式可能在即時執行緒上執行，絕不能阻塞或丟例外。**"""
        q = self._q                     # 先取複本，避免同時 stop() 把它換掉
        if not self.active or q is None:
            return
        try:
            # 下游可能就地改寫（例如套用輸出音量），所以複製一份再排隊。
            # 一塊 4800 個 float32 約 19KB，這個複製便宜到看不出來。
            q.put_nowait(np.asarray(block, dtype=np.float32).copy())
            self.frames += len(block)
        except queue.Full:
            self.dropped += 1
        except Exception:
            pass                        # 錄製失敗絕不能連累音訊

    def stop(self):
        """結束錄製，必要時轉成 mp3。回傳這一次的檔案資訊。

        mp3 轉檔是同步的：FastAPI 會把同步端點丟到執行緒池，不會卡住事件
        迴圈，而 ffmpeg 大約是即時速度的幾十倍，幾分鐘的錄音只要幾秒。
        """
        with self._lock:
            if not self.active:
                return None
            self.active = False
            q, thread, f = self._q, self._thread, self._file
            self._q = self._thread = self._file = None

        try:
            q.put(None, timeout=5)
            thread.join(timeout=30)
        except Exception:
            pass
        try:
            f.close()
        except Exception:
            pass

        wav, final = self._wav_path, self._path
        if self.fmt == "mp3":
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav),
                     "-codec:a", "libmp3lame", "-b:a", MP3_BITRATE, str(final)],
                    check=True, capture_output=True, timeout=600,
                )
                wav.unlink(missing_ok=True)
            except Exception as e:
                # 轉檔失敗就把 wav 留著。使用者要的是錄到的聲音，
                # 格式只是偏好——絕不能因為轉檔失敗就把錄音刪掉。
                self.last_error = f"mp3 encode failed, kept the wav: {e}"
                final = wav
                self._path = wav

        return {
            "name": final.name,
            "bytes": final.stat().st_size if final.exists() else 0,
            "seconds": round(self.frames / self.samplerate, 1) if self.samplerate else 0.0,
            "dropped": self.dropped,
            "error": self.last_error,
        }

    # ---------- 檔案管理 ----------

    def list(self):
        """列出錄音檔，新的在前。"""
        if not self.dir.exists():
            return []
        out = []
        for p in self.dir.iterdir():
            if not NAME_RE.match(p.name):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({
                "name": p.name,
                "bytes": st.st_size,
                "mtime": int(st.st_mtime),
                "recording": self.active and self._path is not None
                             and p.name == self._path.name,
            })
        return sorted(out, key=lambda x: x["mtime"], reverse=True)

    def resolve(self, name: str):
        """把使用者給的檔名換成實際路徑，不合法或不存在就回 None。

        除了樣式白名單之外再檢查一次 parent，兩道關卡都過才動檔案——
        路徑遍歷是這個模組唯一會對外開放檔名的地方。
        """
        if not name or not NAME_RE.match(name):
            return None
        p = (self.dir / name).resolve()
        try:
            if p.parent != self.dir.resolve() or not p.is_file():
                return None
        except OSError:
            return None
        return p

    def delete(self, name: str):
        p = self.resolve(name)
        if p is None:
            return False
        if self.active and self._path is not None and p.name == self._path.name:
            return False            # 正在寫的那個不給刪
        try:
            p.unlink()
            return True
        except OSError:
            return False
