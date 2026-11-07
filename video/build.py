"""Build the launch video's scene page, audio mix and narration timecodes from a real test run.

    python video/build.py        # writes video/build/scenes.html, video/build/mix.wav,
                                 # video/TIMECODES.txt and video/narration.srt
    node video/render.mjs        # renders the frames and muxes video/out/ghost-call-launch.mp4

Input: video/timeline.json from `scripts/e2e_call.py --timeline video/timeline.json` (the call
audio, captions, hints, whisper and summary of that run), plus video/assets/. Every hint, caption
and summary line on screen is from that run; only the edit (which lines, and the pauses left for
the narrator) is chosen here.
"""
from __future__ import annotations

import array
import base64
import html
import json
import math
import re
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VIDEO = ROOT / "video"
sys.path.insert(0, str(ROOT))

from hermes_ghost_call.bridge import webpage  # noqa: E402

FPS = 30
RATE = 24000
DURATION = 0.0  # set by build_edit()

VOICE = "sia"  # narration takes in video/narration/<VOICE>/N*.wav
GAP = 0.3  # breath between one voice and the next; the sound never stops for longer
# The edit, in order: narration lines (N*), call lines (1-based, as in scripts/e2e_call.py CALL)
# and the whisper. build_edit() lays them end to end from the real durations.
ORDER = ["N1", "N2", 2, 3, "N3", 4, "N4", 7, "N5", "whisper", 8, 9, 10, "N6", "N7", "N8"]
PRIVATE = "Target $60/month or less, no new contract. Walk away: cancel and switch."
GLYPH = ('<svg class="mini" viewBox="0 0 100 110"><path d="M12 52 A38 38 0 0 1 88 52 L88 102 '
         'Q81.7 94 75.3 102 Q69 110 62.7 102 Q56.3 94 50 102 Q43.7 110 37.3 102 Q31 94 24.7 102 '
         'Q18.3 110 12 102 Z"/><circle cx="38" cy="54" r="6"/>'
         '<circle cx="62" cy="54" r="6"/></svg>')


def font_faces() -> str:
    out = []
    for name, weight in (("Light", 300), ("Regular", 400), ("Medium", 500), ("SemiBold", 600)):
        data = base64.b64encode((VIDEO / "fonts" / f"InterTight-{name}.ttf").read_bytes()).decode()
        out.append(f'@font-face{{font-family:"Inter";font-weight:{weight};'
                   f'src:url(data:font/ttf;base64,{data}) format("truetype");}}')
    return "\n".join(out)


def page(markup: str, fonts: str, extra_css: str = "") -> str:
    markup = re.sub(r"<script[^>]*>.*?</script>", "", markup, flags=re.S)
    return markup.replace("</head>", f"<style>{fonts}\nhtml,body{{overflow:hidden}}"
                                     f"#priv{{border-radius:4px;padding:0 2px}}{extra_css}"
                                     "</style></head>")


def read_pcm(path: Path) -> array.array:
    a = array.array("h")
    a.frombytes(path.read_bytes())
    return a


def read_wav(path: Path) -> array.array:
    with wave.open(str(path)) as w:
        assert w.getframerate() == RATE and w.getsampwidth() == 2
        a = array.array("h")
        a.frombytes(w.readframes(w.getnframes()))
    return a


def place(track: list[float], clip: array.array, at: float, gain: float = 1.0) -> None:
    k = int(at * RATE)
    for i, v in enumerate(clip):
        if 0 <= k + i < len(track):
            track[k + i] += v * gain


def envelope(track: list[float]) -> list[float]:
    step = RATE // FPS
    env = []
    for f in range(int(DURATION * FPS)):
        chunk = track[f * step:(f + 1) * step]
        rms = math.sqrt(sum(x * x for x in chunk) / len(chunk)) if chunk else 0.0
        env.append(round(min(1.0, rms / 5000) ** 0.7, 3))
    return env


def ts(t: float, sep: str = ".") -> str:
    m, s = divmod(t, 60)
    return f"{int(m):02d}:{s:04.1f}" if sep == "." else \
        f"00:{int(m):02d}:{int(s):02d},{int(round((s % 1) * 1000)):03d}"


def wav_seconds(path: Path) -> float:
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


def build_edit(tl: dict) -> dict:
    """Lay the ORDER end to end from the real durations, so the sound never stops.

    Hints keep the run's real delay after their line; the narration after a hinted line is
    written long enough to cover that delay. The tap on Speak hint falls inside N5 so the whisper
    starts the moment N5 ends (the tap-to-whisper delay is the run's own).
    """
    lines = tl["lines"]
    w = tl["whisper"]
    lag = w["t"] - w["tap"]
    whisper_len = wav_seconds(ROOT / w["wav"])
    narr = {f"N{i}": wav_seconds(VIDEO / "narration" / VOICE / f"N{i}.wav") for i in range(1, 9)}

    def line_len(n: int) -> float:
        return (ROOT / lines[n - 1]["clip"]).stat().st_size / 2 / RATE

    def hint_delay(n: int) -> float | None:
        ln = lines[n - 1]
        later = [h["t"] - ln["began"] for h in tl["hints"] if h["t"] >= ln["ended"]
                 and (n == len(lines) or h["t"] < lines[n]["ended"])]
        return min(later) if later and ln["role"] == "them" else None

    t, out = 0.3, {"segments": [], "narration": [], "scenes": {}}
    prev_hint = None
    for item in ORDER:
        if item == "N2":
            out["scenes"]["chat"] = t - 0.1
        if item == 2:
            out["scenes"]["call"] = t - 0.3
            t += 0.3
        if item == "N6":
            out["hang"] = t - 0.1
            out["scenes"]["sum"] = t + 0.25
            t += 0.6
        if item == "N7":
            out["scenes"]["modes"] = t - 0.1
        if item == "N8":
            out["scenes"]["end"] = t - 0.1
        if isinstance(item, int):
            out["segments"].append((item, round(t, 3)))
            d = hint_delay(item)
            prev_hint = t + d if d is not None else None
            t += line_len(item) + GAP
        elif item == "whisper":
            out["whisper_at"] = round(t, 3)
            t += whisper_len + GAP
        else:
            out["narration"].append((item, round(t, 3), round(narr[item], 3)))
            if item == "N4":
                out["priv"] = (t - 0.2, t + narr[item] + 0.3)
            if item == "N5":
                out["speak_tap"] = round(t + narr[item] + 0.15 - lag, 3)
                if prev_hint is not None:
                    out["speak_tap"] = max(out["speak_tap"], prev_hint + 0.6)
                t = max(t + narr[item] + 0.15, out["speak_tap"] + lag)
                continue
            t += narr[item] + GAP
    out["duration"] = round(t + 1.2, 2)
    out["type"] = (out["scenes"]["chat"] + 0.3, out["scenes"]["chat"] + 1.4)
    out["reply_at"] = out["scenes"]["chat"] + 1.6
    out["sum_at"] = out["scenes"]["sum"] + 0.3
    out["cards_at"] = out["scenes"]["modes"] + 0.3
    sc = out["scenes"]
    out["scene_list"] = [("s-title", 0.0, sc["chat"]), ("s-chat", sc["chat"], sc["call"]),
                         ("s-call", sc["call"], sc["sum"]), ("s-sum", sc["sum"], sc["modes"]),
                         ("s-modes", sc["modes"], sc["end"]),
                         ("s-end", sc["end"], out["duration"] + 1)]
    return out


def main() -> int:
    global DURATION
    tl = json.loads((VIDEO / "timeline.json").read_text(encoding="utf-8"))
    lines = tl["lines"]
    edit = build_edit(tl)
    DURATION = edit["duration"]
    SEGMENTS = edit["segments"]
    NARRATION = edit["narration"]
    SPEAK_TAP, HANG_TAP = edit["speak_tap"], edit["hang"]

    # ---- map the run onto the edit ----------------------------------------------------------
    def seg_of(line_no: int):
        return next((start for n, start in SEGMENTS if n == line_no), None)

    captions, hints = [], []
    for c in tl["captions"]:
        i = max(k for k, ln in enumerate(lines) if ln["began"] <= c["t"] + 1e-6)
        start = seg_of(i + 1)
        if start is not None:
            captions.append({"t": round(start + c["t"] - lines[i]["began"], 3), "role": c["role"],
                             "text": c["text"]})
    for h in tl["hints"]:
        i = max(k for k, ln in enumerate(lines) if ln["role"] == "them" and ln["ended"] <= h["t"])
        start = seg_of(i + 1)
        if start is not None:
            hints.append({"t": round(start + h["t"] - lines[i]["began"], 3), "text": h["text"],
                          "line": h["line"]})
    hints = [h for h in hints if h["t"] < HANG_TAP]  # the page shows no hints once ended
    w = tl["whisper"]
    whisper_at = SPEAK_TAP + (w["t"] - w["tap"])
    whisper = read_wav(ROOT / w["wav"])
    whisper_end = whisper_at + len(whisper) / RATE

    # ---- audio: what each side's speaker plays, and the film mix ----------------------------
    n = int(DURATION * RATE)
    ear, spk = [0.0] * n, [0.0] * n  # your earpiece, their speaker
    for line_no, start in SEGMENTS:
        ln = lines[line_no - 1]
        clip = read_pcm(ROOT / ln["clip"])
        place(ear if ln["role"] == "them" else spk, clip, start)
    place(ear, whisper, whisper_at, 0.85)
    mix = array.array("h", (int(max(-32768, min(32767, a + b)))
                            for a, b in zip(ear, spk, strict=True)))
    build = VIDEO / "build"
    build.mkdir(exist_ok=True)
    with wave.open(str(build / "mix.wav"), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(mix.tobytes())

    # ---- text on screen ---------------------------------------------------------------------
    reply = (VIDEO / "assets" / "start_reply.txt").read_text(encoding="utf-8").splitlines()

    def link(s: str) -> str:
        label, url = s.split(": ", 1)
        url = re.sub(r"(\?t=\w{4})\w+", r"\1…", url)
        return (f'<span class="k">{html.escape(label)}:</span> '
                f'<span class="lnk">{html.escape(url)}</span>')

    reply_html = [GLYPH + html.escape(reply[0].lstrip("👻 ").strip()), link(reply[1]),
                  link(reply[2]), f'<span class="k">{html.escape(reply[3])}</span>']
    summary_html = []
    for i, row in enumerate(tl["summary"].splitlines()):
        if i == 0:
            summary_html.append(GLYPH + html.escape(row.lstrip("👻 ").strip()))
        else:
            key, _, rest = row.partition(": ")
            summary_html.append(f'<span class="k">{html.escape(key)}:</span> {html.escape(rest)}')

    fonts = font_faces()
    goal = tl["goal"]
    you = webpage.render_you(sid="g3b8790", goal=goal, callee_label="Northwind Fiber retention",
                             nonce="v")
    you = you.replace(html.escape(PRIVATE), f'<span id="priv">{html.escape(PRIVATE)}</span>', 1)
    them = webpage.render_them(sid="g3b8790", caller="Winch", nonce="v")
    data = {
        "fps": FPS, "duration": DURATION, "scale": 600 / 580, "scenes": edit["scene_list"],
        "type": edit["type"], "replyAt": edit["reply_at"],
        "cmd": "/ghost start " + goal, "reply": reply_html, "summary": summary_html,
        "sumAt": edit["sum_at"], "cardsAt": edit["cards_at"], "priv": edit["priv"],
        "hang": HANG_TAP,
        "hints": hints, "captions": captions,
        "taps": [{"t": SPEAK_TAP, "el": "speak"}, {"t": HANG_TAP, "el": "hang"}],
        "whisperSpan": [whisper_at, whisper_end],
        "envYou": envelope(ear), "envThem": envelope(spk),
        "pages": {"you": page(you, fonts), "them": page(them, fonts)},
    }
    tpl = (VIDEO / "scenes.template.html").read_text(encoding="utf-8")
    (build / "scenes.html").write_text(
        tpl.replace("__FONTS__", fonts).replace("__DATA__", json.dumps(data)), encoding="utf-8")

    # ---- narration timecodes ----------------------------------------------------------------
    script = dict(re.findall(r"^\| (N\d) \| (.+?) \|$",
                             (VIDEO / "SCRIPT.md").read_text(encoding="utf-8"), flags=re.M))
    rows = ["Ghost Call launch video: narration timecodes",
            f"Video: video/out/ghost-call-launch.mp4, {ts(DURATION)} long, 1920x1080, 30 fps.",
            f"Narrator: ElevenLabs takes in video/narration/{VOICE}/; each line starts at its",
            "time and runs for its length. The call is the real test run (scripts/e2e_call.py).",
            ""]
    srt = []
    for k, (tag, start, room) in enumerate(NARRATION, start=1):
        text = script[tag]
        words = len(text.split())
        rows.append(f"{tag}  {ts(start)} - {ts(start + room)}  ({room:.1f} s, {words} words)")
        rows.append(f"    {text}")
        rows.append("")
        srt += [str(k), f"{ts(start, ',')} --> {ts(start + room, ',')}", text, ""]
    rows.append("Call audio and on-screen events:")
    for line_no, seg in SEGMENTS:
        ln = lines[line_no - 1]
        rows.append(f"    {ts(seg)}  {'provider' if ln['role'] == 'them' else 'caller  '}: "
                    f"{ln['text']}")
    rows += [f"    {ts(h['t'])}  hint on your page: {h['text']}" for h in hints]
    rows += [f"    {ts(SPEAK_TAP)}  tap Speak hint; the whisper plays {ts(whisper_at)} - "
             f"{ts(whisper_end)}", f"    {ts(HANG_TAP)}  hang up; the summary follows"]
    (VIDEO / "TIMECODES.txt").write_text("\n".join(rows) + "\n", encoding="utf-8")
    (VIDEO / "narration.srt").write_text("\n".join(srt), encoding="utf-8")
    (build / "edit.json").write_text(json.dumps({"voice": VOICE, "duration": DURATION,
                                                 "narration": NARRATION}), encoding="utf-8")

    for tag, start, room in NARRATION:  # a slot must not overlap call audio
        for line_no, seg in SEGMENTS:
            dur = (ROOT / lines[line_no - 1]["clip"]).stat().st_size / 2 / RATE
            assert start + room <= seg or start >= seg + dur, (tag, line_no)
        assert start + room <= whisper_at or start >= whisper_end, tag
    print(f"hints {[h['t'] for h in hints]}, whisper {whisper_at:.2f}-{whisper_end:.2f}, "
          f"{len(captions)} captions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
