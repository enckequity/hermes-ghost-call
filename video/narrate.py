"""Draft narrator for the launch video with OpenAI TTS (a stand-in until a final voice exists).

    python video/narrate.py               # both draft voices
    python video/narrate.py onyx          # one voice

Reads the narration lines from video/SCRIPT.md, writes one WAV per line plus a joined MP3 (lines
0.7 s apart) and a timings JSON into video/narration/<voice>/. The key comes from the environment
or the git-ignored demo.env and is never printed. A final ElevenLabs file can replace these: the
video build only needs the per-line WAVs and timings.json.
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import subprocess
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import dev_demo  # noqa: E402

from hermes_ghost_call import llm  # noqa: E402

RATE = 24000
GAP_S = 0.7
VOICES = ("onyx", "coral")
STYLE = ("A calm, warm, confident product narrator. Unhurried, clear, quietly sure of itself. "
         "Not an ad announcer: no hype, no exaggerated emphasis. Short natural pauses at commas "
         "and full stops.")


def narration_lines() -> list[tuple[str, str]]:
    text = (ROOT / "video" / "SCRIPT.md").read_text(encoding="utf-8")
    return re.findall(r"^\| (N\d) \| (.+?) \|$", text, flags=re.M)


def write_wav(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)


async def voice_pass(client, voice: str, lines) -> None:
    out = ROOT / "video" / "narration" / voice
    out.mkdir(parents=True, exist_ok=True)
    joined, timings, t = io.BytesIO(), [], 0.0
    gap = b"\x00\x00" * int(RATE * GAP_S)
    for tag, text in lines:
        pcm = await client.tts(text, voice=voice, style=STYLE, timeout=60)
        write_wav(out / f"{tag}.wav", pcm)
        dur = len(pcm) / 2 / RATE
        timings.append({"id": tag, "text": text, "start": round(t, 3), "duration": round(dur, 3)})
        joined.write(pcm + gap)
        t += dur + GAP_S
        print(f"  {voice} {tag}: {dur:.1f} s", flush=True)
    write_wav(out / "all.wav", joined.getvalue())
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(out / "all.wav"),
                    "-codec:a", "libmp3lame", "-q:a", "2", str(out / "narration.mp3")], check=True)
    (out / "timings.json").write_text(json.dumps({"voice": voice, "total": round(t, 3),
                                                  "lines": timings}, indent=2), encoding="utf-8")
    print(f"{voice}: {t:.1f} s total -> {out / 'narration.mp3'}", flush=True)


async def main() -> int:
    dev_demo.load_demo_env(ROOT / "demo.env")
    import os

    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        print("OPENAI_API_KEY is not set (environment or demo.env).")
        return 1
    lines = narration_lines()
    if len(lines) != 7:
        print(f"expected 7 narration lines in SCRIPT.md, found {len(lines)}")
        return 1
    voices = [v for v in sys.argv[1:] if not v.startswith("-")] or list(VOICES)
    client = llm.OpenAIClient(key)
    try:
        for voice in voices:
            await voice_pass(client, voice, lines)
    finally:
        await client.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
