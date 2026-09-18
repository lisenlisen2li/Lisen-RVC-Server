"""把音訊在「本機裝置」與「UDP 客戶端」之間接起來。

輸入端與輸出端各自可以是 server 或 client，四種組合：

    source  sink    伺服器做的事
    ------  ------  --------------------------------------------------
    server  server  單一 duplex sd.Stream。收放音共用一個時脈，延遲最低
    client  client  UDP 收 → 推論 → UDP 送
    server  client  本機 InputStream → 推論 → UDP 送給已註冊的客戶端
    client  server  UDP 收 → 推論 → 環形緩衝 → 本機 OutputStream

拆成 source/sink 兩個獨立選項而不是一個「模式」，是因為混合的組合真的有用：
把麥克風接在伺服器、輸出到客戶端的耳機，或是用客戶端的麥克風、把結果送進
伺服器上的 BlackHole 餵給那台機器上的通話軟體。

**GPU 保溫只在 source == "client" 時運作。** 保溫必須跟推論在同一條執行緒
（MPS 的 command buffer 不能跨執行緒同時提交），而只有 UDP 迴圈有等待輸入的
空檔可以塞。source == "server" 時推論跑在 CoreAudio 的回呼裡，那裡不能空轉。
"""

import socket
import threading
import time

import numpy as np

from webui.udp_audio import (
    HEADER, MAGIC_IN, MAGIC_OUT, VERSION, FLAG_INT16, FLAG_PARITY,
    CHUNK_SAMPLES, REGISTER, SOCK_BUF, chunk_count, xor_parity,
)

REASSEMBLY_TIMEOUT = 0.5


class Ring:
    """取樣層級環形緩衝。sink == "server" 且音訊從 UDP 來時，用它接在
    網路執行緒與音訊回呼之間。"""

    def __init__(self, capacity: int):
        self.buf = np.zeros(capacity, dtype=np.float32)
        self.cap = capacity
        self._w = 0
        self._n = 0
        self._lock = threading.Lock()
        self.underruns = 0
        self.dropped = 0

    def level(self):
        with self._lock:
            return self._n

    def write(self, data):
        with self._lock:
            n = len(data)
            if n > self.cap:
                data = data[-self.cap:]; n = self.cap
            end = self._w + n
            if end <= self.cap:
                self.buf[self._w:end] = data
            else:
                k = self.cap - self._w
                self.buf[self._w:] = data[:k]
                self.buf[:end - self.cap] = data[k:]
            self._w = end % self.cap
            self._n = min(self.cap, self._n + n)

    def read(self, n):
        with self._lock:
            out = np.zeros(n, dtype=np.float32)
            take = min(n, self._n)
            if take:
                start = (self._w - self._n) % self.cap
                end = start + take
                if end <= self.cap:
                    out[:take] = self.buf[start:end]
                else:
                    k = self.cap - start
                    out[:k] = self.buf[start:]
                    out[k:take] = self.buf[:end - self.cap]
                self._n -= take
            if take < n:
                self.underruns += 1
            return out

    def drop(self, n):
        with self._lock:
            d = min(n, self._n)
            self._n -= d
            self.dropped += d


class AudioRouter:
    def __init__(self, engine):
        self.engine = engine
        self.source = self.sink = "server"
        self.session = 0
        self.port = 0
        self.peer = None
        self.sock = None
        self._stop = threading.Event()
        self._streams = []
        self._pending = {}
        self._last_seq = None
        self._seq_out = 0
        self.ring = None
        self.jitter_ms = 30.0
        self.fec = False
        self._playing = False
        # 統計
        self.blocks_in = self.blocks_out = 0
        self.lost_blocks = self.incomplete = self.bad = 0
        self.parity_sent = 0
        self.last_rx = 0.0

    # ---------- 生命週期 ----------

    def start(self, source, sink, in_dev=None, out_dev=None,
              udp_host="0.0.0.0", udp_port=7867, session=0, jitter_ms=30.0,
              fec=False):
        import sounddevice as sd

        self.stop()
        self._stop.clear()
        self.source, self.sink, self.session = source, sink, session
        self.jitter_ms = jitter_ms
        # 只有送給客戶端才需要同位片。sink == "server" 時結果直接進環形緩衝，
        # 根本沒有網路可掉。
        self.fec = bool(fec) and sink == "client"
        self.blocks_in = self.blocks_out = 0
        self.lost_blocks = self.incomplete = self.bad = 0
        self.parity_sent = 0
        self._pending.clear()
        self._last_seq = None
        self._seq_out = 0
        self._playing = False

        sr = self.engine.params.samplerate
        blk = self.engine.block_frame

        # UDP 只在任一端走客戶端時才需要
        if "client" in (source, sink):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCK_BUF)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCK_BUF)
            except OSError:
                pass
            s.bind((udp_host, udp_port))
            s.settimeout(0.002)     # 短逾時，空檔用來做 GPU 保溫
            self.sock = s
            self.port = s.getsockname()[1]

        if sink == "server" and source == "client":
            target = int(sr * jitter_ms / 1000)
            self.ring = Ring(target + blk * 4)

        from webui.engine import spawn_worker

        if source == "server" and sink == "server":
            self._start_duplex(sd, in_dev, out_dev, sr, blk)
        else:
            if source == "server":
                self._start_input(sd, in_dev, sr, blk)
            if sink == "server":
                self._start_output(sd, out_dev, sr, blk)
            if self.sock is not None:
                spawn_worker(self._udp_loop, name="vc-udp")

        return self.port

    def stop(self):
        self._stop.set()
        for st in self._streams:
            try:
                st.abort(); st.close()
            except Exception:
                pass
        self._streams = []
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
        self.peer = None
        self.ring = None

    def stats(self):
        return {
            "source": self.source,
            "sink": self.sink,
            "port": self.port,
            "peer": f"{self.peer[0]}:{self.peer[1]}" if self.peer else None,
            "blocks_in": self.blocks_in,
            "blocks_out": self.blocks_out,
            "lost_blocks": self.lost_blocks,
            "incomplete": self.incomplete,
            "bad": self.bad,
            "fec": self.fec,
            "parity_sent": self.parity_sent,
            "idle_sec": round(time.time() - self.last_rx, 1) if self.last_rx else None,
            "ring_ms": round(self.ring.level() / self.engine.params.samplerate * 1000, 1)
                       if self.ring else None,
            "ring_underruns": self.ring.underruns if self.ring else None,
        }

    # ---------- 本機音訊串流 ----------

    def _start_duplex(self, sd, in_dev, out_dev, sr, blk):
        """收放音都在伺服器：一條 duplex 串流，單一時脈，延遲最低。"""
        import librosa

        ch = self._out_channels(sd, out_dev)

        def cb(indata, outdata, frames, t, status):
            mono = librosa.to_mono(indata.T)
            out = self.engine.process(mono)
            # 輸出音量套在播放這一端，不套在 engine 裡：錄音要的是沒被喇叭
            # 音量動過的原始轉換結果。
            g = self.engine.out_gain
            if g != 1.0:
                out = np.clip(out * g, -1.0, 1.0)
            outdata[:] = np.repeat(out[:, None], outdata.shape[1], axis=1)
            self.blocks_in += 1
            self.blocks_out += 1

        st = sd.Stream(device=(in_dev, out_dev), callback=cb, blocksize=blk,
                       samplerate=sr, channels=ch, dtype="float32")
        st.start()
        self._streams.append(st)

    def _start_input(self, sd, in_dev, sr, blk):
        """麥克風在伺服器、輸出在客戶端：本機收音 → 推論 → UDP 送出。"""
        import librosa

        def cb(indata, frames, t, status):
            mono = librosa.to_mono(indata.T)
            self.blocks_in += 1
            try:
                out = self.engine.process(mono)
            except Exception:
                return
            self._emit(out)

        st = sd.InputStream(device=in_dev, channels=1, samplerate=sr,
                            blocksize=blk, dtype="float32", callback=cb)
        st.start()
        self._streams.append(st)

    def _start_output(self, sd, out_dev, sr, blk):
        """輸出在伺服器：從環形緩衝讀出來播。音訊來自 UDP，所以要抖動緩衝。"""
        target = int(sr * self.jitter_ms / 1000)
        high = target + blk * 2
        ring = self.ring

        def cb(outdata, frames, t, status):
            if not self._playing:
                if ring.level() < target + frames:
                    outdata.fill(0)
                    return
                self._playing = True
            if ring.level() > high:
                ring.drop(ring.level() - target)   # 時脈漂移補償
            data = ring.read(frames)
            if ring.level() == 0:
                self._playing = False
            # 套在讀出來之後而不是寫進去之前，調整才會立刻聽得到，
            # 不必等緩衝區裡舊的音訊先放完。
            g = self.engine.out_gain
            if g != 1.0:
                data = np.clip(data * g, -1.0, 1.0)
            outdata[:] = (data.reshape(-1, 1) if outdata.shape[1] == 1
                          else np.repeat(data[:, None], outdata.shape[1], axis=1))

        st = sd.OutputStream(device=out_dev, channels=self._out_channels(sd, out_dev),
                             samplerate=sr, blocksize=max(64, blk // 8),
                             dtype="float32", callback=cb)
        st.start()
        self._streams.append(st)

    @staticmethod
    def _out_channels(sd, dev):
        try:
            return min(2, sd.query_devices(dev if dev is not None
                                           else sd.default.device[1])["max_output_channels"]) or 1
        except Exception:
            return 1

    # ---------- 輸出路由 ----------

    def _emit(self, out: np.ndarray):
        """把轉換結果送到 sink。"""
        if self.sink == "server":
            if self.ring is not None:
                self.ring.write(out)
            self.blocks_out += 1
            return
        # sink == "client"
        if self.sock is None or self.peer is None:
            return
        o16 = np.clip(out * 32768.0, -32768, 32767).astype("<i2")
        cnt = chunk_count(len(o16))
        seq = self._seq_out
        self._seq_out = (self._seq_out + 1) & 0xFFFFFFFF
        parts = [o16[i * CHUNK_SAMPLES:(i + 1) * CHUNK_SAMPLES] for i in range(cnt)]
        try:
            for i, part in enumerate(parts):
                self.sock.sendto(
                    HEADER.pack(MAGIC_OUT, VERSION, FLAG_INT16,
                                self.session, seq, i, cnt) + part.tobytes(), self.peer)
            if self.fec:
                # 同位片排在資料片後面。即時音訊沒有重傳的餘裕——等重傳回來
                # 那塊音訊早就該播完了——所以掉的那片只能當場算回來。
                # 一個區塊 8 片，多送這一片是 12.5% 的頻寬，區網上無感。
                self.sock.sendto(
                    HEADER.pack(MAGIC_OUT, VERSION, FLAG_INT16 | FLAG_PARITY,
                                self.session, seq, cnt, cnt)
                    + xor_parity(parts).tobytes(), self.peer)
                self.parity_sent += 1
            self.blocks_out += 1
        except OSError:
            pass

    # ---------- UDP ----------

    def _udp_loop(self):
        sock = self.sock
        prune_tick = 0
        warm = self.source == "client"     # 只有這種組合下 UDP 迴圈擁有 MPS
        while not self._stop.is_set() and sock is not None:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                if warm:
                    self.engine.warmer.tick()
                prune_tick += 1
                if prune_tick >= 100:
                    prune_tick = 0
                    self._prune()
                continue
            except OSError:
                break

            if len(data) < HEADER.size:
                self.bad += 1
                continue
            magic, ver, flags, sess, seq, idx, cnt = HEADER.unpack_from(data, 0)
            if magic != MAGIC_IN or ver != VERSION or sess != self.session:
                self.bad += 1
                continue

            self.peer = addr
            self.last_rx = time.time()

            if cnt == REGISTER:
                continue        # 只是註冊位址，沒有音訊

            if self.source != "client":
                continue        # 這個組合不收客戶端的音訊

            if idx >= cnt or (flags & FLAG_PARITY):
                # 去程不做 FEC，客戶端不會送同位片。這一關主要是防呆：idx 超出
                # 範圍的封包若進了湊片字典，「湊齊了嗎」的計數會被灌滿，但真正
                # 的資料片其實還缺，下面的 concatenate 就 KeyError，整條 UDP
                # 執行緒無聲無息地死掉，症狀是音訊毫無預警全停。
                self.bad += 1
                continue

            ent = self._pending.get(seq)
            if ent is None:
                ent = self._pending[seq] = {"parts": {}, "cnt": cnt, "t": time.time()}
            ent["parts"][idx] = np.frombuffer(data, dtype="<i2", offset=HEADER.size)
            if len(ent["parts"]) < ent["cnt"]:
                continue

            self._pending.pop(seq, None)
            self._prune()

            pcm = np.concatenate([ent["parts"][i] for i in range(ent["cnt"])])
            if not self.engine.ready or len(pcm) != self.engine.block_frame:
                self.bad += 1
                continue

            self.blocks_in += 1
            if self._last_seq is not None:
                gap = (seq - self._last_seq - 1) & 0xFFFFFFFF
                if 0 < gap < 1000:
                    self.lost_blocks += gap
            self._last_seq = seq

            try:
                out = self.engine.process(pcm.astype(np.float32) / 32768.0)
            except Exception:
                continue
            self._emit(out)

    def _prune(self):
        if not self._pending:
            return
        cutoff = time.time() - REASSEMBLY_TIMEOUT
        for k in [k for k, v in self._pending.items() if v["t"] < cutoff]:
            self._pending.pop(k, None)
            self.incomplete += 1
