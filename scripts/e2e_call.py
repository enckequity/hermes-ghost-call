"""End-to-end voice test with two bot callers: no humans, no microphone, real OpenAI.

    python scripts/e2e_call.py            # the ISP haggling call
    python scripts/e2e_call.py --fresh    # re-synthesize the voices
    python scripts/e2e_call.py --room     # speakerphone mode: both voices into one mic, the
                                          # other side sounding like a phone on speaker

What it does, exactly like the two browser pages would:
1. Synthesizes every line of a scripted call with OpenAI TTS, a different voice per side.
2. Starts the bridge in-process, creates a ghost session, joins the You and Them links over
   HTTP and opens both media WebSockets.
3. Streams each side's audio in real time (20 ms mu-law frames, silence between lines), so the
   bridge relays it to the other side and transcribes it with OpenAI realtime STT.
4. Taps Speak hint once, captures the earpiece whisper and transcribes it ("listens" to it).
5. Transcribes everything the Them side received, to prove no hint or whisper reached them.
6. Hangs up and waits for the one summary.

The key comes from the environment or the git-ignored demo.env; it is never printed. Audio is
kept only in a temp folder for this run (the plugin itself never stores audio).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import wave
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import dev_demo  # noqa: E402  (helpers: demo.env loader, port check, scenarios)

PORT = int(os.environ.get("GHOST_E2E_PORT", "8799"))
WORK = Path(tempfile.gettempdir()) / "hermes-ghost-e2e"
VOICES = {"you": "ash", "them": "sage"}
STYLES = {"you": "A calm customer on a phone call, natural pace.",
          "them": "A friendly call-centre agent on a phone call, natural pace."}

CALL = [
    ("them", "Thank you for calling Northwind Fiber, this is Dana. How can I help you today?"),
    ("you", "Hi, my contract ended last month and I'd like a better price on my internet."),
    ("them", "I can do seventy-nine dollars a month if you sign a new two-year contract."),
    ("you", "I don't want a new contract. FastNet offers a gig for fifty-five."),
    ("them", "OK. I can offer seventy dollars a month for six months."),
    ("you", "That's only six months. Honestly, I'm ready to cancel today."),
    ("them", "Let me transfer you to retention. I can offer sixty dollars a month for twelve "
             "months, with no contract."),
    ("you", "Are there any other fees?"),
    ("them", "There is a ten dollar monthly router fee, but I can waive it for you."),
    ("you", "Great, let's do it."),
    ("them", "Done. Your confirmation number is N F 2 2 9 1."),
]
SPEAK_AFTER_LINE = 6  # tap Speak hint in the pause after this line (the $60 offer)
ROOM = False  # --room: speakerphone mode, both voices reach one microphone


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9$ ]", " ", text.lower()).split()


def likeness(a: str, b: str) -> float:
    return SequenceMatcher(None, words(a), words(b)).ratio()


def wav_bytes(pcm: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


# ---- OpenAI helpers (TTS for the script, STT to "listen") -------------------------------------
async def synthesize(client, fresh: bool) -> list[bytes]:
    from hermes_ghost_call.bridge import audio

    WORK.mkdir(parents=True, exist_ok=True)
    out = []
    for role, text in CALL:
        tag = hashlib.sha1(f"{role}|{VOICES[role]}|{text}".encode()).hexdigest()[:12]
        path = WORK / f"line-{tag}.pcm"
        if fresh or not path.exists():
            pcm = await client.tts(text, voice=VOICES[role], style=STYLES[role], timeout=30)
            path.write_bytes(pcm)
        pcm = path.read_bytes()
        if ROOM and role == "them":
            pcm = phone_speaker(pcm)
        out.append(audio.pcm24k_to_ulaw(pcm))
    return out


def phone_speaker(pcm: bytes, gain: float = 0.6, hiss: float = 40.0) -> bytes:
    """Make a clip sound like a phone on speaker a foot away: the telephone band (300-3400 Hz),
    a few dB quieter than the user, and a little room hiss."""
    import array
    import math
    import random

    a = array.array("h")
    a.frombytes(pcm)
    dt = 1.0 / 24000
    rc_hp, rc_lp = 1 / (2 * math.pi * 300), 1 / (2 * math.pi * 3400)
    k_hp, k_lp = rc_hp / (rc_hp + dt), dt / (rc_lp + dt)
    rng = random.Random(7)
    out = array.array("h")
    x_prev = hp = lp = 0.0
    for v in a:
        hp = k_hp * (hp + v - x_prev)  # one-pole high-pass at 300 Hz
        x_prev = v
        lp += k_lp * (hp - lp)  # one-pole low-pass at 3400 Hz
        out.append(int(max(-32768, min(32767, lp * gain + rng.gauss(0, hiss)))))
    return out.tobytes()


async def transcribe(http, key: str, wav: bytes) -> str:
    r = await http.post("https://api.openai.com/v1/audio/transcriptions",
                        headers={"Authorization": f"Bearer {key}"},
                        data={"model": "gpt-4o-mini-transcribe", "language": "en"},
                        files={"file": ("audio.wav", wav, "audio/wav")}, timeout=60)
    r.raise_for_status()
    return r.json().get("text", "")


# ---- one bot participant ----------------------------------------------------------------------
class Leg:
    def __init__(self, role: str, link: str) -> None:
        self.role = role
        self.link = link
        self.events: list[tuple[float, dict]] = []
        self.heard = bytearray()  # mu-law relayed to this side (what its speaker would play)
        self._speech = bytearray()
        self._drained = asyncio.Event()
        self._drained.set()
        self.ws = None

    async def join(self, http) -> None:
        from websockets.asyncio.client import connect

        u = urlparse(self.link)
        token = parse_qs(u.query)["t"][0]
        sid = u.path.split("/")[2]
        r = await http.post(f"http://127.0.0.1:{PORT}/g/{sid}/join/{self.role}",
                            json={"t": token})
        r.raise_for_status()
        self.ws = await connect(f"ws://127.0.0.1:{PORT}/g/{sid}/ws/{self.role}",
                                max_size=8 * 1024 * 1024)
        await self.ws.send(json.dumps({"event": "start", "token": r.json()["media_token"]}))

    async def say(self, ulaw: bytes) -> None:
        self._drained.clear()
        self._speech.extend(ulaw)
        await self._drained.wait()

    async def send(self, msg: dict) -> None:
        await self.ws.send(json.dumps(msg))

    async def pump(self, stop: asyncio.Event) -> None:
        """A microphone: one 20 ms frame every 20 ms, speech when queued, silence otherwise."""
        from hermes_ghost_call.bridge import audio

        silence = bytes([audio.ULAW_SILENCE]) * 160
        loop = asyncio.get_running_loop()
        t0, k = loop.time(), 0
        while not stop.is_set():
            if self._speech:
                frame = bytes(self._speech[:160]).ljust(160, silence[:1])
                del self._speech[:160]
            else:
                frame = silence
                self._drained.set()
            try:
                await self.ws.send(json.dumps({"event": "media",
                                               "payload": base64.b64encode(frame).decode()}))
            except Exception:  # noqa: BLE001  the bridge closed the call
                return
            k += 1
            await asyncio.sleep(max(0.0, t0 + k * 0.02 - loop.time()))

    async def listen(self) -> None:
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if msg.get("event") == "media":
                    self.heard.extend(base64.b64decode(msg.get("payload") or ""))
                else:
                    self.events.append((time.monotonic(), msg))
        except Exception:  # noqa: BLE001  closed
            return

    def kinds(self, kind: str) -> list[tuple[float, dict]]:
        return [(t, m) for t, m in self.events if m.get("event") == kind]


class RecordingCtx(dev_demo.TerminalCtx):
    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.posts: list[dev_demo.TerminalMessage] = []

    def post_message(self, text: str):
        msg = super().post_message(text)
        self.posts.append(msg)
        return msg


# ---- the run ---------------------------------------------------------------------------------
def start_bridge():
    os.environ["PUBLIC_BASE_URL"] = f"http://localhost:{PORT}"
    os.environ["HERMES_GHOST_BRIDGE_PORT"] = str(PORT)
    db = WORK / f"e2e-{int(time.time())}.db"
    os.environ["HERMES_GHOST_DB"] = str(db)
    import uvicorn

    from hermes_ghost_call.bridge import BridgeSettings, create_app

    settings = BridgeSettings.from_env()
    if not dev_demo.port_free(settings.host, settings.port):
        raise SystemExit(f"port {settings.port} is busy; set GHOST_E2E_PORT to another one")
    server = uvicorn.Server(uvicorn.Config(create_app(settings), host=settings.host,
                                           port=settings.port, log_level="warning"))
    loop = asyncio.new_event_loop()
    threading.Thread(target=lambda: loop.run_until_complete(server.serve()), daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    return server, db


async def run(fresh: bool) -> int:
    import httpx

    from hermes_ghost_call import llm, tools

    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        log("OPENAI_API_KEY is not set (environment or demo.env)")
        return 2
    client = llm.OpenAIClient(key)
    log("synthesizing the call (cached after the first run)…")
    clips = await synthesize(client, fresh)
    await client.aclose()

    server, db = start_bridge()
    ctx = RecordingCtx({"user_name": "Winch"})
    service = tools.configure(ctx=ctx)
    scenario = dev_demo.SCENARIOS["isp"]
    out = service.start(goal=scenario["goal"], callee_label="Northwind Fiber retention",
                        mode="room" if ROOM else "web")
    if not out.get("ok"):
        log(f"start failed: {out}")
        return 1
    log(f"ghost {out['session_id']} started ({out['mode']} mode)")

    report: dict = {"session": out["session_id"], "lines": []}
    async with httpx.AsyncClient(timeout=30) as http:
        you = Leg("you", out["links"]["you"])
        await you.join(http)
        stop = asyncio.Event()
        if ROOM:  # one microphone hears both: the phone on speaker and the user
            them = Leg("them", "")
            tasks = [asyncio.create_task(x) for x in (you.pump(stop), you.listen())]
            legs = {"you": you, "them": you}
        else:
            them = Leg("them", out["links"]["them"])
            await them.join(http)
            tasks = [asyncio.create_task(x) for x in
                     (you.pump(stop), them.pump(stop), you.listen(), them.listen())]
            legs = {"you": you, "them": them}
        await asyncio.sleep(2.0)  # both joined, STT sockets open

        whisper_requested_at = None
        for i, ((role, text), clip) in enumerate(zip(CALL, clips, strict=True), start=1):
            log(f"{role:>4} says: {text}")
            began = time.monotonic()
            await legs[role].say(clip)
            ended = time.monotonic()
            report["lines"].append({"role": role, "text": text, "began": began, "ended": ended,
                                    "seconds": round(len(clip) / 8000, 1)})
            await asyncio.sleep(3.5 if role == "them" else 1.2)
            if i == SPEAK_AFTER_LINE:
                whisper_requested_at = time.monotonic()
                log("tapping Speak hint on the You page")
                await you.send({"event": "speak"})
                for _ in range(80):
                    if you.kinds("whisper"):
                        break
                    await asyncio.sleep(0.1)
                await asyncio.sleep(1.0)
        await asyncio.sleep(3.0)
        log("hanging up")
        await you.send({"event": "stop"})
        summary = None
        for _ in range(90):
            summary = next((m.text for m in ctx.posts if m.text.startswith("👻 Ghost call ended")),
                           None)
            if summary:
                break
            await asyncio.sleep(0.5)
        stop.set()
        for t in tasks:
            t.cancel()

        # ---- what each side got --------------------------------------------------------------
        captions = [(t, m) for t, m in you.kinds("caption") if m.get("final")]
        hints = you.kinds("hint")
        whispers = you.kinds("whisper")
        them_saw = sorted({m.get("event") for _, m in them.events})
        whisper_text = ""
        whisper_wav = None
        if whispers and whispers[0][1].get("pcm24"):
            pcm = base64.b64decode(whispers[0][1]["pcm24"])
            whisper_wav = wav_bytes(pcm, 24000)
            (WORK / "whisper.wav").write_bytes(whisper_wav)
            whisper_text = await transcribe(http, key, whisper_wav)
        from hermes_ghost_call.bridge import audio

        them_wav = wav_bytes(audio.ulaw_to_pcm24k(bytes(them.heard)), 24000)
        (WORK / "them-heard.wav").write_bytes(them_wav)
        them_heard_text = await transcribe(http, key, them_wav) if them.heard else ""
        you_wav = wav_bytes(audio.ulaw_to_pcm24k(bytes(you.heard)), 24000)
        (WORK / "you-heard.wav").write_bytes(you_wav)

    server.should_exit = True

    # ---- report ------------------------------------------------------------------------------
    print("\n================ TRANSCRIPTION (bridge realtime STT vs the script) ================")
    stt_scores = []
    lines = report["lines"]
    for i, line in enumerate(lines):
        # every final caption of this speaker from this line's start to their next line's start
        # (room mode: one unlabelled stream, so any caption up to the next line counts)
        nxt = next((ln["began"] for ln in lines[i + 1:] if ROOM or ln["role"] == line["role"]),
                   float("inf"))
        parts = [m.get("text", "") for t, m in captions
                 if (ROOM or m.get("role") == line["role"]) and line["began"] <= t < nxt]
        heard = " ".join(parts)
        score = likeness(line["text"], heard)
        stt_scores.append(score)
        print(f"{line['role']:>4} {score:4.0%} | said : {line['text']}\n"
              f"          | heard: {heard or '(nothing)'}"
              + (f"   [{len(parts)} pieces]" if len(parts) > 1 else ""))

    print("\n================ HINTS (ticker) ================")
    them_ends = [ln["ended"] for ln in report["lines"] if ln["role"] == "them"]
    for t, m in hints:
        before = [e for e in them_ends if e <= t]
        lag = f"{t - before[-1]:4.1f}s after their line" if before else ""
        print(f"  {m.get('line') or m.get('text')}   [{lag}]")

    print("\n================ EARPIECE WHISPER (You only) ================")
    if whispers:
        w = whispers[0][1]
        lag = whispers[0][0] - whisper_requested_at if whisper_requested_at else 0
        print(f"  hint text : {w.get('text')}")
        heard = whisper_text or f"(no audio, fallback {w.get('fallback')})"
        print(f"  heard     : {heard}   [{lag:.1f}s after the tap]")
    else:
        print("  no whisper arrived")

    print("\n================ WHAT THE OTHER PARTY RECEIVED ================")
    print(f"  events on the Them socket: {them_saw}")
    print(f"  their speaker played    : {them_heard_text[:400]}")

    print("\n================ SUMMARY ================")
    print(summary or "(no summary arrived)")

    hint_texts = [m.get("text", "") for _, m in hints]
    leaked = [h for h in hint_texts if likeness(h, them_heard_text) > 0.5 or
              (h and h.lower()[:30] in them_heard_text.lower())]
    checks = {
        "speech was transcribed (mean likeness >= 70%)":
            bool(stt_scores) and sum(stt_scores) / len(stt_scores) >= 0.70,
        "at least 2 hints arrived": len(hints) >= 2,
        "whisper audio says the hint": bool(whisper_text) and
            likeness(whispers[0][1].get("text", ""), whisper_text) >= 0.6,
        "one summary posted": sum(m.text.startswith("👻 Ghost call ended")
                                  for m in ctx.posts) == 1,
    }
    if ROOM:
        checks["no audio sent back to the page (nothing relayed)"] = not you.heard
    else:
        checks["Them socket got only peer/ended events besides audio"] = \
            set(them_saw) <= {"peer", "ended"}
        checks["no hint text in what Them heard"] = not leaked
    print("\n================ CHECKS ================")
    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"\naudio of this run: {WORK} (whisper.wav, them-heard.wav, you-heard.wav)")
    try:
        os.remove(db)
    except OSError:
        pass
    return 0 if all(checks.values()) else 1


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    dev_demo.load_demo_env(ROOT / "demo.env")
    global ROOM
    ROOM = "--room" in sys.argv
    return asyncio.run(run(fresh="--fresh" in sys.argv))


if __name__ == "__main__":
    raise SystemExit(main())
