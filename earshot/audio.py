"""Objective call metrics from a recording.

Everything Earshot claims as a *number* is computed here, from the waveform.
Nothing in this module asks a model for an opinion.

The important design decision: barge-in is measured POST HOC from the recording,
not from when the harness decided to interrupt. That means open-loop interruption
injection (Twilio TwiML, or you talking over it yourself) is good enough - the
interruption just has to land somewhere inside the agent's turn. Where exactly it
landed, and how long the agent kept talking afterwards, are read off the waveform
to within one 10ms frame.
"""

from __future__ import annotations

import math
import wave
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .util import ffprobe_channels, to_pcm_wav

CALLER = "caller"
AGENT = "agent"

# Tunables. Exposed on the CLI so a noisy line can be re-analyzed without edits.
DEFAULTS = dict(
    frame_ms=20.0,
    hop_ms=10.0,
    noise_percentile=20.0,
    threshold_db=10.0,      # how far above the noise floor counts as speech
    merge_gap_ms=200.0,     # gaps shorter than this stay inside one segment
    min_speech_ms=120.0,    # shorter runs are treated as clicks, not speech
    yield_timeout_ms=1200.0,  # past ~1s of talking over, a caller assumes
                              # it never heard them - so that is "did not yield"
    dead_air_ms=3000.0,
    backchannel_ms=900.0,   # caller segments shorter than this may be backchannel
)


# ----------------------------------------------------------------- loading --


def read_wav(path: "str | Path") -> Tuple[int, np.ndarray]:
    """Return (sample_rate, samples[n, channels]) as float32 in [-1, 1]."""
    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        nch = wf.getnchannels()
        width = wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())
    if width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"unsupported sample width {width}")
    return sr, data.reshape(-1, nch)


def load_call(path: "str | Path", workdir: "str | Path") -> Tuple[int, np.ndarray, bool]:
    """Normalize a recording to 16k PCM and return (sr, samples, is_stereo)."""
    path = Path(path)
    stereo = ffprobe_channels(path) >= 2
    norm = Path(workdir) / (path.stem + ".norm.wav")
    to_pcm_wav(path, norm, sample_rate=16000)
    sr, data = read_wav(norm)
    return sr, data, stereo and data.shape[1] >= 2


# --------------------------------------------------------------------- vad --


def frame_db(x: np.ndarray, sr: int, frame_ms: float, hop_ms: float
             ) -> Tuple[np.ndarray, np.ndarray]:
    """Per-frame RMS in dBFS, plus each frame's start time in seconds."""
    n = max(1, int(round(sr * frame_ms / 1000.0)))
    hop = max(1, int(round(sr * hop_ms / 1000.0)))
    if len(x) < n:
        return np.zeros(0), np.zeros(0)
    count = 1 + (len(x) - n) // hop
    idx = np.arange(n)[None, :] + hop * np.arange(count)[:, None]
    frames = x[idx]
    rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
    db = 20.0 * np.log10(np.maximum(rms, 1e-10))
    times = np.arange(count) * hop / sr
    return times, db.astype(np.float32)


@dataclass
class Segment:
    start: float
    end: float
    speaker: str = ""

    @property
    def dur(self) -> float:
        return self.end - self.start


def vad_segments(x: np.ndarray, sr: int, **kw) -> List[Segment]:
    """Energy-gated speech segments with an adaptive noise floor.

    Segment ENDS are the last frame genuinely above threshold - the merge gap
    joins runs but never extends an end - so barge-in stop times are not
    inflated by the hangover.
    """
    o = dict(DEFAULTS, **kw)
    times, db = frame_db(x, sr, o["frame_ms"], o["hop_ms"])
    if len(db) == 0:
        return []

    floor = float(np.percentile(db, o["noise_percentile"]))
    peak = float(np.percentile(db, 97.0))
    thr = floor + o["threshold_db"]
    # On a very hot or very quiet line, keep the gate strictly between the two.
    if peak - floor < o["threshold_db"] + 6.0:
        thr = floor + max(4.0, (peak - floor) * 0.5)
    thr = min(thr, peak - 4.0)

    active = db > thr
    if not active.any():
        return []

    frame_s = o["frame_ms"] / 1000.0
    merge_gap = o["merge_gap_ms"] / 1000.0
    min_speech = o["min_speech_ms"] / 1000.0

    segs: List[Segment] = []
    start = None
    last_active = None
    for i, a in enumerate(active):
        t = float(times[i])
        if a:
            if start is None:
                start = t
            last_active = t
        elif start is not None and last_active is not None:
            if t - (last_active + frame_s) > merge_gap:
                segs.append(Segment(start, last_active + frame_s))
                start = None
                last_active = None
    if start is not None and last_active is not None:
        segs.append(Segment(start, last_active + frame_s))

    return [s for s in segs if s.dur >= min_speech]


# ------------------------------------------------- mono speaker separation --


def _band_features(x: np.ndarray, sr: int, seg: Segment) -> np.ndarray:
    """A crude spectral signature of one segment: log energy in 16 log-spaced bands."""
    a, b = int(seg.start * sr), int(seg.end * sr)
    chunk = x[a:b]
    if len(chunk) < 512:
        return np.zeros(16, dtype=np.float32)
    n = 512
    hop = 256
    count = 1 + (len(chunk) - n) // hop
    idx = np.arange(n)[None, :] + hop * np.arange(count)[:, None]
    win = np.hanning(n)
    spec = np.abs(np.fft.rfft(chunk[idx] * win, axis=1)) ** 2
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    edges = np.geomspace(100.0, min(7800.0, sr / 2 - 100), 17)
    feats = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (freqs >= lo) & (freqs < hi)
        feats.append(np.log(np.maximum(spec[:, m].mean(), 1e-12)))
    v = np.asarray(feats, dtype=np.float32)
    return v - v.mean()


def _kmeans2(vecs: np.ndarray, iters: int = 40) -> Tuple[np.ndarray, float]:
    """Two-cluster k-means. Returns (labels, separation) where separation is the
    ratio of between-cluster to within-cluster distance - a confidence proxy."""
    n = len(vecs)
    if n < 2:
        return np.zeros(n, dtype=int), 0.0
    d = np.linalg.norm(vecs[:, None, :] - vecs[None, :, :], axis=-1)
    i, j = np.unravel_index(np.argmax(d), d.shape)
    cent = vecs[[i, j]].copy()
    labels = np.zeros(n, dtype=int)
    for _ in range(iters):
        dist = np.linalg.norm(vecs[:, None, :] - cent[None, :, :], axis=-1)
        new = dist.argmin(axis=1)
        if (new == labels).all():
            break
        labels = new
        for k in (0, 1):
            if (labels == k).any():
                cent[k] = vecs[labels == k].mean(axis=0)
    between = float(np.linalg.norm(cent[0] - cent[1]))
    within = float(np.mean([
        np.linalg.norm(vecs[labels == k] - cent[k], axis=1).mean()
        for k in (0, 1) if (labels == k).any()
    ]) or 1e-9)
    return labels, between / max(within, 1e-9)


def label_mono(x: np.ndarray, sr: int, segs: Sequence[Segment],
               agent_first: bool = True) -> Tuple[List[Segment], float]:
    """Assign speakers on a single-channel recording by clustering voices.

    The agent normally answers the phone, so the cluster owning the first segment
    is the agent unless told otherwise. Returns (segments, separation_score).
    """
    if not segs:
        return [], 0.0
    vecs = np.stack([_band_features(x, sr, s) for s in segs])
    labels, sep = _kmeans2(vecs)
    agent_label = labels[0] if agent_first else 1 - labels[0]
    out = []
    for s, lab in zip(segs, labels):
        out.append(Segment(s.start, s.end, AGENT if lab == agent_label else CALLER))
    return out, sep


# ----------------------------------------------------------------- metrics --


def _overlapping(a: Segment, b: Segment) -> bool:
    return a.start < b.end and b.start < a.end


def _pct(values: Sequence[float], p: float) -> Optional[float]:
    return float(np.percentile(values, p)) if len(values) else None


@dataclass
class BargeIn:
    at: float             # when the caller started talking over the agent
    stop_ms: float        # how long the agent kept talking after that
    yielded: bool
    agent_turn_start: float


@dataclass
class CallMetrics:
    source: str
    duration_s: float
    stereo: bool
    speaker_confidence: Optional[float]      # None when channels were separate
    n_caller_turns: int
    n_agent_turns: int

    response_latency_ms: List[float] = field(default_factory=list)
    latency_median_ms: Optional[float] = None
    latency_p90_ms: Optional[float] = None
    latency_max_ms: Optional[float] = None
    latency_iqr_ms: Optional[float] = None   # the jitter number that matters

    barge_ins: List[BargeIn] = field(default_factory=list)
    barge_in_stop_median_ms: Optional[float] = None
    barge_in_stop_p90_ms: Optional[float] = None
    barge_in_yield_rate: Optional[float] = None
    barge_in_measurable: bool = True

    agent_interruptions: int = 0             # it talked over the caller
    backchannel_stops: int = 0               # it stopped for a sub-1s utterance
    overlap_s: float = 0.0
    dead_air_events: List[float] = field(default_factory=list)
    longest_dead_air_s: Optional[float] = None

    agent_talk_ratio: Optional[float] = None
    mean_agent_turn_s: Optional[float] = None
    longest_agent_turn_s: Optional[float] = None

    notes: List[str] = field(default_factory=list)
    segments: List[Segment] = field(default_factory=list)

    def to_dict(self, include_segments: bool = False) -> Dict:
        d = asdict(self)
        d["barge_ins"] = [asdict(b) for b in self.barge_ins]
        if include_segments:
            d["segments"] = [asdict(s) for s in self.segments]
        else:
            d.pop("segments", None)
        d["response_latency_ms"] = [round(v, 1) for v in self.response_latency_ms]
        return d


def analyze(path: "str | Path", workdir: "str | Path",
            agent_channel: int = 1, agent_first: bool = True, **kw) -> CallMetrics:
    """Compute every objective metric for one call recording."""
    o = dict(DEFAULTS, **{k: v for k, v in kw.items() if k in DEFAULTS})
    sr, data, stereo = load_call(path, workdir)
    duration = data.shape[0] / sr
    notes: List[str] = []

    if stereo:
        ch_agent = int(agent_channel) % data.shape[1]
        ch_caller = 1 - ch_agent
        agent_segs = [Segment(s.start, s.end, AGENT)
                      for s in vad_segments(data[:, ch_agent], sr, **o)]
        caller_segs = [Segment(s.start, s.end, CALLER)
                       for s in vad_segments(data[:, ch_caller], sr, **o)]
        segs = sorted(agent_segs + caller_segs, key=lambda s: s.start)
        confidence = None
        measurable = True
    else:
        mono = data.mean(axis=1)
        raw = vad_segments(mono, sr, **o)
        segs, confidence = label_mono(mono, sr, raw, agent_first=agent_first)
        agent_segs = [s for s in segs if s.speaker == AGENT]
        caller_segs = [s for s in segs if s.speaker == CALLER]
        measurable = False
        notes.append(
            "Mono recording: the two voices are summed into one signal, so overlap "
            "cannot be observed and barge-in stop latency is NOT measurable. Response "
            "latency, dead air and talk ratio are still valid. Record in dual channel "
            "for the full metric set."
        )
        if confidence is not None and confidence < 1.2:
            notes.append(
                f"Speaker separation is weak (score {confidence:.2f}); treat the "
                "caller/agent split on this call as unreliable."
            )

    # --- response latency: caller stops, agent starts, nothing in between -----
    latencies: List[float] = []
    for c in caller_segs:
        nxt = [a for a in agent_segs if a.start >= c.end]
        if not nxt:
            continue
        a = min(nxt, key=lambda s: s.start)
        if any(c.end < c2.start < a.start for c2 in caller_segs):
            continue
        gap_ms = (a.start - c.end) * 1000.0
        if 0.0 <= gap_ms <= 15000.0:
            latencies.append(gap_ms)

    # --- barge-in: caller starts while the agent is already talking ----------
    barge: List[BargeIn] = []
    backchannel_stops = 0
    if measurable:
        for c in caller_segs:
            host = next((a for a in agent_segs if a.start < c.start < a.end), None)
            if host is None:
                continue
            stop_ms = (host.end - c.start) * 1000.0
            yielded = stop_ms <= o["yield_timeout_ms"]
            barge.append(BargeIn(round(c.start, 3), round(stop_ms, 1), yielded,
                                 round(host.start, 3)))
            if yielded and c.dur * 1000.0 < o["backchannel_ms"]:
                backchannel_stops += 1

    # --- the agent talking over the caller -----------------------------------
    interruptions = 0
    if measurable:
        for a in agent_segs:
            host = next((c for c in caller_segs if c.start < a.start < c.end), None)
            if host is not None and (host.end - a.start) > 0.4:
                interruptions += 1

    # --- overlap, dead air, talk share ---------------------------------------
    overlap = 0.0
    if measurable:
        for a in agent_segs:
            for c in caller_segs:
                if _overlapping(a, c):
                    overlap += min(a.end, c.end) - max(a.start, c.start)

    ordered = sorted(segs, key=lambda s: s.start)
    dead: List[float] = []
    cursor = 0.0
    for s in ordered:
        if s.start - cursor > o["dead_air_ms"] / 1000.0:
            dead.append(round(s.start - cursor, 2))
        cursor = max(cursor, s.end)

    agent_time = sum(s.dur for s in agent_segs)
    caller_time = sum(s.dur for s in caller_segs)
    speech = agent_time + caller_time

    stops = [b.stop_ms for b in barge]
    m = CallMetrics(
        source=str(path),
        duration_s=round(duration, 2),
        stereo=stereo,
        speaker_confidence=None if confidence is None else round(confidence, 2),
        n_caller_turns=len(caller_segs),
        n_agent_turns=len(agent_segs),
        response_latency_ms=latencies,
        latency_median_ms=_pct(latencies, 50),
        latency_p90_ms=_pct(latencies, 90),
        latency_max_ms=max(latencies) if latencies else None,
        latency_iqr_ms=(
            None if len(latencies) < 4
            else round(_pct(latencies, 75) - _pct(latencies, 25), 1)
        ),
        barge_ins=barge,
        barge_in_stop_median_ms=_pct(stops, 50),
        barge_in_stop_p90_ms=_pct(stops, 90),
        barge_in_yield_rate=(
            round(sum(1 for b in barge if b.yielded) / len(barge), 3) if barge else None
        ),
        barge_in_measurable=measurable,
        agent_interruptions=interruptions,
        backchannel_stops=backchannel_stops,
        overlap_s=round(overlap, 2),
        dead_air_events=dead,
        longest_dead_air_s=max(dead) if dead else None,
        agent_talk_ratio=round(agent_time / speech, 3) if speech else None,
        mean_agent_turn_s=round(agent_time / len(agent_segs), 2) if agent_segs else None,
        longest_agent_turn_s=round(max((s.dur for s in agent_segs), default=0.0), 2),
        notes=notes,
        segments=ordered,
    )
    for k in ("latency_median_ms", "latency_p90_ms", "latency_max_ms",
              "barge_in_stop_median_ms", "barge_in_stop_p90_ms"):
        v = getattr(m, k)
        if v is not None:
            setattr(m, k, round(v, 1))
    return m


def aggregate(calls: Sequence[CallMetrics]) -> Dict:
    """Roll several calls of one system into the numbers that go on the card."""
    lat = [v for c in calls for v in c.response_latency_ms]
    stops = [b.stop_ms for c in calls if c.barge_in_measurable for b in c.barge_ins]
    yields = [b.yielded for c in calls if c.barge_in_measurable for b in c.barge_ins]
    ratios = [c.agent_talk_ratio for c in calls if c.agent_talk_ratio is not None]
    turns = [c.mean_agent_turn_s for c in calls if c.mean_agent_turn_s is not None]
    return {
        "calls": len(calls),
        "total_duration_s": round(sum(c.duration_s for c in calls), 1),
        "latency": {
            "n": len(lat),
            "median_ms": _pct(lat, 50) and round(_pct(lat, 50), 1),
            "p90_ms": _pct(lat, 90) and round(_pct(lat, 90), 1),
            "max_ms": round(max(lat), 1) if lat else None,
            "iqr_ms": (round(_pct(lat, 75) - _pct(lat, 25), 1)
                       if len(lat) >= 4 else None),
        },
        "barge_in": {
            "n": len(stops),
            "measurable": any(c.barge_in_measurable for c in calls),
            "stop_median_ms": _pct(stops, 50) and round(_pct(stops, 50), 1),
            "stop_p90_ms": _pct(stops, 90) and round(_pct(stops, 90), 1),
            "yield_rate": round(sum(yields) / len(yields), 3) if yields else None,
            "false_stops": sum(c.backchannel_stops for c in calls),
        },
        "agent_interruptions": sum(c.agent_interruptions for c in calls),
        "overlap_s": round(sum(c.overlap_s for c in calls), 2),
        "dead_air_events": sum(len(c.dead_air_events) for c in calls),
        "longest_dead_air_s": max(
            (c.longest_dead_air_s for c in calls if c.longest_dead_air_s), default=None
        ),
        "agent_talk_ratio": round(sum(ratios) / len(ratios), 3) if ratios else None,
        "mean_agent_turn_s": round(sum(turns) / len(turns), 2) if turns else None,
    }


def aggregate_by_expectation(pairs: Sequence[Tuple[str, CallMetrics]]) -> Dict:
    """Split barge-in behaviour by what the scenario was designed to provoke.

    `yield` scenarios: a real interruption. Every event should end the agent turn
    fast, so we report stop latency and the rate at which it yielded at all.

    `hold` scenarios: a cough, a backchannel, a third party. Every event that
    ended the agent's turn is a FALSE STOP - the failure real callers hate most,
    and the one almost nobody tests for.
    """
    out: Dict[str, Dict] = {}
    for expectation in ("yield", "hold"):
        calls = [m for e, m in pairs if e == expectation and m.barge_in_measurable]
        events = [b for m in calls for b in m.barge_ins]
        stops = [b.stop_ms for b in events]
        yielded = [b for b in events if b.yielded]
        entry = {
            "calls": len(calls),
            "interruptions_detected": len(events),
            "stop_median_ms": _pct(stops, 50) and round(_pct(stops, 50), 1),
            "stop_p90_ms": _pct(stops, 90) and round(_pct(stops, 90), 1),
        }
        if expectation == "yield":
            entry["yield_rate"] = (
                round(len(yielded) / len(events), 3) if events else None
            )
            entry["failed_to_yield"] = len(events) - len(yielded)
        else:
            entry["false_stops"] = len(yielded)
            entry["false_stop_rate"] = (
                round(len(yielded) / len(events), 3) if events else None
            )
        out[expectation] = entry
    return out
