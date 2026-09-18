"""RVC 即時變聲處理核心。

從 gui.py 的 audio_callback 抽出，拿掉裝置 I/O，改成「一塊音訊進、一塊音訊出」
的純處理函式，讓本機 sounddevice 模式與瀏覽器 WebSocket 模式共用同一套邏輯。

保留了 gui.py 裡兩個關鍵修補：
  1. warmup()：即時執行緒堆疊只有約 512KB，librosa 的 numba JIT 與 f0 估計器
     的惰性載入若在該執行緒首次觸發會堆疊溢位，以 SIGBUS 閃退。
  2. 推論前 clamp 到 ±1：重採樣過衝會讓 torchfcpe 在即時路徑上 print 警告，
     在回呼裡做 stdout I/O 會造成音訊斷續。
"""

import os
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

import numpy as np
import torch
import torch.nn.functional as F
import torchaudio.transforms as tat
import librosa

from infer.lib import rtrvc
from infer.modules.gui import TorchGate
from configs.config import Config

# 即時執行緒的堆疊。macOS 的 pthread 預設只有 512KB，numba JIT 與模型惰性載入
# 都會在那裡溢位。所有處理執行緒都以這個大小建立。
WORKER_STACK_BYTES = 16 * 1024 * 1024


def phase_vocoder(a, b, fade_out, fade_in):
    """與 gui.py 相同的相位聲碼器，用於平滑區塊接縫。"""
    window = torch.sqrt(fade_out * fade_in)
    fa = torch.fft.rfft(a * window)
    fb = torch.fft.rfft(b * window)
    absab = torch.abs(fa) + torch.abs(fb)
    n = a.shape[0]
    if n % 2 == 0:
        absab[1:-1] *= 2
    else:
        absab[1:] *= 2
    phia = torch.angle(fa)
    phib = torch.angle(fb)
    deltaphase = phib - phia
    deltaphase = deltaphase - 2 * np.pi * torch.floor(deltaphase / 2 / np.pi + 0.5)
    w = 2 * np.pi * torch.arange(n // 2 + 1).to(a) + deltaphase
    t = torch.arange(n).unsqueeze(-1).to(a) / n
    return (
        a * (fade_out**2)
        + b * (fade_in**2)
        + torch.sum(absab * torch.cos(w * t + phia), -1) * window / n
    )


@dataclass
class Params:
    """使用者可調參數。名稱與 gui.py 的 GUIConfig 對齊，方便對照。"""

    pth_path: str = ""
    index_path: str = ""
    pitch: int = 0
    formant: float = 0.0
    index_rate: float = 0.0
    threhold: int = -60
    rms_mix_rate: float = 1.0
    block_time: float = 0.10
    crossfade_time: float = 0.04
    extra_time: float = 2.5
    f0method: str = "fcpe"
    use_pv: bool = True
    I_noise_reduce: bool = False
    O_noise_reduce: bool = False
    samplerate: int = 48000
    n_cpu: int = 4
    keep_warm: bool = True

    def to_dict(self):
        return asdict(self)


class VoiceChangerEngine:
    """持有模型與環形緩衝區，一次處理一個音訊區塊。

    process() 必須從單一執行緒呼叫，且該執行緒要以 WORKER_STACK_BYTES 的堆疊
    建立（用 spawn_worker() 建立即可）。
    """

    def __init__(self, device: str = "mps"):
        self.device = device
        # configs/config.py 的 Config 會自行解析 sys.argv，會把伺服器自己的參數
        # 當成非法選項而中止，所以建立時暫時清空 argv。
        _argv = sys.argv
        sys.argv = [_argv[0]]
        try:
            self.config = Config()
        finally:
            sys.argv = _argv
        self.config.device = device
        self.rvc: Optional[rtrvc.RVC] = None
        self.params = Params()
        self._lock = threading.Lock()
        self.ready = False
        self.infer_time_ms = 0.0
        self.block_count = 0
        self.in_level = 0.0    # 輸入音量 RMS，供介面儀表用
        self.out_level = 0.0   # 輸出音量 RMS
        self.warmer = GpuWarmer(device)

    # ---------- 設定 ----------

    def configure(self, params: Params):
        """載入模型並配置緩衝區。等同 gui.py 的 start_vc()，但不開音訊串流。

        必須在堆疊夠大的執行緒上呼叫（內含 warmup）。
        """
        with self._lock:
            self.ready = False
            p = self.params = params

            self.rvc = rtrvc.RVC(
                p.pitch,
                p.formant,
                p.pth_path,
                p.index_path,
                p.index_rate,
                p.n_cpu,
                self.device,
                False,  # use_jit
                False,  # is_half：MPS 走 fp32
                False,  # is_dml
            )

            sr = p.samplerate
            self.zc = sr // 100
            self.block_frame = (
                int(np.round(p.block_time * sr / self.zc)) * self.zc
            )
            self.block_frame_16k = 160 * self.block_frame // self.zc
            self.crossfade_frame = (
                int(np.round(p.crossfade_time * sr / self.zc)) * self.zc
            )
            # gui.py 的硬性上限：交叉淡化實際不會超過 4*zc（40ms）
            self.sola_buffer_frame = min(self.crossfade_frame, 4 * self.zc)
            self.sola_search_frame = self.zc
            self.extra_frame = (
                int(np.round(p.extra_time * sr / self.zc)) * self.zc
            )

            dev = self.device
            self.input_wav = torch.zeros(
                self.extra_frame
                + self.crossfade_frame
                + self.sola_search_frame
                + self.block_frame,
                device=dev,
                dtype=torch.float32,
            )
            self.input_wav_denoise = self.input_wav.clone()
            self.input_wav_res = torch.zeros(
                160 * self.input_wav.shape[0] // self.zc, device=dev, dtype=torch.float32
            )
            self.rms_buffer = np.zeros(4 * self.zc, dtype="float32")
            self.sola_buffer = torch.zeros(
                self.sola_buffer_frame, device=dev, dtype=torch.float32
            )
            self.nr_buffer = self.sola_buffer.clone()
            self.output_buffer = self.input_wav.clone()
            self.skip_head = self.extra_frame // self.zc
            self.return_length = (
                self.block_frame + self.sola_buffer_frame + self.sola_search_frame
            ) // self.zc

            self.fade_in_window = (
                torch.sin(
                    0.5
                    * np.pi
                    * torch.linspace(
                        0.0, 1.0, steps=self.sola_buffer_frame, device=dev,
                        dtype=torch.float32,
                    )
                )
                ** 2
            )
            self.fade_out_window = 1 - self.fade_in_window

            self.resampler = tat.Resample(
                orig_freq=sr, new_freq=16000, dtype=torch.float32
            ).to(dev)
            if self.rvc.tgt_sr != sr:
                self.resampler2 = tat.Resample(
                    orig_freq=self.rvc.tgt_sr, new_freq=sr, dtype=torch.float32
                ).to(dev)
            else:
                self.resampler2 = None

            self.tg = TorchGate(sr=sr, n_fft=4 * self.zc, prop_decrease=0.9).to(dev)

            self._warmup()
            self.warmer.enable(p.keep_warm)
            self.ready = True

    def _warmup(self):
        """在目前執行緒先跑完整條路徑，觸發所有惰性初始化。

        必要性見模組開頭說明：若首次執行落在小堆疊的即時執行緒，會 SIGBUS。
        """
        dummy = np.random.randn(self.block_frame + 4 * self.zc).astype("float32")
        librosa.to_mono(dummy[None, :])
        rms = librosa.feature.rms(
            y=dummy, frame_length=4 * self.zc, hop_length=self.zc
        )
        librosa.amplitude_to_db(rms, ref=1.0)

        warm = torch.zeros_like(self.input_wav_res)
        warm.normal_(0, 0.05)
        self.rvc.infer(
            warm,
            self.block_frame_16k,
            self.skip_head,
            self.return_length,
            self.params.f0method,
        )
        self.resampler(self.input_wav[-self.block_frame - 2 * self.zc :])
        if self.resampler2 is not None:
            self.resampler2(torch.zeros(self.block_frame, device=self.device))
        if self.params.I_noise_reduce or self.params.O_noise_reduce:
            self.tg(self.input_wav.unsqueeze(0), self.input_wav.unsqueeze(0))

    def update_live(self, **kw):
        """調整不需重建緩衝區的參數（音高、index 比例等）。"""
        with self._lock:
            if not self.rvc:
                return
            if "pitch" in kw:
                self.params.pitch = int(kw["pitch"])
                self.rvc.set_key(self.params.pitch)
            if "formant" in kw:
                self.params.formant = float(kw["formant"])
                self.rvc.set_formant(self.params.formant)
            if "index_rate" in kw:
                self.params.index_rate = float(kw["index_rate"])
                self.rvc.set_index_rate(self.params.index_rate)
            for k in ("threhold", "rms_mix_rate", "use_pv",
                      "I_noise_reduce", "O_noise_reduce"):
                if k in kw:
                    setattr(self.params, k, kw[k])

    # ---------- 處理 ----------

    def process(self, indata: np.ndarray) -> np.ndarray:
        """處理一個區塊。indata 為 float32 單聲道，長度須等於 block_frame。

        回傳同長度的 float32 單聲道。邏輯與 gui.py 的 audio_callback 一致。
        """
        if not self.ready:
            self.in_level = self.out_level = 0.0
            return np.zeros(len(indata), dtype="float32")

        t0 = time.perf_counter()
        p = self.params
        dev = self.device
        self.in_level = float(np.sqrt(np.mean(indata.astype(np.float64) ** 2)))

        if p.threhold > -60:
            indata = np.append(self.rms_buffer, indata)
            rms = librosa.feature.rms(
                y=indata, frame_length=4 * self.zc, hop_length=self.zc
            )[:, 2:]
            self.rms_buffer[:] = indata[-4 * self.zc :]
            indata = indata[2 * self.zc - self.zc // 2 :]
            db_threhold = librosa.amplitude_to_db(rms, ref=1.0)[0] < p.threhold
            for i in range(db_threhold.shape[0]):
                if db_threhold[i]:
                    indata[i * self.zc : (i + 1) * self.zc] = 0
            indata = indata[self.zc // 2 :]

        self.input_wav[: -self.block_frame] = self.input_wav[self.block_frame :].clone()
        self.input_wav[-indata.shape[0] :] = torch.from_numpy(indata).to(dev)
        self.input_wav_res[: -self.block_frame_16k] = self.input_wav_res[
            self.block_frame_16k :
        ].clone()

        if p.I_noise_reduce:
            self.input_wav_denoise[: -self.block_frame] = self.input_wav_denoise[
                self.block_frame :
            ].clone()
            iw = self.input_wav[-self.sola_buffer_frame - self.block_frame :]
            iw = self.tg(iw.unsqueeze(0), self.input_wav.unsqueeze(0)).squeeze(0)
            iw[: self.sola_buffer_frame] *= self.fade_in_window
            iw[: self.sola_buffer_frame] += self.nr_buffer * self.fade_out_window
            self.input_wav_denoise[-self.block_frame :] = iw[: self.block_frame]
            self.nr_buffer[:] = iw[self.block_frame :]
            self.input_wav_res[-self.block_frame_16k - 160 :] = self.resampler(
                self.input_wav_denoise[-self.block_frame - 2 * self.zc :]
            )[160:]
        else:
            self.input_wav_res[-160 * (indata.shape[0] // self.zc + 1) :] = (
                self.resampler(self.input_wav[-indata.shape[0] - 2 * self.zc :])[160:]
            )

        # 重採樣過衝會讓峰值略超 ±1，torchfcpe 會在此路徑上 print 警告
        torch.clamp_(self.input_wav_res, -1.0, 1.0)

        infer_wav = self.rvc.infer(
            self.input_wav_res,
            self.block_frame_16k,
            self.skip_head,
            self.return_length,
            p.f0method,
        )
        if self.resampler2 is not None:
            infer_wav = self.resampler2(infer_wav)

        if p.O_noise_reduce:
            self.output_buffer[: -self.block_frame] = self.output_buffer[
                self.block_frame :
            ].clone()
            self.output_buffer[-self.block_frame :] = infer_wav[-self.block_frame :]
            infer_wav = self.tg(
                infer_wav.unsqueeze(0), self.output_buffer.unsqueeze(0)
            ).squeeze(0)

        if p.rms_mix_rate < 1:
            src = (
                self.input_wav_denoise[self.extra_frame :]
                if p.I_noise_reduce
                else self.input_wav[self.extra_frame :]
            )
            rms1 = librosa.feature.rms(
                y=src[: infer_wav.shape[0]].cpu().numpy(),
                frame_length=4 * self.zc,
                hop_length=self.zc,
            )
            rms1 = F.interpolate(
                torch.from_numpy(rms1).to(dev).unsqueeze(0),
                size=infer_wav.shape[0] + 1,
                mode="linear",
                align_corners=True,
            )[0, 0, :-1]
            rms2 = librosa.feature.rms(
                y=infer_wav[:].cpu().numpy(),
                frame_length=4 * self.zc,
                hop_length=self.zc,
            )
            rms2 = F.interpolate(
                torch.from_numpy(rms2).to(dev).unsqueeze(0),
                size=infer_wav.shape[0] + 1,
                mode="linear",
                align_corners=True,
            )[0, 0, :-1]
            rms2 = torch.max(rms2, torch.zeros_like(rms2) + 1e-3)
            infer_wav *= torch.pow(rms1 / rms2, torch.tensor(1 - p.rms_mix_rate))

        # SOLA：找出與上一區塊尾端最吻合的偏移量，消除接縫
        conv_input = infer_wav[
            None, None, : self.sola_buffer_frame + self.sola_search_frame
        ]
        cor_nom = F.conv1d(conv_input, self.sola_buffer[None, None, :])
        cor_den = torch.sqrt(
            F.conv1d(
                conv_input**2,
                torch.ones(1, 1, self.sola_buffer_frame, device=dev),
            )
            + 1e-8
        )
        sola_offset = torch.argmax(cor_nom[0, 0] / cor_den[0, 0]).item()
        infer_wav = infer_wav[sola_offset:]

        if p.use_pv:
            infer_wav[: self.sola_buffer_frame] = phase_vocoder(
                self.sola_buffer,
                infer_wav[: self.sola_buffer_frame],
                self.fade_out_window,
                self.fade_in_window,
            )
        else:
            infer_wav[: self.sola_buffer_frame] *= self.fade_in_window
            infer_wav[: self.sola_buffer_frame] += (
                self.sola_buffer * self.fade_out_window
            )

        self.sola_buffer[:] = infer_wav[
            self.block_frame : self.block_frame + self.sola_buffer_frame
        ]
        out = infer_wav[: self.block_frame].cpu().numpy()

        self.out_level = float(np.sqrt(np.mean(out.astype(np.float64) ** 2)))
        self.infer_time_ms = (time.perf_counter() - t0) * 1000
        self.block_count += 1
        return out

    def stop(self):
        with self._lock:
            self.ready = False
            self.rvc = None
            self.warmer.release()
            if self.device == "mps":
                torch.mps.empty_cache()


class GpuWarmer:
    """在空檔對 GPU 做小量運算，避免 Apple Silicon 的 GPU 降頻。

    實測（block 0.10，純引擎按即時速率跑 150 塊）：
        不保溫   中位數 62.1ms   p95 84.0ms   尖峰 8/150
        保溫     中位數 33.4ms   p95 34.5ms   尖峰 0/150
    GPU 在工作斷斷續續時會降頻，每個區塊都得重新拉升，這就是 RTF 會規律性
    衝高的原因。讓它在空檔保持忙碌就不會掉下來。

    **必須與 engine.process() 在同一條執行緒呼叫。** MPS 的 command buffer
    不能跨執行緒同時提交（會直接 assert failure 掛掉），而改用鎖序列化會讓
    保溫迴圈把處理執行緒餓死（Python 的鎖不保證公平，實測會整個卡住）。
    正確用法是在等待輸入的空檔呼叫 tick()。

    代價是持續耗電發熱。桌機插電可以接受，筆電上建議關閉。
    """

    def __init__(self, device: str = "mps", size: int = 512):
        self.device = device
        self.enabled = False
        self._a = self._b = None
        self._size = size

    def enable(self, on: bool):
        self.enabled = bool(on)
        if self.enabled and self._a is None:
            self._a = torch.randn(self._size, self._size, device=self.device)
            self._b = torch.randn(self._size, self._size, device=self.device)

    def tick(self):
        """做一次小運算。單次約 1~2ms，不會影響對輸入的反應速度。"""
        if not self.enabled or self._a is None:
            return
        try:
            (self._a @ self._b).sum().item()   # .item() 同步，確保真的送到 GPU
        except Exception:
            self.enabled = False

    def release(self):
        self._a = self._b = None
        self.enabled = False


def spawn_worker(target, name="vc-worker", daemon=True) -> threading.Thread:
    """以足夠大的堆疊建立處理執行緒。

    macOS 的 pthread 預設堆疊只有 512KB，numba JIT 與模型惰性載入會在那裡溢位
    並以 SIGBUS 讓整個行程閃退，所以這裡明確指定較大的值。
    """
    prev = threading.stack_size()
    threading.stack_size(WORKER_STACK_BYTES)
    try:
        t = threading.Thread(target=target, name=name, daemon=daemon)
    finally:
        threading.stack_size(prev)
    t.start()
    return t
