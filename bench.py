"""
無頭延遲基準測試：完全複製 gui.py start_vc() 的緩衝區計算，
直接量測 rtrvc.RVC.infer() 每塊耗時，不需要音訊裝置。

用法: .venv/bin/python bench.py [模型名] [block_time]
"""
import os, sys, time, logging, warnings
logging.disable(logging.CRITICAL)
warnings.filterwarnings("ignore")
os.environ["NUMBA_DISABLE_JIT"]="0"
from dotenv import load_dotenv
import torch

_orig_load = torch.load
def _patched(*a, **k):
    k.setdefault("weights_only", False)
    return _orig_load(*a, **k)
torch.load = _patched

load_dotenv()
load_dotenv("sha256.env")
os.environ["OMP_NUM_THREADS"] = "4"
if sys.platform == "darwin":
    os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"
sys.path.append(os.getcwd())

import numpy as np
from infer.lib import rtrvc

MODEL      = sys.argv[1] if len(sys.argv) > 1 else "Frieren_e720_s6480"
BLOCK_TIME = float(sys.argv[2]) if len(sys.argv) > 2 else 0.25
EXTRA_TIME = 2.5
CROSSFADE  = 0.05
WARMUP, RUNS = 3, 12

pth   = f"assets/weights/{MODEL}.pth"
index = f"logs/{MODEL}.index"
assert os.path.exists(pth), f"找不到 {pth}"

print(f"{'='*62}\n模型: {MODEL}   block_time: {BLOCK_TIME}s   extra_time: {EXTRA_TIME}s\n{'='*62}")

def bench(device, f0method):
    rvc = rtrvc.RVC(key=0, formant=0, pth_path=pth, index_path=index,
                    index_rate=0.0, n_cpu=4, device=device,
                    use_jit=False, is_half=False, is_dml=False)
    sr = rvc.tgt_sr
    zc = sr // 100
    block_frame     = int(np.round(BLOCK_TIME * sr / zc)) * zc
    block_frame_16k = 160 * block_frame // zc
    crossfade_frame = int(np.round(CROSSFADE * sr / zc)) * zc
    sola_buffer     = min(crossfade_frame, 4 * zc)
    sola_search     = zc
    extra_frame     = int(np.round(EXTRA_TIME * sr / zc)) * zc
    total           = extra_frame + crossfade_frame + sola_search + block_frame
    skip_head       = extra_frame // zc
    return_length   = (block_frame + sola_buffer + sola_search) // zc

    wav16k = torch.randn(160 * total // zc, device=device, dtype=torch.float32) * 0.05

    def once():
        rvc.infer(wav16k, block_frame_16k, skip_head, return_length, f0method, 0.5)
        if device == "mps":
            torch.mps.synchronize()

    for _ in range(WARMUP):
        once()
    ts = []
    for _ in range(RUNS):
        t = time.perf_counter(); once(); ts.append((time.perf_counter() - t) * 1000)
    ts.sort()
    del rvc
    if device == "mps":
        torch.mps.empty_cache()
    return ts[len(ts)//2], min(ts), max(ts), sr

budget = BLOCK_TIME * 1000
print(f"\n{'裝置':<6} {'f0 演算法':<10} {'中位數':>9} {'最快':>9} {'最慢':>9}  判定 (預算 {budget:.0f}ms)")
print("-" * 62)
for device in ["mps", "cpu"]:
    for f0 in (["fcpe","rmvpe"] if os.environ.get("BENCH_ALL") else ["fcpe"]):
        try:
            med, lo, hi, sr = bench(device, f0)
            verdict = "✅ 可即時" if hi < budget else ("⚠️ 邊緣" if med < budget else "❌ 會破音")
            print(f"{device:<6} {f0:<10} {med:>7.1f}ms {lo:>7.1f}ms {hi:>7.1f}ms  {verdict}")
        except Exception as e:
            print(f"{device:<6} {f0:<10} 失敗: {type(e).__name__}: {str(e)[:60]}")
print("-" * 62)
print("判定說明: 最慢一次也要 < block_time 才不會破音；中位數超過就一定破音。")
