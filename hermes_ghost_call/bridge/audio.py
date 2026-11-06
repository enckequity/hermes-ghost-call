"""Tiny pure-Python audio helpers (``audioop`` is gone in Python 3.13).

The call legs carry G.711 mu-law at 8 kHz (Twilio Media Streams and the web pages use the same
frames). Realtime transcription takes PCM16 at 24 kHz, and TTS returns PCM16 at 24 kHz. Nothing
here touches the disk: audio only ever lives in memory.
"""
from __future__ import annotations

import array
import sys

_BIAS = 0x84
_CLIP = 32635


def _ulaw_to_lin(u: int) -> int:
    u = ~u & 0xFF
    sign, exp, mant = u & 0x80, (u >> 4) & 7, u & 0x0F
    s = (((mant << 3) + _BIAS) << exp) - _BIAS
    return -s if sign else s


def _lin_to_ulaw(s: int) -> int:
    sign = 0x80 if s < 0 else 0
    if sign:
        s = -s
    s = min(s, _CLIP) + _BIAS
    exp = 7
    mask = 0x4000
    while exp > 0 and not (s & mask):
        exp -= 1
        mask >>= 1
    mant = (s >> (exp + 3)) & 0x0F
    return ~(sign | (exp << 4) | mant) & 0xFF


ULAW_TO_LIN = [_ulaw_to_lin(i) for i in range(256)]
_ULAW_ENC = bytes(_lin_to_ulaw(s - 32768) for s in range(65536))
ULAW_SILENCE = 0xFF


def _pcm_array(pcm: bytes) -> array.array:
    a = array.array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    if sys.byteorder != "little":
        a.byteswap()
    return a


def _to_bytes(a: array.array) -> bytes:
    if sys.byteorder != "little":
        a = array.array("h", a)
        a.byteswap()
    return a.tobytes()


def ulaw_to_pcm24k(ulaw: bytes) -> bytes:
    """mu-law 8 kHz -> PCM16 LE 24 kHz (linear interpolation, x3)."""
    lin = [ULAW_TO_LIN[b] for b in ulaw]
    if not lin:
        return b""
    out = array.array("h")
    nxt = lin[1:] + lin[-1:]
    for a, b in zip(lin, nxt, strict=True):
        d = b - a
        out.extend((a, a + d // 3, a + (2 * d) // 3))
    return _to_bytes(out)


def pcm24k_to_ulaw(pcm: bytes) -> bytes:
    """PCM16 LE 24 kHz -> mu-law 8 kHz (box-filter decimation /3)."""
    a = _pcm_array(pcm)
    n = len(a) // 3
    enc = _ULAW_ENC
    out = bytearray(n)
    for i in range(n):
        j = 3 * i
        v = (a[j] + a[j + 1] + a[j + 2]) // 3
        out[i] = enc[v + 32768]
    return bytes(out)


def frames(ulaw: bytes, size: int = 160) -> list[bytes]:
    """Split into 20 ms frames, padding the last one with silence."""
    out = [ulaw[i:i + size] for i in range(0, len(ulaw), size)]
    if out and len(out[-1]) < size:
        out[-1] = out[-1] + bytes([ULAW_SILENCE]) * (size - len(out[-1]))
    return out


def ulaw_energy(ulaw: bytes) -> float:
    """Mean absolute amplitude (0..32768) of a mu-law chunk, for a crude client-side VAD."""
    if not ulaw:
        return 0.0
    return sum(abs(ULAW_TO_LIN[b]) for b in ulaw) / len(ulaw)
