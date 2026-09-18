"""原始 UDP 音訊通道。

存在的理由：實測 WebSocket 路徑（asyncio 事件迴圈 + websockets 的 Python 層
框架解析 + 佇列交接）讓推論中位數從純引擎的 37.9ms 升到 58ms，尖峰率從
0.25% 升到 10%。這條通道完全不碰 asyncio——單一執行緒 recvfrom → 湊齊分片
→ 就地推論 → sendto，中間沒有任何佇列或執行緒交接。

封包格式（小端序，標頭 16 bytes）：
    magic  4s   送出用 b'RVCA'，回覆用 b'RVCB'
    ver    B    通訊協定版本
    flags  B    0 = int16 PCM
    sess   I    /api/start 發的工作階段編號，用來擋掉不相干的封包
    seq    I    區塊序號
    idx    B    這是該區塊的第幾個分片
    cnt    B    該區塊共有幾個分片
    pcm    int16[<= CHUNK_SAMPLES]

為什麼要自己切分片：
  · macOS 的 net.inet.udp.maxdgram 預設 9216 bytes，一個 4800 樣本的區塊
    換成 int16 是 9600 bytes，直接送會 EMSGSIZE。
  · 乙太網路 MTU 1500，不切的話會被 IP 分片，掉任何一片就整塊遺失，而且
    核心層的重組失敗我們看不到。自己切就能精確知道少了哪一片。

用 int16 而不是 float32：頻寬砍半，而 16 bit 對語音是 CD 品質，聽不出差異。
引擎本來就會把訊號夾在 ±1，換算無損。
"""

import socket
import struct
import threading
import time

import numpy as np

HEADER = struct.Struct("<4sBBIIBB")
MAGIC_IN = b"RVCA"
MAGIC_OUT = b"RVCB"
VERSION = 1
FLAG_INT16 = 0

# 每個分片的樣本數。16 + 600*2 = 1216 bytes，安全地低於 1500 的 MTU
# （還要扣掉 IP 20 + UDP 8 的標頭）。
CHUNK_SAMPLES = 600

# cnt == 0 代表「註冊封包」：沒有音訊酬載，只是讓伺服器知道客戶端的位址。
# 「輸入走伺服器、輸出走客戶端」時伺服器收不到音訊封包，沒有這個就不知道
# 要把轉換結果送去哪。客戶端在那種組合下會週期性送這種封包當作 keepalive。
REGISTER = 0

# 收送緩衝開大一點。推論落後時由核心緩衝吸收，不要讓封包在網路層就被丟掉。
SOCK_BUF = 1 << 20

# 分片湊不齊的區塊最多留這麼久，避免半套的區塊無限累積。
REASSEMBLY_TIMEOUT = 0.5


def chunk_count(n_samples: int) -> int:
    return (n_samples + CHUNK_SAMPLES - 1) // CHUNK_SAMPLES


def pack_block(pcm_i16: np.ndarray, session: int, seq: int, magic: bytes = MAGIC_IN):
    """把一個區塊切成多個待送封包。客戶端送音訊時用這個。"""
    cnt = chunk_count(len(pcm_i16))
    return [
        HEADER.pack(magic, VERSION, FLAG_INT16, session, seq, i, cnt)
        + pcm_i16[i * CHUNK_SAMPLES : (i + 1) * CHUNK_SAMPLES].tobytes()
        for i in range(cnt)
    ]


class UdpAudio:
    """UDP 音訊收發。一次只服務一個客戶端（最後送封包進來的那個）。"""

    def __init__(self, engine):
        self.engine = engine
        self.sock = None
        self._stop = threading.Event()
        self.session = 0
        self.port = 0
        self.peer = None
        self._pending = {}       # seq -> {"parts": {idx: ndarray}, "cnt": int, "t": float}
        self._last_seq = None
        self._prune_counter = 0
        # 統計，供 /api/status 顯示
        self.blocks_in = 0
        self.blocks_out = 0
        self.lost_blocks = 0
        self.incomplete = 0
        self.bad = 0
        self.last_rx = 0.0

    # ---------- 生命週期 ----------

    def start(self, host: str, port: int, session: int) -> int:
        self.stop()
        self._stop.clear()
        self.session = session
        self.blocks_in = self.blocks_out = self.lost_blocks = 0
        self.incomplete = self.bad = 0
        self._pending.clear()
        self._last_seq = None

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCK_BUF)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCK_BUF)
        except OSError:
            pass
        s.bind((host, port))
        # 短逾時：逾時就做一次 GPU 保溫，把等待封包的空檔填滿
        s.settimeout(0.002)
        self.sock = s
        self.port = s.getsockname()[1]

        # 一定要用 spawn_worker：推論路徑首次執行需要大堆疊，否則 SIGBUS
        from webui.engine import spawn_worker
        spawn_worker(self._loop, name="vc-udp")
        return self.port

    def stop(self):
        self._stop.set()
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
        self.peer = None

    def stats(self):
        return {
            "port": self.port,
            "peer": f"{self.peer[0]}:{self.peer[1]}" if self.peer else None,
            "blocks_in": self.blocks_in,
            "blocks_out": self.blocks_out,
            "lost_blocks": self.lost_blocks,
            "incomplete": self.incomplete,
            "bad": self.bad,
            "idle_sec": round(time.time() - self.last_rx, 1) if self.last_rx else None,
        }

    # ---------- 收送迴圈 ----------

    def _loop(self):
        """單一執行緒完成收、湊、算、送。刻意不切成多執行緒。

        推論就地執行：一個區塊 100ms 才來一次而推論只要約 38ms，餘裕充足，
        而省掉佇列與執行緒交接正是這條通道存在的目的。真的落後時，封包會留在
        核心的接收緩衝裡，不會遺失。
        """
        sock = self.sock
        while not self._stop.is_set() and sock is not None:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                # 空檔保溫。不做的話 GPU 會降頻，推論中位數從 33ms 掉到 62ms、
                # 而且會規律性出現 RTF 0.9 的尖峰。詳見 engine.GpuWarmer。
                self.engine.warmer.tick()
                self._prune_counter += 1
                if self._prune_counter >= 100:      # 約每 0.2 秒整理一次
                    self._prune_counter = 0
                    self._prune()
                continue
            except OSError:
                break   # socket 已關閉

            if len(data) < HEADER.size:
                self.bad += 1
                continue
            magic, ver, flags, sess, seq, idx, cnt = HEADER.unpack_from(data, 0)
            if magic != MAGIC_IN or ver != VERSION or sess != self.session:
                self.bad += 1
                continue

            self.peer = addr
            self.last_rx = time.time()

            ent = self._pending.get(seq)
            if ent is None:
                ent = self._pending[seq] = {"parts": {}, "cnt": cnt, "t": time.time()}
            ent["parts"][idx] = np.frombuffer(data, dtype="<i2", offset=HEADER.size)
            if len(ent["parts"]) < ent["cnt"]:
                continue                      # 還沒湊齊

            self._pending.pop(seq, None)
            self._prune()

            pcm = np.concatenate([ent["parts"][i] for i in range(ent["cnt"])])
            if not self.engine.ready or len(pcm) != self.engine.block_frame:
                self.bad += 1
                continue

            self.blocks_in += 1
            if self._last_seq is not None:
                gap = (seq - self._last_seq - 1) & 0xFFFFFFFF
                if 0 < gap < 1000:            # 排除重連後序號歸零的情況
                    self.lost_blocks += gap
            self._last_seq = seq

            try:
                out = self.engine.process(pcm.astype(np.float32) / 32768.0)
            except Exception:
                continue

            o16 = np.clip(out * 32768.0, -32768, 32767).astype("<i2")
            ocnt = chunk_count(len(o16))
            try:
                for i in range(ocnt):
                    part = o16[i * CHUNK_SAMPLES : (i + 1) * CHUNK_SAMPLES]
                    sock.sendto(
                        HEADER.pack(MAGIC_OUT, VERSION, FLAG_INT16,
                                    self.session, seq, i, ocnt) + part.tobytes(),
                        addr,
                    )
                self.blocks_out += 1
            except OSError:
                pass

    def _prune(self):
        """丟掉湊不齊的區塊，避免半套資料無限累積。"""
        if not self._pending:
            return
        cutoff = time.time() - REASSEMBLY_TIMEOUT
        stale = [k for k, v in self._pending.items() if v["t"] < cutoff]
        for k in stale:
            self._pending.pop(k, None)
            self.incomplete += 1
