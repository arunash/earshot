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

# Below this, an alignment is a coincidence rather than a match. Metrics that
# depend on it are withheld rather than published - a benchmark that reports a
# wrong number is worse than one that reports none.
ALIGN_MIN_SIGMA = 5.0

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
    min_response_ms=150.0,  # nothing answers a phone call faster than this; a
                            # shorter "gap" is a misalignment or an overlap
                            # artifact, not a response
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


def _envelope(x: np.ndarray, sr: int, env_hz: float = 100.0,
              onset: bool = True) -> np.ndarray:
    """Onset-emphasized energy envelope - what alignment correlates on.

    A plain log-energy envelope fails under additive noise: at low SNR the floor
    rises until log(speech+noise) is nearly log(noise) everywhere and the
    contrast the correlation depends on is gone. Subtracting a running baseline
    and keeping only the positive part leaves the ONSETS, which stay put no
    matter how high the floor goes - the moments speech starts are still the
    moments energy jumps.
    """
    hop = max(1, int(sr / env_hz))
    n = len(x) // hop
    if n < 2:
        return np.zeros(0, np.float32)
    e = np.sqrt((x[:n * hop].reshape(n, hop).astype(np.float64) ** 2).mean(axis=1))
    e = np.log(np.maximum(e, 1e-8))
    if not onset:
        return e.astype(np.float32)
    w = max(3, int(env_hz * 0.5))               # 500ms running baseline
    pad = np.pad(e, (w // 2, w // 2), mode="edge")
    base = np.convolve(pad, np.ones(w) / w, mode="valid")[:len(e)]
    return np.maximum(e - base, 0.0).astype(np.float32)


def locate_reference(ref: np.ndarray, signal: np.ndarray, sr: int,
                     env_hz: float = 100.0) -> Tuple[float, float]:
    """Find where a known clean rendering sits inside a recorded channel.

    Correlates ENERGY ENVELOPES rather than waveforms. That is the whole trick:
    a codec, 20% packet loss and a cafe at 0dB SNR destroy waveform similarity
    but barely touch the coarse shape of where speech is and is not - so the
    alignment holds at impairment levels where a sample-domain correlation has
    long since failed.

    Returns (offset_seconds, confidence) where confidence is the correlation
    peak in standard deviations above the rest of the surface. Below about 4
    the alignment should not be trusted.
    """
    a, b = _envelope(ref, sr, env_hz), _envelope(signal, sr, env_hz)
    onset = True
    if a.size and float(a.std()) < 1e-6:
        onset = False
        a, b = (_envelope(ref, sr, env_hz, onset=False),
                _envelope(signal, sr, env_hz, onset=False))
    if len(a) < 4 or len(b) < 4:
        return 0.0, 0.0
    if not onset:
        # A plain log-energy envelope has a large DC term; remove it or the
        # correlation is dominated by overlap length rather than by content.
        a = a - a.mean()
        b = b - b.mean()
    # An onset envelope is already baseline-removed and non-negative. Centring
    # it turns leading silence into a long negative block that pulls the peak
    # off by however much silence the rendering starts with - which for a baked
    # scenario is the whole greeting pause.
    a = a / (a.std() or 1.0)
    b = b / (b.std() or 1.0)
    n = 1 << (len(a) + len(b)).bit_length()
    corr = np.fft.irfft(np.fft.rfft(b, n) * np.conj(np.fft.rfft(a, n)), n)
    search = corr[:max(1, len(b))]
    k = int(np.argmax(search))
    peak = float(search[k])
    bg = float(search.std()) or 1e-9
    return k / env_hz, peak / bg


def detect_agent_channel(data: np.ndarray, sr: int, **kw) -> Tuple[int, float]:
    """Work out which channel of a dual recording is the agent under test.

    The agent answers the phone; the harness (or a human tester) waits for the
    greeting before speaking. So the channel that speaks FIRST is the agent.

    Getting this backwards silently inverts every metric in the report - response
    latency becomes the harness's own scripted pauses, and barge-ins get
    attributed to the wrong party - so it is detected rather than assumed.
    Returns (channel, margin_seconds); a small margin means low confidence.
    """
    onsets = []
    for ch in range(data.shape[1]):
        segs = vad_segments(data[:, ch], sr, **kw)
        onsets.append(segs[0].start if segs else float("inf"))
    if all(o == float("inf") for o in onsets):
        return 0, 0.0
    ch = int(np.argmin(onsets))
    others = [o for i, o in enumerate(onsets) if i != ch]
    margin = (min(others) - onsets[ch]) if others else 0.0
    return ch, float(margin if margin != float("inf") else 0.0)


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

    agent_channel: Optional[int] = None
    agent_channel_margin_s: Optional[float] = None
    caller_from_timeline: bool = False
    alignment_offset_s: Optional[float] = None
    alignment_confidence: Optional[float] = None
    turns_offered: Optional[int] = None
    turns_answered: Optional[int] = None
    response_rate: Optional[float] = None
    false_triggers: Optional[int] = None

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
            agent_channel: "int | str" = "auto", agent_first: bool = True,
            reference: "str | Path | None" = None,
            timeline: "Optional[List[Dict]]" = None,
            **kw) -> CallMetrics:
    """Compute every objective metric for one call recording.

    When `reference` and `timeline` are supplied - which they are for any baked
    scenario - the caller's turns come from ground truth rather than from a
    voice-activity detector, after aligning the reference against the recording.
    That is what keeps latency and barge-in measurable once the caller's channel
    is full of babble.
    """
    o = dict(DEFAULTS, **{k: v for k, v in kw.items() if k in DEFAULTS})
    sr, data, stereo = load_call(path, workdir)
    duration = data.shape[0] / sr
    notes: List[str] = []
    margin = None

    if stereo:
        if reference is not None and agent_channel == "auto":
            # With a known reference there is no need to guess. Correlate it
            # against BOTH channels; the one it matches is ours, and the other
            # is the agent. This matters because the speaks-first heuristic
            # below is defeated by exactly the conditions this tool exists to
            # test: a channel carrying continuous babble has energy from t=0,
            # so the noisy CALLER leg looks like the party that answered.
            _sr, _ref = read_wav(to_pcm_wav(
                reference, Path(workdir) / (Path(reference).stem + ".probe.wav"),
                sample_rate=sr))
            _ref = _ref.reshape(-1)
            scores = [locate_reference(_ref, data[:, ch], sr)
                      for ch in range(data.shape[1])]
            ch_caller = max(range(len(scores)), key=lambda i: scores[i][1])
            ch_agent = 1 - ch_caller
            margin = scores[ch_caller][1] - scores[ch_agent][1]
            if margin < 0.5:
                notes.append(
                    f"Channel assignment is ambiguous: the reference matches "
                    f"both legs about equally ({scores[0][1]:.1f} vs "
                    f"{scores[1][1]:.1f} sigma). Heavy crosstalk is the usual "
                    f"cause. Force it with --agent-channel if the transcript "
                    f"looks swapped.")
        elif agent_channel == "auto":
            ch_agent, margin = detect_agent_channel(data, sr, **o)
            if margin < 0.75:
                notes.append(
                    f"Agent channel detected as {ch_agent}, but only by {margin:.2f}s "
                    f"- both parties start speaking at nearly the same time. If the "
                    f"transcript has the speakers swapped, force it with "
                    f"--agent-channel {1 - ch_agent}.")
        else:
            ch_agent = int(agent_channel) % data.shape[1]
        ch_caller = 1 - ch_agent
        agent_segs = [Segment(s.start, s.end, AGENT)
                      for s in vad_segments(data[:, ch_agent], sr, **o)]

        if reference is not None and timeline:
            # `reference` is the file that was actually PLAYED, impairment and
            # all. Correlating the clean render instead fails at exactly the
            # SNRs this machinery exists to support.
            ref_sr, ref_data = read_wav(to_pcm_wav(
                reference, Path(workdir) / (Path(reference).stem + ".ref16.wav"),
                sample_rate=sr))
            offset, align_conf = locate_reference(
                ref_data.reshape(-1), data[:, ch_caller], sr)
            caller_segs = [Segment(t["start"] + offset, t["end"] + offset, CALLER)
                           for t in timeline]
            notes.append(
                f"Caller turns taken from the baked timeline, aligned at "
                f"{offset:.2f}s (confidence {align_conf:.1f} sigma). Latency and "
                f"barge-in are measured against ground truth, not a detector.")
            aligned = align_conf >= ALIGN_MIN_SIGMA
            if not aligned:
                caller_segs = []
                notes.append(
                    f"ALIGNMENT FAILED ({align_conf:.1f} sigma, need "
                    f"{ALIGN_MIN_SIGMA:.1f}). Latency, response rate and barge-in "
                    f"are NOT reported for this call rather than reported wrongly. "
                    f"Usual cause: the call ended before most of the audio played, "
                    f"so there was too little of the reference present to lock on. "
                    f"A longer script with more speech onsets fixes it.")
        else:
            caller_segs = [Segment(s.start, s.end, CALLER)
                           for s in vad_segments(data[:, ch_caller], sr, **o)]
            aligned = False

        segs = sorted(agent_segs + caller_segs, key=lambda s: s.start)
        confidence = None
        measurable = True
    else:
        aligned = False
        offset = align_conf = None
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
        if o["min_response_ms"] <= gap_ms <= 15000.0:
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

    # --- with a known timeline: did it answer, and did it answer the noise? ---
    offered = answered = triggers = None
    if aligned and caller_segs:
        window = 8.0
        offered = len(caller_segs)
        answered = 0
        answering = set()
        for idx, c in enumerate(caller_segs):
            nxt = caller_segs[idx + 1].start if idx + 1 < len(caller_segs) else 1e9
            hit = next((a for a in agent_segs
                        if c.end <= a.start < min(c.end + window, nxt)), None)
            if hit is not None:
                answered += 1
                answering.add(id(hit))
        # An agent turn that is neither a reply to anything nor an interruption
        # of anything is the agent talking to the noise. The opening greeting -
        # everything before the first caller turn - is excluded.
        first = caller_segs[0].start
        triggers = 0
        for a in agent_segs:
            if a.start <= first or id(a) in answering:
                continue
            if any(c.start <= a.start <= c.end for c in caller_segs):
                continue
            if any(c.end <= a.start <= c.end + window for c in caller_segs):
                continue
            triggers += 1

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
        agent_channel=ch_agent if stereo else None,
        agent_channel_margin_s=None if margin is None else round(margin, 2),
        turns_offered=offered,
        turns_answered=answered,
        response_rate=(round(answered / offered, 3) if offered else None),
        false_triggers=triggers,
        caller_from_timeline=bool(aligned),
        alignment_offset_s=round(offset, 3) if aligned else None,
        alignment_confidence=round(align_conf, 2) if aligned else None,
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
    offered = sum(c.turns_offered or 0 for c in calls)
    answered = sum(c.turns_answered or 0 for c in calls)
    trig = [c.false_triggers for c in calls if c.false_triggers is not None]
    return {
        "calls": len(calls),
        "total_duration_s": round(sum(c.duration_s for c in calls), 1),
        "response_rate": round(answered / offered, 3) if offered else None,
        "turns_offered": offered or None,
        "false_triggers": sum(trig) if trig else None,
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
