"""Render a scenario to audio, impair it, and remember exactly what was said when.

This is what makes noise and network testing possible at all. Twilio's <Say>
puts clean synthetic speech on a clean line - you cannot add a cafe to it. So
the harness renders its own side of the call to a file, mixes in a noise bed at
a measured SNR, degrades it through a codec and a lossy network, and plays that.

The second half is the part that makes the measurements survive. Once the
caller's channel is full of babble, no voice-activity detector can pick the
caller's turns back out of it - so every latency and barge-in number would
collapse exactly when the test got interesting. Earshot sidesteps that: it
GENERATED the audio, so it knows every onset and offset to the sample. The
timeline is written alongside the wav, and at ingest the clean reference is
cross-correlated against the recording to find where the audio actually landed.
The caller side is then ground truth, not an estimate, at any SNR.
"""

from __future__ import annotations

import json
import wave
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from . import impair, tts
from .battery import Scenario, Turn
from .util import info, warn

SR = 16000


def render_scenario(scenario: Scenario, agent_budget_s: float = 6.0,
                    greeting_s: float = 4.0, sr: int = SR
                    ) -> Tuple[np.ndarray, List[Dict[str, Any]]]:
    """Lay the scenario's turns onto a timeline, exactly as the TwiML would.

    Returns (clean audio, timeline) where timeline entries carry the turn's
    start and end in seconds plus what was said - so a transcript is never
    needed to know what the caller's side contained.
    """
    clips: List[Tuple[float, np.ndarray, Turn]] = []
    t = greeting_s
    for turn in scenario.turns:
        if turn.wait == "after_agent":
            t += agent_budget_s + (turn.gap_ms or 300) / 1000.0
        else:                      # during_agent (a barge-in) or fixed
            t += turn.offset_ms / 1000.0
        if turn.play:
            from .audio import read_wav
            from .util import to_pcm_wav
            import tempfile
            with tempfile.TemporaryDirectory() as td:
                p = to_pcm_wav(turn.play, Path(td) / "p.wav", sample_rate=sr)
                _, d = read_wav(p)
            audio = d.reshape(-1).astype(np.float32)
        elif turn.say:
            audio = tts.render(turn.say, sr, turn.voice, turn.rate, turn.volume)
        else:
            continue
        clips.append((t, audio, turn))
        t += len(audio) / sr

    total = int((t + 3.0) * sr)
    out = np.zeros(max(total, sr), np.float32)
    timeline: List[Dict[str, Any]] = []
    for start, audio, turn in clips:
        i = int(start * sr)
        out[i:i + len(audio)] += audio
        timeline.append({
            "start": round(start, 3),
            "end": round(start + len(audio) / sr, 3),
            "text": turn.say or f"<audio {turn.play}>",
            "wait": turn.wait,
            "barge_in": turn.is_barge_in,
        })
    return out, timeline


def bake(scenario: Scenario, out_dir: "str | Path", agent_budget_s: float = 6.0,
         seed: int = 0, sr: int = SR, greeting_s: float = 4.0) -> Dict[str, Any]:
    """Render, impair, and write <id>.wav + <id>.ref.wav + <id>.timeline.json.

    The clean reference is kept because it is what the recording gets aligned
    against at ingest - correlating against the impaired copy would be weaker,
    and against a noisy one weaker still.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    clean, timeline = render_scenario(scenario, agent_budget_s,
                                      greeting_s=greeting_s, sr=sr)

    cond = dict(scenario.condition or {})
    played = impair.apply_condition(clean, sr, cond, seed=seed) if cond else clean

    wav = out_dir / f"{scenario.id}.wav"
    ref = out_dir / f"{scenario.id}.ref.wav"
    _write(wav, played, sr)
    _write(ref, clean, sr)

    meta = {
        "scenario": scenario.id,
        "name": scenario.name,
        "condition": cond,
        "condition_label": describe(cond),
        "duration_s": round(len(played) / sr, 2),
        "sample_rate": sr,
        "agent_budget_s": agent_budget_s,
        "greeting_s": greeting_s,
        "seed": seed,
        "audio": wav.name,
        "reference": ref.name,
        "timeline": timeline,
    }
    with open(out_dir / f"{scenario.id}.timeline.json", "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)
        fh.write("\n")
    return meta


def describe(cond: Dict[str, Any]) -> str:
    """A short human label for an impairment condition."""
    if not cond:
        return "clean"
    bits = []
    if cond.get("noise") and cond["noise"] != "none":
        bits.append(f"{cond['noise']} @ {cond.get('snr_db', '?')}dB SNR")
    if cond.get("codec"):
        bits.append(cond["codec"])
    if cond.get("packet_loss"):
        bits.append(f"{float(cond['packet_loss']) * 100:.0f}% loss"
                    + ("" if cond.get("conceal", True) else ", unconcealed"))
    if cond.get("jitter"):
        bits.append(f"jitter {float(cond['jitter']) * 100:.0f}%")
    if cond.get("dropouts"):
        bits.append(f"{cond['dropouts']} dropout(s)")
    if cond.get("clip_db"):
        bits.append(f"clipped {cond['clip_db']}dB")
    return ", ".join(bits) or "clean"


def _write(path: Path, x: np.ndarray, sr: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
