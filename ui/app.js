/* ==========================================================================
   橙芯绘话 - front end
   Talks to the local llama.cpp servers through the hub proxy (/v1/*), keeps
   conversations in the hub's data folder, and polls /api/state for model health.
   ========================================================================== */
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

/* ------------------------------------------------------------------ state */
const STORAGE_KEY = 'strata-chat-v1';
const THEME_KEY = 'strata-chat-theme';

// Conversations deleted in this window. The hub unions its chat list by id, so
// deletions need an explicit tombstone or a merge would resurrect them.
const deletedIds = new Set();

const state = {
  models: [],           // from /api/state
  sd: null,             // Stable Diffusion status from /api/state
  tts: null,            // IndexTTS status from /api/state
  chat: null,           // active conversation object
  chats: [],            // all conversations, newest first
  streaming: null,      // { controller, msg }
  settings: loadSettings(),
  theme: localStorage.getItem(THEME_KEY) || 'dark',
  attached: [],         // [{ name, dataUrl, mime }]
  comfy: null,          // Qwen-Image / ComfyUI status from /api/state
  drawBackend: 'sd',    // which service the drawing panel currently drives
};

function loadSettings() {
  const base = {
    temperature: 0.7,
    maxTokens: 3072,
    topP: 0.95,
    system: '',
    reasoning: false,
    stream: true,
    // auto-continue: when a reply stops at the output limit, ask the model to
    // carry on instead of making the user type "继续" by hand
    autoContinue: true,
    continueRounds: 3,
    // routing: only one model fits in VRAM, so a switch costs a reload. Ask
    // before switching instead of silently spending 20-60 s on it.
    autoRoute: true,
    routeAsk: true,
    // drawing panel draft: kept so a half-written prompt survives closing the
    // panel or restarting the app
    drawPrompt: '',
    drawNegative: 'lowres, bad anatomy, text, watermark, blurry',
    drawSize: '832x832',
    drawSteps: 26,
    drawCfg: 7,
    drawSeed: -1,
    ttsVoice: '',           // reference voice for speech synthesis
    ttsEngine: 'browser',   // "browser" (Web Speech, instant) or "local" (IndexTTS)
    browserVoice: '',       // chosen system voice name
    browserRate: 1,         // 0.5 - 2
    ttsText: '',            // draft text in the TTS panel
    ttsSavedAt: 0,          // when the preset was last saved by hand
  };
  try {
    return { ...base, ...(JSON.parse(localStorage.getItem('strata-chat-settings')) || {}) };
  } catch { return base; }
}

function saveSettings() {
  localStorage.setItem('strata-chat-settings', JSON.stringify(state.settings));
}

/* --------------------------------------------------------------- utilities */
const uid = () => Math.random().toString(36).slice(2, 10) + Date.now().toString(36).slice(-4);

// SD and TTS are not chat models, but their messages render like one.
const SD_PSEUDO = { id: 'sd', label: 'Stable Diffusion', short: 'SD', color: '#ffa83d', up: false, port: null, ctx: null };
const TTS_PSEUDO = { id: 'tts', label: '语音合成', short: 'TTS', color: '#8fd47a', up: false, port: null, ctx: null };

/* True when the app itself drew pictures in this conversation.
   A picture the user uploaded is not a drawing thread, so only assistant
   messages produced by Stable Diffusion count. */
function isDrawThread(chat) {
  if (!chat) return false;
  return (chat.messages || []).some(m => m.role === 'assistant' && m.model === 'sd');
}

/* True when this conversation holds speech the app synthesised. */
function isSpeakThread(chat) {
  if (!chat) return false;
  return (chat.messages || []).some(m => m.role === 'assistant' && m.model === 'tts');
}

function modelOf(id) {
  if (id === 'sd') return SD_PSEUDO;
  if (id === 'tts') return TTS_PSEUDO;
  const found = state.models.find(m => m.id === id);
  if (found) return found;
  // before the first /api/state answer we still know how the model is named
  return { id, label: id, short: id.toUpperCase(), color: '#ffa83d', up: false, port: null, ctx: null };
}

function fmtNum(n) {
  if (n == null || !isFinite(n)) return '—';
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k';
  return String(Math.round(n));
}

function fmtCtx(n) {
  if (!n) return '—';
  return n >= 1024 ? Math.round(n / 1024) + 'K' : String(n);
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function toast(text, kind = '') {
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = text;
  $('#toasts').appendChild(el);
  setTimeout(() => {
    el.style.transition = 'opacity .25s, transform .25s';
    el.style.opacity = '0';
    el.style.transform = 'translateY(4px)';
    setTimeout(() => el.remove(), 260);
  }, 3600);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    return ok;
  }
}

/* --------------------------------------------------------------- markdown */
marked.setOptions({ gfm: true, breaks: true, headerIds: false, mangle: false });

function renderMarkdown(text) {
  const raw = marked.parse(text || '');
  const clean = DOMPurify.sanitize(raw, { USE_PROFILES: { html: true } });
  return clean;
}

/* decorate a rendered markdown container: code cards, links, images */
function decorate(container) {
  $$('pre > code', container).forEach(code => {
    const pre = code.parentElement;
    if (pre.closest('.code-card')) return;
    const lang = (code.className.match(/language-([\w+#-]+)/) || [, ''])[1] || 'code';
    const card = document.createElement('div');
    card.className = 'code-card';
    const bar = document.createElement('div');
    bar.className = 'bar';
    bar.innerHTML = `<span>${escapeHtml(lang)}</span><span class="grow"></span>`;
    const copy = document.createElement('button');
    copy.textContent = '复制';
    copy.onclick = async () => {
      copy.textContent = (await copyText(code.textContent)) ? '已复制' : '失败';
      setTimeout(() => { copy.textContent = '复制'; }, 1400);
    };
    bar.appendChild(copy);
    pre.replaceWith(card);
    card.append(bar, pre);
  });

  $$('a', container).forEach(a => {
    a.target = '_blank';
    a.rel = 'noreferrer noopener';
  });

  $$('img', container).forEach(img => {
    img.loading = 'lazy';
    img.onclick = () => openViewer(img.src);
  });
}

/* ------------------------------------------------------------------ views */
function applyTheme() {
  document.documentElement.dataset.theme = state.theme;
}

/* Download a model's missing weights, with live progress in its card. */
async function startDownload(id, host) {
  const m = modelOf(id);
  toast(`开始下载 ${m.label} 的模型文件…`);
  const r = await fetch('/api/download', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model: id }),
  }).then(x => x.json()).catch(e => ({ ok: false, message: String(e) }));
  if (!r.ok) {
    toast(r.message || '下载启动失败', 'err');
    return;
  }
  toast(r.message || '下载已开始', 'ok');
  // the field is rebuilt on the next poll, so hand it to trackJob explicitly
  const stop = trackJob('download', host, '下载中', job => {
    if (job.state === 'done') {
      toast('模型下载完成', 'ok');
      pollState(true);
    } else if (job.state === 'error') {
      toast('下载失败：' + (job.error || '未知原因'), 'err');
    }
  });
  // stop watching if the card disappears
  setTimeout(() => { if (!document.body.contains(host)) stop(); }, 1000);
}

/* Version badge in the sidebar footer. The number comes from the hub, so it
   always matches the code actually running - not whatever the UI was cached at. */
function renderVersion(hub) {
  const el = $('#app-version');
  if (!el) return;
  const name = hub.name || '5070-AI';
  const ver = hub.version || '—';
  el.textContent = `v${ver}`;
  el.title = `${name} v${ver}\n点击查看详情`;
  el.onclick = () => showVersionInfo(hub);
}

function showVersionInfo(hub) {
  const name = hub.name || '5070-AI';
  const ver = hub.version || '—';
  const port = hub.port || '—';
  const bits = [
    `${name} v${ver}`,
    `hub 端口 ${port}`,
    `界面标题 ${document.title || '—'}`,
    '',
    '更新代码后如果界面看着不对，',
    '先按 Ctrl+F5 强制刷新一次。',
  ];
  // 复用现有的 info 弹窗
  if ($('#info-modal')) {
    $('#info-title').textContent = '版本信息';
    $('#info-body').innerHTML = bits
      .map(t => `<div style="font-size:12.5px;line-height:1.8">${escapeHtml(t)}</div>`)
      .join('');
    $('#info-modal').classList.add('open');
  } else {
    toast(`${name} v${ver}`);
  }
}

/* Configuration problems, shown above the sidebar.
 *
 * Without this a broken config is silent: the cards just say "未启动", and the
 * user only finds out after clicking 启动 and reading a terse error. Surfacing
 * it on load turns a mystery into a checklist.
 */
function renderConfigWarnings() {
  const box = $('#config-warn');
  if (!box) return;
  const list = state.configProblems || [];
  if (!list.length) {
    box.hidden = true;
    box.innerHTML = '';
    return;
  }
  box.hidden = false;
  box.innerHTML = `
    <div class="cw-head">
      <b>配置有 ${list.length} 处问题</b>
      <span class="hint">修好后再点启动</span>
    </div>
    <ul>${list.map(t => `<li>${escapeHtml(t)}</li>`).join('')}</ul>`;
}

function renderModelList() {
  const box = $('#model-list');
  box.innerHTML = '';
  if (!state.models.length) {
    box.innerHTML = '<div class="hint" style="padding:6px 6px 10px">无法连接到 hub…</div>';
    return;
  }
  const active = state.chat ? state.chat.model : null;

  state.models.forEach(m => {
    const el = document.createElement('div');
    el.className = 'model';
    el.style.setProperty('--mcolor', m.color);
    el.setAttribute('aria-pressed', String(m.id === active));

    const dot = m.up ? 'up' : (m.loading ? 'loading' : 'down');
    const lacks = (m.missing || []).length > 0;
    const statusText = m.up ? `运行中 · :${m.port}`
      : (m.loading ? '加载中…' : (lacks ? `缺 ${m.missing.length} 个模型文件` : '未启动'));
    el.innerHTML = `
      <div class="glyph">${escapeHtml(m.short)}</div>
      <div class="info">
        <div class="name"><span class="dot ${dot}"></span>${escapeHtml(m.label)}</div>
        <div class="desc">${escapeHtml(m.blurb)}</div>
        <div class="meter ${m.loading ? 'on' : ''}"><i></i></div>
        <div class="desc" style="opacity:.75;margin-top:4px">${escapeHtml(statusText)}</div>
        <div class="dl-progress"></div>
      </div>
      <div class="actions">
        ${lacks && m.downloadable
          ? '<button data-act="download" title="下载缺失的模型文件">下载</button>'
          : (m.up
              ? `<button data-act="stop" class="danger" title="停止，释放显存">停止</button>
                 <button data-act="log" title="查看日志">日志</button>`
              : `<button data-act="start" title="启动该模型">启动</button>
                 <button data-act="log" title="查看日志">日志</button>`)}
        <button data-act="info" title="模型信息">详情</button>
      </div>`;

    el.addEventListener('click', ev => {
      const act = ev.target.closest('[data-act]');
      if (act) {
        ev.stopPropagation();
        const kind = act.dataset.act;
        if (kind === 'start') startModel(m.id);
        else if (kind === 'stop') stopModel(m.id);
        else if (kind === 'log') openLog(m.id);
        else if (kind === 'info') openInfo(m.id);
        else if (kind === 'download') startDownload(m.id, $('.dl-progress', el));
        return;
      }
      selectModel(m.id);
    });
    box.appendChild(el);
  });
}

/* SD drawing card in the sidebar */
function renderSdCard() {
  const box = $('#sd-card');
  if (!box) return;
  const sd = state.sd || {};
  box.innerHTML = '';
  const dot = sd.up ? 'up' : (sd.loading ? 'loading' : 'down');
  const status = sd.up
    ? (sd.model ? `就绪 · ${String(sd.model).replace(/\.(safetensors|ckpt)$/i, '')}` : '就绪 · :' + (sd.port || 7860))
    : (sd.loading ? '启动中…（首次可能 1-3 分钟）' : '未启动');
  const el = document.createElement('div');
  el.className = 'sd';
  el.innerHTML = `
    <div class="glyph">SD</div>
    <div class="info">
      <div class="name"><span class="dot ${dot}"></span>Stable Diffusion</div>
      <div class="desc">${escapeHtml(status)}</div>
    </div>
    <div class="actions">
      ${sd.up
        ? `<button data-act="stop" class="danger">停止</button>
           <button data-act="log">日志</button>`
        : `<button data-act="start">启动</button>
           <button data-act="log">日志</button>`}
      <button data-act="draw">画一张</button>
    </div>`;
  el.addEventListener('click', ev => {
    const act = ev.target.closest('[data-act]');
    if (!act) { openDraw(); return; }
    const kind = act.dataset.act;
    if (kind === 'start') startSd();
    else if (kind === 'stop') stopSd();
    else if (kind === 'log') openSdLog();
    else if (kind === 'draw') openDraw();
  });
  box.appendChild(el);
}

/* Qwen-Image card in the sidebar. Same shape as the SD and TTS cards. */
function renderComfyCard() {
  const box = $('#comfy-card');
  if (!box) return;
  const c = state.comfy || {};
  box.innerHTML = '';
  const dot = c.up ? 'up' : (c.loading ? 'loading' : 'down');
  let status;
  if (c.up) {
    const missing = (c.models || []).filter(m => !m.ok).length;
    status = c.ready
      ? `就绪 · Qwen-Image Turbo${c.vram_total ? ` · 显存 ${c.vram_free}/${c.vram_total}G` : ''}`
      : `缺 ${missing} 个模型文件`;
  } else {
    status = c.loading ? '启动中…（首次 10-20 秒）' : '未启动';
  }
  const el = document.createElement('div');
  el.className = 'sd';
  el.innerHTML = `
    <div class="glyph" style="background:linear-gradient(150deg,#c084fc,#6b21a8)">QI</div>
    <div class="info">
      <div class="name"><span class="dot ${dot}"></span>Qwen 图像</div>
      <div class="desc">${escapeHtml(status)}</div>
    </div>
    <div class="actions">
      ${c.up
        ? `<button data-act="stop" class="danger">停止</button>`
        : `<button data-act="start">启动</button>`}
      <button data-act="log">日志</button>
      <button data-act="draw"${c.ready ? '' : ' disabled'}>画一张</button>
    </div>`;
  el.addEventListener('click', ev => {
    const act = ev.target.closest('[data-act]');
    if (!act) return;
    const kind = act.dataset.act;
    if (kind === 'start') startComfy();
    else if (kind === 'stop') stopComfy();
    else if (kind === 'log') openComfyLog();
    else if (kind === 'draw') openQwen();
  });
  box.appendChild(el);
}

/* TTS status card in the sidebar */
function renderTtsCard() {
  const box = $('#tts-card');
  if (!box) return;
  const tts = state.tts || {};
  box.innerHTML = '';
  const dot = tts.up ? 'up' : (tts.loading ? 'loading' : 'down');
  const status = tts.up
    ? `就绪 · ${(tts.voices || []).length} 个音色`
    : (tts.loading ? '启动中…（首次 1-3 分钟）' : '未启动');
  const el = document.createElement('div');
  el.className = 'sd';
  el.innerHTML = `
    <div class="glyph" style="background:linear-gradient(150deg,#8fd47a,#2f7a24)">TTS</div>
    <div class="info">
      <div class="name"><span class="dot ${dot}"></span>IndexTTS 语音</div>
      <div class="desc">${escapeHtml(status)}</div>
    </div>
    <div class="actions">
      ${tts.up
        ? '<button data-act="stop" class="danger">停止</button>'
        : '<button data-act="start">启动</button>'}
      <button data-act="log">日志</button>
      <button data-act="speak">朗读</button>
    </div>`;
  el.addEventListener('click', ev => {
    const act = ev.target.closest('[data-act]');
    if (!act) { openTts(); return; }
    const kind = act.dataset.act;
    if (kind === 'start') startTts();
    else if (kind === 'stop') stopTts();
    else if (kind === 'log') openTtsLog();
    else if (kind === 'speak') openTts();
  });
  box.appendChild(el);
}

/* ---------------------------------------------------------------- progress
   Both long jobs (SD rendering, TTS synthesis) report a real percentage. This
   renders a slim bar plus a label into whatever element is passed in. */
function progressBar(el, job, fallbackText) {
  if (!el) return;
  const st = (job && job.state) || 'idle';
  if (st !== 'running') {
    if (el._timers) { clearInterval(el._timers); el._timers = null; }
    return;
  }
  const pct = Math.max(0, Math.min(100, Math.round((job.progress || 0) * 100)));
  const secs = job.started ? Math.round(Date.now() / 1000 - job.started) : 0;
  el.innerHTML = `
    <div class="pbar"><i style="width:${pct}%"></i></div>
    <div class="pbar-foot">
      <span>${escapeHtml(job.label || fallbackText || '处理中')}</span>
      <span>${pct}%${secs > 2 ? ` · ${secs}s` : ''}</span>
    </div>`;
}

/* Poll /api/progress while a job runs, updating one target element.
   The first poll usually lands before the hub has started the job, so an idle
   state is tolerated for a grace period instead of ending the watch at once. */
function trackJob(kind, el, fallbackText, onDone) {
  let stopped = false;
  const t0 = Date.now();
  const tick = async () => {
    if (stopped) return;
    try {
      const r = await fetch('/api/progress', { cache: 'no-store' });
      const data = await r.json();
      const job = data[kind] || {};
      if (job.state === 'running') {
        progressBar(el, job, fallbackText);
        setTimeout(tick, 500);
        return;
      }
      // not running yet (or already over): keep waiting briefly for it to start
      if (Date.now() - t0 < 8000) {
        setTimeout(tick, 400);
        return;
      }
      stopped = true;
      if (el) el.innerHTML = '';
      if (onDone) onDone(job);
    } catch {
      setTimeout(tick, 1000);
    }
  };
  tick();
  return () => {
    stopped = true;
    if (el) el.innerHTML = '';
  };
}

/* Summary line inside the collapsed group header: which service is up, so the
   sidebar stays informative without expanding. */
function renderServicesSummary() {
  const el = $('#services-summary');
  if (!el) return;
  const bits = [];
  const up = (state.models || []).filter(m => m.up);
  up.forEach(m => bits.push(`<span class="on">${escapeHtml(m.short)}</span>`));
  if (state.sd && state.sd.up) bits.push('<span class="on">SD</span>');
  else if (state.sd && state.sd.loading) bits.push('SD 启动中');
  if (state.tts && state.tts.up) bits.push('<span class="on">TTS</span>');
  else if (state.tts && state.tts.loading) bits.push('TTS 启动中');
  if (!bits.length) {
    el.textContent = '未启动';
    return;
  }
  // a single service can run at a time; name it plainly when only one is up
  el.innerHTML = bits.join(' · ');
}

/* Expand/collapse the model+services group, remembering the choice. */
function setServicesOpen(open) {
  const head = $('#toggle-services');
  const body = $('#services-body');
  if (!head || !body) return;
  head.setAttribute('aria-expanded', String(open));
  body.hidden = !open;
  state.servicesOpen = open;
  try { localStorage.setItem('strata-services-open', open ? '1' : '0'); } catch { /* ignore */ }
}

function renderConvList() {
  const box = $('#conv-list');
  box.innerHTML = '';
  if (!state.chats.length) {
    box.innerHTML = '<div class="hint" style="padding:4px 8px 10px">还没有会话</div>';
    return;
  }
  state.chats.forEach(c => {
    // Label by what the conversation actually contains (generated pictures or
    // speech), so the sidebar never claims "9B" for a drawing or speech thread.
    const m = isSpeakThread(c) ? TTS_PSEUDO
      : (isDrawThread(c) ? SD_PSEUDO : modelOf(c.model));
    const row = document.createElement('div');
    row.className = 'conv';
    row.setAttribute('aria-current', String(state.chat && c.id === state.chat.id));
    row.innerHTML = `<span class="tag" style="--mcolor:${m.color}">${escapeHtml(m.short)}</span>
                     <span class="title">${escapeHtml(c.title || '新对话')}</span>
                     <button class="del" title="删除">×</button>`;
    row.onclick = ev => {
      if (ev.target.closest('.del')) {
        ev.stopPropagation();
        deleteChat(c.id);
        return;
      }
      openChat(c.id);
    };
    box.appendChild(row);
  });
}

function renderChips() {
  const m = state.chat ? modelOf(state.chat.model) : null;
  const sdOn = sdReady();
  // A conversation the app drew pictures in stays a drawing thread even after
  // SD is shut down, so the header says so instead of claiming "9B".
  const picChat = isDrawThread(state.chat);
  const ttsOn = ttsReady();
  const speakThread = isSpeakThread(state.chat);
  const drawThread = sdOn || picChat;
  $('#chip-model').textContent = ttsOn
    ? '语音合成'
    : (sdOn ? 'SD 绘图'
            : (speakThread ? '语音 · 已停'
                           : (picChat ? 'SD 绘图 · 已停'
                                      : (m ? (m.port ? `${m.short} · :${m.port}` : m.short) : '—'))));
  const spokenCount = (state.chat ? state.chat.messages : [])
    .filter(x => x.role === 'assistant' && x.model === 'tts').length;
  $('#chip-ctx').textContent = ttsOn
    ? `${(state.tts.voices || []).length} 个音色`
    : (sdOn
        ? ((state.sd && state.sd.model || '').split(' [')[0].replace(/\.(safetensors|ckpt)$/i, '').slice(0, 26))
        : (speakThread ? `${spokenCount} 段语音`
          : (picChat
              ? `${(state.chat.messages || [])
                    .filter(x => x.role === 'assistant' && x.model === 'sd')
                    .reduce((n, x) => n + (x.images || []).length, 0)} 张生成图`
              : (m ? `ctx ${fmtCtx(m.n_ctx || m.ctx)}` : 'ctx —'))));
  const used = state.chat ? state.chat.messages.reduce((a, x) => a + (x.tokens || 0), 0) : 0;
  $('#chip-use').textContent = ttsOn
    ? '本地合成'
    : (drawThread ? (sdOn ? '离线翻译' : '绘图记录') : `${fmtNum(used)} tok`);
  document.documentElement.style.setProperty('--mcolor',
    ttsOn ? '#8fd47a' : (drawThread ? '#ffa83d' : (m ? m.color : '#ffa83d')));
  $('#hint').textContent = ttsOn ? '' : (sdOn ? '' : (speakThread
    ? '这是语音对话 · 想再合成请先启动语音服务'
    : (picChat
        ? '这是绘图对话 · 想再画请先启动 SD'
        : (m ? (m.up ? `${m.label} 就绪 · 支持图片` : `${m.label} 未启动，先点左侧“启动”`)
             : '9B 快答 · 35B 深思'))));
  const input = $('#input');
  if (input) {
    input.placeholder = ttsOn
      ? '输入要朗读的文字，回车合成'
      : (sdOn
          ? '描述画面，回车出图'
          : (picChat ? 'SD 已停止，启动后可继续画；也可直接给模型发消息'
                     : '给本地模型发消息…  （Enter 发送，Shift+Enter 换行，可粘贴或拖入图片）'));
  }
  $('#chat-title').textContent = state.chat ? (state.chat.title || '新对话') : '新对话';
  const btn = $('#send');
  btn.style.setProperty('--mcolor', m ? m.color : '#ffa83d');
  renderContinueChip();
}

const SUGGESTIONS = [
  { t: '跟我聊聊今天', d: '随便起个头，看看模型的性格和语感' },
  { t: '把这段话改得更自然', d: '贴一段文字，让 9B 重新组织措辞' },
  { t: '解释一遍这个报错', d: '贴日志或代码，35B 慢慢分析' },
  { t: '这张图里有什么？', d: '拖一张图进来，两个模型都能看图' },
];

/* Starter panel shown in a fresh conversation while SD is running. */
const DRAW_IDEAS = [
  { t: '雨夜街头', d: '霓虹灯倒影，电影感', prompt: '雨夜的东京街头，霓虹灯倒影，电影感' },
  { t: '写实人像', d: '柔和光线，皮肤质感', prompt: '写实人像，柔和自然光，皮肤质感细腻，浅景深' },
  { t: '风景大片', d: '雪山日出，体积光', prompt: '雪山日出，粉紫色天空，体积光，广角' },
  { t: '二次元', d: '动漫风格，角色立绘', prompt: '动漫风格少女立绘，赛博朋克风格，霓虹灯背景' },
];

function buildDrawHero() {
  const sd = state.sd || {};
  const model = String(sd.model || '').split(' [')[0].replace(/\.(safetensors|ckpt)$/i, '');
  const hero = document.createElement('div');
  hero.className = 'hero draw-hero';
  hero.innerHTML = `
    <div class="draw-badge">SD</div>
    <h2>画点什么</h2>
    <p>直接写画面描述，回车出图${model ? ` · 当前模型 <b>${escapeHtml(model)}</b>` : ''}</p>
    <div class="suggest draw-ideas">
      ${DRAW_IDEAS.map((s, i) => `<button data-i="${i}"><b>${escapeHtml(s.t)}</b>${escapeHtml(s.d)}</button>`).join('')}
    </div>
    <div class="draw-hero-foot">
      <button id="hero-open-draw" class="mini-btn">打开绘图面板（改模型 / 尺寸 / 步数）</button>
    </div>`;
  $$('.draw-ideas button', hero).forEach(b => {
    b.onclick = () => {
      const idea = DRAW_IDEAS[+b.dataset.i];
      $('#input').value = idea.prompt;
      autoGrow();
      $('#input').focus();
    };
  });
  const openBtn = $('#hero-open-draw', hero);
  if (openBtn) openBtn.onclick = () => openDraw();
  return hero;
}

/* Starter panel shown in a fresh conversation while speech synthesis runs. */
const SPEAK_IDEAS = [
  { t: '读一段新闻', d: '干净叙述，适合播报', prompt: '今天的天气预报：全国大部分地区晴到多云，气温回升，适合外出。' },
  { t: '讲个睡前故事', d: '语速放慢，温柔一点', prompt: '从前有一只小狐狸，它最喜欢在月光下数星星。一天晚上，它发现有一颗星星不见了……' },
  { t: '念一段旁白', d: '纪录片腔调', prompt: '在这片古老的森林里，每一棵树都记得一百年前的那场雨。' },
  { t: '测试我的音色', d: '用参考音频说话', prompt: '你好，这是用我自己的声音合成的语音，听起来怎么样？' },
];

function buildSpeakHero() {
  const tts = state.tts || {};
  const voices = tts.voices || [];
  const cur = state.settings.ttsVoice || tts.defaultVoice || (voices[0] && voices[0].name) || '';
  const hero = document.createElement('div');
  hero.className = 'hero draw-hero speak-hero';
  hero.innerHTML = `
    <div class="draw-badge" style="background:linear-gradient(150deg,#b9f0a0,#3f9a2e);color:#0d1f08">TTS</div>
    <h2>说点什么</h2>
    <p>输入文字，回车合成语音${cur ? ` · 当前音色 <b>${escapeHtml(cur.replace(/\.(wav|mp3)$/i, ''))}</b>` : ''}</p>
    <div class="suggest draw-ideas">
      ${SPEAK_IDEAS.map((s, i) => `<button data-i="${i}"><b>${escapeHtml(s.t)}</b>${escapeHtml(s.d)}</button>`).join('')}
    </div>
    <div class="draw-hero-foot">
      <button id="hero-open-tts" class="mini-btn">打开语音面板（换音色 / 保存预设）</button>
    </div>`;
  $$('.draw-ideas button', hero).forEach(b => {
    b.onclick = () => {
      const idea = SPEAK_IDEAS[+b.dataset.i];
      $('#input').value = idea.prompt;
      autoGrow();
      $('#input').focus();
    };
  });
  const openBtn = $('#hero-open-tts', hero);
  if (openBtn) openBtn.onclick = () => openTts();
  return hero;
}

function renderThread() {
  const inner = $('#thread-inner');
  inner.innerHTML = '';
  const chat = state.chat;

  if (!chat || !chat.messages.length) {
    // With SD running the chat box IS the prompt box, so a brand-new
    // conversation should open as a drawing panel, not a chat welcome.
    if (sdReady()) {
      inner.appendChild(buildDrawHero());
      return;
    }
    // Same idea for speech synthesis: typing means "read this out loud".
    if (ttsReady()) {
      inner.appendChild(buildSpeakHero());
      return;
    }
    const m = chat ? modelOf(chat.model) : null;
    const hero = document.createElement('div');
    hero.className = 'hero';
    hero.innerHTML = `
      <img src="logo.png?v=2" alt="">
      <h2>${m ? escapeHtml(m.label) : '本地模型'}</h2>
      <p>${m ? escapeHtml(m.up ? '已就绪，直接开聊。' : '还没启动，点左侧卡片上的“启动”按钮。') : ''}</p>
      <div class="suggest">
        ${SUGGESTIONS.map((s, i) => `<button data-i="${i}"><b>${escapeHtml(s.t)}</b>${escapeHtml(s.d)}</button>`).join('')}
      </div>`;
    inner.appendChild(hero);
    $$('.suggest button', hero).forEach(b => {
      b.onclick = () => {
        $('#input').value = SUGGESTIONS[+b.dataset.i].t;
        autoGrow();
        $('#input').focus();
      };
    });
    return;
  }

  chat.messages.forEach((msg, idx) => inner.appendChild(buildMessage(msg, idx)));
  scrollToBottom(true);
}

function buildMessage(msg, idx) {
  const m = modelOf(msg.model || (state.chat && state.chat.model));
  const el = document.createElement('div');
  el.className = 'msg ' + msg.role;
  el.dataset.idx = String(idx);
  el.style.setProperty('--mcolor', m.color);

  const avatar = msg.role === 'user' ? '我' : m.short;
  const who = msg.role === 'user' ? '你' : m.label;
  const time = msg.ts ? new Date(msg.ts).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : '';

  const images = (msg.images || []).length
    ? `<div class="thumbs">${msg.images.map(im =>
        `<img src="${im.dataUrl}" alt="${escapeHtml(im.name || '')}" data-full="${im.dataUrl}">`).join('')}</div>`
    : '';

  el.innerHTML = `
    <div class="avatar">${escapeHtml(avatar)}</div>
    <div class="body">
      <div class="who"><b>${escapeHtml(who)}</b><span class="time">${time}</span></div>
      ${msg.role === 'assistant' && msg.reasoning
        ? `<details class="reasoning"><summary class="hint" style="cursor:pointer;margin-bottom:6px">思考过程</summary>
             <div class="bubble md reasoning-body"></div></details>` : ''}
      <div class="bubble ${msg.role === 'user' ? '' : 'md'}">
        ${images}
        <div class="text"></div>
      </div>
      <div class="msg-foot"></div>
    </div>`;

  const textBox = $('.text', el);
  if (msg.model === 'tts' && msg.audio) {
    // a spoken reply: show the player plus the text it read out
    textBox.innerHTML = `
      <audio controls preload="none" src="${escapeHtml(msg.audio)}" style="width:100%;max-width:420px"></audio>
      <div style="font-size:12px;color:var(--text-faint);margin-top:6px">
        朗读：${escapeHtml(msg.spoken || '')}
      </div>`;
  } else if (msg.role === 'user') {
    textBox.textContent = msg.content || '';
  } else {
    textBox.innerHTML = renderMarkdown(msg.content || '');
    decorate(textBox);
    if (msg.reasoning) {
      const rb = $('.reasoning-body', el);
      rb.innerHTML = renderMarkdown(msg.reasoning);
    }
  }

  $$('.thumbs img', el).forEach(img => {
    img.onclick = () => openViewer(img.dataset.full);
  });

  const foot = $('.msg-foot', el);
  if (msg.model === 'tts' && msg.audio) {
    // spoken reply: a copy button for the text it read, nothing else
    const spoken = msg.spoken || '';
    if (spoken) {
      const copy = document.createElement('button');
      copy.textContent = '复制文字';
      copy.onclick = async () => {
        copy.textContent = (await copyText(spoken)) ? '已复制' : '失败';
        setTimeout(() => { copy.textContent = '复制文字'; }, 1400);
      };
      foot.appendChild(copy);
    }
  } else if (msg.role === 'assistant') {
    const bits = [];
    if (msg.tokens) bits.push(`${fmtNum(msg.tokens)} tok`);
    if (msg.tps) bits.push(`${msg.tps.toFixed(1)} tok/s`);
    if (msg.ms) bits.push(`${(msg.ms / 1000).toFixed(1)}s`);
    if (msg.rounds > 1) bits.push(`共 ${msg.rounds} 段`);
    if (msg.truncated) bits.push('达到上限');
    if (msg.model) bits.push(modelOf(msg.model).short);
    foot.innerHTML = `<span>${bits.join(' · ')}</span>`;
    // generated pictures get a save button, both in fresh and reloaded chats
    const pics = (msg.images || []).map(im => im.dataUrl).filter(Boolean);
    if (pics.length) {
      const save = document.createElement('button');
      save.textContent = '保存图片';
      save.onclick = async () => {
        save.disabled = true;
        save.textContent = '保存中…';
        let saved = 0;
        for (let i = 0; i < pics.length; i++) {
          const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
          const r = await fetch('/api/sd/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              name: `strata-${stamp}${pics.length > 1 ? '-' + (i + 1) : ''}.png`,
              dataUrl: pics[i],
            }),
          }).then(x => x.json()).catch(() => ({ ok: false }));
          if (r.ok) saved++;
        }
        save.textContent = saved ? `已保存 ${saved} 张` : '保存失败';
        if (saved) toast(`已保存 ${saved} 张到 outputs 目录`, 'ok');
        setTimeout(() => { save.disabled = false; save.textContent = '保存图片'; }, 2200);
      };
      foot.appendChild(save);
    }
    if (msg.content) {
      const copy = document.createElement('button');
      copy.textContent = '复制';
      copy.onclick = async () => {
        copy.textContent = (await copyText(msg.content)) ? '已复制' : '失败';
        setTimeout(() => { copy.textContent = '复制'; }, 1400);
      };
      foot.appendChild(copy);
      // speak this reply with the local TTS
      const say = document.createElement('button');
      say.textContent = '朗读';
      say.onclick = () => speakText(msg.content, say);
      foot.appendChild(say);
      const regen = document.createElement('button');
      regen.textContent = '重新生成';
      regen.onclick = () => regenerate(idx);
      foot.appendChild(regen);
    }
  } else if (msg.content) {
    // user messages get a copy button too, so both sides are copyable
    const copy = document.createElement('button');
    copy.textContent = '复制';
    copy.onclick = async () => {
      copy.textContent = (await copyText(msg.content)) ? '已复制' : '失败';
      setTimeout(() => { copy.textContent = '复制'; }, 1400);
    };
    foot.appendChild(copy);
  }
  return el;
}

function scrollToBottom(force = false) {
  const t = $('#thread');
  const near = t.scrollHeight - t.scrollTop - t.clientHeight < 140;
  if (force || near) t.scrollTop = t.scrollHeight;
}

/* -------------------------------------------------------------- chat logic */
const PREFERRED_MODEL = '9b';   // everyday default: fast to load, fast to answer

/* The model a brand-new conversation should use: 9B when it exists, else the
   first one the hub reports. */
function defaultModelId() {
  if (state.models.some(m => m.id === PREFERRED_MODEL)) return PREFERRED_MODEL;
  return (state.models[0] && state.models[0].id) || PREFERRED_MODEL;
}

function newChat(modelId) {
  // No chat model can be loaded while SD or TTS holds the GPU, so a new
  // conversation belongs to whichever service is running. Tagging it correctly
  // keeps the sidebar honest and avoids a pointless model swap later.
  const auto = ttsReady() ? 'tts' : (sdReady() ? 'sd' : defaultModelId());
  const model = modelId || auto;
  const chat = {
    id: uid(),
    title: '新对话',
    model,
    createdAt: Date.now(),
    updatedAt: Date.now(),
    messages: [],
  };
  state.chats.unshift(chat);
  state.chat = chat;
  persist();
  renderAll();
  $('#input').focus();
  return chat;
}

function openChat(id) {
  const c = state.chats.find(x => x.id === id);
  if (!c) return;
  if (state.streaming) stopStream();
  state.chat = c;
  renderAll();
}

function deleteChat(id) {
  const i = state.chats.findIndex(c => c.id === id);
  if (i < 0) return;
  state.chats.splice(i, 1);
  deletedIds.add(id);
  if (state.chat && state.chat.id === id) {
    state.chat = state.chats[0] || null;
  }
  persist();
  renderAll();
}

/* ------------------------------------------------------------------------
   Drawing from the chat box
   "画一张/画个/帮我画/生成图片/画图" at the start of a message means: give the
   rest of the sentence to Stable Diffusion instead of the language model.
   ------------------------------------------------------------------------ */
const DRAW_VERB = /^(?:请|帮我|麻烦)?\s*(?:画|绘制|生成)(?:一)?(?:张|幅|个|下)?\s*(?:图|图片|画|照片)?[\s:：,，-]*/;

function parseDrawRequest(text) {
  const body = (text || '').trim();
  if (!body) return null;
  if (!/^(?:请|帮我|麻烦)?\s*(?:画|绘制|生成图)/.test(body)) return null;
  const m = body.match(DRAW_VERB);
  const prompt = m && m[0].length < body.length ? body.slice(m[0].length).trim() : '';
  // a bare "画一张" with nothing after it is not enough to work with
  if (prompt.length < 2) return null;
  return { prompt };
}

/* Render one SD picture as an assistant message inside the conversation. */
/* Build the chat message element for an SD result once it is available.
   `regen` is an optional callback for the "再来一张" button. */
function appendSdResult(chat, prompt, result, elapsedMs, regen) {
  const msg = {
    role: 'assistant', content: '', model: 'sd', ts: Date.now(), images: [],
    ms: Math.round(elapsedMs),
  };
  const el = document.createElement('div');
  el.className = 'msg assistant';
  el.style.setProperty('--mcolor', '#ffa83d');
  el.innerHTML = `
    <div class="avatar">SD</div>
    <div class="body">
      <div class="who"><b>Stable Diffusion</b><span class="time">刚刚</span></div>
      <div class="bubble md"><div class="text"></div></div>
      <div class="msg-foot"></div>
    </div>`;

  const images = result.images || [];
  msg.images = images.map(src => ({ dataUrl: src, name: 'sd.png' }));
  const secs = (elapsedMs / 1000).toFixed(1);
  const textBox = $('.text', el);
  const usedPrompt = result.translated_prompt || result.prompt || prompt;
  textBox.innerHTML = `
    <div class="thumbs">${images.map(src =>
      `<img src="${src}" alt="" data-full="${src}">`).join('')}</div>
    <div style="font-size:12px;color:var(--text-faint);margin-top:2px">
      ${escapeHtml(prompt)}${result.seed != null ? ` · seed ${result.seed}` : ''}
      ${usedPrompt && usedPrompt !== prompt
        ? `\n<span style="opacity:.7">→ ${escapeHtml(usedPrompt.replace(/, detailed skin texture.*$/, ''))}</span>`
        : ''}
    </div>`;
  $$('.thumbs img', el).forEach(img => { img.onclick = () => openViewer(img.dataset.full); });

  const foot = $('.msg-foot', el);
  foot.innerHTML = `<span>${secs}s · ${images.length} 张</span>`;
  if (images.length) {
    const save = document.createElement('button');
    save.textContent = '保存图片';
    save.onclick = async () => {
      save.disabled = true;
      save.textContent = '保存中…';
      let saved = 0;
      for (let i = 0; i < images.length; i++) {
        const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
        const r = await fetch('/api/sd/save', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name: `strata-${stamp}${images.length > 1 ? '-' + (i + 1) : ''}.png`,
            dataUrl: images[i],
          }),
        }).then(x => x.json()).catch(() => ({ ok: false }));
        if (r.ok) saved++;
      }
      save.textContent = saved ? `已保存 ${saved} 张` : '保存失败';
      if (saved) toast(`已保存 ${saved} 张到 outputs 目录`, 'ok');
      setTimeout(() => { save.disabled = false; save.textContent = '保存图片'; }, 2200);
    };
    foot.appendChild(save);
  }
  const copy = document.createElement('button');
  copy.textContent = '复制提示词';
  copy.onclick = async () => {
    copy.textContent = (await copyText(prompt)) ? '已复制' : '失败';
    setTimeout(() => { copy.textContent = '复制提示词'; }, 1400);
  };
  foot.appendChild(copy);
  if (regen) {
    const again = document.createElement('button');
    again.textContent = '再来一张';
    again.onclick = regen;
    foot.appendChild(again);
  }

  const hero = $('.hero', $('#thread-inner'));
  if (hero) hero.remove();
  $('#thread-inner').appendChild(el);

  chat.messages.push(msg);
  chat.updatedAt = Date.now();
  persist();
  renderConvList();
  renderChips();
  scrollToBottom(true);
  return el;
}

/* Show a "could not draw" message in the chat, with a start button. */
function appendSdError(el, message) {
  const textBox = $('.text', el);
  if (textBox) {
    textBox.innerHTML = '';
    const bubble = textBox.closest('.bubble');
    if (bubble) bubble.style.display = 'none';
  }
  const host = el && el.querySelector('.body');
  if (!host) return;
  const line = document.createElement('div');
  line.className = 'statusline error';
  line.innerHTML = `<span>${escapeHtml(message)}</span>`;
  const b = document.createElement('button');
  b.textContent = '启动 SD';
  b.onclick = () => startSd();
  line.appendChild(b);
  host.appendChild(line);
}

async function drawInChat(chat, ask, originalText) {
  chat.messages.push({ role: 'user', content: originalText, images: [], ts: Date.now() });
  if (chat.title === '新对话') chat.title = originalText.slice(0, 40).replace(/\s+/g, ' ');

  // temporary "drawing…" bubble; appendSdResult will replace it
  const el = document.createElement('div');
  el.className = 'msg assistant';
  el.style.setProperty('--mcolor', '#ffa83d');
  el.innerHTML = `
    <div class="avatar">SD</div>
    <div class="body">
      <div class="who"><b>Stable Diffusion</b><span class="time">刚刚</span></div>
      <div class="bubble md"><div class="text"><span class="thinking"><i></i><i></i><i></i> 正在画…</span></div></div>
      <div class="msg-foot"></div>
    </div>`;
  const hero = $('.hero', $('#thread-inner'));
  if (hero) hero.remove();
  $('#thread-inner').appendChild(el);
  scrollToBottom(true);

  renderConvList();
  persist();

  const started = performance.now();
  // a cancel button, so a slow render does not force you to wait it out
  const foot = $('.msg-foot', el);
  const cancel = document.createElement('button');
  cancel.textContent = '取消';
  cancel.onclick = async () => {
    cancel.disabled = true;
    cancel.textContent = '正在停止…';
    await fetch('/api/sd/interrupt', { method: 'POST' }).catch(() => {});
  };
  foot.appendChild(cancel);

  // live progress bar, fed by SD's own progress endpoint
  const prog = document.createElement('div');
  prog.className = 'tts-prog';
  $('.bubble', el).appendChild(prog);
  const stopTrack = trackJob('sd', prog, '正在画');

  const r = await generateImage({
    prompt: ask.prompt,
    negative: $('#draw-neg') ? $('#draw-neg').value : '',
    size: $('#draw-size') ? $('#draw-size').value : '832x832',
    steps: $('#draw-steps') ? Number($('#draw-steps').value) || 28 : 28,
    cfg: $('#draw-cfg') ? Number($('#draw-cfg').value) || 7 : 7,
    seed: -1,
  });
  stopTrack();

  if (!r) {
    appendSdError(el, '画不出来。SD 可能没启动，点下面按钮启动。');
    persist();
    return;
  }

  // replace the placeholder bubble with the real result
  el.remove();
  appendSdResult(chat, ask.prompt, r, performance.now() - started,
    () => drawInChat(chat, ask, originalText));
}

function selectModel(id) {
  if (!state.chat) { newChat(id); return; }
  if (state.chat.model === id) return;
  if (state.chat.messages.length) {
    const c = newChat(id);           // switching mid-chat starts a fresh thread
    toast(`已为新对话切换到 ${modelOf(id).label}`, 'ok');
    void c;
    return;
  }
  state.chat.model = id;
  persist();
  renderAll();
}

function buildApiMessages(chat) {
  const msgs = [];
  if (state.settings.system.trim()) {
    msgs.push({ role: 'system', content: state.settings.system.trim() });
  }
  chat.messages.forEach(m => {
    if (m.role === 'user' && (m.images || []).length) {
      const parts = [];
      if (m.content) parts.push({ type: 'text', text: m.content });
      m.images.forEach(im => parts.push({ type: 'image_url', image_url: { url: im.dataUrl } }));
      msgs.push({ role: 'user', content: parts });
    } else if (m.content) {
      msgs.push({ role: m.role, content: m.content });
    }
  });
  return msgs;
}

async function send(textOverride) {
  const input = $('#input');
  const text = (textOverride != null ? textOverride : input.value).trim();
  const images = state.attached.slice();
  if (!text && !images.length) return;
  if (state.streaming) return;

  if (!state.chat) newChat();
  const chat = state.chat;

  // When SD is running, no chat model can be loaded alongside it (one GPU),
  // so the chat box is simply the prompt box: anything typed becomes a drawing
  // request. No "画一张" prefix needed. The local translator turns the Chinese
  // description into an English prompt on the way to SD.
  // Without SD, require the explicit drawing verb so ordinary chat is untouched.
  // With speech synthesis running there is no chat model loaded, so plain text
  // means "speak this". It goes straight to TTS and lands in the thread.
  if (!images.length && ttsReady()) {
    if (state.speaking) return;
    state.speaking = true;
    input.value = '';
    autoGrow();
    try {
      await speakInChat(chat, text);
    } finally {
      state.speaking = false;
    }
    return;
  }

  const drawAsk = parseDrawRequest(text);
  const sdMode = sdReady() && !images.length;
  if (!images.length && (sdMode || drawAsk)) {
    if (state.drawing) return;
    state.drawing = true;
    input.value = '';
    autoGrow();
    try {
      await drawInChat(chat, drawAsk || { prompt: text }, text);
    } finally {
      state.drawing = false;
    }
    return;
  }

  // Which model should answer this? A switch costs a 20-60 s reload because
  // only one fits in VRAM, so routing only ever *upgrades* 9B -> 35B. It never
  // drags you back down from 35B: that model answers everything fine, and you
  // picked it on purpose.
  const pick = state.settings.autoRoute
    ? routeMessage(text, images, state.models)
    : { model: null, reason: '' };
  const upgrading = pick.model === '35b' && chat.model !== '35b';
  if (upgrading) {
    const target = modelOf(pick.model);
    const current = modelOf(chat.model);
    if (!state.settings.routeAsk) {
      await switchForRoute(pick, current, target, chat);
    } else {
      const go = await confirmRoute(pick, current, target);
      if (go) await switchForRoute(pick, current, target, chat);
    }
    if (chat.model !== '35b' && !modelOf(chat.model).up) return;
  }

  // A conversation started while SD was running carries model "sd"; once SD is
  // gone it must fall back to a real chat model instead of reporting "not
  // started" for a model that does not exist.
  if (!modelOf(chat.model).port) chat.model = defaultModelId();

  const model = modelOf(chat.model);

  if (!model.up) {
    // Do not auto-start here: loading this model means stopping the other one,
    // which only the user can decide. Offer the switch instead.
    const other = state.models.find(m => m.id !== model.id && (m.up || m.loading));
    if (other) {
      toast(`${other.label} 正在运行，先切换模型再发消息`, 'err');
      const go = await confirmReplace(other.label, model.label);
      if (go) await startModel(model.id, true);
    } else {
      toast(`${model.label} 还没启动，点左侧卡片上的“启动”`, 'err');
    }
    return;
  }

  chat.messages.push({
    role: 'user',
    content: text,
    images: images.map(im => ({ name: im.name, dataUrl: im.dataUrl })),
    ts: Date.now(),
  });
  if (chat.title === '新对话') {
    chat.title = (text || '图片').slice(0, 40).replace(/\s+/g, ' ');
  }
  input.value = '';
  state.attached = [];
  autoGrow();
  renderAttach();
  chat.updatedAt = Date.now();
  persist();
  renderConvList();
  renderThread();
  scrollToBottom(true);

  await runReply(chat);
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

/* ------------------------------------------------------------------------
   Message routing
   9B is the default and answers everything it reasonably can. Only a strong
   signal upgrades to 35B, because the upgrade costs a 20-60 s model reload.
   The bias is deliberately one-sided: stay on 9B unless 35B is clearly needed.
   ------------------------------------------------------------------------ */
const ROUTE_LONG_CHARS = 2000;   // really long input, not just a paragraph
const ROUTE_IMAGE_CHARS = 400;   // 9B reads images fine for simple asks

function routeMessage(text, images, models) {
  const body = (text || '').trim();
  const chars = body.length;
  const hasImages = !!(images && images.length);
  const available = new Set((models || []).map(m => m.id));

  const wantDeep = [];

  // Images: 9B can see them, so an image alone is not a reason to reload.
  // Only a big image task (long question alongside it) justifies 35B.
  if (hasImages && chars >= ROUTE_IMAGE_CHARS) {
    wantDeep.push(`图片加 ${chars} 字说明，35B 看得更细`);
  }
  if (chars >= ROUTE_LONG_CHARS) wantDeep.push(`长文本 ${chars} 字，35B 更稳`);
  if (/```|(^|\n)\s*(def|class|function|import|#include|public\s+static)\b/.test(body)) {
    wantDeep.push('包含代码，35B 更靠谱');
  }
  // A heavy reasoning ask, measured by both signal words and real length.
  const heavyWords = /分析|推理|证明|推导|比较|权衡|架构|重构|优化方案|详细说明|总结全文/;
  if (heavyWords.test(body) && chars >= 600) {
    wantDeep.push('需要深入分析的长问题');
  }

  // No deep signal: 9B takes it. It is the default, so this is the common path.
  let target = '9b';
  let reason = '';
  if (wantDeep.length) { target = '35b'; reason = wantDeep[0]; }

  if (target && !available.has(target)) target = null;
  return { model: target, reason, chars, hasImages };
}

/* Swap models to follow a routing decision. Frees the old one first: both do
   not fit in VRAM at the same time. */
async function switchForRoute(pick, current, target, chat) {
  toast(`${pick.reason}：切换到 ${target.label}…`);
  await startModel(target.id, true);
  const ok = await waitUntilUp(target.id, 120);
  if (!ok) {
    toast(`${target.label} 没有起来，先看日志`, 'err');
    return;
  }
  // keep the conversation on one thread; only the backend model changes
  chat.model = target.id;
  chat.updatedAt = Date.now();
  persist();
  renderAll();
}

/* Poll /api/state until the model answers, or give up. */
async function waitUntilUp(id, seconds) {
  const tries = Math.max(1, Math.round(seconds / 2));
  for (let i = 0; i < tries; i++) {
    await sleep(2000);
    await pollState(true);
    const m = state.models.find(x => x.id === id);
    if (m && m.up) return true;
    if (m && !m.up && !m.loading && i > 3) return false;   // died while loading
  }
  return false;
}

function confirmRoute(pick, current, target) {
  return new Promise(resolve => {
    $('#route-reason').textContent = pick.reason;
    $('#route-from').textContent = current.label;
    $('#route-to').textContent = target.label;
    const modal = $('#route-modal');
    const done = value => {
      modal.classList.remove('open');
      resolve(value);
    };
    $('#route-ok').onclick = () => done(true);
    $('#route-cancel').onclick = () => done(false);
    $('#route-cancel2').onclick = () => done(false);
    modal._resolve = done;
    modal.classList.add('open');
  });
}

/**
 * Drives one assistant answer, including automatic continuation.
 *
 * llama.cpp stops at max_tokens and reports finish_reason "length"; when that
 * happens we append the partial answer as an assistant turn plus a "继续" user
 * turn and keep generating, so a long answer is not cut in half. Every round
 * writes into the same bubble and the same message object.
 */
async function runReply(chat) {
  const model = modelOf(chat.model);
  const inner = $('#thread-inner');

  const msg = {
    role: 'assistant', content: '', reasoning: '', model: model.id,
    ts: Date.now(), rounds: 0,
  };
  const el = document.createElement('div');
  el.className = 'msg assistant';
  el.style.setProperty('--mcolor', model.color);
  el.innerHTML = `
    <div class="avatar">${escapeHtml(model.short)}</div>
    <div class="body">
      <div class="who"><b>${escapeHtml(model.label)}</b><span class="time">刚刚</span></div>
      <div class="bubble md"><div class="text"><span class="thinking"><i></i><i></i><i></i></span></div></div>
      <div class="msg-foot"></div>
    </div>`;
  const hero = $('.hero', inner);
  if (hero) hero.remove();
  inner.appendChild(el);
  scrollToBottom(true);

  const maxRounds = state.settings.autoContinue ? Math.max(0, state.settings.continueRounds) : 0;
  let truncated = false;
  let aborted = false;
  let stalled = false;

  for (let round = 0; ; round++) {
    const before = msg.content.length;
    const res = await streamRound(chat, msg, el, {
      continuation: round > 0,
      round,
    });
    if (res.aborted) { aborted = true; break; }
    if (!res.ok) break;

    msg.rounds = round + 1;
    truncated = res.truncated;

    if (!truncated) break;
    if (round >= maxRounds) break;
    // a continuation round that adds nothing means the model is stuck; stop
    // instead of burning rounds (and context) on empty replies
    if (round > 0 && msg.content.length === before) {
      stalled = true;
      break;
    }

    showContinueBadge(el, round + 1, true);
    scrollToBottom();
    await sleep(160);
  }

  const box = $('.thinking', el);
  if (box) box.remove();
  showContinueBadge(el, msg.rounds, false);

  if (msg.content) {
    chat.messages.push(msg);
    chat.updatedAt = Date.now();
    msg.truncated = truncated && !aborted && !stalled;
    // keep the live element addressable by index so 继续写 / 重新生成 can find it
    el.dataset.idx = String(chat.messages.length - 1);
    renderFoot($('.msg-foot', el), msg);
    persist();
    renderChips();
  } else if (!msg.reasoning) {
    el.remove();
  }

  state.streaming = null;
  updateSendButton();
  renderChips();
  scrollToBottom();
}

/* the little "自动续写中 · 第 N 段" marker inside the bubble */
function showContinueBadge(el, round, active) {
  const body = el.querySelector('.body');
  let badge = body.querySelector('.continue-badge');
  if (!round || (round < 2 && !active)) {
    if (badge && !active) badge.remove();
    if (!active) return;
  }
  if (!badge) {
    badge = document.createElement('div');
    badge.className = 'continue-badge';
    body.insertBefore(badge, body.querySelector('.msg-foot'));
  }
  if (active) {
    badge.innerHTML = `<span class="dot-spin"></span>自动续写中…（第 ${round + 1} 段）`;
  } else {
    badge.innerHTML = `已自动续写 ${round - 1} 次，共 ${round} 段`;
    clearTimeout(badge._timer);
    badge._timer = setTimeout(() => badge.remove(), 6000);
  }
}

/**
 * One request/response round. Appends into `msg`/`el` so a continuation looks
 * like the same answer. Returns { ok, truncated, aborted }.
 */
async function streamRound(chat, msg, el, opts) {
  const model = modelOf(chat.model);
  const textBox = $('.text', el);
  const foot = $('.msg-foot', el);
  const controller = new AbortController();
  state.streaming = { controller, chat, msg, el };
  updateSendButton();

  if (opts.round === 0) {
    textBox.innerHTML = '<span class="thinking"><i></i><i></i><i></i></span>';
  }

  const messages = buildApiMessages({ ...chat, messages: chat.messages.slice() });
  if (opts.continuation && msg.content) {
    // llama.cpp can continue an unfinished assistant turn natively: it feeds
    // the partial text as the final message and asks the model to keep going
    // without repeating it. Much cleaner than a fake "继续" user turn.
    messages.push({ role: 'assistant', content: msg.content });
  }

  const body = {
    model: model.id,
    messages,
    stream: state.settings.stream,
    temperature: state.settings.temperature,
    top_p: state.settings.topP,
    max_tokens: state.settings.maxTokens,
    cache_prompt: true,
  };
  if (opts.continuation && msg.content) {
    // keep the partial answer as the prompt tail instead of starting a new turn
    body.continue_final_message = true;
    body.add_generation_prompt = false;
  }
  if (!state.settings.reasoning) body.reasoning_effort = 'none';

  const started = performance.now();
  let pending = msg.content;          // continues where the last round stopped
  let raf = 0;
  let truncated = false;

  const paint = () => {
    raf = 0;
    textBox.innerHTML = renderMarkdown(pending || '');
    decorate(textBox);
  };
  const schedulePaint = () => {
    if (!raf) raf = requestAnimationFrame(paint);
  };

  const showError = (message, canStart) => {
    const line = document.createElement('div');
    line.className = 'statusline error';
    line.innerHTML = `<span>${escapeHtml(message)}</span>`;
    if (canStart) {
      const b = document.createElement('button');
      b.textContent = '启动 ' + model.short;
      b.onclick = () => startModel(model.id);
      line.appendChild(b);
    }
    el.querySelector('.body').appendChild(line);
    if (!msg.content) {
      textBox.innerHTML = '';
      textBox.closest('.bubble').style.display = 'none';
    }
  };

  try {
    const resp = await fetch('/v1/chat/completions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Accept': 'text/event-stream' },
      body: JSON.stringify(body),
      signal: controller.signal,
    });

    if (!resp.ok) {
      let detail = `HTTP ${resp.status}`;
      try {
        const j = await resp.json();
        detail = (j.error && (j.error.message || j.error)) || detail;
      } catch { /* keep the status code */ }
      showError(detail, resp.status === 503);
      return { ok: false };
    }

    if (!state.settings.stream) {
      const data = await resp.json();
      const choice = (data.choices || [])[0] || {};
      let text = (choice.message && choice.message.content) || '';
      const think = (choice.message && choice.message.reasoning_content) || '';
      text = stripEcho(text, msg.content || '');
      pending += text;
      msg.reasoning += think;
      msg.content = pending;
      paint();
      accumulateTimings(msg, data, started);
      truncated = choice.finish_reason === 'length';
      return { ok: true, truncated };
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buf = '';
    let usage = null;
    let roundTokens = 0;
    let roundTimings = null;
    let roundText = '';
    const base = msg.content || '';

    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf('\n')) >= 0) {
        const raw = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!raw || raw.startsWith(':')) continue;
        const line = raw.startsWith('data:') ? raw.slice(5).trim() : raw;
        if (line === '[DONE]') continue;
        let chunk;
        try { chunk = JSON.parse(line); } catch { continue; }

        if (chunk.error) {
          showError(chunk.error.message || String(chunk.error), false);
          return { ok: false };
        }
        if (chunk.usage) usage = chunk.usage;
        if (chunk.timings) { msg.timings = chunk.timings; roundTimings = chunk.timings; }

        const choice = (chunk.choices || [])[0];
        if (!choice) continue;
        if (choice.finish_reason === 'length') truncated = true;
        const delta = choice.delta || {};
        if (delta.reasoning_content) {
          msg.reasoning += delta.reasoning_content;
        }
        if (typeof delta.content === 'string' && delta.content) {
          roundTokens++;
          roundText += delta.content;
          pending = base + stripEcho(roundText, base);
          schedulePaint();
        }
      }
      scrollToBottom();
    }

    if (raf) { cancelAnimationFrame(raf); raf = 0; }
    msg.content = base + stripEcho(roundText, base);
    pending = msg.content;
    paint();
    accumulateTimings(msg, { usage, timings: roundTimings }, started, roundTokens);

    if (msg.reasoning) renderReasoning(el, msg);

    // Some servers end the stream without finish_reason even when the budget
    // ran out; treat a round that used the whole budget as truncated too.
    if (!truncated && usage && usage.completion_tokens >= body.max_tokens) {
      truncated = true;
    }
    return { ok: true, truncated };
  } catch (err) {
    if (err.name === 'AbortError') {
      msg.content = pending;
      if (pending) paint();
      return { ok: false, aborted: true };
    }
    showError('请求失败：' + (err.message || err), false);
    return { ok: false };
  }
}

function renderReasoning(el, msg) {
  const body = el.querySelector('.body');
  let det = body.querySelector('.reasoning');
  const html = renderMarkdown(msg.reasoning);
  if (!det) {
    det = document.createElement('details');
    det.className = 'reasoning';
    det.innerHTML = `<summary class="hint" style="cursor:pointer;margin-bottom:6px">思考过程</summary>
                     <div class="bubble md reasoning-body"></div>`;
    body.insertBefore(det, body.querySelector('.bubble'));
  }
  $('.reasoning-body', det).innerHTML = html;
}

/**
 * Strip the repeated tail some models produce when asked to continue.
 *
 * With continue_final_message llama.cpp feeds the partial answer as the prompt
 * tail; the model may then re-emit its last sentence (or the whole thing) before
 * moving on. Only an exact prefix of the echo is removed, so genuine content is
 * never lost.
 */
function stripEcho(text, base) {
  if (!base) return text;
  if (text.startsWith(base)) return text.slice(base.length);
  // find the longest suffix of `base` that `text` starts with (>= 12 chars)
  const max = Math.min(base.length, text.length);
  for (let n = max; n >= 12; n--) {
    if (base.endsWith(text.slice(0, n))) return text.slice(n);
  }
  return text;
}

/* Totals across all rounds of one answer (auto-continue makes several). */
function accumulateTimings(msg, payload, started, roundTokens = 0) {
  const ms = performance.now() - started;
  msg.rounds = msg.rounds || 1;
  const usage = payload.usage || null;
  const t = payload.timings || null;

  let roundCount = 0;
  if (usage && usage.completion_tokens) {
    roundCount = usage.completion_tokens;
  } else if (t && t.predicted_n) {
    roundCount = t.predicted_n;
  } else {
    roundCount = roundTokens;
  }

  // The usage/timings of a continuation round describe that round only,
  // so add them up instead of overwriting the values of round 0.
  msg.tokens = (msg.tokens || 0) + roundCount;
  msg.ms = (msg.ms || 0) + Math.round(ms);
  if (t && t.predicted_per_second) {
    msg.tps = t.predicted_per_second;   // last round's speed is what you see
  } else if (roundCount && ms > 0) {
    msg.tps = roundCount / (ms / 1000);
  }
  if (t && t.prompt_n) msg.promptTokens = (msg.promptTokens || 0) + t.prompt_n;
}

function renderFoot(foot, msg) {
  const bits = [];
  if (msg.tokens) bits.push(`${fmtNum(msg.tokens)} tok`);
  if (msg.tps) bits.push(`${msg.tps.toFixed(1)} tok/s`);
  if (msg.ms) bits.push(`${(msg.ms / 1000).toFixed(1)}s`);
  if (msg.rounds > 1) bits.push(`共 ${msg.rounds} 段`);
  if (msg.truncated) bits.push('达到上限');
  foot.innerHTML = `<span>${bits.join(' · ')}</span>`;
  if (msg.content) {
    const copy = document.createElement('button');
    copy.textContent = '复制';
    copy.onclick = async () => {
      copy.textContent = (await copyText(msg.content)) ? '已复制' : '失败';
      setTimeout(() => { copy.textContent = '复制'; }, 1400);
    };
    foot.appendChild(copy);
  }
  // A stopped-at-the-limit answer gets an explicit "continue" action for the
  // cases auto-continue does not cover (switched off, or rounds exhausted).
  if (msg.truncated) {
    const more = document.createElement('button');
    more.textContent = '继续写';
    more.className = 'more';
    more.onclick = () => continueAnswer(msg);
    foot.appendChild(more);
  }
  const regen = document.createElement('button');
  regen.textContent = '重新生成';
  const idx = state.chat ? state.chat.messages.indexOf(msg) : -1;
  regen.onclick = () => regenerate(idx);
  foot.appendChild(regen);
}

/* Manually extend a finished answer, reusing the same bubble. */
async function continueAnswer(msg) {
  const chat = state.chat;
  if (!chat || state.streaming || !msg.content) return;
  const idx = chat.messages.indexOf(msg);
  if (idx < 0) return;
  const el = $(`.msg.assistant[data-idx="${idx}"]`);
  if (!el) return;

  msg.truncated = false;
  const res = await streamRound(chat, msg, el, { continuation: true, round: msg.rounds || 1 });
  msg.rounds = (msg.rounds || 1) + 1;
  msg.truncated = !!res.truncated;
  el.dataset.idx = String(idx);
  renderFoot($('.msg-foot', el), msg);
  persist();
  renderChips();
  state.streaming = null;
  updateSendButton();
}

function regenerate(idx) {
  const chat = state.chat;
  if (!chat || state.streaming) return;
  if (idx < 0) idx = chat.messages.length - 1;
  // drop everything from the assistant message onward, then re-send the prompt
  if (chat.messages[idx] && chat.messages[idx].role === 'assistant') {
    chat.messages.splice(idx, 1);
  }
  const lastUser = [...chat.messages].reverse().find(m => m.role === 'user');
  if (!lastUser) return;
  const keep = chat.messages.indexOf(lastUser);
  const prompt = lastUser.content;
  const images = (lastUser.images || []).slice();
  chat.messages = chat.messages.slice(0, keep);
  state.attached = images;
  renderThread();
  persist();
  send(prompt);
}

function stopStream() {
  if (state.streaming) {
    state.streaming.controller.abort();
  }
}

function updateSendButton() {
  const btn = $('#send');
  if (state.streaming) {
    btn.outerHTML = `<button class="stop" id="send">■ 停止</button>`;
    $('#send').onclick = stopStream;
  } else {
    const el = $('#send');
    if (!el.classList.contains('send')) {
      el.outerHTML = `<button class="send" id="send">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M4 12h15"/><path d="m13 6 6 6-6 6"/></svg>发送</button>`;
      $('#send').onclick = () => send();
    }
  }
  renderChips();
}

/* ------------------------------------------------------------- attachments */
const MAX_IMAGE_EDGE = 1280;

function fileToAttachment(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error('读取失败'));
    reader.onload = () => {
      const img = new Image();
      img.onerror = () => reject(new Error('不是有效图片'));
      img.onload = () => {
        const scale = Math.min(1, MAX_IMAGE_EDGE / Math.max(img.width, img.height));
        const w = Math.max(1, Math.round(img.width * scale));
        const h = Math.max(1, Math.round(img.height * scale));
        const canvas = document.createElement('canvas');
        canvas.width = w;
        canvas.height = h;
        const ctx = canvas.getContext('2d');
        ctx.imageSmoothingQuality = 'high';
        ctx.drawImage(img, 0, 0, w, h);
        resolve({
          name: file.name || 'image',
          dataUrl: canvas.toDataURL('image/jpeg', 0.86),
          w, h,
        });
      };
      img.src = reader.result;
    };
    reader.readAsDataURL(file);
  });
}

async function addFiles(files) {
  const list = [...files].filter(f => /^image\/(png|jpe?g|webp)$/i.test(f.type));
  if (!list.length) return;
  for (const f of list) {
    try {
      const att = await fileToAttachment(f);
      if (att.dataUrl.length > 6_000_000) {
        toast('图片太大，跳过 ' + f.name, 'err');
        continue;
      }
      state.attached.push(att);
    } catch (e) {
      toast('无法读取 ' + f.name, 'err');
    }
  }
  renderAttach();
}

function renderAttach() {
  const row = $('#attach-row');
  row.innerHTML = '';
  state.attached.forEach((att, i) => {
    const el = document.createElement('div');
    el.className = 'thumb';
    el.innerHTML = `<img src="${att.dataUrl}" alt="" title="点击预览">
      <button title="移除">×</button>`;
    // clicking the picture previews it; only the × removes it
    $('img', el).onclick = () => openViewer(att.dataUrl);
    $('button', el).onclick = ev => {
      ev.stopPropagation();
      state.attached.splice(i, 1);
      renderAttach();
    };
    row.appendChild(el);
  });
}

/* ---------------------------------------------------------------- serving */
function renderAll() {
  applyTheme();
  renderModelList();
  renderSdCard();
  renderTtsCard();
  renderComfyCard();
  renderServicesSummary();
  renderConvList();
  renderChips();
  renderThread();
}

async function pollState(force = false) {
  try {
    const r = await fetch('/api/state', { cache: 'no-store' });
    if (!r.ok) throw new Error('state ' + r.status);
    const data = await r.json();
    const sdWas = sdReady();
    const ttsWas = ttsReady();
    state.models = data.models || [];
    state.sd = data.sd || null;
    state.tts = data.tts || null;
    state.comfy = data.comfy || null;
    const sdNow = sdReady();
    const ttsNow = ttsReady();
    const hub = data.hub || {};
    $('#hub-dot').classList.toggle('off', !state.models.length);
    $('#hub-text').textContent = `hub :${hub.port || '—'}`;
    renderVersion(hub);
    state.configProblems = hub.problems || [];
    renderConfigWarnings();
    if (!state.chat) {
      // default to 9B rather than whatever happens to be loaded
      const pick = state.models.find(m => m.id === PREFERRED_MODEL)
        || state.models.find(m => m.up)
        || state.models[0];
      if (pick) newChat(pick.id);
    }
    renderModelList();
    renderSdCard();
    renderTtsCard();
    renderComfyCard();
    renderServicesSummary();
    renderChips();
    // an empty conversation shows a different starter panel depending on which
    // service is up, so redraw whenever either flag flips
    if ((sdWas !== sdNow || ttsWas !== ttsNow)
        && state.chat && !state.chat.messages.length) {
      renderThread();
    }
  } catch {
    $('#hub-dot').classList.add('off');
    $('#hub-text').textContent = 'hub 断开';
  }
}

async function startComfy() {
  toast('正在启动 Qwen 图像（会先卸载其他模型）…');
  const r = await fetch('/api/comfy/start', { method: 'POST' })
    .then(x => x.json()).catch(e => ({ ok: false, message: String(e) }));
  toast(r.message || (r.ok ? '已启动' : '启动失败'));
  await pollState(true);
}

async function stopComfy() {
  const r = await fetch('/api/comfy/stop', { method: 'POST' })
    .then(x => x.json()).catch(e => ({ ok: false, message: String(e) }));
  toast(r.message || '已停止');
  await pollState(true);
}

async function openComfyLog() {
  const r = await fetch('/api/comfy/log');
  const t = r.ok ? (await r.json()).text : '读不到日志';
  showInfo('Qwen 图像日志', `<pre class="log-pre">${escapeHtml(t || '（空）')}</pre>`);
}

async function startModel(id, replace = false) {
  toast('正在启动 ' + modelOf(id).label + '…');
  const r = await fetch('/api/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model: id, replace }),
  }).then(x => x.json()).catch(e => ({ ok: false, message: String(e) }));

  // another model is loaded and will not fit next to this one: ask first
  if (r && r.needsReplace) {
    const go = await confirmReplace(r.running, modelOf(id).label);
    if (go) await startModel(id, true);   // await so two launches never overlap
    return;
  }

  toast(r.message || (r.ok ? '已发送启动指令' : '启动失败'), r.ok ? 'ok' : 'err');
  pollState(true);
  // watch it come up
  let tries = 0;
  const timer = setInterval(async () => {
    tries++;
    await pollState();
    const m = state.models.find(x => x.id === id);
    if ((m && m.up) || tries > 90) {
      clearInterval(timer);
      if (m && m.up) toast(modelOf(id).label + ' 已就绪', 'ok');
      else if (tries > 90) toast('超时：请查看日志', 'err');
    }
  }, 2000);
}

/* yes/no prompt shown before swapping models (only one fits in VRAM) */
function confirmReplace(running, wanted) {
  return new Promise(resolve => {
    $('#switch-running').textContent = running;
    $('#switch-wanted').textContent = wanted;
    const modal = $('#switch-modal');
    const done = value => {
      modal.classList.remove('open');
      resolve(value);
    };
    $('#switch-ok').onclick = () => done(true);
    $('#switch-cancel').onclick = () => done(false);
    $('#switch-cancel2').onclick = () => done(false);
    modal._resolve = done;
    modal.classList.add('open');
  });
}

async function stopModel(id) {
  const r = await fetch('/api/stop', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model: id }),
  }).then(x => x.json()).catch(e => ({ ok: false, message: String(e) }));
  toast(r.message || '已停止', r.ok ? 'ok' : 'err');
  setTimeout(() => pollState(true), 400);
}

/* ------------------------------------------------------------------ SD 绘图
   Stable Diffusion runs behind the hub at /api/sd/*. It needs the whole GPU,
   so starting it unloads whichever Qwen model was resident. */
async function startSd() {
  const other = state.models.find(m => m.up || m.loading);
  toast('正在启动 SD 绘图…' + (other ? `（会先卸载 ${other.label}，显卡同时只能跑一个）` : ''));
  const r = await fetch('/api/sd/start', { method: 'POST' })
    .then(x => x.json()).catch(e => ({ ok: false, message: String(e) }));
  toast(r.message || (r.ok ? 'SD 开始启动' : '启动失败'), r.ok ? 'ok' : 'err');
  pollState(true);
  let tries = 0;
  const timer = setInterval(async () => {
    tries++;
    await pollState();
    const sd = state.sd || {};
    if (sd.up || tries > 150) {
      clearInterval(timer);
      if (sd.up) toast('SD 已就绪', 'ok');
      else if (tries > 150) toast('SD 启动超时，点“日志”看看卡在哪', 'err');
    }
  }, 2000);
}

async function stopSd() {
  const r = await fetch('/api/sd/stop', { method: 'POST' })
    .then(x => x.json()).catch(e => ({ ok: false, message: String(e) }));
  toast(r.message || 'SD 已停止', r.ok ? 'ok' : 'err');
  setTimeout(() => pollState(true), 400);
}

/* ------------------------------------------------------------------ 语音合成
   IndexTTS2 runs as its own Gradio app; the hub uploads the reference voice and
   drives /gradio_api/call/gen_single on our behalf. */
async function startTts() {
  const other = state.models.find(m => m.up || m.loading);
  toast('正在启动语音合成…' + (other ? `（会先卸载 ${other.label}）` : ''));
  const r = await fetch('/api/tts/start', { method: 'POST' })
    .then(x => x.json()).catch(e => ({ ok: false, message: String(e) }));
  toast(r.message || (r.ok ? '开始启动' : '启动失败'), r.ok ? 'ok' : 'err');
  pollState(true);
  let tries = 0;
  const timer = setInterval(async () => {
    tries++;
    await pollState();
    const t = state.tts || {};
    if (t.up || tries > 200) {
      clearInterval(timer);
      if (t.up) toast('语音合成已就绪', 'ok');
      else if (tries > 200) toast('启动超时，点“日志”看看', 'err');
    }
  }, 2000);
}

async function stopTts() {
  const r = await fetch('/api/tts/stop', { method: 'POST' })
    .then(x => x.json()).catch(e => ({ ok: false, message: String(e) }));
  toast(r.message || '已停止', r.ok ? 'ok' : 'err');
  setTimeout(() => pollState(true), 400);
}

async function openTtsLog() {
  $('#log-title').textContent = '语音合成 · 日志';
  $('#log-body').textContent = '读取中…';
  $('#log-modal').classList.add('open');
  try {
    const r = await fetch('/api/tts/log');
    const data = await r.json();
    const body = $('#log-body');
    body.textContent = data.log || '（日志为空）';
    body.parentElement.scrollTop = body.parentElement.scrollHeight;
  } catch (e) {
    $('#log-body').textContent = '读取日志失败：' + e;
  }
}

function ttsReady() {
  return !!(state.tts && state.tts.up);
}

/* Store the TTS panel's text + voice, so a half-written script survives a
   close or restart. `markSaved` also stamps the time, like the SD panel does. */
function saveTtsPreset(markSaved) {
  const txt = $('#tts-text');
  if (txt) state.settings.ttsText = txt.value;
  const sel = $('#tts-voice');
  if (sel && sel.value) state.settings.ttsVoice = sel.value;
  if (markSaved) state.settings.ttsSavedAt = Date.now();
  saveSettings();
  if (markSaved) showTtsSavedAt();
}

function showTtsSavedAt() {
  const el = $('#tts-saved-at');
  if (!el) return;
  const ts = state.settings.ttsSavedAt;
  if (!ts) { el.textContent = ''; return; }
  const t = new Date(ts).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  el.textContent = `已保存 ${t}`;
}

function openTts(prefill) {
  if (prefill != null) {
    $('#tts-text').value = prefill;
  } else if (state.settings.ttsText != null) {
    $('#tts-text').value = state.settings.ttsText;
  }
  const sel = $('#tts-voice');
  const voices = (state.tts && state.tts.voices) || [];
  const fallback = (state.tts && state.tts.defaultVoice) || (voices[0] && voices[0].name) || '';
  const saved = state.settings.ttsVoice || fallback;
  sel.innerHTML = voices.length
    ? voices.map(v => `<option value="${escapeHtml(v.name)}"${v.name === saved ? ' selected' : ''}>${escapeHtml(v.name.replace(/\.(wav|mp3|flac|m4a)$/i, ''))}${v.user ? ' · 我的' : ''}</option>`).join('')
    : '<option value="">（没有参考音频，放进 data\\voices）</option>';
  // keep the chosen voice in settings so 朗读 uses it too
  if (sel.value && sel.value !== state.settings.ttsVoice) {
    state.settings.ttsVoice = sel.value;
    saveSettings();
  }
  showTtsSavedAt();
  $('#tts-status').textContent = ttsReady() ? '' : '语音合成未启动，先点侧栏的“启动”';
  $('#tts-modal').classList.add('open');
}

/* Read a reply out loud.
 *
 * Two engines, picked by settings.ttsEngine:
 *   "browser" - Web Speech API, instant, no VRAM, no service to start
 *   "local"   - IndexTTS2, cloned voice, needs the GPU service running
 * Browser is the default because reading a sentence should never force you to
 * unload the chat model you are talking to. */
async function speakText(text, el) {
  // Messages are Markdown; reading the raw source makes the voice engine
  // announce "asterisk asterisk" and "hash hash". Strip it first. The chat
  // bubble keeps its formatting - this only affects what gets spoken.
  const raw = (text || '').trim();
  const body = (window.MarkdownSpeech ? window.MarkdownSpeech.strip(raw) : raw).trim();
  if (!body) return;

  const engine = state.settings.ttsEngine || 'browser';
  const canBrowser = window.BrowserTTS && window.BrowserTTS.available;

  if (engine === 'browser' && canBrowser) {
    speakWithBrowser(body, el);
    return;
  }

  if (!ttsReady()) {
    // no local service: fall back to the browser rather than failing outright
    if (canBrowser) {
      toast('语音服务没启动，改用浏览器朗读');
      speakWithBrowser(body, el);
      return;
    }
    toast('语音合成没有在运行，先点侧栏“语音”卡片上的启动', 'err');
    return;
  }

  if (el) {
    el.disabled = true;
    el.textContent = '合成中…';
  }
  try {
    const r = await fetch('/api/tts/speak', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text: body.slice(0, 800),
        voice: state.settings.ttsVoice || (state.tts.defaultVoice || (state.tts.voices || [])[0]?.name) || '',
      }),
    }).then(x => x.json()).catch(e => ({ ok: false, message: String(e) }));
    if (!r.ok) {
      toast(r.message || '合成失败', 'err');
      return;
    }
    toast(`语音已生成（${Math.round((r.bytes || 0) / 1024)} KB）`, 'ok');
    appendSpeechMessage(state.chat, r, body);
  } finally {
    if (el) {
      el.disabled = false;
      el.textContent = '朗读';
    }
  }
}

/* Speak immediately in the page, with a 停止 toggle on the button. */
function speakWithBrowser(text, el) {
  const BT = window.BrowserTTS;
  if (BT.speaking) {
    BT.stop();
    if (el) el.textContent = '朗读';
    return;
  }
  if (el) {
    el.disabled = false;
    el.textContent = '停止';
  }
  BT.speak(text, {
    voice: state.settings.browserVoice || '',
    rate: Number(state.settings.browserRate) || 1,
  }, () => {
    if (el) el.textContent = '朗读';
  });
}

/* Put an audio player into the conversation so it is kept with the chat. */
function appendSpeechMessage(chat, result, text) {
  if (!chat) return;
  const msg = {
    role: 'assistant', content: '', model: 'tts', ts: Date.now(),
    audio: result.url, spoken: text.slice(0, 200),
  };
  chat.messages.push(msg);
  chat.updatedAt = Date.now();
  persist();
  renderThread();
  scrollToBottom(true);
}

/* Speak a line typed into the chat box, and record both the text and the
   generated audio in the conversation. */
async function speakInChat(chat, text) {
  const body = (text || '').trim();
  if (!body) return;
  chat.messages.push({ role: 'user', content: body, images: [], ts: Date.now() });
  if (chat.title === '新对话') chat.title = body.slice(0, 40).replace(/\s+/g, ' ');
  chat.updatedAt = Date.now();
  renderThread();
  renderConvList();
  persist();
  scrollToBottom(true);

  const voice = state.settings.ttsVoice || (state.tts.defaultVoice || (state.tts.voices || [])[0]?.name) || '';
  const t0 = performance.now();

  // a placeholder in the thread that carries the live progress bar
  const holder = document.createElement('div');
  holder.className = 'msg assistant';
  holder.style.setProperty('--mcolor', '#8fd47a');
  holder.innerHTML = `
    <div class="avatar">TTS</div>
    <div class="body">
      <div class="who"><b>语音合成</b><span class="time">刚刚</span></div>
      <div class="bubble" style="padding:12px 14px"><div class="tts-prog"></div></div>
    </div>`;
  $('#thread-inner').appendChild(holder);
  scrollToBottom(true);
  const stopTrack = trackJob('tts', $('.tts-prog', holder), '合成中');
  holder.dataset.placeholder = '1';

  const r = await fetch('/api/tts/speak', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text: body.slice(0, 800), voice }),
  }).then(x => x.json()).catch(e => ({ ok: false, message: String(e) }));
  stopTrack();
  holder.remove();

  if (!r.ok) {
    toast(r.message || '合成失败', 'err');
    const line = document.createElement('div');
    line.className = 'statusline error';
    line.innerHTML = `<span>${escapeHtml(r.message || '合成失败')}</span>`;
    const b = document.createElement('button');
    b.textContent = '打开语音面板';
    b.onclick = () => openTts();
    line.appendChild(b);
    $('#thread-inner').appendChild(line);
    scrollToBottom(true);
    return;
  }
  toast(`语音已生成（${Math.round((performance.now() - t0) / 1000)}s）`, 'ok');
  appendSpeechMessage(chat, r, body);
}

async function openSdLog() {
  $('#log-title').textContent = 'SD 绘图 · 启动日志';
  $('#log-body').textContent = '读取中…';
  $('#log-modal').classList.add('open');
  try {
    const r = await fetch('/api/sd/log');
    const data = await r.json();
    const body = $('#log-body');
    body.textContent = data.log || '（日志为空）';
    body.parentElement.scrollTop = body.parentElement.scrollHeight;
  } catch (e) {
    $('#log-body').textContent = '读取日志失败：' + e;
  }
}

function sdReady() {
  return !!(state.sd && state.sd.up);
}

/* Which checkpoint is currently loaded, ignoring the hash suffix SD appends */
function sdCurrentName() {
  return String((state.sd && state.sd.model) || '').split(' [')[0].trim();
}

function sameCheckpoint(a, b) {
  const norm = s => String(s || '').split(' [')[0].trim()
    .replace(/\.(safetensors|ckpt)$/i, '');
  return norm(a) === norm(b);
}

/* Render the clickable checkpoint cards inside the chat page's draw panel. */
function renderDrawCheckpoints() {
  const box = $('#draw-ckpts');
  if (!box) return;
  const ckpts = (state.sd && state.sd.checkpoints) || [];
  const cur = sdCurrentName();
  box.innerHTML = '';
  if (!ckpts.length) {
    box.innerHTML = '<div class="hint" style="padding:4px 0">'
      + (state.sd && state.sd.loading ? 'SD 正在启动，稍后会自动出现模型列表。'
                                      : 'SD 未启动，点左侧 SD 卡片上的“启动”后这里会列出模型。')
      + '</div>';
    return;
  }
  ckpts.forEach(c => {
    const name = String(c.name || '');
    const active = sameCheckpoint(name, state.sdSwitching ? state.sdSwitchTarget : cur);
    const display = c.display || name.replace(/\.(safetensors|ckpt)$/i, '');
    const note = c.note || '';
    const row = document.createElement('div');
    row.className = 'ck' + (active ? ' active' : '');
    row.innerHTML = `
      <div class="txt">
        <div class="nm" title="${escapeHtml(name)}">${escapeHtml(display)}</div>
        ${note ? `<div class="note">${escapeHtml(note)}</div>` : ''}
        <div class="st">${active ? (state.sdSwitching ? '正在加载…' : '当前使用中') : '点击切换'}</div>
      </div>
      <span class="pill">${active ? (state.sdSwitching ? '加载中' : '使用中') : '切换'}</span>`;
    row.onclick = () => switchDrawModel(name);
    box.appendChild(row);
  });
}

/* Switch the SD checkpoint from inside the chat page. */
/* Match the size / cfg controls to whichever checkpoint family is loaded. */
function applyFamilyDefaults(hit) {
  const fam = (hit && hit.family) || (state.sd && state.sd.family) || 'sd15';
  const d = (hit && hit.defaults) || (state.sd && state.sd.defaults)
    || (fam === 'sdxl'
      ? { width: 1024, height: 1024, cfg: 5 }
      : { width: 832, height: 832, cfg: 7 });

  rebuildSizeOptions(fam, d);

  const cfg = $('#draw-cfg');
  if (cfg) {
    cfg.value = d.cfg;
    $('#draw-cfg-val').textContent = Number(d.cfg).toFixed(1);
  }

  const note = $('#draw-family-note');
  if (note) {
    note.textContent = fam === 'sdxl'
      ? `SDXL · 推荐 ${d.width}×${d.height}，CFG 5 左右`
      : `SD1.5 · 推荐 ${d.width}×${d.height}，CFG 7 左右`;
    note.dataset.family = fam;
  }
  saveDrawDraft();
}

function rebuildSizeOptions(fam, defaults) {
  const sel = $('#draw-size');
  if (!sel) return;
  const cur = sel.value;
  const base = fam === 'sdxl'
    ? [['1024x1024', '方图 1024×1024（SDXL 原生）'],
       ['1152x896', '横版 1152×896'],
       ['896x1152', '竖版 896×1152'],
       ['1216x832', '宽屏 1216×832'],
       ['768x768', '小图 768×768']]
    : [['832x832', '方图 832×832'],
       ['768x1024', '竖版 768×1024'],
       ['1024x768', '横版 1024×768'],
       ['512x512', '小图 512×512'],
       ['640x896', '竖版 640×896']];
  sel.innerHTML = base.map(([v, label], i) =>
    `<option value="${v}"${i === 0 ? ' selected' : ''}>${label}</option>`).join('');
  // keep the user's choice if it still exists in the new list
  if ([...sel.options].some(o => o.value === cur)) sel.value = cur;
  else sel.value = base[0][0];
}

async function switchDrawModel(name) {
  if (state.sdSwitching || sameCheckpoint(name, sdCurrentName())) return;
  if (!sdReady()) { toast('SD 还没启动，点卡片上的“启动”', 'err'); return; }
  state.sdSwitching = true;
  state.sdSwitchTarget = name;
  const hit = ((state.sd && state.sd.checkpoints) || []).find(c => sameCheckpoint(c.name, name));
  const short = (hit && hit.display) || name.split(' [')[0].replace(/\.(safetensors|ckpt)$/i, '');
  $('#draw-status').textContent = `切换中 → ${short}（加载要 20-60 秒，别关窗口）`;
  $('#draw-go').disabled = true;
  renderDrawCheckpoints();

  const t0 = Date.now();
  try {
    const r = await fetch('/api/sd/select', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ checkpoint: name }),
    }).then(x => x.json());
    if (!r.ok) throw new Error(r.message || '切换失败');
    await pollState(true);
    const secs = r.seconds != null ? r.seconds : ((Date.now() - t0) / 1000).toFixed(1);
    // SD1.5 and SDXL want different sizes / cfg; adopting the new model's
    // recommended values avoids 512px SDXL (mushy) and 1024px SD1.5 (twin heads)
    applyFamilyDefaults(hit);
    toast(`已切换到 ${short}（${secs}s）`, 'ok');
    $('#draw-status').textContent = `已切换 · ${short}（${secs}s）`;
  } catch (e) {
    toast('切换失败：' + e.message, 'err');
    $('#draw-status').textContent = '切换失败：' + e.message;
    await pollState(true);
  } finally {
    state.sdSwitching = false;
    state.sdSwitchTarget = null;
    $('#draw-go').disabled = false;
    renderDrawCheckpoints();
  }
}

/* Put the saved drawing draft back into the panel's fields. */
function restoreDrawDraft() {
  const s = state.settings || {};
  const set = (sel, val) => { const el = $(sel); if (el != null && val != null) el.value = val; };
  set('#draw-neg', s.drawNegative);
  set('#draw-size', s.drawSize);
  set('#draw-steps', s.drawSteps);
  set('#draw-cfg', s.drawCfg);
  set('#draw-seed', s.drawSeed);
  set('#draw-prompt', s.drawPrompt);
  const cfgVal = $('#draw-cfg-val');
  if (cfgVal) cfgVal.textContent = Number(s.drawCfg || 7).toFixed(1);
}

/* Remember whatever is in the drawing fields, so it survives a close/restart. */
function saveDrawDraft(markSaved) {
  const get = (sel) => { const el = $(sel); return el ? el.value : null; };
  const prompt = get('#draw-prompt');
  if (prompt != null) state.settings.drawPrompt = prompt;
  const neg = get('#draw-neg');
  if (neg != null) state.settings.drawNegative = neg;
  const size = get('#draw-size');
  if (size != null) state.settings.drawSize = size;
  const steps = get('#draw-steps');
  if (steps != null) state.settings.drawSteps = Number(steps) || 26;
  const cfg = get('#draw-cfg');
  if (cfg != null) state.settings.drawCfg = Number(cfg) || 7;
  const seed = get('#draw-seed');
  if (seed != null) state.settings.drawSeed = Number(seed);
  if (markSaved) state.settings.drawSavedAt = Date.now();
  saveSettings();
  if (markSaved) showDrawSavedAt();
}

/* "已保存 17:42" note next to the buttons */
function showDrawSavedAt() {
  const el = $('#draw-saved-at');
  if (!el) return;
  const ts = state.settings.drawSavedAt;
  if (!ts) { el.textContent = ''; return; }
  const t = new Date(ts).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  el.textContent = `已保存 ${t}`;
}

/* The Qwen-Image panel reuses the drawing modal (same prompt, size and
   button) but talks to ComfyUI instead of the SD WebUI. A flag on the state
   decides which backend the generate button reaches. */
function openQwen(prefill) {
  state.drawBackend = 'comfy';
  const title = $('#draw-title');
  if (title) title.textContent = 'Qwen 图像';
  const modal = $('#draw-modal');
  if (!modal) return;
  const c = state.comfy || {};
  if (!c.up) {
    toast('Qwen 图像没在运行，先点侧栏的“启动”', 'err');
    return;
  }
  if (!c.ready) {
    // running but missing one of the three files Qwen-Image needs
    const missing = (c.models || []).filter(m => !m.ok).map(m => m.role);
    toast('Qwen 图像缺模型：' + (missing.join('、') || '未知'), 'err');
    return;
  }
  // Qwen-Image is native at 1024; leave the size controls showing that
  applyFamilyDefaults({ family: 'sdxl', defaults: { width: 1024, height: 1024,
                                                   steps: 8, cfg: 1 } });
  const note = $('#draw-family-note');
  if (note) note.textContent = 'Qwen-Image 2.1 Turbo · 原生 1024×1024，8 步，CFG 1';
  if (prefill != null) $('#draw-prompt').value = prefill;
  else restoreDrawDraft();
  // controlnet is an SD concept; hide it while Qwen drives
  const cn = $('#draw-cn-on');
  if (cn) {
    const block = cn.closest('.cn-block');
    if (block) block.hidden = true;
  }
  const cp = $('#draw-ckpts');
  if (cp) cp.hidden = true;
  $('#draw-status').textContent = '';
  modal.classList.add('open');
  setTimeout(() => $('#draw-prompt').focus(), 120);
}

function openDraw(prefill) {
  state.drawBackend = 'sd';
  const title = $('#draw-title');
  if (title) title.textContent = 'SD 绘图';
  const cn = $('#draw-cn-on');
  if (cn) {
    const block = cn.closest('.cn-block');
    if (block) block.hidden = false;
  }
  const cp = $('#draw-ckpts');
  if (cp) cp.hidden = false;
  renderControlnetModels();
  if (state.cnImage) showControlPreview(state.cnImage);
  // size/cfg follow whichever checkpoint is loaded
  const cur = ((state.sd && state.sd.checkpoints) || []).find(c => sameCheckpoint(c.name, sdCurrentName()));
  applyFamilyDefaults(cur || null);
  const modal = $('#draw-modal');
  if (prefill != null) {
    $('#draw-prompt').value = prefill;
  } else {
    restoreDrawDraft();
  }
  renderDrawCheckpoints();
  showDrawSavedAt();
  $('#draw-status').textContent = sdReady() ? '' : 'SD 未启动，先点侧栏的“启动”';
  modal.classList.add('open');
  if (!state.sdSwitching) setTimeout(() => $('#draw-prompt').focus(), 120);
}

/* ---------------------------------------------------------------- 语音输入
   MediaRecorder captures WebM/Opus, which the local whisper service decodes
   directly - no ffmpeg, no temp files in the browser. Recording is disabled
   while another recording is running so the button cannot get out of sync. */
const Recorder = {
  media: null,
  chunks: [],
  active: false,
  startedAt: 0,
  maxMs: 60000,          // hard stop at 60 s so a forgotten recording ends
  _timer: null,
};

async function startRecording() {
  if (Recorder.active) return;
  if (!navigator.mediaDevices || !window.MediaRecorder) {
    toast('这个浏览器不支持录音', 'err');
    return;
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    toast('拿不到麦克风权限：' + (e.message || e), 'err');
    return;
  }
  const mime = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
    ? 'audio/webm;codecs=opus'
    : '';
  let rec;
  try {
    rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
  } catch (e) {
    stream.getTracks().forEach(t => t.stop());
    toast('录音初始化失败：' + (e.message || e), 'err');
    return;
  }
  Recorder.media = rec;
  Recorder.chunks = [];
  Recorder.active = true;
  Recorder.startedAt = Date.now();

  rec.ondataavailable = ev => { if (ev.data && ev.data.size) Recorder.chunks.push(ev.data); };
  rec.onstop = async () => {
    stream.getTracks().forEach(t => t.stop());
    clearTimeout(Recorder._timer);
    Recorder.active = false;
    setMicState('idle');
    const blob = new Blob(Recorder.chunks, { type: rec.mimeType || 'audio/webm' });
    Recorder.chunks = [];
    if (blob.size < 1200) {           // basically silence / accidental tap
      toast('录音太短，没听到内容');
      return;
    }
    await transcribeBlob(blob);
  };

  rec.start();
  setMicState('recording');
  Recorder._timer = setTimeout(() => {
    if (Recorder.active) { stopRecording(); toast('录音到 60 秒上限，已停止'); }
  }, Recorder.maxMs);
}

function stopRecording() {
  if (!Recorder.active || !Recorder.media) return;
  try { Recorder.media.stop(); } catch { /* already stopped */ }
}

function setMicState(mode) {
  const btn = $('#mic');
  if (!btn) return;
  btn.classList.toggle('rec', mode === 'recording');
  btn.classList.toggle('busy', mode === 'busy');
  btn.title = mode === 'recording' ? '点击停止录音'
    : mode === 'busy' ? '识别中…'
    : '点击开始录音（再点一次结束）';
}

async function transcribeBlob(blob) {
  setMicState('busy');
  const input = $('#input');
  const before = input.value;
  toast('识别中…');
  try {
    const r = await fetch('/api/stt', {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: blob,
    }).then(x => x.json()).catch(e => ({ ok: false, message: String(e) }));
    if (!r.ok) {
      toast(r.message || '识别失败', 'err');
      return;
    }
    const text = (r.text || '').trim();
    if (!text) { toast('没听清，再试一次'); return; }
    // append rather than replace, so dictation can continue an existing draft
    input.value = before ? `${before}${/\s$/.test(before) ? '' : ' '}${text}` : text;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    autoGrow();
    input.focus();
    toast(`已转写（${r.seconds || 0}s 音频）`, 'ok');
  } finally {
    setMicState('idle');
  }
}

/* ------------------------------------------------------------------ ControlNet
   The extension does the real work; we only carry a reference image plus a
   model choice to the backend. The image is sent as a data URL so nothing has
   to be written to disk for a one-off generation. */
function renderControlnetModels() {
  const sel = $('#draw-cn-model');
  if (!sel) return;
  const list = (state.sd && state.sd.controlnet) || [];
  const cur = state.settings.cnModel || '';
  if (!list.length) {
    sel.innerHTML = '<option value="">（没有找到 ControlNet 模型）</option>';
    sel.disabled = true;
    return;
  }
  sel.disabled = false;
  sel.innerHTML = list.map(m =>
    `<option value="${escapeHtml(m.key)}"${m.key === cur ? ' selected' : ''}>`
    + `${escapeHtml(m.label)}</option>`).join('');
  if (!cur && list[0]) state.settings.cnModel = list[0].key;
}

/* Shrink the reference image before upload: SD only needs the structure, and a
   phone photo would otherwise be several MB through the local socket. */
function readControlImage(file, maxEdge = 1024) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error('读取失败'));
    reader.onload = () => {
      const img = new Image();
      img.onerror = () => reject(new Error('不是有效图片'));
      img.onload = () => {
        const scale = Math.min(1, maxEdge / Math.max(img.width, img.height));
        const w = Math.round(img.width * scale);
        const h = Math.round(img.height * scale);
        const cv = document.createElement('canvas');
        cv.width = w; cv.height = h;
        cv.getContext('2d').drawImage(img, 0, 0, w, h);
        resolve({ dataUrl: cv.toDataURL('image/jpeg', 0.9), width: w, height: h,
                  name: file.name, bytes: file.size });
      };
      img.src = reader.result;
    };
    reader.readAsDataURL(file);
  });
}

function showControlPreview(info) {
  const box = $('#draw-cn-preview');
  if (!box) return;
  if (!info) {
    box.hidden = true;
    $('#draw-cn-drop').hidden = true;
    return;
  }
  box.hidden = false;
  $('img', box).src = info.dataUrl;
  $('.cn-meta', box).textContent =
    `${info.width}×${info.height} · ${Math.round((info.bytes || 0) / 1024)} KB`;
  $('#draw-cn-drop').hidden = false;
}

/* What the backend expects for the controlnet field, or null when disabled. */
function controlnetPayload() {
  if (!$('#draw-cn-on') || !$('#draw-cn-on').checked) return null;
  if (!state.cnImage) {
    toast('ControlNet 已启用但还没选参考图', 'err');
    return false;   // false = abort
  }
  return {
    model: $('#draw-cn-model').value,
    image: state.cnImage.dataUrl,
    weight: Number($('#draw-cn-weight').value) || 1,
    guidance_end: Number($('#draw-cn-end').value) || 1,
    module: 'none',      // the picture is already the control map
  };
}

async function generateImage(opts = {}) {
  const prompt = (opts.prompt || '').trim();
  if (!prompt) { toast('先写点提示词', 'err'); return null; }

  // Qwen-Image goes through ComfyUI; everything else through the SD WebUI.
  // They are separate services, so the request shape differs.
  if (state.drawBackend === 'comfy') {
    if (!((state.comfy || {}).ready)) {
      toast('Qwen 图像没就绪', 'err');
      return null;
    }
    const [qw, qh] = String(opts.size || '1024x1024').split('x').map(Number);
    const rq = await fetch('/api/comfy/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        prompt,
        negative_prompt: opts.negative || '',
        width: qw, height: qh,
        steps: opts.steps, cfg_scale: opts.cfg,
        seed: opts.seed,
      }),
    }).then(x => x.json()).catch(e => ({ ok: false, message: String(e) }));
    if (!rq.ok) { toast(rq.message || '生成失败', 'err'); return null; }
    return rq;
  }

  if (!sdReady()) { toast('SD 还没启动', 'err'); return null; }

  const [w, h] = String(opts.size || '832x832').split('x').map(Number);
  const r = await fetch('/api/sd/generate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      prompt,
      negative_prompt: opts.negative || '',
      width: w, height: h,
      steps: opts.steps, cfg_scale: opts.cfg,
      seed: opts.seed, batch_size: opts.batch,
      checkpoint: opts.checkpoint || undefined,
      controlnet: opts.controlnet || undefined,
    }),
  }).then(x => x.json()).catch(e => ({ ok: false, message: String(e) }));
  if (!r.ok) { toast(r.message || '生成失败', 'err'); return null; }
  return r;
}

async function openLog(id) {
  const m = modelOf(id);
  state.currentLogModel = id;
  $('#log-title').textContent = `${m.label} · 服务日志`;
  $('#log-body').textContent = '读取中…';
  $('#log-modal').classList.add('open');
  try {
    const r = await fetch('/api/log?model=' + encodeURIComponent(id));
    const data = await r.json();
    const body = $('#log-body');
    body.textContent = data.log || '（日志为空）';
    body.parentElement.scrollTop = body.parentElement.scrollHeight;
  } catch (e) {
    $('#log-body').textContent = '读取日志失败：' + e;
  }
}

function openInfo(id) {
  const t = $('#info-title');
  if (t) t.textContent = '模型信息';

  const m = modelOf(id);
  $('#info-body').innerHTML = `
    <h4 style="margin-top:0">${escapeHtml(m.label)}</h4>
    <p style="color:var(--text-dim);font-size:12.8px;margin-top:0">${escapeHtml(m.about || m.blurb)}</p>
    <dl class="kv">
      <dt>状态</dt><dd>${m.up ? '运行中' : (m.loading ? '加载中' : '未启动')}</dd>
      <dt>接口</dt><dd>http://127.0.0.1:${m.port}/v1</dd>
      <dt>上下文</dt><dd>${m.n_ctx || m.ctx} tokens</dd>
      <dt>模型名</dt><dd>${escapeHtml(m.model || '—')}</dd>
      <dt>启动脚本</dt><dd>${escapeHtml(m.bat || '')}</dd>
      <dt>桌面快捷方式</dt><dd>${escapeHtml(m.shortcut || '')}</dd>
    </dl>
    <h4>怎么用</h4>
    <p style="color:var(--text-dim);font-size:12.8px">
      这个窗口就是专用聊天端：左侧选择模型，直接输入即可。模型服务由 hub 管理，
      不需要再单独双击 bat。想只换模型就点卡片上的“停止”；<b style="color:var(--text)">关掉本窗口会卸载模型、释放显存</b>，下次打开再重新加载。
    </p>`;
  $('#info-modal').classList.add('open');
}

function openViewer(src) {
  const v = $('#viewer');
  $('img', v).src = src;
  v.dataset.src = src;
  const btn = $('#viewer-save');
  if (btn) { btn.disabled = false; btn.textContent = '保存到本地'; }
  v.classList.add('open');
}

/* Download the picture currently shown in the viewer via the hub, so the file
   lands in outputs\ instead of the browser's download folder. */
async function saveViewerImage() {
  const v = $('#viewer');
  const src = v.dataset.src || $('img', v).src;
  if (!src) return;
  const btn = $('#viewer-save');
  btn.disabled = true;
  btn.textContent = '保存中…';
  try {
    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
    const r = await fetch('/api/sd/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: `strata-${stamp}.png`, dataUrl: src }),
    }).then(x => x.json());
    if (r.ok) {
      btn.textContent = '已保存';
      toast(`已保存到 ${r.path}`, 'ok');
    } else {
      btn.textContent = '保存失败';
      toast(r.message || '保存失败', 'err');
    }
  } catch (e) {
    btn.textContent = '保存失败';
    toast('保存失败：' + e, 'err');
  } finally {
    setTimeout(() => {
      if (btn) { btn.disabled = false; btn.textContent = '保存到本地'; }
    }, 2200);
  }
}

/* ---------------------------------------------------------------- persist */
let saveTimer = 0;

function persist() {
  const payload = {
    current: state.chat ? state.chat.id : null,
    chats: state.chats,
    deleted: [...deletedIds],
  };
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    const sending = [...deletedIds];
    fetch('/api/chats', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }).then(() => {
      // only forget a tombstone once the server has it
      sending.forEach(id => deletedIds.delete(id));
    }).catch(() => {
      // hub down: fall back to localStorage without images
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify({
          current: payload.current,
          chats: payload.chats.map(c => ({
            ...c,
            messages: c.messages.map(m => ({ ...m, images: [] })),
          })),
        }));
      } catch { /* quota */ }
    });
  }, 500);
}

async function loadChats() {
  try {
    const r = await fetch('/api/chats', { cache: 'no-store' });
    if (r.ok) {
      const data = await r.json();
      state.chats = (data.chats || []).sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
      state.chat = state.chats.find(c => c.id === data.current) || state.chats[0] || null;
      return;
    }
  } catch { /* fall through */ }
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const data = JSON.parse(raw);
      state.chats = data.chats || [];
      state.chat = state.chats.find(c => c.id === data.current) || state.chats[0] || null;
    }
  } catch { /* ignore */ }
}

/* ------------------------------------------------------------------- boot */
function autoGrow() {
  const ta = $('#input');
  ta.style.height = 'auto';
  ta.style.height = Math.min(220, ta.scrollHeight) + 'px';
}

function bind() {
  const input = $('#input');
  input.addEventListener('input', autoGrow);
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      send();
    }
  });
  $('#send').onclick = () => send();

  $('#new-chat').onclick = () => newChat();
  $('#refresh-models').onclick = () => { pollState(true); toast('已刷新模型状态'); };
  $('#toggle-theme').onclick = () => {
    state.theme = state.theme === 'dark' ? 'light' : 'dark';
    localStorage.setItem(THEME_KEY, state.theme);
    applyTheme();
  };

  $('#attach').onclick = () => $('#file-input').click();
  $('#file-input').onchange = e => {
    addFiles(e.target.files);
    e.target.value = '';
  };

  // paste images straight into the composer
  input.addEventListener('paste', e => {
    const items = [...(e.clipboardData ? e.clipboardData.items : [])];
    const files = items.filter(i => i.kind === 'file').map(i => i.getAsFile()).filter(Boolean);
    if (files.length) {
      e.preventDefault();
      addFiles(files);
    }
  });

  // drag & drop
  const composer = $('#composer');
  ['dragenter', 'dragover'].forEach(t => composer.addEventListener(t, e => {
    e.preventDefault();
    composer.classList.add('drag');
  }));
  ['dragleave', 'drop'].forEach(t => composer.addEventListener(t, e => {
    e.preventDefault();
    if (t === 'dragleave' && composer.contains(e.relatedTarget)) return;
    composer.classList.remove('drag');
  }));
  composer.addEventListener('drop', e => {
    if (e.dataTransfer && e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
  });

  // modals
  $$('[data-close]').forEach(b => b.onclick = () => {
    if (b.dataset.close === 'draw-modal') saveDrawDraft();
    if (b.dataset.close === 'tts-modal') saveTtsPreset();
    $(`#${b.dataset.close}`).classList.remove('open');
  });
  $('#log-refresh').onclick = () => openLog(state.currentLogModel || '35b');
  $$('.modal').forEach(m => m.addEventListener('click', e => {
    if (e.target === m) {
      if (m.id === 'draw-modal') saveDrawDraft();
      if (m.id === 'tts-modal') saveTtsPreset();
      m.classList.remove('open');
      // dismissing a prompt must not leave its promise hanging forever
      if (m._resolve) m._resolve(false);
    }
  }));
  // clicking the backdrop closes; clicking the picture or the toolbar does not
  $('#viewer').onclick = ev => {
    if (ev.target.closest('.viewer-bar')) return;
    if (ev.target.tagName === 'IMG') return;
    $('#viewer').classList.remove('open');
  };
  $('#viewer-save').onclick = saveViewerImage;

  $('#export-chat').onclick = async () => {
    const btn = $('#export-chat');
    btn.disabled = true;
    btn.textContent = '导出中…';
    try {
      const r = await fetch('/api/chats/export', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chat: state.chat ? state.chat.id : '' }),
      }).then(x => x.json());
      if (r.ok) toast(r.message || '已导出', 'ok');
      else toast(r.message || '导出失败', 'err');
    } catch (e) {
      toast('导出失败：' + e, 'err');
    } finally {
      btn.disabled = false;
      btn.textContent = '导出';
    }
  };

  $('#cleanup-images').onclick = async () => {
    const btn = $('#cleanup-images');
    btn.disabled = true;
    btn.textContent = '清理中…';
    try {
      const r = await fetch('/api/images/cleanup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ days: 30 }),
      }).then(x => x.json());
      toast(r.message || `清理了 ${r.removed || 0} 个文件`, r.ok ? 'ok' : 'err');
    } catch (e) {
      toast('清理失败：' + e, 'err');
    } finally {
      btn.disabled = false;
      btn.textContent = '清理图';
    }
  };

  document.addEventListener('keydown', e => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test((e.target || {}).tagName || '')
      || (e.target && e.target.isContentEditable);

    if (e.key === 'Escape') {
      // 有弹窗就先关弹窗；没有弹窗时，Esc 用来停止生成
      const openModals = $$('.modal.open');
      if (openModals.length) {
        openModals.forEach(m => {
          m.classList.remove('open');
          if (m._resolve) m._resolve(false);
        });
        $('#viewer').classList.remove('open');
        return;
      }
      if (state.streaming) {
        e.preventDefault();
        stopStream();
        toast('已停止生成');
        return;
      }
    }

    // 输入框里只响应"带修饰键"的组合，避免干扰正常打字
    if (typing && !(e.ctrlKey || e.metaKey)) return;

    const mod = e.ctrlKey || e.metaKey;

    // Ctrl/Cmd+Shift+N 新对话
    if (mod && e.shiftKey && e.key.toLowerCase() === 'n') {
      e.preventDefault();
      newChat();
      return;
    }

    // Ctrl/Cmd+K 打开绘图面板
    if (mod && !e.shiftKey && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      openDraw();
      return;
    }

    // Ctrl/Cmd+, 打开设置
    if (mod && e.key === ',') {
      e.preventDefault();
      openSettings();
      return;
    }

    // Ctrl/Cmd+/ 显示快捷键帮助
    if (mod && e.key === '/') {
      e.preventDefault();
      showShortcuts();
      return;
    }

    // Ctrl/Cmd+E 导出当前对话
    if (mod && !e.shiftKey && e.key.toLowerCase() === 'e') {
      e.preventDefault();
      if ($('#export-chat')) $('#export-chat').click();
      return;
    }

    // 有弹窗打开时不处理下面的单键快捷键
    if ($$('.modal.open').length) return;

    // Ctrl/Cmd+Enter 强制发送（即使焦点不在输入框）
    if (mod && e.key === 'Enter') {
      e.preventDefault();
      if ($('#input').value.trim()) $('#send').click();
      return;
    }

    // T 切主题，/ 聚焦输入框
    if (!mod && !e.altKey && !typing) {
      if (e.key.toLowerCase() === 't') {
        e.preventDefault();
        $('#toggle-theme').click();
        return;
      }
      if (e.key === '/') {
        e.preventDefault();
        $('#input').focus();
        return;
      }
    }
  });

  $('#open-log').onclick = () => openLog(state.chat ? state.chat.model : (state.models[0] || {}).id || '35b');
  $('#open-draw').onclick = () => openDraw();

  // ---- 语音输入
  const mic = $('#mic');
  if (mic) {
    mic.onclick = () => {
      if (Recorder.active) stopRecording();
      else startRecording();
    };
  }

  // ---- ControlNet 控件
  const cnOn = $('#draw-cn-on');
  if (cnOn) {
    cnOn.checked = !!state.settings.cnEnabled;
    $('#draw-cn-body').hidden = !cnOn.checked;
    cnOn.onchange = () => {
      state.settings.cnEnabled = cnOn.checked;
      saveSettings();
      $('#draw-cn-body').hidden = !cnOn.checked;
      renderControlnetModels();
      if (cnOn.checked && !state.cnImage) {
        $('#draw-cn-hint').textContent = '还没选参考图，点上面的「选参考图」。';
      }
    };
  }
  const cnChoose = $('#draw-cn-choose');
  if (cnChoose) {
    cnChoose.onclick = () => $('#draw-cn-file').click();
  }
  const cnFile = $('#draw-cn-file');
  if (cnFile) {
    cnFile.onchange = async () => {
      const f = cnFile.files && cnFile.files[0];
      if (!f) return;
      try {
        state.cnImage = await readControlImage(f);
        showControlPreview(state.cnImage);
        $('#draw-cn-hint').textContent = '已就绪，直接点生成。';
      } catch (e) {
        toast('读取参考图失败：' + e.message, 'err');
      } finally {
        cnFile.value = '';   // allow choosing the same file again
      }
    };
  }
  const cnDrop = $('#draw-cn-drop');
  if (cnDrop) {
    cnDrop.onclick = () => {
      state.cnImage = null;
      showControlPreview(null);
      $('#draw-cn-hint').textContent = '选一张图，生成的画面会沿用它的结构。';
    };
  }
  const cnModel = $('#draw-cn-model');
  if (cnModel) {
    cnModel.onchange = () => {
      state.settings.cnModel = cnModel.value;
      saveSettings();
    };
  }
  const cnWeight = $('#draw-cn-weight');
  if (cnWeight) {
    cnWeight.oninput = () => {
      $('#draw-cn-weight-val').textContent =
        (Number(cnWeight.value) || 0).toFixed(2);
    };
    cnWeight.oninput();
  }
  const cnEnd = $('#draw-cn-end');
  if (cnEnd) {
    cnEnd.oninput = () => {
      $('#draw-cn-end-val').textContent = (Number(cnEnd.value) || 1).toFixed(2);
    };
    cnEnd.oninput();
  }
  $('#open-tts').onclick = () => openTts();

  // collapse group: default closed (the summary line already says what is up),
  // and the choice is remembered across restarts
  const svcHead = $('#toggle-services');
  if (svcHead) {
    svcHead.onclick = () => setServicesOpen(svcHead.getAttribute('aria-expanded') !== 'true');
    let saved = null;
    try { saved = localStorage.getItem('strata-services-open'); } catch { /* ignore */ }
    setServicesOpen(saved === '1');
  }

  // ---- 语音合成面板
  // explicit preset save / clear, matching the drawing panel
  $('#tts-save').onclick = () => {
    saveTtsPreset(true);
    toast('预设已保存', 'ok');
  };
  $('#tts-clear').onclick = () => {
    $('#tts-text').value = '';
    state.settings.ttsText = '';
    saveSettings();
    const el = $('#tts-saved-at');
    if (el) el.textContent = '';
    $('#tts-status').textContent = '已清空';
    $('#tts-text').focus();
  };
  // auto-save while typing or switching voice, and when the panel closes
  const ttsText = $('#tts-text');
  if (ttsText) {
    const auto = () => saveTtsPreset();
    ttsText.addEventListener('input', auto);
  }
  const ttsVoiceSel = $('#tts-voice');
  if (ttsVoiceSel) {
    const autoVoice = () => saveTtsPreset();
    ttsVoiceSel.addEventListener('change', autoVoice);
  }

  $('#tts-go').onclick = async () => {
    const btn = $('#tts-go');
    const status = $('#tts-status');
    const text = $('#tts-text').value.trim();
    if (!text) { status.textContent = '先写点要读的文字'; return; }
    if (!ttsReady()) { status.textContent = '语音合成未启动，先点侧栏的“启动”'; return; }
    btn.disabled = true;
    status.textContent = '';
    const barHost = $('#tts-progress');
    const stopTrack = trackJob('tts', barHost, '合成中', () => {
      if (barHost) barHost.innerHTML = '';
    });
    try {
      const voice = $('#tts-voice').value;
      if (voice) { state.settings.ttsVoice = voice; saveSettings(); }
      const r = await fetch('/api/tts/speak', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, voice }),
      }).then(x => x.json()).catch(e => ({ ok: false, message: String(e) }));
      if (!r.ok) {
        status.textContent = r.message || '合成失败';
        return;
      }
      $('#tts-out').innerHTML = `
        <div style="margin-top:12px">
          <audio controls autoplay src="${escapeHtml(r.url)}" style="width:100%"></audio>
          <div class="hint" style="margin-top:6px">
            ${Math.round((r.bytes || 0) / 1024)} KB · 已存到 data\\speech
          </div>
        </div>`;
      status.textContent = '完成';
      if (!state.chat) newChat();
      appendSpeechMessage(state.chat, r, text);
    } finally {
      if (typeof stopTrack === 'function') stopTrack();
      if (barHost) barHost.innerHTML = '';
      btn.disabled = false;
    }
  };

  // persist the drawing draft as it is typed / changed (auto-save, no stamp)
  ['#draw-prompt', '#draw-neg', '#draw-size', '#draw-steps', '#draw-cfg', '#draw-seed']
    .forEach(sel => {
      const el = $(sel);
      if (!el) return;
      const auto = () => saveDrawDraft();
      el.addEventListener('input', auto);
      el.addEventListener('change', auto);
    });

  // explicit save: also stamps the time so you can see it took effect
  $('#draw-save').onclick = () => {
    saveDrawDraft(true);
    toast('提示词已保存', 'ok');
  };

  // clear the prompt fields (and forget the stored draft)
  $('#draw-clear').onclick = () => {
    $('#draw-prompt').value = '';
    $('#draw-neg').value = '';
    state.settings.drawPrompt = '';
    state.settings.drawNegative = '';
    saveSettings();
    const el = $('#draw-saved-at');
    if (el) el.textContent = '';
    $('#draw-status').textContent = '已清空提示词';
    $('#draw-prompt').focus();
  };

  // ---- drawing panel
  $('#draw-cfg').addEventListener('input', () => {
    $('#draw-cfg-val').textContent = Number($('#draw-cfg').value).toFixed(1);
  });
  const cancelBtn = $('#draw-cancel');
  if (cancelBtn) {
    cancelBtn.onclick = async () => {
      cancelBtn.disabled = true;
      cancelBtn.textContent = '正在停止…';
      await fetch('/api/sd/interrupt', { method: 'POST' }).catch(() => {});
      setTimeout(() => {
        cancelBtn.disabled = false;
        cancelBtn.textContent = '取消';
      }, 1500);
    };
  }
  $('#draw-go').onclick = async () => {
    const btn = $('#draw-go');
    const status = $('#draw-status');
    btn.disabled = true;
    const t0 = performance.now();
    // expose a cancel action while the render runs
    if (cancelBtn) cancelBtn.hidden = false;
    // show a live bar driven by SD's own progress endpoint
    const barHost = $('#draw-progress');
    const stopTrack = trackJob('sd', barHost, '生成中', () => {
      if (barHost) barHost.innerHTML = '';
    });
    const cn = controlnetPayload();
    if (cn === false) {
      if (typeof stopTrack === 'function') stopTrack();
      if (barHost) barHost.innerHTML = '';
      btn.disabled = false;
      return;
    }
    try {
      const [w, h] = $('#draw-size').value.split('x').map(Number);
      const r = await generateImage({
        prompt: $('#draw-prompt').value,
        negative: $('#draw-neg').value,
        size: $('#draw-size').value,
        steps: Number($('#draw-steps').value) || 28,
        cfg: Number($('#draw-cfg').value) || 7,
        seed: Number($('#draw-seed').value),
        controlnet: cn || undefined,
      });
      const out = $('#draw-out');
      if (r) {
        out.innerHTML = '';
        r.images.forEach((src, i) => {
          const fig = document.createElement('figure');
          fig.innerHTML = `<img src="${src}" alt=""><figcaption>${w}×${h} · seed ${r.seed ?? '?'}</figcaption>`;
          $('img', fig).onclick = () => openViewer(src);
          out.appendChild(fig);
        });
        // also drop the result into the chat thread, so it is kept with the
        // conversation and survives closing this panel. The typed prompt goes
        // in as a user turn first, otherwise you only see the picture and
        // cannot tell what it was generated from.
        if (!state.chat) newChat();
        const chat = state.chat;
        const promptText = $('#draw-prompt').value.trim();
        chat.messages.push({ role: 'user', content: promptText, images: [], ts: Date.now() });
        if (chat.title === '新对话') chat.title = promptText.slice(0, 40).replace(/\s+/g, ' ');
        renderThread();          // show the typed prompt in the chat right away
        renderConvList();
        persist();
        appendSdResult(chat, promptText, r, performance.now() - t0, null);

        status.textContent = `完成 · ${r.images.length} 张（已发到聊天框）`;
      } else {
        status.textContent = '生成失败，看提示';
      }
    } finally {
      if (typeof stopTrack === 'function') stopTrack();
      if (barHost) barHost.innerHTML = '';
      btn.disabled = false;
      if (cancelBtn) cancelBtn.hidden = true;
    }
  };

  // auto-continue toggle + settings
  $('#chip-continue').onclick = () => {
    state.settings.autoContinue = !state.settings.autoContinue;
    saveSettings();
    renderContinueChip();
    toast(state.settings.autoContinue
      ? `自动续写已开（最多 ${state.settings.continueRounds} 段）`
      : '自动续写已关', state.settings.autoContinue ? 'ok' : '');
  };
  $('#open-settings').onclick = openSettings;
  bindSettings();
}

function renderContinueChip() {
  const on = state.settings.autoContinue;
  const chip = $('#chip-continue');
  chip.classList.toggle('on', on);
  chip.title = on
    ? `输出被截断时自动续写，最多 ${state.settings.continueRounds} 段；点击关闭`
    : '已关闭自动续写；点击开启';
  $('#continue-label').textContent = on ? '自动续写' : '续写关闭';
}

/* Keyboard cheat sheet, opened with Ctrl+/. Kept as a plain modal because the
   list is short and does not need a route. */
const SHORTCUTS = [
  ['Ctrl / Cmd + Enter', '发送消息（焦点不在输入框时也能用）'],
  ['Enter', '发送（输入框内）'],
  ['Shift + Enter', '换行'],
  ['Esc', '关闭弹窗 / 停止生成'],
  ['Ctrl / Cmd + Shift + N', '新对话'],
  ['Ctrl / Cmd + K', '打开绘图面板'],
  ['Ctrl / Cmd + ,', '打开设置'],
  ['Ctrl / Cmd + E', '导出当前对话'],
  ['Ctrl / Cmd + /', '显示这个列表'],
  ['/', '聚焦输入框'],
  ['T', '切换深色 / 浅色'],
];

function showShortcuts() {
  const body = $('#info-body');
  if (!body) return;
  $('#info-title').textContent = '快捷键';
  body.innerHTML = `<table class="kbd-table">${
    SHORTCUTS.map(([k, d]) =>
      `<tr><td><kbd>${escapeHtml(k)}</kbd></td><td>${escapeHtml(d)}</td></tr>`
    ).join('')
  }</table>`;
  $('#info-modal').classList.add('open');
}

function openSettings() {
  $('#set-autoroute').checked = !!state.settings.autoRoute;
  $('#set-routeask').checked = !!state.settings.routeAsk;
  $('#set-autocontinue').checked = !!state.settings.autoContinue;
  $('#set-rounds').value = state.settings.continueRounds;
  $('#set-maxtokens').value = state.settings.maxTokens;
  $('#set-temp').value = state.settings.temperature;
  $('#temp-val').textContent = Number(state.settings.temperature).toFixed(2);
  $('#set-topp').value = state.settings.topP;
  $('#topp-val').textContent = Number(state.settings.topP).toFixed(2);
  $('#set-reasoning').checked = !!state.settings.reasoning;
  $('#set-system').value = state.settings.system || '';
  fillBrowserVoices();
  $('#settings-modal').classList.add('open');
}

/* Populate the system-voice picker from whatever the browser exposes. */
function fillBrowserVoices() {
  const sel = $('#set-browser-voice');
  if (!sel) return;
  const BT = window.BrowserTTS;
  const voices = BT ? BT.list() : [];
  const cur = state.settings.browserVoice || '';
  if (!voices.length) {
    sel.innerHTML = '<option value="">（系统没有可用的语音包）</option>';
    sel.disabled = true;
    return;
  }
  sel.disabled = false;
  // Prefer Simplified Chinese (zh-CN) over Traditional (zh-TW/HK), and the
  // "Natural" neural voices over the old robotic ones.
  const score = v => {
    let s = 0;
    if (/^zh[-_]?(CN|Hans|SG)/i.test(v.lang)) s += 100;
    else if (/^zh/i.test(v.lang)) s += 40;
    if (/natural/i.test(v.name)) s += 30;
    if (v.local) s += 5;
    return s;
  };
  const sorted = [...voices].sort((a, b) => score(b) - score(a)
    || a.name.localeCompare(b.name));
  sel.innerHTML = sorted.map(v =>
    `<option value="${escapeHtml(v.name)}"${v.name === cur ? ' selected' : ''}>`
    + `${escapeHtml(v.name)} · ${escapeHtml(v.lang)}${v.local ? '' : ' (在线)'}</option>`).join('');
  // only auto-pick when the user has not chosen one yet
  if (!cur && sorted[0]) {
    state.settings.browserVoice = sorted[0].name;
    saveSettings();
    sel.value = sorted[0].name;
  }
}

function bindSettings() {
  const read = () => {
    state.settings.autoRoute = $('#set-autoroute').checked;
    state.settings.routeAsk = $('#set-routeask').checked;
    state.settings.autoContinue = $('#set-autocontinue').checked;
    state.settings.continueRounds = Math.max(0, Math.min(10, parseInt($('#set-rounds').value, 10) || 0));
    state.settings.maxTokens = Math.max(128, Math.min(32768, parseInt($('#set-maxtokens').value, 10) || 3072));
    state.settings.temperature = parseFloat($('#set-temp').value);
    state.settings.topP = parseFloat($('#set-topp').value);
    state.settings.reasoning = $('#set-reasoning').checked;
    state.settings.system = $('#set-system').value;
    const eng = $('#set-tts-engine');
    if (eng) state.settings.ttsEngine = eng.value;
    const bv = $('#set-browser-voice');
    if (bv && bv.value) state.settings.browserVoice = bv.value;
    const br = $('#set-browser-rate');
    if (br) state.settings.browserRate = parseFloat(br.value) || 1;
    saveSettings();
    renderContinueChip();
  };
  ['#set-autoroute', '#set-routeask', '#set-autocontinue', '#set-rounds', '#set-maxtokens',
   '#set-temp', '#set-topp', '#set-reasoning', '#set-system',
   '#set-tts-engine', '#set-browser-voice', '#set-browser-rate']
    .forEach(sel => {
      const el = $(sel);
      if (el) el.addEventListener('input', read);
    });
  $('#set-browser-rate').addEventListener('input', () => {
    $('#rate-val').textContent = (parseFloat($('#set-browser-rate').value) || 1).toFixed(1) + '×';
  });
  $('#set-temp').addEventListener('input', () => {
    $('#temp-val').textContent = Number($('#set-temp').value).toFixed(2);
  });
  $('#set-topp').addEventListener('input', () => {
    $('#topp-val').textContent = Number($('#set-topp').value).toFixed(2);
  });
}

async function boot() {
  bind();
  applyTheme();
  renderAll();
  await loadChats();
  renderAll();
  await pollState();
  renderAll();
  setInterval(() => pollState(), 4000);
  autoGrow();
  $('#input').focus();

  // A shortcut can request a panel via #tts / #draw. Open it once state is
  // loaded so the voice or checkpoint lists are already populated.
  const want = (location.hash || '').replace('#', '').toLowerCase();
  if (want === 'tts') {
    openTts();
  } else if (want === 'draw') {
    openDraw();
  }
  if (want) history.replaceState(null, '', location.pathname);
}

/* A small, deliberate debug surface. Automated tests drive the app
   through this instead of poking at module-private state, which does not live
   on `window`. */
window.ChengXin = {
  get state() { return state; },
  controlnetPayload,
  applyFamilyDefaults,
  rebuildSizeOptions,
  renderControlnetModels,
  openDraw,
  openQwen,
  generateImage,
  openSettings,
  openTts,
  newChat,
  startRecording,
  stopRecording,
  startComfy,
  stopComfy,
  get recorder() { return Recorder; },
  version: () => (state.hubVersion || ''),
};

boot();
