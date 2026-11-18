import { mountStage } from './stage.js';
import { PageScreen } from './page.js';

// ---------------------------------------------------------------- settings
// Energy figures come from the Candil design page (Rev B/C energy model) and are estimates until Build 0 measures them.
const PACK_WH = 198, USABLE = 0.9, READY_W = 9, QUICK_WH = 0.18, CHARGE = 0.86;
const usableWh = PACK_WH * USABLE * CHARGE;
const EST_HOURS = Math.round(usableWh / READY_W);
const EST_ANSWERS = Math.round(usableWh / QUICK_WH / 50) * 50;
// The Candil brain on Modal: lastbook's engine, Whisper and Candil's voice. If it cannot be reached, the sample answers are shown.
const BRAIN = 'https://myrddinforetold--candil-brain-brain-web.us-east.modal.run';
fetch(BRAIN + '/health').catch(() => {});   // wakes the brain while the page loads

const SCENARIOS = {
  wifi: {
    world: 'net',
    audio: './audio/q-wifi.mp3',
    question: 'The Wi-Fi is out. What should I do?',
    sample: {
      headline: 'Check whether it is only your home, then save your phone battery.',
      steps: [
        'Look at the router. If its lights are off, its power may be out. Restart it once.',
        'Check mobile data on your phone, or ask a neighbour, to see if the outage is wider.',
        'Text rather than call. Texts often get through when networks are busy.',
        'Listen to a battery or wind-up radio for official news and warnings.',
        'Turn on low-power mode and keep phones charged while you can.',
      ],
      source: 'FEMA and Ready.gov guidance on staying in touch in an emergency',
    },
  },
  cold: {
    world: 'power',
    audio: './audio/q-cold.mp3',
    question: 'The lights are out and we have no power. It’s cold here. What can we do to stay warm, what should we do right now, and what are the most important things?',
    sample: {
      headline: 'Stay together in one room, and never heat it with a grill or generator.',
      steps: [
        'Gather everyone in one small room. Close the doors and cover the windows.',
        'Wear layers, a hat and dry clothes. Share blankets, and eat and drink.',
        'Never use a generator, grill, camp stove or gas oven to heat the home. The fumes can kill.',
        'Use torches instead of candles.',
        'Check on older neighbours. If the home gets too cold, go to a warming centre.',
      ],
      source: 'CDC guidance on power outages and extreme cold',
    },
  },
};

// ---------------------------------------------------------------- page and stage
const $ = id => document.getElementById(id);
const pageCanvas = document.createElement('canvas');
const page = new PageScreen(pageCanvas);
const world = { net: true, power: true, lid: true, inside: false };
let stage = null, busy = false, view = { mode: 'ready' };

const status = () => world.power ? 'Mains · Packs 86%' : `Packs 86% · about ${EST_HOURS} h`;
function drawPage(v = view) { view = v; page.draw({ ...v, status: status() }); stage?.redrawPage(); }

const wait = ms => new Promise(r => setTimeout(r, ms));
const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

// ---------------------------------------------------------------- sound: the button's click and the power dying
const sfx = (() => {
  let ctx = null;
  const ac = () => { if (!ctx) { try { ctx = new (window.AudioContext || window.webkitAudioContext)(); } catch { return null; } } if (ctx.state === 'suspended') ctx.resume(); return ctx; };
  const noise = (c, ms) => { const b = c.createBuffer(1, Math.ceil(c.sampleRate * ms / 1000), c.sampleRate), d = b.getChannelData(0); for (let i = 0; i < d.length; i++) d[i] = (Math.random() * 2 - 1) * (1 - i / d.length) ** 3; return b; };
  return {
    unlock: ac,
    click(level = 1) {           // a short, damped tactile click
      const c = ac(); if (!c) return; const t = c.currentTime;
      const src = c.createBufferSource(); src.buffer = noise(c, 18);
      const bp = c.createBiquadFilter(); bp.type = 'bandpass'; bp.frequency.value = 3200; bp.Q.value = 1.2;
      const g = c.createGain(); g.gain.value = 0.55 * level;
      src.connect(bp).connect(g).connect(c.destination); src.start(t);
      const o = c.createOscillator(), og = c.createGain(); o.frequency.setValueAtTime(190, t); o.frequency.exponentialRampToValueAtTime(90, t + 0.05);
      og.gain.setValueAtTime(0.18 * level, t); og.gain.exponentialRampToValueAtTime(0.001, t + 0.07);
      o.connect(og).connect(c.destination); o.start(t); o.stop(t + 0.08);
    },
    thunk() {                    // the room's power dying: a low thud and the hum stopping
      const c = ac(); if (!c) return; const t = c.currentTime;
      const o = c.createOscillator(), g = c.createGain(); o.type = 'sine';
      o.frequency.setValueAtTime(95, t); o.frequency.exponentialRampToValueAtTime(38, t + 0.35);
      g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.32, t + 0.012); g.gain.exponentialRampToValueAtTime(0.001, t + 0.5);
      o.connect(g).connect(c.destination); o.start(t); o.stop(t + 0.55);
      const src = c.createBufferSource(); src.buffer = noise(c, 60); const lp = c.createBiquadFilter(); lp.type = 'lowpass'; lp.frequency.value = 900;
      const ng = c.createGain(); ng.gain.value = 0.25; src.connect(lp).connect(ng).connect(c.destination); src.start(t);
    },
  };
})();

let toastTimer = 0;
function toast(text, ms = 3600) {
  const t = $('toast'); t.textContent = text; t.classList.add('on');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('on'), ms);
}

function setChip(id, on, label) {
  const c = $(id); c.classList.toggle('off', !on);
  c.querySelector('span').innerHTML = `${label} <b>${on ? 'on' : 'out'}</b>`;
}

function setNet(on) {
  world.net = on; $('b-net').setAttribute('aria-pressed', String(!on));
  setChip('chip-net', on, 'Internet');
  if (!on) toast('The internet is down. Candil never needed it: its library and its AI are inside the case.');
}

let powerSeq = 0;
async function setPower(on) {
  // the room's lights flicker and cut to black; when they come back they stutter on
  const my = ++powerSeq;
  world.power = on; $('b-power').setAttribute('aria-pressed', String(!on));
  setChip('chip-power', on, 'Power');
  const room = lv => { document.body.classList.toggle('dark', lv < 0.5); stage.setLight(lv); };
  document.body.classList.add('cut');
  const seq = on ? [[1, 60], [0, 90], [1, 0]] : [[0.35, 70], [1, 60], [0, 45], [0.7, 80], [0, 0]];
  if (!on) { stage.setFrontLight(0); $('hud').classList.remove('on'); }
  for (const [lv, ms] of seq) { if (my !== powerSeq) return; room(lv); if (!on && lv === 0 && ms === 0) sfx.thunk(); if (ms) await wait(ms); }
  document.body.classList.remove('cut');
  drawPage();
  if (on) { stage.setFrontLight(0); updateFrame(); return; }
  await wait(650); if (my !== powerSeq) return;
  stage.setFrontLight(1);                        // Candil notices the dark and lights its own page
  $('hud').classList.add('on'); updateFrame();
  toast('The power is out. The ring glows in the dark and the screen has its own warm light.');
}

function setLid(open) {
  world.lid = open; $('b-lid').setAttribute('aria-pressed', String(open));
  $('b-lid').textContent = open ? 'Lid open' : 'Lid closed';
  stage.setOpen(open);
}

function setInside(on) {
  world.inside = on; $('b-inside').setAttribute('aria-pressed', String(on));
  if (on && !world.lid) setLid(true);
  stage.setExplode(on ? 1 : 0); stage.view(on ? 'inside' : 'hero');
}

function showPanel(html) { $('panel-body').innerHTML = html; $('panel').classList.add('on'); $('panel').scrollTop = 0; updateFrame(); }
function hidePanel() { $('panel').classList.remove('on'); updateFrame(); }
$('panel-x').addEventListener('click', hidePanel);

// keep the device framed in the part of the screen the header, controls and answer panel leave clear
function updateFrame() {
  if (!stage) return;
  const W = innerWidth, H = innerHeight, phone = W <= 700;
  const dock = $('dock').getBoundingClientRect();
  document.documentElement.style.setProperty('--dock-h', Math.round(dock.height) + 'px');
  const hud = $('hud').classList.contains('on') ? $('hud').getBoundingClientRect().bottom : 0;
  const top = Math.max(document.querySelector('header').getBoundingClientRect().bottom, hud) + 4;
  let bottom = H - dock.top + 6, right = 0;
  const panel = $('panel');
  if (panel.classList.contains('on')) {
    if (phone) bottom = Math.max(bottom, H - (H - (dock.height + 8 + 16) - panel.offsetHeight) + 6);
    else right = panel.offsetWidth + 30;
  }
  stage.setFrame({ t: top, r: right, b: bottom, l: 0 });
}
addEventListener('resize', () => updateFrame());

// ---------------------------------------------------------------- asking
function primeAudio(src) {
  // called inside the click: a muted play unlocks this element so it may play aloud a few seconds later
  const a = new Audio(src); a.preload = 'auto'; a.muted = true;
  a.play().then(() => { a.pause(); a.currentTime = 0; a.muted = false; }).catch(() => { a.muted = false; });
  return a;
}

async function listen(sc, onWords, primed) {
  // plays the question out loud; the words appear as they are said
  const words = sc.question.split(' ');
  let audio = primed || null;
  try {
    if (!audio) audio = new Audio(sc.audio);
    if (!(audio.readyState >= 1)) await new Promise((res, rej) => { audio.addEventListener('loadedmetadata', res, { once: true }); audio.addEventListener('error', rej, { once: true }); setTimeout(rej, 4000); });
  } catch { audio = null; }
  const dur = audio ? audio.duration * 1000 : words.length * 380;
  if (audio) { audio.muted = false; audio.currentTime = 0; audio.play().catch(() => {}); }
  const t0 = performance.now();
  return new Promise(res => {
    const step = () => {
      const k = Math.min(1, (performance.now() - t0) / dur);
      onWords(words.slice(0, Math.max(1, Math.round(k * words.length))).join(' '));
      if (k < 1) requestAnimationFrame(step); else setTimeout(() => res(audio), 250);
    };
    step();
  });
}

async function getAnswer(sc, blob) {
  // the same recording the visitor just heard goes to the brain: it writes it out, searches the library and answers
  const t0 = performance.now();
  const body = new FormData();
  body.append('audio', blob || await (await fetch(sc.audio)).blob(), blob ? 'question.webm' : 'question.mp3');
  body.append('tier', 'quick'); body.append('voice', '0');
  const slow = setTimeout(() => toast('Waking the cloud brain. The first answer takes longer.'), 9000);
  try {
    const r = await fetch(BRAIN + '/ask', { method: 'POST', body });
    const a = await r.json();
    if (a.error || !a.page) throw new Error(a.error || 'no answer');
    return { question: a.question, ...a.page, md: a.answer_md, sources: a.sources || [], model: a.model, timing: a.timing,
             ms: performance.now() - t0, live: true };
  } catch (e) {
    console.warn('brain:', e);
    if (!sc) return { question: '', headline: 'I could not reach the Candil brain just now.', steps: ['Please try again in a moment.'], source: '—', live: false, failed: true };
    return { question: sc.question, ...sc.sample, live: false, failed: true };
  } finally { clearTimeout(slow); }
}

async function speakAnswer(a) {
  // Candil's own voice, sentence by sentence: the next part is made while this one plays
  const parts = [a.headline, ...a.steps.map((t, i) => `${i + 1}. ${t}`), ...(a.warning ? ['Warning. ' + a.warning] : [])].filter(Boolean);
  const get = text => { const f = new FormData(); f.append('text', text); return fetch(BRAIN + '/speak', { method: 'POST', body: f }).then(r => r.blob()).catch(() => null); };
  let next = get(parts[0]);
  for (let i = 0; i < parts.length; i++) {
    const blob = await next;
    next = i + 1 < parts.length ? get(parts[i + 1]) : null;
    if (!blob || !blob.size) continue;
    const au = new Audio(URL.createObjectURL(blob));
    await new Promise(res => { au.onended = res; au.onerror = res; au.play().catch(res); });
  }
}

function showAnswer(a) {
  const src = (a.sources || []).slice(0, 4).map(s => `<li>${esc(s.title || '')}${s.publisher ? ' · ' + esc(s.publisher) : ''}${s.page ? ', p. ' + s.page : ''}</li>`).join('');
  showPanel(`
    <h3>Candil heard</h3><div class="q">“${esc(a.question)}”</div>
    <h3>Answer</h3>
    ${a.live ? '' : '<div class="note">' + (a.failed ? 'The cloud brain did not answer, so this is the sample text.' : 'Sample answer.') + '</div>'}
    <div class="hl">${esc(a.headline)}</div>
    <ol>${a.steps.map(s => `<li>${esc(s)}</li>`).join('')}</ol>
    ${a.warning ? `<div class="hl" style="font-size:17px">⚠ ${esc(a.warning)}</div>` : ''}
    ${src ? `<h3>Sources in the library</h3><ol class="src">${src}</ol>` : `<div class="src">Source: ${esc(a.source)}</div>`}
    <div class="meta">${a.live ? `Real Candil software, running in the cloud for this demo · ${esc(a.model || '')}<br>Heard in ${a.timing?.heard_s ?? '–'} s · answered in ${a.timing?.answer_s ?? '–'} s` : 'Live software: not reached'}<br>Internet: ${world.net ? 'on' : 'out'} · Power: ${world.power ? 'on' : 'out'}</div>`);
}

async function ask(id) {
  if (busy || !stage) return;
  busy = true; document.querySelectorAll('button.ask').forEach(b => b.disabled = true);
  const sc = SCENARIOS[id];
  const primed = primeAudio(sc.audio); sfx.unlock();
  try {
    if (sc.world === 'net' && world.net) { setNet(false); await wait(1800); }
    if (sc.world === 'power' && world.power) { setPower(false); await wait(2200); }
    if (world.inside) { setInside(false); await wait(900); }
    if (!world.lid) { setLid(true); await wait(1300); }
    hidePanel();
    stage.view('ask', 1.3); await wait(1300);
    drawPage({ mode: 'ready' });
    await stage.press(); sfx.click();
    stage.setRing('listening');
    drawPage({ mode: 'listening', heard: '' });
    await listen(sc, heard => {
      if (heard !== view.heard) drawPage({ mode: 'listening', heard });
    }, primed);
    sfx.click(0.45); await stage.release();
    stage.setRing('thinking');
    drawPage({ mode: 'thinking', question: sc.question });
    const a = await getAnswer(sc);
    stage.setRing('answering');
    drawPage({ mode: 'answer', answer: { question: a.question || sc.question, headline: a.headline, steps: a.steps, warning: a.warning, source: a.source } });
    stage.view('read', 1.6);
    showAnswer(a);
    if (a.live) await speakAnswer(a); else await wait(6000);
    stage.setRing('ready');
  } finally {
    busy = false; document.querySelectorAll('button.ask').forEach(b => b.disabled = false);
  }
}

// ---------------------------------------------------------------- start
async function start() {
  $('hud-est').textContent = `about ${EST_HOURS} h listening, or ${EST_ANSWERS.toLocaleString()} answers · estimate`;
  await Promise.all([
    document.fonts.load('600 40px Barlow'), document.fonts.load('500 40px Barlow'), document.fonts.load('400 40px Barlow'),
    page.ready,
  ]);
  drawPage({ mode: 'ready' });
  const [{ default: b64 }, manifest] = await Promise.all([
    import('./model/revc_glb.js'), fetch('./model/revc_manifest.json').then(r => r.json()),
  ]);
  const bin = atob(b64), buf = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
  stage = await mountStage($('stage'), {
    glbBuffer: buf.buffer, manifest, pageCanvas,
    textures: { card: './textures/lid-inside.jpg', lid_label: './textures/lid-print.png' },
    articulations: [
      { match: i => i.group === 'lid', pivot: [0, 0.003, 0.091], closed: -90, explodeMul: 0.5 },
      { match: i => /^(Bail handle|Handle grip)/.test(i.name), pivot: [0, 0.206, -0.008], closed: 84 },
    ],
  });
  $('loading').remove();
  updateFrame();
  // first impression: closed, then the lid drops open
  stage.setClosedNow(); world.lid = false; setLid(false);
  setTimeout(() => setLid(true), 1100);

  $('b-net').addEventListener('click', () => setNet(!world.net));
  $('b-power').addEventListener('click', () => { sfx.unlock(); setPower(!world.power); });
  $('b-lid').addEventListener('click', () => setLid(!world.lid));
  $('b-inside').addEventListener('click', () => setInside(!world.inside));
  document.querySelectorAll('button.ask').forEach(b => b.addEventListener('click', () => ask(b.dataset.scenario)));
  setupMic();
}
// ---------------------------------------------------------------- ask your own: hold to talk
function setupMic() {
  const btn = $('b-mic');
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) { btn.title = 'This browser cannot record'; return; }
  btn.disabled = false; btn.title = 'Hold to talk, let go to ask';
  btn.innerHTML = '<span class="dot"></span><span class="long">Hold to ask your own</span><span class="short">Hold to ask</span>';
  let rec = null, chunks = [], stream = null, t0 = 0, held = false;
  const start = async e => {
    e.preventDefault(); held = true;
    if (busy || rec) return;
    busy = true; sfx.unlock();
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
    catch { busy = false; toast('Microphone access was not allowed.'); return; }
    if (!held) {                       // the permission prompt took the press: start on the next one
      stream.getTracks().forEach(t => t.stop()); busy = false;
      toast('Microphone ready. Hold the button while you talk.'); return;
    }
    if (!world.lid) { setLid(true); }
    hidePanel();
    stage.view('ask', 0.9); stage.press().then(() => sfx.click()); stage.setRing('listening');
    drawPage({ mode: 'listening', heard: '' });
    chunks = []; rec = new MediaRecorder(stream); rec.ondataavailable = ev => ev.data.size && chunks.push(ev.data); rec.start(); t0 = performance.now();
    btn.setAttribute('aria-pressed', 'true');
  };
  const stop = async () => {
    held = false;
    if (!rec) return;
    const r = rec; rec = null; btn.setAttribute('aria-pressed', 'false');
    await new Promise(res => { r.onstop = res; r.stop(); });
    stream.getTracks().forEach(t => t.stop());
    sfx.click(0.45); await stage.release();
    if (performance.now() - t0 < 700) { stage.setRing('ready'); drawPage({ mode: 'ready' }); busy = false; toast('Hold the button while you talk.'); return; }
    stage.setRing('thinking'); drawPage({ mode: 'thinking', question: '…' });
    const a = await getAnswer(null, new Blob(chunks, { type: r.mimeType || 'audio/webm' }));
    stage.setRing('answering');
    drawPage({ mode: 'answer', answer: { question: a.question || '…', headline: a.headline, steps: a.steps, warning: a.warning, source: a.source } });
    stage.view('read', 1.4); showAnswer(a);
    if (a.live) await speakAnswer(a);
    stage.setRing('ready'); busy = false;
  };
  btn.addEventListener('pointerdown', start);
  btn.addEventListener('pointerup', stop); btn.addEventListener('pointerleave', stop); btn.addEventListener('pointercancel', stop);
  btn.addEventListener('contextmenu', e => e.preventDefault());
}

window.__demo = { ask: id => ask(id), setPower: on => setPower(on), setNet: on => setNet(on), setInside: on => setInside(on), stage: () => stage };
start().catch(e => { $('loading').textContent = 'The 3D model could not load in this browser.'; console.error(e); });
