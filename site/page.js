// Candil e-paper page, drawn on a 1872 x 1404 canvas (the 7.8-inch panel at full resolution).
// Layout follows the brand answer page: status bar, "You asked", the answer in one line, numbered steps, source.
const W = 1872, H = 1404, M = 96;
const INK = '#1b1c1e', MUTED = '#5f5e5a', PAPER = '#e9e6de';
const SANS = 'Barlow, "Helvetica Neue", Arial, sans-serif';

const img = src => new Promise(res => { const i = new Image(); i.onload = () => res(i); i.onerror = () => res(null); i.src = src; });

export class PageScreen {
  constructor(canvas) {
    this.c = canvas; canvas.width = W; canvas.height = H;
    this.g = canvas.getContext('2d');
    this.ready = Promise.all([img('./brand/candil-wordmark-ink.svg'), img('./brand/candil-mark-mono-ink.svg')])
      .then(([wm, mk]) => { this.wm = wm; this.mk = mk; });
  }

  // wrap text into lines that fit width at the current font
  wrap(text, width) {
    const g = this.g, words = String(text).split(/\s+/), lines = [];
    let line = '';
    for (const w of words) {
      const t = line ? line + ' ' + w : w;
      if (g.measureText(t).width > width && line) { lines.push(line); line = w; } else line = t;
    }
    if (line) lines.push(line);
    return lines;
  }

  bar(status) {
    const g = this.g;
    g.fillStyle = PAPER; g.fillRect(0, 0, W, H);
    if (this.wm) { const h = 44, w = h * this.wm.width / this.wm.height; g.drawImage(this.wm, M, 54, w, h); }
    g.font = `500 36px ${SANS}`; g.fillStyle = MUTED; g.textBaseline = 'middle';
    g.textAlign = 'left'; g.fillText('Offline · Library 2026.11 · English', M + 250, 77);
    g.textAlign = 'right'; g.fillText(status, W - M, 77); g.textAlign = 'left';
    g.fillStyle = INK; g.fillRect(M, 128, W - 2 * M, 3);
    g.textBaseline = 'alphabetic';
  }

  foot(left, right = 'Press the ring to ask a follow-up') {
    const g = this.g;
    g.fillStyle = INK; g.fillRect(M, H - 166, W - 2 * M, 3);
    g.font = `500 34px ${SANS}`; g.fillStyle = MUTED; g.textBaseline = 'middle';
    const srcLines = this.wrap(left, W - 2 * M - 720);
    g.fillText(srcLines[0] + (srcLines.length > 1 ? '…' : ''), M, H - 90);
    g.font = `600 34px ${SANS}`; g.fillStyle = INK; g.textAlign = 'right';
    g.fillText(right, W - M, H - 90);
    const tw = g.measureText(right).width;
    if (this.mk) g.drawImage(this.mk, W - M - tw - 66, H - 113, 46, 46);
    g.textAlign = 'left'; g.textBaseline = 'alphabetic';
  }

  ready_(status) {
    const g = this.g; this.bar(status);
    if (this.mk) g.drawImage(this.mk, M, 330, 150, 150);
    g.fillStyle = INK; g.font = `600 112px ${SANS}`;
    g.fillText('Press the ring and ask.', M, 640);
    g.font = `400 52px ${SANS}`; g.fillStyle = MUTED;
    g.fillText('Hold the ring while you talk. Let go, and Candil answers', M, 760);
    g.fillText('out loud and shows the steps here.', M, 830);
    this.foot('Every answer comes from the library on this device and names its source.', 'Ready');
  }

  listening(status, heard) {
    const g = this.g; this.bar(status);
    g.font = `500 34px ${SANS}`; g.fillStyle = MUTED; g.fillText('Listening', M, 210);
    g.font = `600 108px ${SANS}`; g.fillStyle = INK;
    const lines = heard ? this.wrap('“' + heard + '”', W - 2 * M) : ['…'];
    lines.slice(0, 7).forEach((l, i) => g.fillText(l, M, 380 + i * 126));
    this.foot('Let go of the ring when you have finished.', 'Listening');
  }

  thinking(status, question) {
    const g = this.g; this.bar(status);
    this.asked(question);
    g.font = `600 108px ${SANS}`; g.fillStyle = MUTED;
    g.fillText('Looking in the library…', M, 470);
    this.foot('Candil answers only from the library and says when it cannot find something.', 'Thinking');
  }

  asked(question) {
    const g = this.g;
    g.font = `500 34px ${SANS}`; g.fillStyle = MUTED; g.fillText('You asked', M, 210);
    g.font = `600 58px ${SANS}`; g.fillStyle = INK;
    const q = this.wrap(question, W - 2 * M);
    q.slice(0, 2).forEach((l, i) => g.fillText(i === 1 && q.length > 2 ? l + '…' : l, M, 280 + i * 68));
    return Math.min(q.length, 2);
  }

  // answer: { question, headline, steps: [..], source }
  answer(status, a) {
    const g = this.g; this.bar(status);
    const qn = this.asked(a.question);
    const top = 340 + (qn - 1) * 68;
    // fit the headline and steps: shrink together until they fit above the footer
    for (let s = 1; s > 0.5; s -= 0.05) {
      g.font = `600 ${Math.round(100 * s)}px ${SANS}`;
      const hl = this.wrap(a.headline, W - 2 * M);
      g.font = `400 ${Math.round(50 * s)}px ${SANS}`;
      const steps = a.steps.map(t => this.wrap(t, W - 2 * M - 110 * s));
      const lh = Math.round(62 * s), gap = Math.round(30 * s), hh = Math.round(110 * s);
      const total = hl.length * hh + 50 * s + steps.reduce((n, l) => n + l.length * lh + gap, 0);
      if (top + total < H - 200 || s <= 0.55) {
        let y = top + Math.round(92 * s);
        g.font = `600 ${Math.round(100 * s)}px ${SANS}`; g.fillStyle = INK;
        hl.forEach(l => { g.fillText(l, M, y); y += hh; });
        y += Math.round(40 * s);
        steps.forEach((ls, i) => {
          const r = Math.round(33 * s), cx = M + r + 3, cy = y - Math.round(17 * s);
          g.lineWidth = Math.max(3, Math.round(6 * s)); g.strokeStyle = INK;
          g.beginPath(); g.arc(cx, cy, r, 0, Math.PI * 2); g.stroke();
          g.font = `600 ${Math.round(36 * s)}px ${SANS}`; g.textAlign = 'center'; g.textBaseline = 'middle';
          g.fillStyle = INK; g.fillText(String(i + 1), cx, cy + 1); g.textAlign = 'left'; g.textBaseline = 'alphabetic';
          g.font = `400 ${Math.round(50 * s)}px ${SANS}`;
          ls.forEach(l => { g.fillText(l, M + Math.round(110 * s), y); y += lh; });
          y += gap;
        });
        break;
      }
    }
    this.foot('Source: ' + a.source);
  }

  draw(view) {
    const st = view.status || 'Packs 86%';
    if (view.mode === 'listening') this.listening(st, view.heard);
    else if (view.mode === 'thinking') this.thinking(st, view.question);
    else if (view.mode === 'answer') this.answer(st, view.answer);
    else this.ready_(st);
  }
}
