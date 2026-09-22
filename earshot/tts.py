"""Local speech rendering, so a battery can be baked to audio offline.

Uses the platform's own synthesizer. That is a deliberate trade: the voice is
less natural than a cloud TTS, but it is free, needs no network, and - the part
that matters for a benchmark - it is identical on every run. A battery you bake
today and re-bake in six months produces the same waveform, so a regression in
the score is a regression in the system under test and not a change in the voice.

The accent set is real: macOS ships en_IN, en_GB, en_AU, en_IE and en_ZA voices,
which is what makes S04 more than a gesture.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from .util import die, have, warn

# Friendly alias -> (macOS voice, espeak-ng voice). Chosen for accent coverage.
VOICES: Dict[str, Dict[str, str]] = {
    "default":          {"say": "Samantha", "espeak": "en-us"},
    "us_female":        {"say": "Samantha", "espeak": "en-us"},
    "us_male":          {"say": "Alex",     "espeak": "en-us+m3"},
    "indian_english":   {"say": "Rishi",    "espeak": "en-in"},
    "indian_female":    {"say": "Tara",     "espeak": "en-in+f3"},
    "british_english":  {"say": "Daniel",   "espeak": "en-gb"},
    "australian":       {"say": "Karen",    "espeak": "en-gb"},
    "irish":            {"say": "Moira",    "espeak": "en-gb"},
    "south_african":    {"say": "Tessa",    "espeak": "en-gb"},
    # macOS has no Nigerian voice; Tessa is the nearest available African
    # English. Substitute a real speaker's recording with turn.play if it
    # matters to you - and for S04 it probably does.
    "nigerian_english": {"say": "Tessa",    "espeak": "en-gb"},
    "spanish_english":  {"say": "Paulina",  "espeak": "en-us"},
}

RATE_WPM = {"slow": 130, None: 180, "fast": 250}
GAIN = {"quiet": 0.30, None: 1.0, "loud": 1.0}

# Emotional delivery, via macOS speech-synthesis embedded commands. Measured
# effect on Samantha: angry lifts f0 from ~182Hz to ~302Hz and shortens the
# utterance; distressed drops pitch, slows down and quietens.
#
# This matters more than it looks. Without it a "furious caller" scenario is
# angry WORDS in a calm voice, and an agent's de-escalation behaviour is never
# actually provoked - you measure its reading comprehension, not its manner.
TONES = {
    None:         ("", 1.0),
    "angry":      ("[[pbas 58]][[pmod 6]][[volm 1.0]][[rate 215]]", 1.0),
    "distressed": ("[[pbas 42]][[pmod 8]][[volm 0.55]][[rate 145]]", 0.62),
    "flat":       ("[[pbas 45]][[pmod 0]][[volm 0.8]][[rate 175]]", 0.85),
    "rushed":     ("[[pbas 52]][[pmod 4]][[volm 0.95]][[rate 240]]", 1.0),
    "hesitant":   ("[[pbas 46]][[pmod 5]][[volm 0.7]][[rate 140]]", 0.8),
}


def engine() -> Optional[str]:
    if have("say"):
        return "say"
    if have("espeak-ng") or have("espeak"):
        return "espeak"
    return None


def render(text: str, sr: int, voice: str = "default",
           rate: Optional[str] = None, volume: Optional[str] = None,
           tone: Optional[str] = None) -> np.ndarray:
    """Render one line to mono float32 at `sr`, optionally in a given tone."""
    eng = engine()
    if eng is None:
        die("No local TTS found. macOS has `say` built in; on Linux install "
            "espeak-ng. Or supply your own audio with `play:` in the battery.")

    v = VOICES.get(voice, {}).get(eng, voice)
    wpm = RATE_WPM.get(rate, RATE_WPM[None])
    if tone and tone not in TONES:
        die(f"unknown tone {tone!r}; choose from "
            f"{', '.join(t for t in TONES if t)}")
    prefix, tone_gain = TONES.get(tone, TONES[None])
    if eng != "say":
        prefix = ""      # embedded commands are a macOS speech-synthesis feature

    with tempfile.TemporaryDirectory() as td:
        raw = Path(td) / ("out.aiff" if eng == "say" else "out.wav")
        if eng == "say":
            subprocess.run(["say", "-v", v, "-r", str(wpm), "-o", str(raw),
                            prefix + text], check=True, capture_output=True)
        else:
            binary = "espeak-ng" if have("espeak-ng") else "espeak"
            subprocess.run([binary, "-v", v, "-s", str(wpm), "-w", str(raw), text],
                           check=True, capture_output=True)
        wav = Path(td) / "norm.wav"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw),
                        "-ar", str(sr), "-ac", "1", "-acodec", "pcm_s16le",
                        str(wav)], check=True)
        from .audio import read_wav
        _, data = read_wav(wav)

    x = data.reshape(-1).astype(np.float32)
    peak = float(np.max(np.abs(x))) or 1.0
    # Peak-normalize for a consistent reference, then apply the deliberate level
    # differences. Without the tone gain a "quiet, distressed" caller would be
    # normalized back up to the same loudness as a shouting one.
    x = x / peak * 0.72 * GAIN.get(volume, 1.0) * tone_gain
    return x


def available_voices() -> List[str]:
    eng = engine()
    if eng != "say":
        return sorted(VOICES)
    try:
        out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True,
                             check=True).stdout
    except Exception:
        return sorted(VOICES)
    have_names = {ln.split()[0] for ln in out.splitlines() if ln.strip()}
    return sorted(k for k, v in VOICES.items() if v["say"] in have_names)
