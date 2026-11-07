"""Lay a narrator's lines into their slots and mux them onto the rendered video (no re-render).

    python video/voice_mix.py [name]

Reads video/narration/<name>/N1..N8 (.mp3 or .wav; name defaults to build.py's VOICE), places
each line at the start build.py gave it (video/build/edit.json), mixes them over the call audio
(video/build/mix.wav) and writes video/out/ghost-call-launch-<name>.mp4 next to the
silent-narrator render. Prints each line's length and start.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

VIDEO = Path(__file__).resolve().parent
sys.path.insert(0, str(VIDEO))

import build  # noqa: E402


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                          "default=nw=1:nk=1", str(path)], capture_output=True, text=True,
                         check=True)
    return float(out.stdout.strip())


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else build.VOICE
    edit = json.loads((VIDEO / "build" / "edit.json").read_text(encoding="utf-8"))
    folder = VIDEO / "narration" / name
    video = VIDEO / "out" / "ghost-call-launch.mp4"
    inputs = ["-i", str(video), "-i", str(VIDEO / "build" / "mix.wav")]
    filters: list[str] = []
    labels: list[str] = []
    ok = True
    for k, (tag, start, room) in enumerate(edit["narration"], start=2):
        src = next((p for p in (folder / f"{tag}.mp3", folder / f"{tag}.wav") if p.exists()), None)
        if src is None:
            print(f"missing {folder / tag}.mp3")
            return 1
        d = duration(src)
        over = d > room + 0.05
        ok &= not over
        print(f"  {tag}: {d:4.1f} s at {start:5.1f} s{'  LONGER THAN THE EDIT' if over else ''}")
        inputs += ["-i", str(src)]
        ms = int(start * 1000)
        filters.append(f"[{k}:a]aresample=48000,aformat=channel_layouts=mono,"
                       f"loudnorm=I=-16:TP=-2:LRA=7,aresample=48000,adelay={ms}:all=1[n{k}]")
        labels.append(f"[n{k}]")
    filters.append("[1:a]aresample=48000,loudnorm=I=-19:TP=-3:LRA=9,aresample=48000[call]")
    filters.append(f"[call]{''.join(labels)}amix=inputs={len(labels) + 1}:normalize=0:"
                   "duration=first,alimiter=limit=0.95[a]")
    target = VIDEO / "out" / f"ghost-call-launch-{name}.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex",
                    ";".join(filters), "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac",
                    "-b:a", "192k", "-movflags", "+faststart", str(target)], check=True)
    print(f"wrote {target}")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
