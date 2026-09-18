"""RVC 即時變聲 Web 介面伺服器。

兩種運作模式：
  local  — 音訊留在本機走 sounddevice，網頁只當遙控器。延遲最低（約 150ms）。
  remote — 瀏覽器收音也放音，音訊走 WebSocket。人不在機器旁時用，延遲較高
           （約 250~350ms，成本主要在播放端的抖動緩衝，不是網路）。

存取控制：伺服器綁定 0.0.0.0 時，同網段任何人都連得到，因此一律要求 token。
token 於啟動時產生並印在終端機，不寫入磁碟。

音訊不落地：語音只存在於記憶體，不寫檔、不寫入日誌。**唯一的例外是錄製**——
使用者在介面上明確按下錄製，轉換後的輸出才會寫進 ROOT/recordings/，並且留在
磁碟上直到使用者自己刪除。介面提供列表、下載與刪除，隱私權政策那一節也有
對應的說明；改動錄製行為時三個地方要一起改，別讓說明變成謊話。
"""

import argparse
import asyncio
import json
import os
import queue
import atexit
import secrets
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import logging

import numpy as np

# 這些套件會把根 logger 設成 DEBUG，日誌會被 numba 的位元碼傾印灌爆
logging.basicConfig(level=logging.WARNING)
for _n in ("numba", "faiss", "faiss.loader", "matplotlib", "fairseq", "urllib3"):
    logging.getLogger(_n).setLevel(logging.WARNING)

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")
load_dotenv(ROOT / "sha256.env")

import torch

_orig_load = torch.load


def _patched_load(*a, **k):
    k.setdefault("weights_only", False)
    return _orig_load(*a, **k)


torch.load = _patched_load
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
os.environ.setdefault("OMP_NUM_THREADS", "4")

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException, Depends
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from webui.engine import VoiceChangerEngine, Params, spawn_worker
from webui.audio_router import AudioRouter
from webui.recorder import Recorder

STATIC = Path(__file__).resolve().parent / "static"
TOKEN = os.environ.get("RVC_TOKEN") or secrets.token_urlsafe(12)

# 由 --no-token 設定。只有在伺服器綁定 loopback、且前面有會驗證身分的反向代理
# 時才該關閉；main() 會強制檢查這個前提。
TOKEN_REQUIRED = True

app = FastAPI(title="RVC Realtime Voice Changer")
engine = VoiceChangerEngine(device="mps")
router = AudioRouter(engine)
recorder = Recorder(ROOT)
engine.recorder = recorder

# 由 main() 設定，供 /api/start 開 UDP 通道時使用
UDP_HOST = "0.0.0.0"
UDP_PORT = 7867

state = {
    "mode": None,          # None | "local" | "remote"
    "running": False,
    "error": None,
    "配置中": False,
}


# ---------- 存取控制 ----------

def require_token(token: str = Query(default="")):
    """所有 API 都要帶 token。綁 0.0.0.0 時這是唯一的存取控制。

    以 --no-token 停用時，存取控制改由前方的反向代理負責。那只有在後端無法
    被繞過（綁 loopback）時才成立，main() 會擋掉其他組合。
    """
    if not TOKEN_REQUIRED:
        return True
    if not secrets.compare_digest(token, TOKEN):
        raise HTTPException(status_code=401, detail="invalid_token")
    return True


# ---------- 模型清單 ----------

def list_models():
    weights, logs = ROOT / "assets" / "weights", ROOT / "logs"
    pths = {f.stem: f for f in weights.glob("*.pth")} if weights.exists() else {}
    idxs = {f.stem: f for f in logs.glob("*.index")} if logs.exists() else {}
    out = []
    for name in sorted(pths):
        out.append({
            "name": name,
            "pth": str(pths[name]),
            "index": str(idxs[name]) if name in idxs else "",
            "has_index": name in idxs,
        })
    return out


def list_audio_devices():
    """列舉音訊裝置。

    注意：串流執行中絕對不能呼叫 sd._terminate()。PortAudio 一旦在活的串流底下
    被終止，已註冊到 CoreAudio 的 IOProc 不會被解除註冊，coreaudiod 會抱著一個
    沒有消費者的（往往還是聚合的）裝置持續空轉吃 CPU，而且每次都再疊一個。
    只有閒置時才重新初始化，以便抓到新插拔的裝置。
    """
    import sounddevice as sd
    if not state["running"]:
        sd._terminate(); sd._initialize()
    ins, outs = [], []
    for i, d in enumerate(sd.query_devices()):
        item = {"index": i, "name": d["name"], "sr": int(d["default_samplerate"])}
        if d["max_input_channels"] > 0:
            ins.append(item)
        if d["max_output_channels"] > 0:
            outs.append({**item, "channels": d["max_output_channels"]})
    return {"input": ins, "output": outs}


# ---------- REST ----------

@app.get("/api/models")
def api_models(_=Depends(require_token)):
    return {"models": list_models()}


@app.get("/api/devices")
def api_devices(_=Depends(require_token)):
    try:
        return list_audio_devices()
    except Exception as e:
        return JSONResponse({"error": str(e), "input": [], "output": []})


@app.get("/api/status")
def api_status(_=Depends(require_token)):
    return {
        "mode": state["mode"],
        "running": state["running"],
        "configuring": state["配置中"],
        "ready": engine.ready,
        "error": state["error"],
        "infer_time_ms": round(engine.infer_time_ms, 1),
        "block_frame": getattr(engine, "block_frame", 0),
        "samplerate": engine.params.samplerate,
        "blocks": engine.block_count,
        "params": engine.params.to_dict(),
        "budget_ms": round(engine.params.block_time * 1000),
        "in_level": round(engine.in_level, 4),
        "out_level": round(engine.out_level, 4),
        # RTF = 每塊實際耗時 / 每塊代表的時長。>1 就是算不完，一定破音。
        "rtf": round(
            engine.infer_time_ms / (engine.params.block_time * 1000), 3
        ) if engine.params.block_time else 0.0,
        "audio": router.stats() if state["running"] else None,
        "timing": engine.timing(),
        "recording": recorder.status(),
    }


# ---------- 錄製 ----------
#
# 這是整個服務唯一會把語音寫上磁碟的地方，所以每個端點都要求 token，檔名一律
# 由伺服器產生、比對白名單樣式（見 recorder.NAME_RE），使用者傳來的字串永遠
# 不直接拼進路徑。刪除端點是刻意提供的：個資法第八條要求當事人查得到、也刪
# 得掉自己的資料，介面上必須真的有那顆按鈕。

@app.post("/api/record/start")
def api_record_start(payload: dict, _=Depends(require_token)):
    if not state["running"]:
        raise HTTPException(400, "not_running")
    try:
        return recorder.start(engine.params.samplerate,
                              payload.get("format", "wav"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {e}")


@app.post("/api/record/stop")
def api_record_stop(_=Depends(require_token)):
    r = recorder.stop()
    if r is None:
        raise HTTPException(400, "not_recording")
    return r


@app.get("/api/recordings")
def api_recordings(_=Depends(require_token)):
    return {"recordings": recorder.list()}


@app.post("/api/recordings/delete")
def api_recording_delete(payload: dict, _=Depends(require_token)):
    """用 POST 而不是 DELETE：客戶端的內嵌介面是 stdlib 的 http.server，
    只分派 do_GET / do_POST，多一個方法就要多一段轉發程式碼。"""
    if not recorder.delete(payload.get("name", "")):
        raise HTTPException(400, "delete_failed")
    return {"ok": True}


@app.get("/api/recordings/file")
def api_recording_file(name: str = Query(default=""), _=Depends(require_token)):
    p = recorder.resolve(name)
    if p is None:
        raise HTTPException(404, "not_found")
    return FileResponse(str(p), filename=p.name,
                        media_type="audio/mpeg" if p.suffix == ".mp3" else "audio/wav")


@app.post("/api/live")
async def api_live(payload: dict, _=Depends(require_token)):
    """即時調整不需重建緩衝區的參數。"""
    engine.update_live(**payload)
    return {"ok": True, "params": engine.params.to_dict()}


@app.post("/api/start")
async def api_start(payload: dict, _=Depends(require_token)):
    """載入模型並開始。mode = local | remote。"""
    if state["running"] or state["配置中"]:
        raise HTTPException(400, "already_running")

    # source / sink 各自可以是 server（本機音效卡）、client（原生客戶端走 UDP）
    # 或 browser（瀏覽器走 WebSocket）。browser 目前只支援兩端都是 browser。
    mode = payload.get("mode")
    source = payload.get("source")
    sink = payload.get("sink")
    if mode in ("local", "remote", "udp"):      # 舊參數，保留相容
        source = sink = {"local": "server", "remote": "browser", "udp": "client"}[mode]
    if source not in ("server", "client", "browser") or \
       sink not in ("server", "client", "browser"):
        raise HTTPException(400, "invalid_mode")
    if ("browser" in (source, sink)) and source != sink:
        raise HTTPException(400, "browser_must_be_both")

    p = Params(**{k: v for k, v in payload.get("params", {}).items()
                  if k in Params.__dataclass_fields__})
    if not p.pth_path or not Path(p.pth_path).exists():
        raise HTTPException(400, "model_not_found")
    if p.index_rate > 0 and not (p.index_path and Path(p.index_path).exists()):
        p.index_rate = 0.0   # 沒有 index 檔就不要開 index 比例

    state.update(error=None, 配置中=True, mode=f"{source}->{sink}")
    done = threading.Event()

    session_holder = {}

    def build():
        try:
            engine.configure(p)
            if source == "browser":
                pass                      # 音訊走 WebSocket，不需要開任何串流
            else:
                dev = payload.get("device", {})
                sess = secrets.randbits(32)
                session_holder["session"] = sess
                session_holder["port"] = router.start(
                    source, sink,
                    in_dev=dev.get("input"), out_dev=dev.get("output"),
                    udp_host=UDP_HOST, udp_port=UDP_PORT, session=sess,
                    jitter_ms=float(payload.get("jitter_ms", 30.0)),
                    # 預設關閉：沒帶這個欄位的舊客戶端收不到同位片，行為不變
                    fec=bool(payload.get("fec", False)),
                )
            state["mode"] = f"{source}->{sink}"
            state["running"] = True
        except BaseException as e:
            import traceback
            state["error"] = f"{type(e).__name__}: {e}"
            traceback.print_exc()
        finally:
            state["配置中"] = False
            done.set()

    # 一定要用 spawn_worker：warmup 需要大堆疊，否則 SIGBUS
    spawn_worker(build, name="vc-configure")
    await asyncio.get_event_loop().run_in_executor(None, done.wait)

    if state["error"]:
        raise HTTPException(500, state["error"])
    return {"ok": True, "block_frame": engine.block_frame,
            "samplerate": engine.params.samplerate, **session_holder}


@app.post("/api/stop")
def api_stop(_=Depends(require_token)):
    # 先收錄音。停掉轉換之後就不會再有區塊進來，開著的檔案只會一直空等，
    # 而使用者按下「停止」時心裡想的就是「這一段結束了」。
    rec = recorder.stop()
    router.stop()
    engine.stop()
    state.update(running=False, mode=None)
    return {"ok": True, "recording": rec}


# ---------- 遠端模式：WebSocket 音訊 ----------

@app.websocket("/ws/audio")
async def ws_audio(ws: WebSocket, token: str = Query(default="")):
    if TOKEN_REQUIRED and not secrets.compare_digest(token, TOKEN):
        await ws.close(code=4401)
        return
    await ws.accept()

    loop = asyncio.get_running_loop()
    inq: queue.Queue = queue.Queue(maxsize=8)
    # 輸出走 asyncio.Queue，由工作執行緒以 call_soon_threadsafe 投遞。
    # 先前是 `await loop.run_in_executor(None, outq.get, True, 0.2)`：每個區塊
    # 都要走一次執行緒池交接，而那個池執行緒阻塞在 queue.get 期間會週期性
    # 搶 GIL，直接偷走正在推論的工作執行緒的時間。實測讓 process() 從 43ms
    # 膨脹到 76ms。改成單向的 call_soon_threadsafe 就沒有這個交接。
    outq: asyncio.Queue = asyncio.Queue(maxsize=8)
    stop = threading.Event()

    def _deliver(out):
        # 只在事件迴圈執行緒上跑，所以這裡動 asyncio.Queue 是安全的
        if outq.full():
            try:
                outq.get_nowait()   # 丟掉最舊的，寧可掉一塊也不要累積延遲
            except asyncio.QueueEmpty:
                pass
        outq.put_nowait(out)

    def worker():
        """處理執行緒。用大堆疊建立，避免 numba/模型載入造成 SIGBUS。"""
        while not stop.is_set():
            try:
                blk = inq.get(timeout=0.002)
            except queue.Empty:
                # 空檔保溫，理由同 udp_audio；詳見 engine.GpuWarmer
                engine.warmer.tick()
                continue
            try:
                out = engine.process(blk)
            except Exception:
                out = np.zeros(len(blk), dtype="float32")
            if not stop.is_set():
                try:
                    loop.call_soon_threadsafe(_deliver, out)
                except RuntimeError:
                    break   # 事件迴圈已關閉

    spawn_worker(worker, name="vc-ws")

    async def pump_out():
        while not stop.is_set():
            try:
                out = await outq.get()
            except asyncio.CancelledError:
                break
            try:
                # out 從引擎出來就是 contiguous float32，不需要再 astype 複製一次
                await ws.send_bytes(out.tobytes())
            except Exception:
                break

    pump = asyncio.create_task(pump_out())
    try:
        while True:
            data = await ws.receive_bytes()
            blk = np.frombuffer(data, dtype=np.float32).copy()
            if engine.ready and len(blk) == engine.block_frame:
                try:
                    inq.put_nowait(blk)
                except queue.Full:
                    pass  # 輸入端塞車，丟棄最新的以免延遲雪球
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        stop.set()
        pump.cancel()


# ---------- 結束時的清理 ----------

def _cleanup():
    """關閉音訊串流。

    不做這件事的話，行程結束後 IOProc 會留在 coreaudiod 裡空轉。
    走 atexit 與 FastAPI 的 shutdown 事件兩條路，確保 Ctrl-C、SIGTERM
    與正常結束都會經過。
    """
    try:
        router.stop()
    except Exception:
        pass
    # 行程要結束了，把錄到一半的檔案收乾淨（wav 標頭要補、mp3 要轉檔），
    # 否則留下的是一個長度欄位不對的半成品。
    try:
        recorder.stop()
    except Exception:
        pass


atexit.register(_cleanup)


# 用 router.on_shutdown 而不是已棄用的 @app.on_event("shutdown")
app.router.on_shutdown.append(_cleanup)


def _install_signal_handlers():
    """補上 uvicorn 沒攔到的路徑，並把原本的處理器接回去。"""
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        prev = signal.getsignal(sig)

        def handler(signum, frame, _prev=prev):
            _cleanup()
            if callable(_prev):
                _prev(signum, frame)
            else:
                raise SystemExit(0)

        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass


# ---------- 靜態檔 ----------

@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


def _lan_ip() -> str:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.1.1", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def ensure_cert(ip: str):
    """產生（或重用）自簽憑證，供 HTTPS 使用。

    瀏覽器只在安全情境下提供 navigator.mediaDevices：https、localhost、
    127.0.0.1。用區域網路 IP 走 http 時它是 undefined，遠端音訊模式完全
    無法取得麥克風。所以遠端音訊一定要 HTTPS。

    現代瀏覽器只看 subjectAltName，不看 CN，因此把 localhost 與目前的
    區域網路 IP 都寫進 SAN；IP 變動時重新產生。
    """
    import subprocess

    cdir = ROOT / "certs"
    cdir.mkdir(exist_ok=True)
    cert, key, stamp = cdir / "cert.pem", cdir / "key.pem", cdir / "san.txt"
    san = f"DNS:localhost,IP:127.0.0.1,IP:{ip}"

    if cert.exists() and key.exists() and stamp.exists() and stamp.read_text() == san:
        return str(cert), str(key)

    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(key), "-out", str(cert), "-days", "825",
         "-subj", "/CN=RVC Realtime Voice Changer",
         "-addext", f"subjectAltName={san}"],
        check=True, capture_output=True,
    )
    stamp.write_text(san)
    os.chmod(key, 0o600)
    print(f"  Generated a self-signed certificate for {san}")
    return str(cert), str(key)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=7866)
    ap.add_argument("--https", action="store_true",
                    help="serve over HTTPS with a self-signed certificate "
                         "(required for Remote audio mode over the network)")
    ap.add_argument("--no-token", action="store_true",
                    help="drop the token check because an authenticating reverse "
                         "proxy sits in front. With a non-loopback --host this also "
                         "needs --trust-lan, since the backend is then reachable "
                         "directly and the proxy can be bypassed.")
    ap.add_argument("--udp-port", type=int, default=7867,
                    help="UDP port for the raw audio channel used by the native "
                         "client (mode=udp). Bypasses asyncio and WebSocket framing.")
    ap.add_argument("--trust-lan", action="store_true",
                    help="state that every host able to reach this port is trusted, "
                         "so --no-token is allowed on a non-loopback --host. Only "
                         "true while nothing forwards this port in from the internet.")
    args = ap.parse_args()

    global TOKEN_REQUIRED, UDP_HOST, UDP_PORT
    UDP_HOST, UDP_PORT = args.host, args.udp_port
    if args.no_token:
        loopback = args.host in ("127.0.0.1", "localhost", "::1")
        if not loopback and not args.trust_lan:
            ap.error(
                f"--no-token on --host {args.host} also needs --trust-lan.\n"
                "\n"
                "  Binding a non-loopback address means the backend is reachable\n"
                "  directly, so the reverse proxy that was supposed to authenticate\n"
                "  callers can simply be bypassed. That is fine if you trust every\n"
                "  host on this network - say so with --trust-lan. It is NOT fine if\n"
                "  anything forwards this port in from the internet.\n"
                "\n"
                "  Alternatives: keep the token, run the proxy on this machine and\n"
                "  bind 127.0.0.1, or firewall this port to the proxy's address."
            )
        TOKEN_REQUIRED = False

    lan_ip = _lan_ip()
    ssl_args = {}
    scheme = "http"
    if args.https:
        cert, key = ensure_cert(lan_ip)
        ssl_args = {"ssl_certfile": cert, "ssl_keyfile": key}
        scheme = "https"

    bar = "=" * 66
    print(f"\n{bar}\n  RVC Realtime Voice Changer - Web UI\n{bar}")
    q = f"/?token={TOKEN}" if TOKEN_REQUIRED else "/"
    print(f"  Local   {scheme}://127.0.0.1:{args.port}{q}")
    if args.host == "0.0.0.0":
        print(f"  Network {scheme}://{lan_ip}:{args.port}{q}")

    if args.host == "0.0.0.0":
        print("")
        print("  [!] Bound to 0.0.0.0 - anyone on this network who can reach this")
        print("      port may use the service.")
        if TOKEN_REQUIRED:
            print("      They also need the token above. It is regenerated on every")
            print("      start and never written to disk.")
        print("      Use --host 127.0.0.1 to restrict access to this machine.")

    if not TOKEN_REQUIRED:
        print("")
        print("  [!] Token check disabled (--no-token).")
        if args.host in ("127.0.0.1", "localhost", "::1"):
            print("      Bound to loopback, so your reverse proxy is the only way in")
            print("      and its authentication is the access control.")
        else:
            print("      Bound to a non-loopback address with --trust-lan, so ANY host")
            print("      that can reach this port can use the service without")
            print("      credentials. Make sure nothing forwards this port in from")
            print("      the internet.")

    print(f"  UDP     {args.host}:{args.udp_port}  (raw audio channel, mode=udp)")

    if args.https:
        print("")
        print("  Browsers do not trust a self-signed certificate, so the first")
        print("  visit shows a warning - accept it once per device and it will")
        print("  be remembered.")
    elif args.host == "0.0.0.0":
        print("")
        print("  [i] Running over plain HTTP. Remote audio mode needs microphone")
        print("      access, which browsers only grant in a secure context, so it")
        print("      works on 127.0.0.1 but NOT over the network. Use --https, or")
        print("      put an HTTPS reverse proxy in front.")
    print(f"{bar}\n")

    _install_signal_handlers()

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", **ssl_args)


if __name__ == "__main__":
    main()
