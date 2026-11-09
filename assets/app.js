const $ = (id) => document.getElementById(id);
const S = {
  state: null, segments: [], audioCtx: null, ws: null, media: null,
  node: null, src: null, recording: false, partial: "", pollTimer: null,
  reveal: false, visCount: -1,
  openItems: new Set(),      // 哪几项清单点开看详情了（轮询重画时要保住）
  foldedGroups: new Set(),   // 哪几组折叠了
  leftSig: "", midSig: "",
};

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
function mmss(x) {
  x = Math.max(0, Math.floor(x || 0));
  return Math.floor(x / 60) + ":" + String(x % 60).padStart(2, "0");
}
function msg(t, cls) { $("stMsg").textContent = t || ""; $("stMsg").className = cls || ""; }

let toastTimer = null;
function showToast(text, kind, ms) {
  const el = $("toast");
  el.textContent = text;
  el.className = "toast show" + (kind ? " " + kind : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = "toast" + (kind ? " " + kind : ""); }, ms || 3200);
}

// ---------------------------------------------------------------- 初始化
function selfCheck() {
  const problems = [];
  if (!window.isSecureContext) problems.push("当前不是安全上下文（必须用 127.0.0.1 或 https 打开）");
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) problems.push("浏览器不支持麦克风采集");
  if (typeof AudioWorkletNode === "undefined") problems.push("浏览器不支持 AudioWorklet");
  if (typeof WebSocket === "undefined") problems.push("浏览器不支持 WebSocket");
  console.log("[自检]", {
    origin: location.origin,
    secure: window.isSecureContext,
    mediaDevices: !!navigator.mediaDevices,
    audioWorklet: typeof AudioWorkletNode,
  });
  if (problems.length) showToast("环境有问题：" + problems.join("；"), "err", 20000);
  return problems.length === 0;
}

let pollFails = 0;

// 「服务正常 / 服务连不上」——这是看后端轮询通不通，跟"有没有在录音"是两回事。
// 以前顶栏那个"未连接"其实是"没在录音"，后端明明在跑也显示未连接，容易吓人。
// 圆点画三态：绿=服务正常、红=连不上、绿+呼吸=正在录音
function paintDot() {
  const el = $("dot");
  if (!el) return;
  const online = S.online !== false;          // 首次还没判定时先按正常画
  el.className = "dot" + (online ? " ok" : "") + (S.recording ? " rec" : "");
  el.title = (online ? "服务正常" : "服务连不上") + (S.recording ? " · 正在录音" : " · 没在录音");
}

function setOnline(ok) {
  if (S.online === ok) return;
  const wasOffline = S.online === false;
  S.online = ok;
  const el = $("conn");
  el.textContent = ok ? "服务正常" : "服务连不上 · 重试中";
  el.className = "sub" + (ok ? "" : " bad");
  paintDot();
  if (ok && wasOffline) showToast("后端连接已恢复", "ok", 2500);
}

async function boot() {
  selfCheck();
  try {
    const cases = await (await fetch("/api/cases")).json();
    $("caseSel").innerHTML = cases.map((c) =>
      '<option value="' + c.id + '">' + esc(c.name) + "</option>").join("");
    await refreshState();
    await loadTranscript();
  } catch (e) {
    setOnline(false);
    msg("连不上后端：确认 app.py 在跑、端口对不对", "err");
    showToast("连不上后端（服务没起或端口不对）", "err", 12000);
  }
}

async function refreshState() {
  let st;
  try {
    st = await (await fetch("/api/state")).json();
  } catch (e) {
    pollFails += 1;
    if (pollFails >= 2) setOnline(false);   // 连续两次失败才判定掉线，避免偶发抖动
    throw e;
  }
  pollFails = 0;
  setOnline(true);
  S.state = st;
  if (!$("caseSel").dataset.init) {
    const cases = [...$("caseSel").options].map((o) => o.value);
    if (cases.includes(st.case)) $("caseSel").value = st.case;
    $("caseSel").dataset.init = "1";
  }
  renderLeft(st);
  renderMid(st);
  renderStatus(st);
}

async function loadTranscript() {
  const t = await (await fetch("/api/transcript")).json();
  S.segments = t.segments || [];
  renderRight();
}

// ---------------------------------------------------------------- 左栏
// 轮询每 1.5 秒来一次。以前每次都把整栏 innerHTML 重画，结果：
//   - 点开的详情 1.5 秒后自己合上（实测 open=1 → 2 秒后 open=0）；
//   - 往下滚的清单被顶回顶部（实测 250 → 167）。
// 现在两件事：内容没变就不碰 DOM；真要重画，也把"展开/折叠/滚动位置"原样恢复。
function leftSignature(st) {
  return JSON.stringify([
    st.totals,
    (st.asks || []).map((a) => [a.id, a.state, a.reasons]),
    (st.groups || []).map((g) => [g.id, g.covered, g.items.map((i) => [
      i.id, i.state, i.manual ? 1 : 0, i.matched, i.negated,
      (i.evidence && i.evidence.length) ? i.evidence[i.evidence.length - 1] : "",
    ])]),
  ]);
}

function renderLeft(st, force) {
  const sig = leftSignature(st);
  if (!force && sig === S.leftSig) return;
  S.leftSig = sig;

  const t = st.totals || {};
  $("leftCount").textContent = "已覆盖 " + (t.covered || 0) + "/" + (t.total || 0);

  let html = "";
  if (st.asks && st.asks.length) {
    html += '<div class="blocktitle">这轮最该补问</div>';
    html += st.asks.map((a) =>
      '<div class="askcard">' +
        '<div class="lab">' + esc(a.label) + "</div>" +
        '<div class="need">' + esc(a.need) + "</div>" +
        ((a.reasons && a.reasons.length)
          ? '<div class="why">刚聊到：' + esc(a.reasons.join("、")) + "</div>" : "") +
        '<div class="say" data-say="' + esc(a.ask) + '">' + esc(a.ask) +
          '<span class="copy">点击复制</span></div>' +
      "</div>").join("");
  }

  html += '<div class="blocktitle" style="margin-top:14px">清单分组</div>';
  html += (st.groups || []).map((g) => {
    const pct = g.total ? Math.round((g.covered / g.total) * 100) : 0;
    const folded = S.foldedGroups.has(g.id);
    const items = folded ? "" : g.items.map((i) => {
      const ev = i.evidence && i.evidence.length
        ? '<div class="ev" data-ev="' + esc(i.evidence[i.evidence.length - 1]) + '">来源：' +
          esc(i.evidence[i.evidence.length - 1]) + "</div>" : "";
      const hit = (i.matched && i.matched.length)
        ? '<div class="hit">命中：' + esc(i.matched.join("、")) + "</div>" : "";
      const neg = (i.negated && i.negated.length)
        ? '<div class="neg">客户说没有/记不清：' + esc(i.negated.join("、")) + "</div>" : "";
      const open = S.openItems.has(i.id) ? " open" : "";
      return '<div class="item' + open + '" data-item="' + esc(i.id) + '" data-state="' + i.state + '">' +
        '<span class="chip s' + i.state + (i.manual ? " manual" : "") +
          '" data-cycle="' + esc(i.id) + '" title="点一下改状态：红→黄→绿→红"></span>' +
        (i.manual ? '<span class="auto" data-auto="' + esc(i.id) +
          '" title="这一项是你人工改过的，点这里恢复自动判定">↺</span>' : "") +
        '<div><div class="txt">' + esc(i.label) + "</div>" +
        '<div class="itemneed">' + esc(i.need) + hit + neg + ev + "</div></div>" +
      "</div>";
    }).join("");
    return '<div class="group">' +
      '<div class="grouphead' + (folded ? " folded" : "") + '" data-group="' + esc(g.id) + '">' +
        '<span class="caret">' + (folded ? "▸" : "▾") + "</span>" +
        "<span>" + esc(g.name) + "</span>" +
        '<span class="bar"><i style="width:' + pct + '%"></i></span>' +
        '<span class="num">' + g.covered + "/" + g.total + "</span>" +
      "</div>" + items + "</div>";
  }).join("");

  const body = $("leftBody");
  const scroll = body.scrollTop;
  body.innerHTML = html;
  body.scrollTop = scroll;
}

// ---------------------------------------------------------------- 中栏
const TAGNAME = { confirm: "数字确认", risk: "风险", evidence: "证据", ask: "补问" };
function importCard(st) {
  const im = st.import || {};
  if (!im.running) return "";
  // 只有当前这一场次的导入才显示进度卡
  if (im.dir && st.sessionDir && im.dir !== st.sessionDir) return "";
  const pct = im.total ? Math.min(100, Math.round((im.done / im.total) * 100)) : 0;
  return '<div class="rc imp">' +
    '<div class="head"><span class="tag">导入</span>' +
      '<span class="t">' + (im.ready ? "回放中 · 后台转写" : "正在导入录音") +
        '</span><span class="time">' + pct + '%</span></div>' +
    '<div class="b">已转 ' + (im.done || 0) + " / " + (im.total || 0) + " 秒" +
      " · 已出 " + (st.segments || 0) + " 段</div>" +
    '<div class="pbar"><i style="width:' + pct + '%"></i></div>' +
  "</div>";
}

function reminderCard(r) {
  return '<div class="rc">' +
    '<div class="head"><span class="tag ' + esc(r.type) + '">' +
      esc(TAGNAME[r.type] || r.type) + "</span>" +
      '<span class="t">' + esc(r.title) + "</span>" +
      '<span class="time">' + mmss(r.at) + "</span></div>" +
    '<div class="b">' + esc(r.body) + "</div>" +
    '<div class="src" data-src="' + esc(r.src) + '">来源：' + esc(r.src) + "</div>" +
  "</div>";
}

function renderMid(st, force) {
  const rs = st.reminders || [];
  const card = importCard(st);
  const im = st.import || {};
  const sig = JSON.stringify([
    rs.map((r) => [r.type, r.title, r.at, r.body, r.src]),
    card ? [im.done, im.ready] : null,
  ]);
  if (!force && sig === S.midSig) return;
  S.midSig = sig;

  $("midCount").textContent = rs.length + " 条";
  const body = $("midBody");
  const scroll = body.scrollTop;
  if (!rs.length && !card) {
    body.innerHTML = '<div class="empty">还没有提醒。<br>客户一说到金额、日期、凭证，这里会立刻出卡片——不等模型。</div>';
  } else {
    body.innerHTML = (card || "") + rs.map(reminderCard).join("");
  }
  body.scrollTop = scroll;
}

function renderStatus(st) {
  $("stModel").textContent = "模型：" + (st.asrModel || "—");
  $("stSession").textContent = "场次：" + (st.sessionDir || "—");
  $("stChars").textContent = "字数：" + (st.chars || 0);
  const t = st.totals || {};
  $("stCover").textContent = "覆盖：" + (t.covered || 0) + "/" + (t.total || 0) +
    "（黄 " + (t.partial || 0) + " / 红 " + (t.missing || 0) + "）";
  $("timer").textContent = mmss(st.duration || 0);
  const im = st.import || {};
  // 导入状态是全局的：切到别的场次后，别再挂着上一场的"导入完成"
  if (im.dir && st.sessionDir && im.dir !== st.sessionDir) {
    $("stImport").textContent = "";
    $("stImport").className = "";
    return;
  }
  if (im.running) {
    const pct = im.total ? Math.round((im.done / im.total) * 100) : 0;
    $("stImport").textContent = "导入中 " + pct + "%（" + im.done + "/" + im.total + "s）";
    $("stImport").className = "warn";
  } else if (im.error) {
    $("stImport").textContent = "导入失败：" + im.error;
    $("stImport").className = "err";
  } else if (im.total) {
    $("stImport").textContent = "导入完成 " + im.total + "s";
    $("stImport").className = "";
  } else {
    $("stImport").textContent = "";
  }
}

// ---------------------------------------------------------------- 右栏
function playhead() {
  const a = $("audio");
  return isFinite(a.currentTime) ? a.currentTime : 0;
}

function visibleSegments() {
  if (!S.reveal) return S.segments;
  const t = playhead() + 0.5;               // 略微提前，别让字卡在声音后面
  return S.segments.filter((s) => s.start <= t);
}

function renderRight(scrollBottom) {
  const box = $("rightBody");
  const vis = visibleSegments();
  $("rightCount").textContent = S.reveal
    ? "已显示 " + vis.length + "/" + S.segments.length
    : S.segments.length + " 段";
  $("btnReveal").hidden = !S.reveal;
  let html = vis.map((s) =>
    '<div class="seg" data-seg="' + esc(s.id) + '" data-start="' + s.start + '">' +
      '<div class="meta"><span>' + mmss(s.start) + "</span>" +
        '<span class="id">' + esc(s.id) + "</span></div>" +
      '<div class="t">' + esc(s.text) + "</div>" +
    "</div>").join("");
  if (S.partial) {
    html += '<div class="seg partial"><div class="meta"><span>…</span></div>' +
      '<div class="t">' + esc(S.partial) + "</div></div>";
  }
  if (!html) html = '<div class="hint">点「开始录音」，或「导入录音」选一个视频/音频文件。</div>';
  box.innerHTML = html;
  if (scrollBottom) box.scrollTop = box.scrollHeight;
}

function scrollBottom() { const b = $("rightBody"); b.scrollTop = b.scrollHeight; }

// ---------------------------------------------------------------- 录音
function makeWorklet(ctx) {
  return new AudioWorkletNode(ctx, "record-audio-processor");
}

async function startRec() {
  if (S.recording) return;
  S.reveal = false; S.visCount = -1;      // 实时录音不按播放头过滤，说了就显示
  const btn = $("btnRec");
  btn.disabled = true;
  btn.textContent = "连接中…";
  showToast("正在请求麦克风…", null, 2500);

  const fail = (why) => {
    btn.disabled = false;
    btn.textContent = "开始录音";
    showToast("开不了录音：" + why, "err", 10000);
    msg("开不了录音：" + why, "err");
    console.error("startRec failed:", why);
  };

  try {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      return fail("浏览器不支持采集。必须用 http://127.0.0.1 或 https 打开（用局域网 IP 的 http 不行）");
    }
    // 先问权限状态，好给准确提示
    try {
      const p = await navigator.permissions.query({ name: "microphone" });
      if (p.state === "denied") {
        return fail("麦克风权限被拒了。点地址栏左边的图标 → 把麦克风改成「允许」，然后刷新页面");
      }
    } catch (e) { /* 有的浏览器不支持 query，忽略 */ }

    S.recStartSegs = S.segments.length;
    S.media = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
      video: false,
    });
  } catch (e) {
    const name = (e && e.name) || "";
    if (name === "NotAllowedError") return fail("麦克风被拒绝。点地址栏左边图标 → 网站设置 → 麦克风「允许」，再刷新");
    if (name === "NotFoundError") return fail("这台机器没有可用的麦克风");
    if (name === "NotReadableError") return fail("麦克风被别的程序占着（会议/录音软件），关掉再试");
    return fail((e && e.message) || String(e));
  }

  try {
    // 强制 16k 在部分 Windows 机器上会抛错，兜底用系统默认采样率（后面会重采样）
    try {
      S.audioCtx = new AudioContext({ sampleRate: 16000 });
    } catch (e) {
      S.audioCtx = new AudioContext();
    }
    if (S.audioCtx.state === "suspended") await S.audioCtx.resume();
    await S.audioCtx.audioWorklet.addModule("./audio_process.js");
    S.src = S.audioCtx.createMediaStreamSource(S.media);
    S.node = makeWorklet(S.audioCtx);
    S.node.port.onmessage = (ev) => pushPcm(ev.data.data);
    S.src.connect(S.node);
    S.node.connect(S.audioCtx.destination);
  } catch (e) {
    stopRec();
    return fail("音频初始化失败：" + ((e && e.message) || e));
  }

  const proto = location.protocol === "https:" ? "wss" : "ws";
  S.ws = new WebSocket(proto + "://" + location.host + "/asr?samplerate=16000");
  S.ws.binaryType = "arraybuffer";
  S.ws.onopen = () => {
    S.recording = true;
    setRecUI(true);
    btn.textContent = "录音中";
    showToast("录音中，本地识别（录音不出本机）", "ok", 3000);
    msg("录音中，本地识别");
  };
  S.ws.onmessage = (ev) => {
    const d = JSON.parse(ev.data);
    if (d.type === "partial") { S.partial = d.text; renderRight(true); }
    else if (d.type === "segment") {
      S.partial = "";
      S.segments.push(d.segment);
      renderRight(true);
      refreshState();
      reloadAudioLater();
    }
  };
  S.ws.onclose = () => {
    setRecUI(false);
    btn.textContent = "开始录音";
    msg("已停止");
  };
  S.ws.onerror = () => {
    showToast("转写连接出错，看看服务还在不在", "err", 6000);
    msg("连接出错", "err");
  };
  setTimeout(() => {
    if (!S.recording && S.ws && S.ws.readyState !== 1) {
      fail("连不上服务（WebSocket 没建立）");
    }
  }, 5000);
}

let reloadTimer = null;
function reloadAudioLater() {
  clearTimeout(reloadTimer);
  reloadTimer = setTimeout(() => {
    const a = $("audio");
    const wasPlaying = !a.paused;
    a.src = "/api/audio?t=" + Date.now();
    if (wasPlaying) a.play().catch(() => {});
  }, 2500);
}

function resampleTo16k(int16, fromRate) {
  if (fromRate === 16000) return int16;
  const ratio = 16000 / fromRate;
  const n = Math.floor(int16.length * ratio);
  const out = new Int16Array(n);
  for (let i = 0; i < n; i++) {
    const p = i / ratio, i0 = Math.floor(p), i1 = Math.min(i0 + 1, int16.length - 1);
    out[i] = int16[i0] + (int16[i1] - int16[i0]) * (p - i0);
  }
  return out;
}

let vuSmooth = 0;
let vuSilentSince = 0;

function updateVu(int16) {
  if (!int16 || !int16.length) return;
  let sum = 0;
  for (let i = 0; i < int16.length; i++) {
    const v = int16[i] / 32768;
    sum += v * v;
  }
  const rms = Math.sqrt(sum / int16.length);
  vuSmooth = vuSmooth * 0.7 + rms * 0.3;
  const db = 20 * Math.log10(vuSmooth + 1e-9);
  const pct = Math.max(0, Math.min(100, (db + 60) * (100 / 55)));
  const el = $("vu");
  if (el) {
    $("vuBar").style.width = pct + "%";
    el.className = "vu" + (pct > 85 ? " hot" : pct > 55 ? " mid" : "");
  }
  // 连续 3 秒低于 -55dB，当麦克风没收到声音
  if (db < -55) {
    if (!vuSilentSince) vuSilentSince = Date.now();
    else if (Date.now() - vuSilentSince > 3000) {
      if (el) el.className = "vu dead";
      if (S.recording) msg("麦克风没收到声音：查 Windows 输入设备和 Chrome 的麦克风选择", "err");
    }
  } else {
    vuSilentSince = 0;
  }
}

function pushPcm(int16) {
  updateVu(int16);
  if (!S.ws || S.ws.readyState !== 1) return;
  const pcm = resampleTo16k(int16, S.audioCtx ? S.audioCtx.sampleRate : 16000);
  S.ws.send(pcm.buffer);
}

function stopRec() {
  if (S.ws) { try { S.ws.close(); } catch (e) {} S.ws = null; }
  if (S.src) { try { S.src.disconnect(); } catch (e) {} }
  if (S.node) { try { S.node.disconnect(); } catch (e) {} }
  if (S.media) S.media.getTracks().forEach((t) => t.stop());
  if (S.audioCtx) { try { S.audioCtx.close(); } catch (e) {} }
  S.media = S.node = S.src = S.audioCtx = null;
  S.recording = false;
  S.partial = "";
  setRecUI(false);
  setTimeout(async () => {
    await loadTranscript().catch(() => {});
    reloadAudioLater();
    if (S.recStartSegs != null && S.segments.length === S.recStartSegs) {
      showToast("这次录音一段字都没出——先看左上角电平表：说话时它不动，就是麦克风没收到声音",
        "err", 9000);
    }
    S.recStartSegs = null;
    if ($("vu")) { $("vu").className = "vu"; $("vuBar").style.width = "0"; }
  }, 800);
}

// 停录音并等后端收尾（flush VAD、把最后一句落盘）。换场次、导入前都要等这一步。
async function stopRecAndWait() {
  if (!S.recording && !S.ws) return;
  stopRec();
  await new Promise((r) => setTimeout(r, 600));
}

function setRecUI(on) {
  $("btnRec").disabled = on;
  $("btnStop").disabled = !on;
  paintDot();
  const badge = $("recState");
  if (badge) badge.hidden = !on;      // "录音中"单独显示，不再冒充连接状态
}

// ---------------------------------------------------------------- 回放
function bindPlayer() {
  const a = $("audio");
  a.addEventListener("timeupdate", () => {
    const d = a.duration || (S.state && S.state.duration) || 1;
    const p = Math.min(100, (a.currentTime / d) * 100);
    $("played").style.width = p + "%";
    $("head").style.left = p + "%";
    $("ptime").textContent = mmss(a.currentTime);
    if (S.reveal) {                       // 边播边露字：只在"能看到新句子"时才重画
      const n = visibleSegments().length;
      if (n !== S.visCount) { S.visCount = n; renderRight(true); }
    }
  });
  a.addEventListener("ended", () => {
    if (S.reveal) {
      S.reveal = false;
      renderRight(false);
      showToast("播放结束，全文已显示", "ok", 3500);
    }
  });
  $("playIco").onclick = () => {
    if (a.paused) { a.play().catch(() => {}); } else { a.pause(); }
  };
  a.addEventListener("play", () => ($("playIco").textContent = "❚❚ 播放中"));
  a.addEventListener("pause", () => ($("playIco").textContent = "▶ 回放"));
  $("track").addEventListener("click", (ev) => {
    const r = $("track").getBoundingClientRect();
    const d = a.duration || 1;
    a.currentTime = ((ev.clientX - r.left) / r.width) * d;
    a.play().catch(() => {});
  });
}

async function startPlayback() {
  const a = $("audio");
  a.src = "/api/audio?t=" + Date.now();
  a.currentTime = 0;
  S.reveal = true;
  S.visCount = -1;
  renderRight(false);
  try {
    await a.play();
    showToast("回放中，文字跟着进度一句句出来", "ok", 4500);
  } catch (e) {
    showToast("浏览器拦了自动播放，点左上角「▶ 回放」开始", "err", 7000);
  }
}

function seekTo(start) {
  const a = $("audio");
  if (!a.src) a.src = "/api/audio?t=" + Date.now();
  a.currentTime = Math.max(0, start - 0.2);
  a.play().catch(() => {});
}

// ---------------------------------------------------------------- 事件
function bindEvents() {
  $("btnRec").onclick = startRec;
  $("btnStop").onclick = stopRec;

  $("btnNew").onclick = async () => {
    if (S.recording) await stopRecAndWait();
    const caseId = $("caseSel").value;
    const r = await (await fetch("/api/reset", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ case: caseId }),
    })).json();
    S.segments = []; S.partial = ""; S.reveal = false; S.visCount = -1; renderRight();
    $("audio").src = "";
    $("btnReveal").hidden = true;
    msg("已新开一场：" + r.state.sessionDir);
    refreshState();
  };

  $("caseSel").onchange = async () => {
    msg("切换案件类型会新开一场，点「新开一场」生效");
  };

  $("btnReveal").onclick = async () => {
    S.reveal = false;
    await loadTranscript();
    renderRight(false);
    showToast("已显示全部转写", "ok", 2000);
  };

  // 导出纪要：后端拼 Markdown，浏览器直接下载
  $("btnExport").onclick = () => {
    if (!S.state) return;
    location.href = "/api/export?fmt=md";
    showToast("正在导出这一场的会谈纪要", "ok", 2500);
  };

  $("btnCopyAll").onclick = async () => {
    if (!S.segments.length) { showToast("这一场还没有转写内容", "err", 2500); return; }
    const text = S.segments.map((s) => "[" + mmss(s.start) + "] " + s.text).join("\n");
    try {
      await navigator.clipboard.writeText(text);
      showToast("全文已复制（" + S.segments.length + " 段）", "ok", 2500);
    } catch (e) {
      showToast("浏览器不给复制，用「导出纪要」拿文件", "err", 4000);
    }
  };

  $("btnHist").onclick = async (ev) => {
    ev.stopPropagation();
    const p = $("histPanel");
    if (!p.hidden) { p.hidden = true; return; }
    const rows = await (await fetch("/api/sessions")).json();
    const cur = S.state && S.state.sessionDir;
    p.innerHTML = '<div class="htitle">历史场次（' + rows.length + " 场，文件一直在本地）</div>" +
      rows.map((r) => {
        const t = new Date(r.mtime * 1000);
        const hh = (t.getMonth() + 1) + "月" + t.getDate() + "日 " +
          String(t.getHours()).padStart(2, "0") + ":" + String(t.getMinutes()).padStart(2, "0");
        return '<div class="hitem' + (r.dir === cur ? " cur" : "") + '" data-dir="' + esc(r.dir) + '">' +
          '<div class="l1"><span class="tagc">' + esc(r.caseName) + "</span>" +
            '<span>' + hh + " · " + mmss(r.duration) + "</span>" +
            (r.dir === cur ? '<span class="dim" style="margin-left:auto;color:#6ba7e8">当前</span>' : "") +
          "</div>" +
          '<div class="l2"><span>' + r.segments + " 段</span><span>" + r.chars + " 字</span>" +
            '<span class="dim">' + esc(r.dir) + "</span></div>" +
        "</div>";
      }).join("");
    p.hidden = false;
  };

  $("histPanel").onclick = async (ev) => {
    const it = ev.target.closest("[data-dir]");
    if (!it) return;
    const dir = it.dataset.dir;
    $("histPanel").hidden = true;
    showToast("正在打开 " + dir + " …");
    const r = await (await fetch("/api/sessions/load", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ dir }),
    })).json();
    if (!r.ok) { showToast("打开失败：" + (r.detail || "未知"), "err", 6000); return; }
    S.reveal = false; S.visCount = -1; S.partial = "";
    await loadTranscript();
    await refreshState();
    $("audio").src = "/api/audio?t=" + Date.now();
    $("btnReveal").hidden = true;
    renderRight(false);
    showToast("已切到 " + dir, "ok", 3000);
  };

  document.addEventListener("click", (ev) => {
    const p = $("histPanel");
    if (p && !p.hidden && !ev.target.closest("#histPanel") && !ev.target.closest("#btnHist")) {
      p.hidden = true;
    }
  });

  $("btnImport").onclick = async () => {
    // 录音中点导入：先把这一场体面地停掉（录音照样存盘、时间轴保留），再选文件。
    // 直接导的话，导入会新开一场并关掉正在写的 audio.wav，录音连接当场崩（seek of closed file）。
    if (S.recording) {
      showToast("正在录音，先停一下再导入——刚才录的不会丢", "ok", 4000);
      await stopRecAndWait();
    }
    $("fileInput").click();
  };
  $("fileInput").onchange = async () => {
    const f = $("fileInput").files[0];
    if (!f) return;
    $("fileInput").value = "";
    showToast("正在上传 " + (f.size / 1048576).toFixed(1) + " MB …");
    $("btnImport").classList.add("busy");
    $("btnImport").textContent = "导入中…";
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/upload");
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) msg("上传中 " + Math.round((e.loaded / e.total) * 100) + "%");
    };
    xhr.onload = async () => {
      $("btnImport").classList.remove("busy");
      $("btnImport").textContent = "导入录音";
      if (xhr.status === 200) {
        await loadTranscript();
        await refreshState();
        for (let i = 0; i < 12; i++) {            // 等整段录音落盘，最多等 6 秒
          if (S.state && S.state.import && S.state.import.ready) break;
          await new Promise((r) => setTimeout(r, 500));
          await refreshState();
        }
        await startPlayback();
      } else {
        showToast("导入失败：" + String(xhr.responseText || "").slice(0, 120), "err", 6000);
      }
    };
    xhr.onerror = () => {
      $("btnImport").classList.remove("busy");
      $("btnImport").textContent = "导入录音";
      showToast("上传出错", "err");
    };
    const fd = new FormData();
    fd.append("file", f);
    xhr.send(fd);
  };

  // 左栏：色块点一下改状态——人工改过的依次是 红→黄→绿→恢复自动；
  // 没人工改过的，就在自动判定基础上往前推一格。
  $("leftBody").addEventListener("click", async (ev) => {
    const chip = ev.target.closest("[data-cycle]");
    if (chip) {
      const id = chip.dataset.cycle;
      const cur = Number(chip.closest(".item").dataset.state || 0);
      const next = (cur + 1) % 3;
      await fetch("/api/override", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ id, state: next }),
      });
      await refreshState();
      return;
    }
    // 人工改过的项旁边有个 ↺：点它回到自动判定
    const auto = ev.target.closest("[data-auto]");
    if (auto) {
      await fetch("/api/override", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ id: auto.dataset.auto, state: -1 }),
      });
      await refreshState();
      showToast("已恢复自动判定", "ok", 1800);
      return;
    }
    // 分组标题：点了折叠 / 展开这一组
    const gh = ev.target.closest("[data-group]");
    if (gh) {
      const gid = gh.dataset.group;
      if (S.foldedGroups.has(gid)) S.foldedGroups.delete(gid);
      else S.foldedGroups.add(gid);
      renderLeft(S.state, true);
      return;
    }
    const say = ev.target.closest("[data-say]");
    if (say) {
      navigator.clipboard.writeText(say.dataset.say).catch(() => {});
      const c = say.querySelector(".copy");
      if (c) { c.textContent = "已复制"; setTimeout(() => (c.textContent = "点击复制"), 1200); }
      return;
    }
    const evEl = ev.target.closest("[data-ev]");
    if (evEl) {
      const id = evEl.dataset.ev;
      const seg = S.segments.find((s) => s.id === id);
      if (seg) { seekTo(seg.start); highlight(id); }
      return;
    }
    const item = ev.target.closest(".item");
    if (item) {
      const id = item.dataset.item;
      if (S.openItems.has(id)) S.openItems.delete(id);
      else S.openItems.add(id);
      item.classList.toggle("open");
    }
  });

  // 中栏：点来源回跳
  $("midBody").addEventListener("click", (ev) => {
    const el = ev.target.closest("[data-src]");
    if (!el) return;
    const id = el.dataset.src;
    const seg = S.segments.find((s) => s.id === id);
    if (seg) { seekTo(seg.start); highlight(id); }
  });

  // 右栏：点句回跳
  $("rightBody").addEventListener("click", (ev) => {
    const el = ev.target.closest("[data-seg]");
    if (!el) return;
    seekTo(Number(el.dataset.start));
    highlight(el.dataset.seg);
  });
}

function highlight(id) {
  document.querySelectorAll(".seg.active").forEach((e) => e.classList.remove("active"));
  const el = document.querySelector('[data-seg="' + id + '"]');
  if (el) {
    el.classList.add("active");
    el.scrollIntoView({ block: "center", behavior: "smooth" });
  }
}

function setIntervalOnce() {
  if (S.timer) return;              // 只允许一个轮询，别越挂越多
  let n = 0;
  S.timer = setInterval(async () => {
    try {
      await refreshState();
      const imp = !!(S.state && S.state.import && S.state.import.running);
      if (n++ % 3 === 0 || imp) await loadTranscript();   // 导入中每次刷新都拉转写
      if (S.wasImporting && !imp) {
        showToast("转写完成，可以开始看了", "ok", 4000);
        n = 0;
      }
      S.wasImporting = imp;
    } catch (e) { /* 网络抖动不打断轮询 */ }
  }, 1500);
}

boot().then(() => {
  bindEvents();
  bindPlayer();
  setIntervalOnce();
});
