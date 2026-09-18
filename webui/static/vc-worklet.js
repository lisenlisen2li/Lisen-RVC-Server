// AudioWorklet 處理器：擷取端把 128 樣本的量子累積成一個完整區塊；
// 播放端維持抖動緩衝，避免網路到達時間不均造成斷音。

class CaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.blockSize = options.processorOptions.blockSize;
    this.buf = new Float32Array(this.blockSize);
    this.filled = 0;
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    let i = 0;
    while (i < ch.length) {
      const n = Math.min(ch.length - i, this.blockSize - this.filled);
      this.buf.set(ch.subarray(i, i + n), this.filled);
      this.filled += n;
      i += n;
      if (this.filled === this.blockSize) {
        // 複製一份再送出，緩衝區會被下一輪覆寫
        this.port.postMessage(this.buf.slice());
        this.filled = 0;
      }
    }
    return true;
  }
}

class PlaybackProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.blockSize = options.processorOptions.blockSize;
    // 抖動緩衝：湊滿這麼多區塊才開始播，之後維持水位
    this.minBlocks = options.processorOptions.jitterBlocks || 2;
    this.queue = [];
    this.current = null;
    this.offset = 0;
    this.started = false;
    this.underruns = 0;
    this.port.onmessage = (e) => {
      if (e.data === 'reset') {
        this.queue = []; this.current = null; this.offset = 0;
        this.started = false; this.underruns = 0;
        return;
      }
      this.queue.push(e.data);
      // 上限保護：緩衝堆太多代表延遲在累積，丟掉最舊的
      while (this.queue.length > this.minBlocks + 6) this.queue.shift();
    };
  }

  process(_inputs, outputs) {
    const out = outputs[0];
    const ch0 = out[0];

    if (!this.started) {
      if (this.queue.length < this.minBlocks) {
        for (let c = 0; c < out.length; c++) out[c].fill(0);
        return true;
      }
      this.started = true;
    }

    for (let i = 0; i < ch0.length; i++) {
      if (!this.current || this.offset >= this.current.length) {
        this.current = this.queue.shift() || null;
        this.offset = 0;
        if (!this.current) {
          // 緩衝見底：補靜音並要求重新蓄水，避免持續斷續
          this.underruns++;
          this.started = false;
          for (let c = 0; c < out.length; c++) out[c].fill(0, i);
          this.port.postMessage({ underruns: this.underruns });
          return true;
        }
      }
      const v = this.current[this.offset++];
      for (let c = 0; c < out.length; c++) out[c][i] = v;
    }
    return true;
  }
}

registerProcessor('vc-capture', CaptureProcessor);
registerProcessor('vc-playback', PlaybackProcessor);
