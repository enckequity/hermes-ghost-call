"""The two self-contained web call pages (inline CSS and JS, no external requests).

``you``: the user's page. Mic + headphones, a live hint ticker, Speak (earpiece whisper played on
this page only, with the outgoing mic muted while it plays), Hint (an explicit ask) and Hang up.
``them``: the other party's page. Just the call: no hints, no captions, nothing from Ghost.

Wire protocol (JSON over one websocket per page): the page sends ``start`` (media token), then
``media`` (base64 G.711 mu-law, 8 kHz, 20 ms frames), ``speak`` / ``hint`` (you only) and
``stop``; the bridge sends ``media``, ``peer``, ``hint`` / ``caption`` / ``whisper`` /
``notice`` (you only) and ``ended``.
"""
from __future__ import annotations

from html import escape

CODEC_JS = r"""
const CODEC = (function () {
  function lin2ulaw(s) {
    const BIAS = 0x84, CLIP = 32635;
    const sign = (s >> 8) & 0x80;
    if (sign) s = -s;
    if (s > CLIP) s = CLIP;
    s += BIAS;
    let exp = 7;
    for (let mask = 0x4000; (s & mask) === 0 && exp > 0; exp--, mask >>= 1) {}
    const mant = (s >> (exp + 3)) & 0x0f;
    return (~(sign | (exp << 4) | mant)) & 0xff;
  }
  function ulaw2lin(u) {
    u = ~u & 0xff;
    const sign = u & 0x80, exp = (u >> 4) & 7, mant = u & 15;
    const s = (((mant << 3) + 0x84) << exp) - 0x84;
    return sign ? -s : s;
  }
  const ULAW = new Float32Array(256);
  for (let i = 0; i < 256; i++) ULAW[i] = ulaw2lin(i) / 32768;
  function Downsampler(inRate) {
    const ratio = inRate / 8000;
    let phase = 0, acc = 0;
    this.push = function (f32) {
      const out = [];
      for (let i = 0; i < f32.length; i++) {
        const x = f32[i];
        let rem = 1;
        while (rem > 1e-9) {
          const need = ratio - phase;
          if (rem >= need) { acc += x * need; out.push(acc / ratio); acc = 0; phase = 0; rem -= need; }
          else { acc += x * rem; phase += rem; rem = 0; }
        }
      }
      return out;
    };
  }
  function Framer(onFrame) {
    let buf = new Uint8Array(160), n = 0;
    this.push = function (samples) {
      for (let i = 0; i < samples.length; i++) {
        let v = Math.round(samples[i] * 32767);
        if (v > 32767) v = 32767; else if (v < -32768) v = -32768;
        buf[n++] = lin2ulaw(v);
        if (n === 160) { onFrame(buf); buf = new Uint8Array(160); n = 0; }
      }
    };
  }
  function decode(bytes) {
    const out = new Float32Array(bytes.length);
    for (let i = 0; i < bytes.length; i++) out[i] = ULAW[bytes[i]];
    return out;
  }
  function pcm16(bytes) {
    const n = bytes.length >> 1, out = new Float32Array(n), dv = new DataView(bytes.buffer, bytes.byteOffset, n * 2);
    for (let i = 0; i < n; i++) out[i] = dv.getInt16(i * 2, true) / 32768;
    return out;
  }
  function b64ToBytes(b64) {
    const bin = atob(b64), u = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);
    return u;
  }
  function bytesToB64(u8) {
    let s = '';
    for (let i = 0; i < u8.length; i++) s += String.fromCharCode(u8[i]);
    return btoa(s);
  }
  return { lin2ulaw, ulaw2lin, Downsampler, Framer, decode, pcm16, b64ToBytes, bytesToB64 };
})();
"""

APP_JS = r"""
(function () {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const body = document.body;
  const role = body.getAttribute('data-role');
  const sid = body.getAttribute('data-sid');
  const room = body.getAttribute('data-room') === '1';  // speakerphone: no other page
  const token = new URLSearchParams(location.search).get('t') || '';
  let ac = null, ws = null, stream = null, proc = null, src = null;
  let playHead = 0, sources = [], over = false, wake = null, muteUntil = 0, whispering = 0;
  const SILENT_FRAME = new Uint8Array(160).fill(0xff);

  function status(t) { $('status').textContent = t; }
  function send(o) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); }
  function show(id, on) { const e = $(id); if (e) e.hidden = !on; }

  function playFloat(f32, rate, onEnd) {
    if (!ac || !f32.length) { if (onEnd) onEnd(); return; }
    const buf = ac.createBuffer(1, f32.length, rate);
    buf.getChannelData(0).set(f32);
    const s = ac.createBufferSource();
    s.buffer = buf; s.connect(ac.destination);
    const now = ac.currentTime;
    if (playHead < now + 0.03) playHead = now + 0.06;
    s.start(playHead); playHead += buf.duration;
    sources.push(s);
    s.onended = () => { const i = sources.indexOf(s); if (i >= 0) sources.splice(i, 1); if (onEnd) onEnd(); };
  }
  function playUlaw(b64) { playFloat(CODEC.decode(CODEC.b64ToBytes(b64)), 8000); }

  // Earpiece whisper: local playback only; the outgoing mic sends silence while it plays.
  function whisper(m) {
    whispering += 1;
    const done = () => { whispering = Math.max(0, whispering - 1); muteUntil = performance.now() + 150; };
    if (m.pcm24 && ac) {
      const f = CODEC.pcm16(CODEC.b64ToBytes(m.pcm24));
      const buf = ac.createBuffer(1, f.length, 24000);
      buf.getChannelData(0).set(f);
      const s = ac.createBufferSource();
      s.buffer = buf; s.connect(ac.destination); s.onended = done; s.start();
    } else if ('speechSynthesis' in window) {
      const u = new SpeechSynthesisUtterance(m.text || '');
      u.rate = 1.15; u.volume = 0.8; u.onend = done; u.onerror = done;
      speechSynthesis.speak(u);
    } else { done(); }
  }

  function startMic() {
    src = ac.createMediaStreamSource(stream);
    proc = ac.createScriptProcessor(2048, 1, 1);
    const down = new CODEC.Downsampler(ac.sampleRate);
    const framer = new CODEC.Framer((bytes) => {
      const muted = whispering > 0 || performance.now() < muteUntil || $('mute').checked;
      send({ event: 'media', payload: CODEC.bytesToB64(muted ? SILENT_FRAME : bytes) });
    });
    proc.onaudioprocess = (e) => {
      if (!ws || ws.readyState !== 1) return;
      framer.push(down.push(e.inputBuffer.getChannelData(0)));
    };
    const zero = ac.createGain(); zero.gain.value = 0;
    src.connect(proc); proc.connect(zero); zero.connect(ac.destination);
  }

  function addHint(m) {
    const list = $('hints');
    if (!list) return;
    $('latest').textContent = m.text;
    $('latest').setAttribute('data-n', m.n);
    const li = document.createElement('li');
    li.textContent = m.line;
    li.setAttribute('data-n', m.n);
    li.title = 'Tap to hear it in your earpiece';
    li.addEventListener('click', () => send({ event: 'speak', n: m.n }));
    list.insertBefore(li, list.firstChild);
    while (list.children.length > 40) list.removeChild(list.lastChild);
  }
  function caption(m) {
    const c = $('caption');
    if (!c) return;
    c.textContent = (m.role === 'call' ? '' : m.role === 'them' ? 'them: ' : 'you: ') + m.text;
    c.className = 'caption' + (m.final ? '' : ' partial');
  }
  function notice(t) {
    const n = $('notice');
    if (!n) return;
    n.textContent = t; n.hidden = false;
    clearTimeout(notice.t); notice.t = setTimeout(() => { n.hidden = true; }, 6000);
  }

  function finish(msg) {
    if (over) return;
    over = true;
    try { if (ws && ws.readyState <= 1) ws.close(); } catch (e) {}
    try { if (stream) stream.getTracks().forEach((t) => t.stop()); } catch (e) {}
    try { if (proc) proc.disconnect(); if (src) src.disconnect(); } catch (e) {}
    try { if (wake) wake.release(); } catch (e) {}
    sources.forEach((s) => { try { s.stop(); } catch (e) {} });
    setTimeout(() => { try { if (ac) ac.close(); } catch (e) {} }, 300);
    status(msg);
    ['join', 'hang', 'controls'].forEach((id) => show(id, false));
    $('dot').className = 'dot off';
  }

  function openSocket(info) {
    ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host
      + '/g/' + encodeURIComponent(sid) + '/ws/' + role);
    ws.onopen = () => {
      send({ event: 'start', token: info.media_token });
      startMic();
      if (room) { status('Listening to the call on speaker.'); $('dot').className = 'dot on'; }
      else {
        status(role === 'you' ? 'Connected. Waiting for the other side…' : 'Connected. Waiting for the caller…');
        $('dot').className = 'dot wait';
      }
      show('hang', true); show('controls', role === 'you');
    };
    ws.onmessage = (ev) => {
      let m;
      try { m = JSON.parse(ev.data); } catch (e) { return; }
      if (m.event === 'media' && m.payload) playUlaw(m.payload);
      else if (m.event === 'peer') { status(m.joined ? 'On the call.' : 'The other side left.'); $('dot').className = 'dot on'; }
      else if (m.event === 'hint') addHint(m);
      else if (m.event === 'caption') caption(m);
      else if (m.event === 'whisper') whisper(m);
      else if (m.event === 'notice') notice(m.text);
      else if (m.event === 'ended') finish('Call ended.');
    };
    ws.onerror = () => finish('Connection problem. The call ended.');
    ws.onclose = () => finish('Call ended.');
  }

  async function join() {
    $('join').disabled = true;
    status('Allow the microphone when asked…');
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      ac = new AC();
      const resumed = ac.resume();
      const unlock = ac.createBufferSource();
      unlock.buffer = ac.createBuffer(1, 1, 22050); unlock.connect(ac.destination); unlock.start(0);
      // Room mode hears a phone loudspeaker: noise suppression would eat its voice.
      stream = await navigator.mediaDevices.getUserMedia({ audio: room
        ? { echoCancellation: false, noiseSuppression: false, autoGainControl: true }
        : { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
      await resumed;
      try { if (navigator.wakeLock) wake = await navigator.wakeLock.request('screen'); } catch (e) {}
      status('Connecting…');
      const r = await fetch('/g/' + encodeURIComponent(sid) + '/join/' + role, {
        method: 'POST', cache: 'no-store', credentials: 'omit',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ t: token }) });
      if (!r.ok) throw new Error('link');
      const info = await r.json();
      try { history.replaceState(null, '', location.pathname); } catch (e) {}
      show('join', false);
      openSocket(info);
    } catch (e) {
      const denied = e && (e.name === 'NotAllowedError' || e.name === 'NotFoundError');
      finish(denied ? 'Microphone blocked. Allow it in your browser settings and ask for a new link.'
                    : 'This link is no longer valid. Ask for a new one.');
    }
  }
  $('join').addEventListener('click', join);
  $('hang').addEventListener('click', () => { send({ event: 'stop' }); finish('You hung up.'); });
  if ($('speak')) $('speak').addEventListener('click', () => send({ event: 'speak' }));
  if ($('hint')) $('hint').addEventListener('click', () => send({ event: 'hint' }));
  window.addEventListener('pagehide', () => { if (!over) send({ event: 'stop' }); });
})();
"""

CSS = """
:root { --bg:#0e0f12; --card:#17191e; --line:#262931; --text:#eceef3; --mute:#8a8f9c;
  --accent:#9ab4ff; --ok:#4ccf8a; --warn:#f2b84b; --bad:#f06b6b; }
@media (prefers-color-scheme: light) { :root { --bg:#f6f6f8; --card:#fff; --line:#e3e4e8;
  --text:#14161a; --mute:#646a77; --accent:#3557d6; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); min-height:100vh;
  font:16px/1.45 -apple-system, BlinkMacSystemFont, "SF Pro Text", "Inter", sans-serif;
  display:flex; justify-content:center; }
main { width:100%; max-width:520px; padding:28px 16px 40px; }
h1 { font-size:20px; font-weight:600; margin:0 0 4px; letter-spacing:-.01em; }
.sub { color:var(--mute); margin:0 0 20px; font-size:14px; }
.badge { display:flex; align-items:center; gap:8px; font-size:14px; color:var(--mute);
  margin:0 0 16px; }
.dot { width:9px; height:9px; border-radius:50%; background:var(--mute); flex:none; }
.dot.wait { background:var(--warn); } .dot.on { background:var(--ok); } .dot.off { background:var(--bad); }
.card { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:16px;
  margin:0 0 14px; }
.warn { font-size:14px; color:var(--mute); }
button { font:inherit; border:0; border-radius:12px; padding:14px 18px; cursor:pointer;
  background:var(--line); color:var(--text); }
button.primary { background:var(--accent); color:#0b0d12; font-weight:600; width:100%; }
button.end { background:transparent; color:var(--bad); border:1px solid var(--line); width:100%; }
button:disabled { opacity:.5; }
.row { display:flex; gap:10px; margin:0 0 14px; } .row button { flex:1; }
#latest { font-size:20px; line-height:1.35; min-height:2.7em; margin:0; }
#latest:empty::before { content:"Hints appear here as the other side talks."; color:var(--mute);
  font-size:16px; }
ul#hints { list-style:none; margin:0; padding:0; font-size:14px; color:var(--mute); }
ul#hints li { padding:8px 0; border-top:1px solid var(--line); cursor:pointer; }
.sub.tight { margin:0 0 6px; }
.caption { font-size:13px; color:var(--mute); min-height:1.4em; margin:8px 0 0; }
.caption.partial { opacity:.7; }
#notice { font-size:14px; color:var(--warn); margin:0 0 12px; }
#status { font-size:14px; color:var(--mute); margin:12px 0; }
label.mute { display:flex; gap:8px; align-items:center; font-size:14px; color:var(--mute);
  margin:0 0 14px; }
"""


def _page(*, title: str, body_attrs: str, content: str, nonce: str) -> str:
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<meta name=\"referrer\" content=\"no-referrer\">"
            f"<title>{escape(title)}</title><style nonce=\"{nonce}\">{CSS}</style></head>"
            f"<body {body_attrs}><main>{content}</main>"
            f"<script nonce=\"{nonce}\">{CODEC_JS}{APP_JS}</script></body></html>")


def render_you(*, sid: str, goal: str, callee_label: str, nonce: str, tap_mode: bool = False,
               room: bool = False) -> str:
    other = escape(callee_label or "the other side")
    hint_note = ("Tap Hint whenever you want one." if tap_mode
                 else "Hints stream in as they talk. Tap one to hear it.")
    if room:
        return _page(title="Ghost Call", body_attrs=_attrs(sid, "you") + ' data-room="1"',
                     content=_room_content(goal, hint_note), nonce=nonce)
    content = f"""
<h1>Ghost Call</h1>
<p class="sub">Goal: {escape(goal)}</p>
<div class="badge"><span id="dot" class="dot"></span>
  <span>Ghost is listening (muted to the other party)</span></div>
<div class="card warn">Use headphones. Whispers play on this page only, and your mic sends
silence while one plays, so {other} never hears Ghost.</div>
<button id="join" class="primary">Join call</button>
<p id="status">Tap Join when you are ready.</p>
<p id="notice" hidden></p>
<div id="controls" hidden>
  <div class="card"><p id="latest"></p><p id="caption" class="caption"></p></div>
  <div class="row"><button id="speak">Speak hint</button><button id="hint">Hint</button></div>
  <label class="mute"><input type="checkbox" id="mute"> Mute my mic</label>
  <div class="card"><p class="sub tight">{escape(hint_note)}</p>
    <ul id="hints"></ul></div>
</div>
<button id="hang" class="end" hidden>Hang up</button>
"""
    return _page(title="Ghost Call", body_attrs=_attrs(sid, "you"), content=content,
                 nonce=nonce)


def _room_content(goal: str, hint_note: str) -> str:
    """Speakerphone mode: the call runs on the phone; this page only listens and shows hints."""
    return f"""
<h1>Ghost Call</h1>
<p class="sub">Goal: {escape(goal)}</p>
<div class="badge"><span id="dot" class="dot"></span>
  <span>Ghost listens through this mic. It never joins the call.</span></div>
<div class="card warn">Put your phone call on speaker next to this device. Hints show here.
If you tap Speak hint, wear headphones on this device, or the phone will hear the whisper.</div>
<button id="join" class="primary">Start listening</button>
<p id="status">Tap Start listening when the call begins.</p>
<p id="notice" hidden></p>
<div id="controls" hidden>
  <div class="card"><p id="latest"></p><p id="caption" class="caption"></p></div>
  <div class="row"><button id="speak">Speak hint</button><button id="hint">Hint</button></div>
  <label class="mute"><input type="checkbox" id="mute"> Pause listening</label>
  <div class="card"><p class="sub tight">{escape(hint_note)}</p>
    <ul id="hints"></ul></div>
</div>
<button id="hang" class="end" hidden>End</button>
"""


def render_them(*, sid: str, caller: str, nonce: str) -> str:
    content = f"""
<h1>Incoming call</h1>
<p class="sub">From {escape(caller or "a caller")}</p>
<div class="badge"><span id="dot" class="dot"></span><span>Browser call</span></div>
<button id="join" class="primary">Answer</button>
<p id="status">Tap Answer to talk. Headphones help avoid echo.</p>
<label class="mute" hidden><input type="checkbox" id="mute"></label>
<div id="controls" hidden></div>
<button id="hang" class="end" hidden>Hang up</button>
"""
    return _page(title="Call", body_attrs=_attrs(sid, "them"), content=content, nonce=nonce)


def _attrs(sid: str, role: str) -> str:
    return f'data-sid="{escape(sid)}" data-role="{escape(role)}"'


def render_error() -> str:
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<title>Link unavailable</title><style>body{font:16px -apple-system,"
            "BlinkMacSystemFont,'SF Pro Text','Inter',sans-serif;background:#0e0f12;color:#eceef3;"
            "display:flex;justify-content:center;padding:48px 16px}</style></head><body>"
            "<p>This call link is not available. Links work once and expire.</p></body></html>")

