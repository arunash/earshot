"""Speaker-attributed transcription.

Each leg of the call is transcribed SEPARATELY and then merged by timestamp.
That is why Earshot wants dual-channel recordings: it gives perfect speaker
attribution without a diarizer, which is the usual source of eval noise.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from .audio import AGENT, CALLER, Segment, load_call
from .util import have, read_json, warn, to_pcm_wav

import wave


def _write_channel(data: np.ndarray, sr: int, ch: int, path: Path) -> Path:
    mono = data[:, ch] if data.ndim > 1 and data.shape[1] > ch else data.reshape(-1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(mono, -1, 1) * 32767).astype("<i2").tobytes())
    return path


# ------------------------------------------------------------ whisper.cpp --


def _whisper_cpp_model() -> Optional[str]:
    env = os.environ.get("WHISPER_CPP_MODEL")
    if env and Path(env).exists():
        return env
    for cand in (
        Path.home() / ".cache/whisper.cpp",
        Path.home() / "cpa-live/models",
        Path("/opt/homebrew/share/whisper.cpp/models"),
        Path.cwd() / "models",
    ):
        if cand.is_dir():
            hits = sorted(cand.glob("ggml-*.bin"))
            if hits:
                return str(hits[0])
    return None


def _whisper_cpp(wav: Path, model: str, language: str = "en") -> List[Dict]:
    with tempfile.TemporaryDirectory() as td:
        prefix = Path(td) / "out"
        cmd = ["whisper-cli", "-m", model, "-f", str(wav), "-oj",
               "-of", str(prefix), "-np", "-l", language]
        subprocess.run(cmd, check=True, capture_output=True)
        data = read_json(str(prefix) + ".json")
    out = []
    for seg in data.get("transcription", []):
        off = seg.get("offsets", {})
        out.append({
            "start": off.get("from", 0) / 1000.0,
            "end": off.get("to", 0) / 1000.0,
            "text": seg.get("text", "").strip(),
        })
    return out


# ---------------------------------------------------------- faster-whisper --


def _faster_whisper(wav: Path, model_size: str = "small.en") -> List[Dict]:
    from faster_whisper import WhisperModel  # type: ignore

    model = WhisperModel(model_size, device="auto", compute_type="int8")
    segments, _ = model.transcribe(str(wav), vad_filter=False)
    return [{"start": s.start, "end": s.end, "text": s.text.strip()} for s in segments]


# --------------------------------------------------------------- dispatch --


def available(engine: str = "auto") -> Optional[str]:
    """Which transcription engine will actually be used, or None."""
    if engine in ("none", "off"):
        return None
    if engine in ("auto", "whisper-cpp") and have("whisper-cli") and _whisper_cpp_model():
        return "whisper-cpp"
    if engine in ("auto", "faster-whisper"):
        try:
            import faster_whisper  # noqa: F401
            return "faster-whisper"
        except ImportError:
            pass
    return None


def transcribe_call(recording: "str | Path", workdir: "str | Path",
                    agent_channel: int = 1, engine: str = "auto",
                    segments: Optional[List[Segment]] = None,
                    language: str = "en") -> Dict:
    """Return {'engine':..., 'turns':[{speaker,start,end,text}], 'text': "..."}.

    On a stereo recording each channel is transcribed on its own. On a mono
    recording there is one pass and speaker labels come from the analyzer's
    clustering, matched by timestamp overlap - less reliable, and flagged as such.
    """
    eng = available(engine)
    if eng is None:
        return {"engine": None, "turns": [], "text": "",
                "note": "No transcription engine available. Install whisper.cpp "
                        "(brew install whisper-cpp) or `pip install faster-whisper`, "
                        "or paste transcripts into the run directory yourself."}

    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    sr, data, stereo = load_call(recording, workdir)
    model = _whisper_cpp_model() if eng == "whisper-cpp" else None

    def run(wav: Path) -> List[Dict]:
        if eng == "whisper-cpp":
            return _whisper_cpp(wav, model, language=language)
        return _faster_whisper(wav)

    turns: List[Dict] = []
    if stereo:
        ch_a = agent_channel % data.shape[1]
        for ch, who in ((ch_a, AGENT), (1 - ch_a, CALLER)):
            wav = _write_channel(data, sr, ch, workdir / f"_leg_{who}.wav")
            for t in run(wav):
                if t["text"]:
                    turns.append(dict(t, speaker=who))
            wav.unlink(missing_ok=True)
    else:
        wav = _write_channel(data.mean(axis=1), sr, 0, workdir / "_leg_mono.wav")
        raw = run(wav)
        wav.unlink(missing_ok=True)
        for t in raw:
            who = "unknown"
            if segments:
                mid = (t["start"] + t["end"]) / 2.0
                hit = next((s for s in segments if s.start <= mid <= s.end), None)
                if hit:
                    who = hit.speaker
            if t["text"]:
                turns.append(dict(t, speaker=who))

    turns.sort(key=lambda t: t["start"])
    text = "\n".join(
        f"[{t['start']:7.2f}] {t['speaker'].upper():6s}: {t['text']}" for t in turns
    )
    out = {"engine": eng, "stereo": stereo, "turns": turns, "text": text}
    if not stereo:
        out["note"] = ("Mono source: speaker labels are inferred by clustering and "
                       "may be wrong. Verify before quoting them as evidence.")
    return out
