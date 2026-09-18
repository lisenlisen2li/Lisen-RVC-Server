// RVC 即時變聲器前端。
// 本機模式：只呼叫 REST，音訊在伺服器端走 sounddevice。
// 遠端模式：另外用 AudioWorklet 擷取麥克風、以 WebSocket 往返音訊。

// 把 JS 錯誤直接畫在頁面上。原生客戶端的視窗預設沒有開發者工具，
// 沒有這個的話「畫面出得來但全部沒反應」會完全無從查起。
function showFatal(msg) {
  let el = document.getElementById('fatalBar');
  if (!el) {
    el = document.createElement('div');
    el.id = 'fatalBar';
    el.style.cssText = 'position:fixed;left:0;right:0;top:0;z-index:999;'
      + 'background:#dc2626;color:#fff;padding:10px 14px;font:12px/1.5 ui-monospace,monospace;'
      + 'white-space:pre-wrap;max-height:40vh;overflow:auto';
    document.body.appendChild(el);
  }
  el.textContent = 'UI error — controls are not bound:\n' + msg;
}
window.addEventListener('error', (e) =>
  showFatal(`${e.message}\n  at ${e.filename}:${e.lineno}:${e.colno}`));
window.addEventListener('unhandledrejection', (e) =>
  showFatal('unhandled promise rejection: ' + (e.reason && e.reason.stack || e.reason)));

const TOKEN = new URLSearchParams(location.search).get('token') || '';
const $ = (id) => document.getElementById(id);

let lang = localStorage.getItem('rvc.lang') || (navigator.language.startsWith('ja') ? 'ja'
         : navigator.language.startsWith('zh') ? 'zh' : 'en');
let T = window.I18N[lang];

let models = [];
let selected = null;
let mode = 'local';
let running = false;
let audio = null;
let underrunCount = 0;
let isClient = false;   // 由 /client/info 判定：跑在原生客戶端的內嵌介面裡
let serverUrl = '';

// navigator.mediaDevices 只存在於安全情境（https、localhost、127.0.0.1）。
// 用區域網路 IP 走 http 時它是 undefined，遠端音訊完全拿不到麥克風。
const micAvailable = () =>
  !!(window.isSecureContext && navigator.mediaDevices &&
     navigator.mediaDevices.getUserMedia);

// ---------- i18n ----------

function applyLang() {
  T = window.I18N[lang];
  document.documentElement.lang = lang === 'zh' ? 'zh-Hant' : lang;
  document.querySelectorAll('[data-i18n]').forEach((el) => {
    const v = T[el.dataset.i18n];
    if (typeof v === 'string') el.textContent = v;
  });
  document.querySelectorAll('[data-i18n-opt]').forEach((el) => {
    const v = T[el.dataset.i18nOpt];
    if (typeof v === 'string') el.textContent = v;
  });
  $('notice').innerHTML = T.notice;
  $('privacyList').innerHTML = T.privacy.map((x) => `<li>${x}</li>`).join('');
  $('modelCount').textContent = T.modelCount(models.length);
  $('modeHint').textContent = mode === 'local' ? T.modeHintLocal : T.modeHintRemote;
  $('modePill').textContent = mode === 'local' ? T.modeLocal : T.modeRemote;
  checkSampleRates();
  localStorage.setItem('rvc.lang', lang);
}

function errText(code) {
  return (T.err && T.err[code]) || code;
}

// ---------- 小工具 ----------

function toast(msg, isErr) {
  const t = $('toast');
  t.textContent = msg;
  t.className = isErr ? 'show err' : 'show';
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.className = ''; }, isErr ? 6000 : 2800);
}

async function api(path, opts = {}) {
  const sep = path.includes('?') ? '&' : '?';
  const res = await fetch(`${path}${sep}token=${encodeURIComponent(TOKEN)}`, {
    headers: { 'Content-Type': 'application/json' }, ...opts,
  });
  if (!res.ok) {
    let code = res.statusText;
    try { code = (await res.json()).detail || code; } catch (_) {}
    throw new Error(errText(code));
  }
  return res.json();
}

function collectParams() {
  return {
    pth_path: selected ? selected.pth : '',
    index_path: selected ? selected.index : '',
    pitch: +$('pitch').value,
    index_rate: +$('indexRate').value,
    block_time: +$('blockTime').value,
    crossfade_time: +$('crossTime').value,
    extra_time: +$('extraTime').value,
    rms_mix_rate: +$('rmsMix').value,
    f0method: $('f0method').value,
    use_pv: $('usePv').checked,
    I_noise_reduce: $('inr').checked,
    O_noise_reduce: $('onr').checked,
    keep_warm: $('keepWarm').checked,
    samplerate: 48000,
  };
}

// ---------- 模型 ----------

function renderSlots() {
  $('slots').innerHTML = models.map((m, i) => `
    <div class="slot${selected && selected.name === m.name ? ' on' : ''}" data-i="${i}">
      <div class="mark">${m.name.charAt(0).toUpperCase()}</div>
      <div class="nm" title="${m.name}">${m.name}</div>
    </div>`).join('');
  $('slots').querySelectorAll('.slot').forEach((el) => {
    el.addEventListener('click', () => selectModel(models[+el.dataset.i]));
  });
  $('modelCount').textContent = T.modelCount(models.length);
}

function selectModel(m) {
  selected = m;
  $('selName').textContent = m.name;
  $('avatar').textContent = m.name.charAt(0).toUpperCase();
  const tags = ['RVC', m.has_index ? 'index' : 'no index'];
  $('selTags').innerHTML = tags.map((t) => `<span class="tag">${t}</span>`).join('');
  renderSlots();
  localStorage.setItem('rvc.model', m.name);
}

// ---------- 裝置 ----------

let devices = null;

// 兩邊的裝置清單都留著：輸入和輸出可以各自選邊
let devSets = { client: null, server: null };

async function loadDevices() {
  try {
    devSets.server = await api('/api/devices');
    if (isClient) devSets.client = await (await fetch('/client/devices')).json();
    devices = devSets[isClient ? 'client' : 'server'];
    fillDevices();
  } catch (_) {}
}

const devOpt = (x) => `<option value="${x.index}">${x.name} · ${x.sr}Hz</option>`;

function sideOf(which) {
  if (!isClient) return 'server';
  return $(which === 'in' ? 'inSide' : 'outSide').value;
}

function fillDevices() {
  const si = devSets[sideOf('in')], so = devSets[sideOf('out')];
  if (si) $('devIn').innerHTML = si.input.map(devOpt).join('');
  if (so) $('devOut').innerHTML = so.output.map(devOpt).join('');
  // 監聽只有在輸出走這台機器時才有意義
  const monOk = isClient && sideOf('out') === 'client' && devSets.client;
  $('monRow').hidden = !monOk;
  if (monOk) {
    $('devMon').innerHTML = '<option value="">—</option>' +
      devSets.client.output.map(devOpt).join('');
  }
  checkSampleRates();
}

// 輸入與輸出取樣率不同時，macOS 會建立聚合裝置，兩個時脈漂移會造成規律卡頓。
function checkSampleRates() {
  const si = devSets[sideOf('in')], so = devSets[sideOf('out')];
  if (!si || !so) return;
  // 只有收放音在同一台機器時才會有聚合裝置的時脈漂移問題
  if (sideOf('in') !== sideOf('out')) { $('srHint').textContent = ''; return; }
  const i = si.input.find((x) => x.index == $('devIn').value);
  const o = so.output.find((x) => x.index == $('devOut').value);
  const el = $('srHint');
  if (i && o && i.sr !== o.sr) {
    el.textContent = T.srMismatch(i.sr, o.sr);
    el.className = 'tiny warn';
  } else if (i && o) {
    el.textContent = T.srMatch(i.sr);
    el.className = 'tiny';
  }
}

// ---------- 介面綁定 ----------

function setMode(m) {
  if (isClient) {
    // 客戶端模式沒有 local/remote 之分：音訊固定走本機音效卡 + UDP
    mode = 'client';
    $('modePill').textContent = T.modeClient;
    $('modeHint').textContent = T.modeHintClient;
    $('modeHint').className = 'tiny';
    $('devFold').style.display = '';
    $('inSide').hidden = false;
    $('outSide').hidden = false;
    $('modeHead').hidden = true;
    $('modeRow').hidden = true;
    $('jitterRow').style.display = '';
    $('jitterHint').style.display = '';
    $('underrunRow').style.display = '';
    $('lostRow').hidden = false;
    $('fecRow').hidden = false;
    $('rttRow').hidden = false;
    $('bufRow').hidden = false;
    // 瀏覽器模式的抖動緩衝以「區塊」為單位（主執行緒抖動大，只能粗調）；
    // 客戶端可以細到毫秒，所以換一組刻度。
    const j = $('jitterBlocks');
    if (j.max !== '150') {
      j.min = '10'; j.max = '150'; j.step = '5'; j.value = '30';
      $('jitterOut').textContent = '30';
    }
    return;
  }
  mode = m;
  document.querySelectorAll('.bigbtn.mode').forEach((b) =>
    b.classList.toggle('active', b.dataset.mode === m));
  $('devFold').style.display = m === 'local' ? '' : 'none';
  $('jitterRow').style.display = m === 'local' ? 'none' : '';
  $('jitterHint').style.display = m === 'local' ? 'none' : '';
  $('underrunRow').style.display = m === 'local' ? 'none' : '';
  $('modePill').textContent = m === 'local' ? T.modeLocal : T.modeRemote;
  if (m === 'remote' && !micAvailable()) {
    $('modeHint').textContent = T.insecure + ' ' + T.insecureFix;
    $('modeHint').className = 'tiny warn';
  } else {
    $('modeHint').textContent = m === 'local' ? T.modeHintLocal : T.modeHintRemote;
    $('modeHint').className = 'tiny';
  }
  localStorage.setItem('rvc.mode', m);
}

function bindUI() {
  const sliders = {
    pitch: [$('pitchOut'), (v) => v, 'pitch'],
    indexRate: [$('indexOut'), (v) => (+v).toFixed(2), 'index_rate'],
    rmsMix: [$('rmsOut'), (v) => (+v).toFixed(2), 'rms_mix_rate'],
    blockTime: [$('blockOut'), (v) => (+v).toFixed(3), null],
    extraTime: [$('extraOut'), (v) => (+v).toFixed(2), null],
    crossTime: [$('crossOut'), (v) => (+v).toFixed(3), null],
    jitterBlocks: [$('jitterOut'), (v) => v, null],
  };
  for (const [id, [out, fmt, liveKey]] of Object.entries(sliders)) {
    const el = $(id);
    el.addEventListener('input', () => {
      out.textContent = fmt(el.value);
      if (running && liveKey) {
        api('/api/live', { method: 'POST', body: JSON.stringify({ [liveKey]: +el.value }) })
          .catch(() => {});
      }
      if (id === 'jitterBlocks' && running && mode === 'remote') toast(T.jitterRestart);
    });
  }
  // keep_warm 不在這裡：它要重建緩衝區，只能在下次「開始」時生效
  for (const [id, key] of [['usePv', 'use_pv'], ['inr', 'I_noise_reduce'], ['onr', 'O_noise_reduce']]) {
    $(id).addEventListener('change', () => {
      if (running) api('/api/live', { method: 'POST', body: JSON.stringify({ [key]: $(id).checked }) }).catch(() => {});
    });
  }

  document.querySelectorAll('.bigbtn.mode').forEach((b) =>
    b.addEventListener('click', () => setMode(b.dataset.mode)));
  document.querySelectorAll('.fold-head').forEach((h) =>
    h.addEventListener('click', () => h.parentElement.classList.toggle('closed')));

  $('keepWarm').addEventListener('change', () => { if (running) toast(T.jitterRestart); });
  $('inSide').addEventListener('change', fillDevices);
  $('outSide').addEventListener('change', fillDevices);
  $('devIn').addEventListener('change', checkSampleRates);
  $('devOut').addEventListener('change', checkSampleRates);
  $('startBtn').addEventListener('click', start);
  $('stopBtn').addEventListener('click', stop);
  $('f0method').addEventListener('change', () => {
    if ($('f0method').value === 'rmvpe') toast(T.rmvpeBroken, true);
  });
  $('lang').addEventListener('change', () => { lang = $('lang').value; applyLang(); });
  $('themeBtn').addEventListener('click', toggleTheme);
  $('serverBtn').addEventListener('click', () => showServerBar($('serverBar').hidden));
  $('serverSave').addEventListener('click', saveServer);
  $('serverCancel').addEventListener('click', () => showServerBar(false));
  $('serverInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') saveServer(); });
  $('reloadBtn').addEventListener('click', () => location.reload());
  $('reloadLink').addEventListener('click', (e) => { e.preventDefault(); location.reload(); });
}

function showServerBar(on) {
  $('serverBar').hidden = !on;
  if (on) { $('serverInput').value = serverUrl; $('serverInput').focus(); }
}

async function saveServer() {
  const v = $('serverInput').value.trim();
  try {
    const r = await fetch('/client/config', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ server: v }),
    });
    if (!r.ok) { toast(T.serverBad, true); return; }
    toast(T.serverSaved);
    setTimeout(() => location.reload(), 700);
  } catch (e) {
    toast(T.connFail + '：' + e.message, true);
  }
}

function toggleTheme() {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  localStorage.setItem('rvc.theme', next);
}

// ---------- 開始 / 停止 ----------

async function start() {
  if (!selected) return;
  if (mode === 'remote' && !micAvailable()) {
    toast(T.insecure + ' ' + T.insecureFix, true);
    return;
  }
  $('startBtn').disabled = true;
  underrunCount = 0;
  toast(T.loading);

  const body = {
    mode,
    params: collectParams(),
    device: mode === 'local' ? { input: +$('devIn').value, output: +$('devOut').value } : {},
  };

  try {
    if (isClient) {
      const res = await fetch('/client/start', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source: sideOf('in'),
          sink: sideOf('out'),
          input: sideOf('in') === 'client' ? +$('devIn').value : null,
          output: sideOf('out') === 'client' ? +$('devOut').value : null,
          server_input: sideOf('in') === 'server' ? +$('devIn').value : null,
          server_output: sideOf('out') === 'server' ? +$('devOut').value : null,
          monitor: (sideOf('out') === 'client' && $('devMon').value !== '')
                   ? +$('devMon').value : null,
          jitter_ms: +$('jitterBlocks').value,   // 客戶端模式下滑桿單位就是毫秒
          model: selected.name,
          params: collectParams(),
        }),
      });
      if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
      running = true;
      $('stopBtn').disabled = false;
      $('stopBtn').classList.add('active');
      toast(T.started);
      return;
    }
    const r = await api('/api/start', { method: 'POST', body: JSON.stringify(body) });
    if (mode === 'remote') await startRemoteAudio(r.block_frame, r.samplerate);
    running = true;
    $('stopBtn').disabled = false;
    $('startBtn').classList.remove('active');
    $('stopBtn').classList.add('active');
    toast(T.started);
  } catch (e) {
    $('startBtn').disabled = false;
    toast(T.startFail + '：' + e.message, true);
    await api('/api/stop', { method: 'POST' }).catch(() => {});
  }
}

async function stop() {
  $('stopBtn').disabled = true;
  stopRemoteAudio();
  if (isClient) {
    try { await fetch('/client/stop', { method: 'POST' }); } catch (_) {}
  } else {
    try { await api('/api/stop', { method: 'POST' }); } catch (_) {}
  }
  running = false;
  $('startBtn').disabled = false;
  $('stopBtn').classList.remove('active');
  toast(T.stopped);
}

// ---------- 遠端音訊 ----------

async function startRemoteAudio(blockFrame, sampleRate) {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      // 這三項會破壞變聲品質，一律關閉
      echoCancellation: false, noiseSuppression: false, autoGainControl: false,
      channelCount: 1, sampleRate,
    },
  });

  const ctx = new AudioContext({ sampleRate, latencyHint: 'interactive' });
  await ctx.audioWorklet.addModule('/static/vc-worklet.js');

  const src = ctx.createMediaStreamSource(stream);
  const cap = new AudioWorkletNode(ctx, 'vc-capture', {
    numberOfInputs: 1, numberOfOutputs: 0,
    processorOptions: { blockSize: blockFrame },
  });
  const play = new AudioWorkletNode(ctx, 'vc-playback', {
    numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [2],
    processorOptions: { blockSize: blockFrame, jitterBlocks: +$('jitterBlocks').value },
  });

  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws/audio?token=${encodeURIComponent(TOKEN)}`);
  ws.binaryType = 'arraybuffer';
  await new Promise((resolve, reject) => {
    ws.onopen = resolve;
    ws.onerror = () => reject(new Error('WebSocket'));
    setTimeout(() => reject(new Error('WebSocket timeout')), 8000);
  });

  cap.port.onmessage = (e) => { if (ws.readyState === 1) ws.send(e.data.buffer); };
  play.port.onmessage = (e) => {
    if (e.data && typeof e.data.underruns === 'number') underrunCount = e.data.underruns;
  };
  ws.onmessage = (e) => play.port.postMessage(new Float32Array(e.data));

  src.connect(cap);
  play.connect(ctx.destination);
  if (ctx.state === 'suspended') await ctx.resume();
  audio = { ctx, stream, ws, cap, play, src };
}

function stopRemoteAudio() {
  if (!audio) return;
  try { audio.ws.close(); } catch (_) {}
  try { audio.src.disconnect(); audio.cap.disconnect(); audio.play.disconnect(); } catch (_) {}
  try { audio.stream.getTracks().forEach((t) => t.stop()); } catch (_) {}
  try { audio.ctx.close(); } catch (_) {}
  audio = null;
}

// ---------- 狀態輪詢 ----------

// RMS 直接映射到寬度會幾乎看不到動靜，開根號後視覺回應比較接近聽感。
const vuScale = (rms) => Math.min(100, Math.sqrt(Math.max(0, rms)) * 140);

function poll() {
  setInterval(async () => {
    try {
      const s = await api('/api/status');
      let cs = null;
      if (isClient) {
        try {
          cs = await (await fetch('/client/status')).json();
          $('rtt').textContent = cs.rtt_ms != null ? cs.rtt_ms.toFixed(1) + ' ms' : '—';
          $('buf').textContent = cs.buffer_ms.toFixed(0) + ' ms';
          underrunCount = cs.underruns;
          // 遺失區塊直接對應「少了 100ms 音訊」，是斷音最常見的成因，
          // 跟 underruns 一起看才分得出是掉包還是緩衝設太小。
          const srvLost = (s.audio && s.audio.incomplete) || 0;
          $('lost').textContent = `${cs.lost} / ${srvLost}`;
          // 回程掉包被同位片救回的區塊數。這些本來每一個都是一次緩衝見底的
          // 斷音，所以這個數字在爬、而 underruns 沒爬，就是 FEC 正在做事。
          $('fecFixed').textContent = cs.fec ? cs.recovered : '—';
        } catch (_) {}
      }
      $('inLevel').style.width = vuScale(s.in_level) + '%';
      $('outLevel').style.width = vuScale(s.out_level) + '%';
      $('inLevelNum').textContent = s.in_level.toFixed(3);
      $('outLevelNum').textContent = s.out_level.toFixed(3);

      if (!s.running) {
        if (running) { running = false; $('startBtn').disabled = false; $('stopBtn').disabled = true; }
        return;
      }

      $('inferMs').textContent = s.infer_time_ms.toFixed(1) + ' ms';
      $('budgetMs').textContent = s.budget_ms + ' ms';
      $('blocks').textContent = s.blocks;
      $('underruns').textContent = underrunCount;
      $('rtfNum').textContent = s.rtf.toFixed(3);

      // 刻度中央是 RTF=1（算不完的分界），所以除以 2 才是條的比例
      $('rtfFill').style.width = Math.min(100, s.rtf * 50) + '%';
      $('rtfFill').style.background =
        s.rtf > 0.9 ? 'var(--bad)' : s.rtf > 0.7 ? 'var(--warn)' : 'var(--green)';
      $('rtfHint').textContent =
        s.rtf > 0.9 ? T.verdictBad : s.rtf > 0.7 ? T.verdictWarn : T.verdictOk;

      const p = s.params;
      let total = (p.block_time + Math.min(p.crossfade_time, 0.04)) * 1000;
      if (mode === 'remote') total += p.block_time * 1000 * (1 + +$('jitterBlocks').value) + 10;
      if (isClient && cs) total = p.block_time * 1000 + (cs.rtt_ms || 0) + cs.jitter_ms + 10;
      $('totalDelay').textContent = Math.round(total) + ' ms';
    } catch (_) {}
  }, 500);
}

// ---------- 啟動 ----------

async function init() {
  document.documentElement.dataset.theme = localStorage.getItem('rvc.theme') || 'light';
  $('lang').value = lang;

  // 不在這裡檢查 TOKEN 是否存在：伺服器可能以 --no-token 啟動（前面有會驗證
  // 身分的反向代理）。直接打 API，真的被擋下來才回報是 token 的問題。
  try {
    const info = await fetch('/client/info');
    if (info.ok) {
      const j = await info.json();
      isClient = j.client === true;
      serverUrl = j.server || '';
      if (isClient) {
        $('serverBtn').hidden = false;
        $('serverBtn').textContent = serverUrl.replace(/^https?:\/\//, '') || '—';
      }
    }
  } catch (_) {}

  try {
    models = (await api('/api/models')).models;
  } catch (e) {
    applyLang();
    bindUI();
    const isAuth = e.message === T.err.invalid_token;
    toast(isAuth ? T.noToken : T.connFail + '：' + e.message, true);
    $('startBtn').disabled = true;
    // 連不上時直接把位址輸入列打開。使用者最可能就是打錯了網址，
    // 而這個頁面是客戶端自己服務的，所以永遠改得到。
    if (isClient && !isAuth) showServerBar(true);
    return;
  }

  if (!models.length) { applyLang(); toast(T.noModels, true); return; }

  const saved = localStorage.getItem('rvc.model');
  selectModel(models.find((m) => m.name === saved) || models[0]);

  await loadDevices();
  bindUI();
  setMode(localStorage.getItem('rvc.mode') || 'local');
  applyLang();
  poll();
}

init().catch((e) => showFatal((e && e.stack) || String(e)));
