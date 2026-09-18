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
let dirty = false;      // 動過需要重新開始才會生效的設定
let applying = false;
let recording = false;
let pollTimer = null;
// 遠端（瀏覽器放音）模式的輸出音量。那條路徑的音訊不經過伺服器的音效卡，
// 也不經過客戶端，只能在這裡乘上去。
let browserGain = 1.0;

// 音量滑桿的最低刻度＝靜音。**必須與 webui/engine.py 和 rvc_client.py 的
// MUTE_DB 相同**，否則同一個數字在三處會是不同的音量。
const MUTE_DB = -40;
const dbToLinear = (db) => (db <= MUTE_DB ? 0 : Math.pow(10, db / 20));

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
  // T.mute 會出現在音量滑桿旁邊，換語言時要跟著重畫
  syncLabels();
  setRecUI();
  renderRecordings(recFiles);
  updateApply();
  checkSampleRates();
  try { localStorage.setItem('rvc.lang', lang); } catch (_) {}
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
    in_gain_db: +$('gainIn').value,
    out_gain_db: +$('gainOut').value,
    samplerate: 48000,
  };
}

// ---------- 設定記憶 ----------
//
// 滑桿、核取方塊、下拉與音量都直接記在 localStorage，下次開起來就是上次的樣子。
// **裝置存名稱，不存 index。** 插拔一個 USB 麥克風就會讓後面所有裝置的 index
// 位移，存 index 的話下次開起來會靜靜地選到完全不同的裝置——在通話中把聲音
// 送錯地方，比忘記設定糟糕得多。名稱對不上就維持預設，不亂猜。

const PREF_RANGES = ['pitch', 'indexRate', 'rmsMix', 'blockTime', 'extraTime',
                     'crossTime', 'jitterBlocks', 'gainIn', 'gainOut', 'gainMon'];
const PREF_SELECTS = ['f0method', 'recFormat', 'inSide', 'outSide'];
const PREF_CHECKS = ['usePv', 'inr', 'onr', 'keepWarm'];
const PREF_DEVICES = ['devIn', 'devOut', 'devMon'];

// 輸入與輸出可以各自選邊，兩邊的裝置清單不同，所以要分開記。
function devKey(id) {
  const side = id === 'devIn' ? sideOf('in') : id === 'devOut' ? sideOf('out') : 'client';
  return `rvc.dev.${id}.${side}`;
}

// devOpt() 產生的文字是「名稱 · 48000Hz」，取分隔符前面那段當作識別。
const devName = (opt) => (opt ? opt.textContent.split(' · ')[0] : '');

function savePrefs() {
  try {
    for (const id of [...PREF_RANGES, ...PREF_SELECTS]) localStorage.setItem(`rvc.${id}`, $(id).value);
    for (const id of PREF_CHECKS) localStorage.setItem(`rvc.${id}`, $(id).checked ? '1' : '0');
    for (const id of PREF_DEVICES) {
      const el = $(id);
      if (el.options.length) localStorage.setItem(devKey(id), devName(el.options[el.selectedIndex]));
    }
  } catch (_) {}   // 無痕視窗裡 localStorage 會丟例外，記不起來不該讓介面壞掉
}

function restoreDevices() {
  for (const id of PREF_DEVICES) {
    const want = localStorage.getItem(devKey(id));
    if (!want) continue;
    const el = $(id);
    for (const o of el.options) {
      if (devName(o) === want) { el.value = o.value; break; }
    }
    // 找不到就不動。裝置被拔掉或換了一台機器時，維持預設選擇，
    // 不要靜靜地改選到另一個裝置。
  }
}

function restorePrefs() {
  try {
    // 先還原選邊，因為它決定 devIn/devOut 該列出哪一台機器的裝置
    for (const id of ['inSide', 'outSide']) {
      const v = localStorage.getItem(`rvc.${id}`);
      if (v !== null) $(id).value = v;
    }
    fillDevices();
    restoreDevices();

    for (const id of PREF_RANGES) {
      const v = localStorage.getItem(`rvc.${id}`);
      if (v === null) continue;
      const el = $(id);
      // 滑桿範圍會隨模式改變（客戶端的抖動緩衝刻度跟瀏覽器的不一樣），
      // 超出範圍的舊值交給瀏覽器夾回去，不要硬塞。
      el.value = v;
    }
    for (const id of ['f0method', 'recFormat']) {
      const v = localStorage.getItem(`rvc.${id}`);
      if (v !== null) $(id).value = v;
    }
    for (const id of PREF_CHECKS) {
      const v = localStorage.getItem(`rvc.${id}`);
      if (v !== null) $(id).checked = v === '1';
    }
  } catch (_) {}
  syncLabels();
  checkSampleRates();
}

// 把每個滑桿旁邊的數字重畫一次。還原設定後、切換語言後都要跑。
function syncLabels() {
  for (const [id, [out, fmt]] of Object.entries(SLIDERS)) out.textContent = fmt($(id).value);
  for (const id of ['gainIn', 'gainOut', 'gainMon']) $(id + 'Out').textContent = fmtDb($(id).value);
}

// ---------- 音量 ----------

const fmtDb = (v) =>
  (+v <= MUTE_DB ? T.mute : ((+v > 0 ? '+' : '') + (+v).toFixed(1) + ' dB'));

// 輸入增益永遠套在伺服器的 engine 上——一處就涵蓋四種路由組合。
// 輸出與監聽則套在「實際放音的那一端」，所以要看輸出走哪邊決定送去哪裡。
function pushGain(id) {
  const v = +$(id).value;
  if (id === 'gainOut' && mode === 'remote') browserGain = dbToLinear(v);
  if (!running) return;
  if (id === 'gainIn') {
    api('/api/live', { method: 'POST', body: JSON.stringify({ in_gain_db: v }) }).catch(() => {});
  } else if (id === 'gainMon') {
    clientLive({ mon_gain_db: v });
  } else if (isClient && sideOf('out') === 'client') {
    clientLive({ out_gain_db: v });
  } else if (!isClient && mode === 'remote') {
    // 瀏覽器放音，已經在上面乘進 browserGain 了，伺服器沒有東西可調
  } else {
    api('/api/live', { method: 'POST', body: JSON.stringify({ out_gain_db: v }) }).catch(() => {});
  }
}

function clientLive(body) {
  fetch('/client/live', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).catch(() => {});
}

// ---------- 「套用」：改了要重建緩衝區的設定才會出現 ----------

// 這些參數會改變緩衝區大小或模型載入方式，engine 無法在執行中換掉，
// 只能停止後重新開始。其餘的都走 /api/live 即時生效。
const REBUILD_IDS = ['blockTime', 'extraTime', 'crossTime', 'f0method', 'keepWarm',
                     'jitterBlocks', 'devIn', 'devOut', 'devMon', 'inSide', 'outSide'];

function markDirty() {
  if (!running || applying) return;
  dirty = true;
  updateApply();
}

function updateApply() {
  const show = running && dirty && !applying;
  $('applyBtn').hidden = !show;
  $('applyHint').hidden = !show;
  if (show) $('applyHint').textContent = T.applyPending;
}

async function applyRestart() {
  if (!running || applying) return;
  applying = true;
  $('applyBtn').disabled = true;
  $('applyHint').hidden = false;
  $('applyHint').textContent = T.applying;
  try {
    await stop(true);
    await start(true);
    dirty = false;
    toast(T.started);
  } catch (e) {
    toast(T.startFail + '：' + e.message, true);
  } finally {
    applying = false;
    $('applyBtn').disabled = false;
    updateApply();
  }
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
  try { localStorage.setItem('rvc.model', m.name); } catch (_) {}
  // 換模型要重新載入權重，engine 沒辦法在執行中換掉
  markDirty();
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
  // 沒有監聽輸出就沒有監聽音量可調
  $('gainMonRow').hidden = !monOk;
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
  try { localStorage.setItem('rvc.mode', m); } catch (_) {}
}

// 滑桿 → [顯示數值的元素, 格式化函式, /api/live 的鍵或 null]。
// null 表示改了要重新開始才生效，會由 REBUILD_IDS 那條路亮出「套用」。
const SLIDERS = {
  pitch: [$('pitchOut'), (v) => v, 'pitch'],
  indexRate: [$('indexOut'), (v) => (+v).toFixed(2), 'index_rate'],
  rmsMix: [$('rmsOut'), (v) => (+v).toFixed(2), 'rms_mix_rate'],
  blockTime: [$('blockOut'), (v) => (+v).toFixed(3), null],
  extraTime: [$('extraOut'), (v) => (+v).toFixed(2), null],
  crossTime: [$('crossOut'), (v) => (+v).toFixed(3), null],
  jitterBlocks: [$('jitterOut'), (v) => v, null],
};

function bindUI() {
  for (const [id, [out, fmt, liveKey]] of Object.entries(SLIDERS)) {
    const el = $(id);
    el.addEventListener('input', () => {
      out.textContent = fmt(el.value);
      if (running && liveKey) {
        api('/api/live', { method: 'POST', body: JSON.stringify({ [liveKey]: +el.value }) })
          .catch(() => {});
      }
      savePrefs();
    });
  }
  // 音量三條都是即時生效，不需要重新開始，所以不進 REBUILD_IDS
  for (const id of ['gainIn', 'gainOut', 'gainMon']) {
    $(id).addEventListener('input', () => {
      $(id + 'Out').textContent = fmtDb($(id).value);
      pushGain(id);
      savePrefs();
    });
  }
  // keep_warm 不在這裡：它要重建緩衝區，改由 REBUILD_IDS 處理
  for (const [id, key] of [['usePv', 'use_pv'], ['inr', 'I_noise_reduce'], ['onr', 'O_noise_reduce']]) {
    $(id).addEventListener('change', () => {
      if (running) api('/api/live', { method: 'POST', body: JSON.stringify({ [key]: $(id).checked }) }).catch(() => {});
      savePrefs();
    });
  }
  // 改到這些就亮出「套用」。統一掛在這裡，比在每個控制項各寫一次不容易漏。
  for (const id of REBUILD_IDS) {
    $(id).addEventListener('change', () => { markDirty(); savePrefs(); });
    $(id).addEventListener('input', markDirty);
  }

  document.querySelectorAll('.bigbtn.mode').forEach((b) =>
    b.addEventListener('click', () => setMode(b.dataset.mode)));
  document.querySelectorAll('.fold-head').forEach((h) =>
    h.addEventListener('click', () => h.parentElement.classList.toggle('closed')));

  $('inSide').addEventListener('change', fillDevices);
  $('outSide').addEventListener('change', fillDevices);
  $('devIn').addEventListener('change', checkSampleRates);
  $('devOut').addEventListener('change', checkSampleRates);
  $('startBtn').addEventListener('click', () => start());
  $('stopBtn').addEventListener('click', () => stop());
  $('applyBtn').addEventListener('click', applyRestart);
  $('recBtn').addEventListener('click', toggleRecord);
  $('recFormat').addEventListener('change', savePrefs);
  $('f0method').addEventListener('change', () => {
    if ($('f0method').value === 'rmvpe') toast(T.rmvpeBroken, true);
  });
  $('lang').addEventListener('change', () => { lang = $('lang').value; applyLang(); });
  $('themeBtn').addEventListener('click', toggleTheme);
  $('serverBtn').addEventListener('click', () => showServerBar($('serverBar').hidden));
  $('serverSave').addEventListener('click', saveServer);
  $('serverRetry').addEventListener('click', () => { toast(T.loading); connect(); });
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

// quiet：由「套用」呼叫時用的，它自己會報告結果，這裡就不要再跳一次提示。
// 失敗時 quiet 會把例外往上丟，讓「套用」知道重開沒成功。
async function start(quiet) {
  if (!selected) return;
  if (mode === 'remote' && !micAvailable()) {
    toast(T.insecure + ' ' + T.insecureFix, true);
    return;
  }
  $('startBtn').disabled = true;
  underrunCount = 0;
  browserGain = dbToLinear(+$('gainOut').value);
  if (!quiet) toast(T.loading);

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
          // 輸出與監聽音量由客戶端套用，重開之後要帶著走，否則每按一次
          // 「套用」使用者剛調好的喇叭音量就會跳回 0dB
          out_gain_db: +$('gainOut').value,
          mon_gain_db: +$('gainMon').value,
          model: selected.name,
          params: collectParams(),
        }),
      });
      if (!res.ok) throw new Error(errText((await res.json()).detail || res.statusText));
      running = true;
      $('stopBtn').disabled = false;
      $('stopBtn').classList.add('active');
      if (!quiet) toast(T.started);
      dirty = false;
      updateApply();
      setRecUI();
      return;
    }
    const r = await api('/api/start', { method: 'POST', body: JSON.stringify(body) });
    if (mode === 'remote') await startRemoteAudio(r.block_frame, r.samplerate);
    running = true;
    $('stopBtn').disabled = false;
    $('startBtn').classList.remove('active');
    $('stopBtn').classList.add('active');
    if (!quiet) toast(T.started);
    dirty = false;
    updateApply();
    setRecUI();
  } catch (e) {
    $('startBtn').disabled = false;
    await api('/api/stop', { method: 'POST' }).catch(() => {});
    setRecUI();
    if (quiet) throw e;      // 讓「套用」知道重開失敗了
    toast(T.startFail + '：' + e.message, true);
  }
}

async function stop(quiet) {
  $('stopBtn').disabled = true;
  stopRemoteAudio();
  if (isClient) {
    try { await fetch('/client/stop', { method: 'POST' }); } catch (_) {}
  } else {
    try { await api('/api/stop', { method: 'POST' }); } catch (_) {}
  }
  running = false;
  // 停止轉換時伺服器會順手把錄音收起來（沒有音訊再進來了），
  // 所以這裡的狀態要跟著改，不然按鈕會停在「停止錄製」。
  if (recording) { recording = false; refreshRecordings(); }
  $('startBtn').disabled = false;
  $('stopBtn').classList.remove('active');
  setRecUI();
  if (!quiet) { dirty = false; updateApply(); toast(T.stopped); }
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
  ws.onmessage = (e) => {
    const f = new Float32Array(e.data);
    // 瀏覽器放音時音訊不經過任何一端的音效卡，輸出音量只能在這裡乘上去。
    // 就地乘沒問題：這個陣列是剛從 ArrayBuffer 建出來的，沒有別人在看。
    if (browserGain !== 1) {
      for (let i = 0; i < f.length; i++) f[i] = Math.max(-1, Math.min(1, f[i] * browserGain));
    }
    play.port.postMessage(f);
  };

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

// ---------- 錄製 ----------
//
// 錄的是伺服器端 engine 產出的訊號，所以四種路由組合與瀏覽器模式全都錄得到。
// 檔案寫在跑模型那台機器上；客戶端模式下，下載與刪除都是透過本機介面轉發
// 過去的，使用者不必自己去翻伺服器的資料夾。

let recFiles = [];

const fmtBytes = (n) =>
  (n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB'
                : n >= 1024 ? Math.round(n / 1024) + ' KB' : n + ' B');

const fmtClock = (sec) => {
  const s = Math.max(0, Math.round(sec));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
};

function setRecUI() {
  $('recBtnLabel').textContent = recording ? T.recStop : T.recStart;
  $('recBtn').classList.toggle('on', recording);
  // 沒在轉換就沒有音訊可錄。已經在錄的時候要留著按鈕才停得下來。
  $('recBtn').disabled = !running && !recording;
  $('recFormat').disabled = recording;
  if (!recording) $('recElapsed').textContent = '—';
}

function renderRecordings(list) {
  recFiles = list || [];
  $('recCount').textContent = T.recCount(recFiles.length);
  if (!recFiles.length) {
    $('recList').innerHTML = `<li class="empty">${T.recNone}</li>`;
    return;
  }
  // 檔名一律由伺服器產生並比對過白名單樣式（recorder.NAME_RE），
  // 只可能是 rvc-日期-時間.wav|mp3，不會夾帶標記。
  $('recList').innerHTML = recFiles.map((f) => `
    <li${f.recording ? ' class="on"' : ''}>
      <span class="nm">${f.name}</span>
      <span class="sz">${f.recording ? '●' : fmtBytes(f.bytes)}</span>
      <a class="icobtn wide" download
         href="/api/recordings/file?name=${encodeURIComponent(f.name)}&token=${encodeURIComponent(TOKEN)}"
         >${T.recDownload}</a>
      <button class="icobtn wide del" data-name="${f.name}">${T.recDelete}</button>
    </li>`).join('');
  $('recList').querySelectorAll('button.del').forEach((b) =>
    b.addEventListener('click', () => removeRecording(b.dataset.name)));
}

async function refreshRecordings() {
  try {
    renderRecordings((await api('/api/recordings')).recordings);
  } catch (_) {}
}

async function toggleRecord() {
  $('recBtn').disabled = true;
  try {
    if (recording) {
      // mp3 要等 ffmpeg 轉完才回來，長一點的錄音會停在這裡幾秒
      $('recElapsed').textContent = T.recEncoding;
      const r = await api('/api/record/stop', { method: 'POST' });
      recording = false;
      toast(T.recStopped(r.name));
      // 掉塊代表磁碟跟不上，錄出來的東西會少一段，要講出來而不是默默吞掉
      if (r.dropped) toast(T.recDropped(r.dropped), true);
    } else {
      if (!running) { toast(T.recNeedRunning, true); return; }
      await api('/api/record/start', {
        method: 'POST', body: JSON.stringify({ format: $('recFormat').value }),
      });
      recording = true;
      toast(T.recStarted);
    }
  } catch (e) {
    toast(e.message, true);
  } finally {
    $('recBtn').disabled = false;
    setRecUI();
    refreshRecordings();
  }
}

async function removeRecording(name) {
  if (!confirm(T.recConfirmDelete(name))) return;
  try {
    await api('/api/recordings/delete', { method: 'POST', body: JSON.stringify({ name }) });
    toast(T.recDeleted);
  } catch (e) {
    toast(e.message, true);
  }
  refreshRecordings();
}

// ---------- 狀態輪詢 ----------

// RMS 直接映射到寬度會幾乎看不到動靜，開根號後視覺回應比較接近聽感。
const vuScale = (rms) => Math.min(100, Math.sqrt(Math.max(0, rms)) * 140);

function startPolling() {
  // connect() 在「重試」之後會再跑一次，沒有這道防護就會疊出第二個計時器
  if (pollTimer) return;
  pollTimer = setInterval(tick, 500);
}

async function tick() {
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

      // 錄製狀態以伺服器為準：它會在停止轉換、或行程收尾時自己把錄音收起來，
      // 介面不能一直顯示「錄製中」。
      const rec = s.recording || {};
      if (!!rec.active !== recording) {
        recording = !!rec.active;
        setRecUI();
        refreshRecordings();
      }
      if (recording) $('recElapsed').textContent = fmtClock(rec.seconds || 0);

      if (!s.running) {
        if (running) {
          running = false;
          $('startBtn').disabled = false;
          $('stopBtn').disabled = true;
          dirty = false;
          updateApply();
          setRecUI();
        }
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
}

// ---------- 啟動 ----------

// 刻意拆成兩段。boot() 只做一次，把介面畫出來、把按鈕接好；connect() 才去
// 碰伺服器，而且可以重跑。客戶端的介面是本機服務的，所以伺服器連不上時使用者
// 看到的是一個完整、可操作的頁面加上一則錯誤，而不是一片空白——而那正是最
// 需要位址欄與「重試」的時候。
async function boot() {
  document.documentElement.dataset.theme = localStorage.getItem('rvc.theme') || 'light';
  $('lang').value = lang;

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

  bindUI();
  applyLang();
  await connect();
}

async function connect() {
  $('serverErr').hidden = true;
  $('startBtn').disabled = true;

  // 不檢查 TOKEN 是否存在：伺服器可能以 --no-token 啟動（前面有會驗證身分的
  // 反向代理）。直接打 API，真的被擋下來才回報是 token 的問題。
  try {
    models = (await api('/api/models')).models;
  } catch (e) {
    const isAuth = e.message === T.err.invalid_token;
    toast(isAuth ? T.noToken : T.connFail + '：' + e.message, true);
    // token 錯的話換位址沒有用，把輸入列打開只會誤導
    if (isClient && !isAuth) {
      showServerBar(true);
      $('serverErr').hidden = false;
      $('serverErr').textContent = `${T.offlineTitle}：${e.message} ${T.offlineHint}`;
    }
    return;
  }

  if (!models.length) { toast(T.noModels, true); return; }

  showServerBar(false);
  const saved = localStorage.getItem('rvc.model');
  selectModel(models.find((m) => m.name === saved) || models[0]);

  await loadDevices();
  // 順序有講究：setMode 在客戶端模式會換掉抖動緩衝滑桿的刻度，
  // restorePrefs 必須排在它後面，否則存下來的值會被預設值蓋掉。
  setMode(localStorage.getItem('rvc.mode') || 'local');
  restorePrefs();
  dirty = false;
  updateApply();
  $('startBtn').disabled = false;
  await refreshRecordings();
  setRecUI();
  startPolling();
}

boot().catch((e) => showFatal((e && e.stack) || String(e)));
