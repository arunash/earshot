"""Noise beds and channel impairments, synthesized rather than sampled.

Why synthesize: a benchmark has to be reproducible by anyone who clones it. A
noise corpus means a download, a licence, and a file everyone's copy has to match
byte for byte. These beds are generated from a seed, so two people running the
same battery a year apart impair their audio identically.

The beds are shaped, not white. White noise barely troubles a modern ASR stack;
SPEECH-SHAPED noise does, and multi-talker babble and TV dialogue do most of all,
because they carry the spectral and temporal signature the endpointer is looking
for. That is why S02 uses cafe babble and a television rather than a hiss.
"""

from __future__ import annotations

import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from .util import die, have


# ------------------------------------------------------------ spectral shape --


def _shape(x: np.ndarray, sr: int, tilt_db_per_oct: float,
           corner_hz: float = 500.0, hp_hz: float = 60.0) -> np.ndarray:
    """Apply a spectral tilt above `corner_hz` via FFT. Flat below it."""
    n = len(x)
    spec = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    gain = np.ones_like(freqs)
    above = freqs > corner_hz
    octaves = np.log2(np.maximum(freqs[above], 1e-9) / corner_hz)
    gain[above] = 10 ** (tilt_db_per_oct * octaves / 20.0)
    gain[freqs < hp_hz] *= (freqs[freqs < hp_hz] / max(hp_hz, 1e-9)) ** 2
    out = np.fft.irfft(spec * gain, n=n)
    return out.astype(np.float32)


def _norm(x: np.ndarray, crest: float = 4.0) -> np.ndarray:
    """Unit RMS, with the crest factor tamed to something acoustically real.

    Brown-noise beds come out of `cumsum` with enormous low-frequency
    excursions - peaks 15x RMS. Left alone they dominate the mix and force the
    limiter to crush the speech, so every SNR below them would be a lie. Real
    room and road noise sits nearer 3-4x, so soft-limit to that and renormalize.
    """
    r = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
    if r <= 0:
        return x.astype(np.float32)
    y = x / r
    y = np.tanh(y / crest) * crest          # soft knee, no hard edges
    r2 = float(np.sqrt(np.mean(y.astype(np.float64) ** 2)))
    return (y / r2).astype(np.float32) if r2 > 0 else y.astype(np.float32)


def _syllabic(n: int, sr: int, rng: np.random.Generator,
              rate_hz: float = 4.0, depth: float = 0.8,
              pause_prob: float = 0.0) -> np.ndarray:
    """A speech-like amplitude envelope: syllable-rate modulation plus pauses."""
    t = np.arange(n) / sr
    env = 1.0 - depth * 0.5 * (1 + np.sin(2 * np.pi * rate_hz * t + rng.random() * 6.28))
    # slow drift, so it does not sound like a tremolo
    drift = 1.0 + 0.25 * np.sin(2 * np.pi * 0.23 * t + rng.random() * 6.28)
    env = env * drift
    if pause_prob > 0:
        # a single talker stops between phrases; babble never does
        seg = int(sr * 0.6)
        for s in range(0, n, seg):
            if rng.random() < pause_prob:
                e = min(n, s + int(seg * (0.6 + rng.random())))
                env[s:e] *= 0.05
    return np.maximum(env, 0.0).astype(np.float32)


# ----------------------------------------------------------------- the beds --


def speech_shaped(n: int, sr: int, rng: np.random.Generator) -> np.ndarray:
    """Stationary noise with the long-term average spectrum of speech."""
    return _norm(_shape(rng.standard_normal(n).astype(np.float32), sr, -9.0))


def babble(n: int, sr: int, rng: np.random.Generator, talkers: int = 6) -> np.ndarray:
    """Cafe babble: several speech-shaped talkers, independently modulated.

    Above about four talkers the individual voices stop being intelligible and
    it becomes the wall of sound a real room has - which is exactly the
    condition that defeats a naive energy-gated endpointer.
    """
    out = np.zeros(n, np.float32)
    for _ in range(talkers):
        out += speech_shaped(n, sr, rng) * _syllabic(n, sr, rng,
                                                     rate_hz=3.2 + rng.random() * 2.4,
                                                     depth=0.75)
    return _norm(out)


def tv_dialogue(n: int, sr: int, rng: np.random.Generator) -> np.ndarray:
    """One intelligible talker with phrase pauses. The worst case for a VAD.

    A television in the room is more disruptive than a louder, steadier noise,
    because every pause looks like a turn boundary and every phrase looks like
    the caller starting to speak.
    """
    return _norm(speech_shaped(n, sr, rng)
                 * _syllabic(n, sr, rng, rate_hz=3.6, depth=0.95, pause_prob=0.45))


def road(n: int, sr: int, rng: np.random.Generator) -> np.ndarray:
    """In-car rumble: low-frequency, steady, with slow load changes."""
    brown = np.cumsum(rng.standard_normal(n)).astype(np.float32)
    brown -= brown.mean()
    x = _shape(brown, sr, -14.0, corner_hz=120.0, hp_hz=30.0)
    t = np.arange(n) / sr
    return _norm(x * (1.0 + 0.2 * np.sin(2 * np.pi * 0.11 * t)))


def traffic(n: int, sr: int, rng: np.random.Generator) -> np.ndarray:
    """Curbside: road bed plus vehicles passing."""
    base = road(n, sr, rng) * 0.7
    t = np.arange(n) / sr
    for _ in range(max(1, n // (sr * 6))):
        c = rng.random() * (n / sr)
        width = 0.8 + rng.random() * 1.2
        env = np.exp(-((t - c) ** 2) / (2 * width ** 2))
        base += _shape(rng.standard_normal(n).astype(np.float32), sr, -6.0,
                       corner_hz=300.0) * env * 0.9
    return _norm(base)


def wind(n: int, sr: int, rng: np.random.Generator) -> np.ndarray:
    """Handset wind buffeting: gusty, broadband, with hard transients."""
    brown = np.cumsum(rng.standard_normal(n)).astype(np.float32)
    brown -= brown.mean()
    x = _shape(brown, sr, -7.0, corner_hz=200.0, hp_hz=40.0)
    t = np.arange(n) / sr
    gust = np.ones(n, np.float32)
    for _ in range(max(1, n // (sr * 3))):
        c = rng.random() * (n / sr)
        gust += 2.2 * np.exp(-((t - c) ** 2) / (2 * (0.35 ** 2)))
    return _norm(x * gust)


BEDS = {
    "babble": babble,
    "tv": tv_dialogue,
    "road": road,
    "traffic": traffic,
    "wind": wind,
    "ssn": speech_shaped,
}

BED_NOTES = {
    "babble": "cafe / restaurant, 6 talkers",
    "tv": "one intelligible talker with pauses - hardest case for an endpointer",
    "road": "in-car rumble, low frequency and steady",
    "traffic": "curbside with vehicles passing",
    "wind": "handset wind buffeting with gusts",
    "ssn": "stationary speech-shaped noise - the calibration reference",
}


# ------------------------------------------------------------- SNR mixing --


def speech_rms(x: np.ndarray, sr: int) -> float:
    """RMS over speech-active frames only.

    Measuring over the whole file would count the silence between turns as
    signal, so every SNR would be wrong by however much silence the clip has -
    and clips with different pacing would not be comparable.
    """
    from .audio import vad_segments, Segment
    segs = vad_segments(x, sr)
    if not segs:
        return float(np.sqrt(np.mean(x.astype(np.float64) ** 2))) or 1e-9
    parts = [x[int(s.start * sr):int(s.end * sr)] for s in segs]
    cat = np.concatenate(parts) if parts else x
    return float(np.sqrt(np.mean(cat.astype(np.float64) ** 2))) or 1e-9


def mix_at_snr(speech: np.ndarray, sr: int, bed: str, snr_db: float,
               seed: int = 0) -> np.ndarray:
    """Add a noise bed at a measured signal-to-noise ratio, in dB."""
    if bed in ("none", None):
        return speech
    if bed not in BEDS:
        die(f"unknown noise bed {bed!r}; choose from {', '.join(BEDS)}")
    rng = np.random.default_rng(seed)
    noise = BEDS[bed](len(speech), sr, rng)
    s_rms = speech_rms(speech, sr)
    n_rms = float(np.sqrt(np.mean(noise.astype(np.float64) ** 2))) or 1e-9
    target_n = s_rms / (10 ** (snr_db / 20.0))
    out = speech + noise * (target_n / n_rms)
    peak = float(np.max(np.abs(out)))
    if peak > 0.99:
        out = out * (0.99 / peak)
    return out.astype(np.float32)


# ------------------------------------------------- channel impairments --


def packet_loss(x: np.ndarray, sr: int, rate: float, frame_ms: float = 20.0,
                conceal: bool = True, seed: int = 0) -> np.ndarray:
    """Drop RTP-sized frames at `rate`, with or without concealment.

    `conceal=True` repeats the previous frame, which is what a real jitter
    buffer does and which sounds like a stutter. `conceal=False` leaves a hole,
    which sounds like a click. Both are worth testing: some ASR front-ends cope
    far better with one than the other.
    """
    if rate <= 0:
        return x
    rng = np.random.default_rng(seed)
    n = int(sr * frame_ms / 1000.0)
    out = x.copy()
    for i in range(0, len(x) - n, n):
        if rng.random() < rate:
            out[i:i + n] = out[i - n:i] if (conceal and i >= n) else 0.0
    return out


def jitter(x: np.ndarray, sr: int, severity: float, frame_ms: float = 20.0,
           seed: int = 0) -> np.ndarray:
    """Jitter-buffer adaptation: frames duplicated and dropped to re-time.

    Network jitter does not reach the far end as varying delay - the receiving
    buffer absorbs it by stretching and compressing the stream. What the agent's
    ASR actually hears is a signal whose timing keeps shifting, which is a
    different impairment from loss and worth measuring on its own.
    """
    if severity <= 0:
        return x
    rng = np.random.default_rng(seed)
    n = int(sr * frame_ms / 1000.0)
    frames = [x[i:i + n] for i in range(0, len(x) - n, n)]
    out: List[np.ndarray] = []
    for f in frames:
        r = rng.random()
        if r < severity / 2:
            out.append(f)
            out.append(f)      # buffer underran, repeat to fill
        elif r < severity:
            continue           # buffer overran, drop to catch up
        else:
            out.append(f)
    y = np.concatenate(out) if out else x
    if len(y) < len(x):
        y = np.pad(y, (0, len(x) - len(y)))
    return y[:len(x)].astype(np.float32)


def dropouts(x: np.ndarray, sr: int, count: int, ms: float = 320.0,
             seed: int = 0) -> np.ndarray:
    """A handful of long gaps - a lift, a tunnel, a cell handover."""
    if count <= 0:
        return x
    rng = np.random.default_rng(seed)
    out = x.copy()
    n = int(sr * ms / 1000.0)
    for _ in range(count):
        i = int(rng.random() * max(1, len(x) - n))
        out[i:i + n] = 0.0
    return out


def clip(x: np.ndarray, headroom_db: float) -> np.ndarray:
    """Overdrive, as a handset AGC does when someone shouts into it."""
    if headroom_db >= 0:
        return x
    ceiling = 10 ** (headroom_db / 20.0)
    return np.clip(x, -ceiling, ceiling).astype(np.float32) / max(ceiling, 1e-9) * 0.99


def codec_roundtrip(x: np.ndarray, sr: int, kind: str = "g726") -> np.ndarray:
    """Encode and decode through a real telephony codec.

    g726 at 16kbps is a genuinely bad line; opus at 8kbps is a bad VoIP leg.
    Nothing models a codec's artifacts as convincingly as the codec.
    """
    if kind in ("none", None):
        return x
    if not have("ffmpeg"):
        die("codec impairment needs ffmpeg")
    with tempfile.TemporaryDirectory() as td:
        src, dst = Path(td) / "in.wav", Path(td) / "out.wav"
        _write(src, x, sr)
        if kind.startswith("g726"):
            rate = kind.split(":")[1] if ":" in kind else "16000"
            mid = Path(td) / "m.wav"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
                            "-ar", "8000", "-ac", "1", "-acodec", "g726",
                            "-b:a", rate, str(mid)], check=True)
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(mid),
                            "-ar", str(sr), "-acodec", "pcm_s16le", str(dst)],
                           check=True)
        elif kind.startswith("opus"):
            rate = kind.split(":")[1] if ":" in kind else "8000"
            mid = Path(td) / "m.ogg"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
                            "-c:a", "libopus", "-b:a", rate, str(mid)], check=True)
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(mid),
                            "-ar", str(sr), "-ac", "1", "-acodec", "pcm_s16le",
                            str(dst)], check=True)
        else:
            die(f"unknown codec {kind!r} (g726[:rate] | opus[:rate] | none)")
        from .audio import read_wav
        _, data = read_wav(dst)
    y = data.reshape(-1)
    if len(y) < len(x):
        y = np.pad(y, (0, len(x) - len(y)))
    return y[:len(x)].astype(np.float32)


def _write(path: Path, x: np.ndarray, sr: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())


def apply_condition(x: np.ndarray, sr: int, cond: Dict, seed: int = 0) -> np.ndarray:
    """Apply one named impairment condition to a clean rendering.

    Order matters and mirrors the real signal path: the room adds noise to the
    microphone first, then the codec and the network degrade what the microphone
    captured.
    """
    y = x
    if cond.get("noise") and cond.get("noise") != "none":
        y = mix_at_snr(y, sr, cond["noise"], float(cond.get("snr_db", 15)), seed)
    if cond.get("clip_db"):
        y = clip(y, float(cond["clip_db"]))
    if cond.get("codec"):
        y = codec_roundtrip(y, sr, cond["codec"])
    if cond.get("packet_loss"):
        y = packet_loss(y, sr, float(cond["packet_loss"]),
                        conceal=cond.get("conceal", True), seed=seed + 1)
    if cond.get("jitter"):
        y = jitter(y, sr, float(cond["jitter"]), seed=seed + 2)
    if cond.get("dropouts"):
        y = dropouts(y, sr, int(cond["dropouts"]),
                     float(cond.get("dropout_ms", 320)), seed=seed + 3)
    return y
