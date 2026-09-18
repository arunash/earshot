"""Calibration check: synthesize a call with known ground truth, measure it,
and assert the analyzer recovers the truth.

Run `earshot selftest` before trusting a report. If the numbers in a report ever
look implausible, this tells you whether the analyzer or the recording is at fault.
"""

from __future__ import annotations

import tempfile
import wave
from pathlib import Path
from typing import List, Tuple

import numpy as np

from .audio import analyze
from .util import bold, green, red, dim

SR = 16000


def _voice(dur: float, f0: float, seed: int) -> np.ndarray:
    """A crude voiced-speech stand-in: harmonic stack under a syllabic envelope."""
    rng = np.random.default_rng(seed)
    n = int(dur * SR)
    t = np.arange(n) / SR
    sig = sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, 12))
    env = 0.5 + 0.5 * np.sin(2 * np.pi * 4.5 * t + rng.random() * 6)
    return (sig * env * 0.28).astype(np.float32)


# (start, duration) per leg. Deliberately includes a barge-in and a dead-air gap.
AGENT_TURNS: List[Tuple[float, float]] = [
    (0.50, 2.50),    # greeting
    (6.35, 1.95),    # answer, cut short by the barge-in below at 8.00
    (11.60, 2.00),
    (17.00, 1.50),
    (23.50, 1.20),
]
CALLER_TURNS: List[Tuple[float, float]] = [
    (3.60, 2.00),
    (8.00, 3.00),    # barge-in: lands 1.65s into the agent's 6.35 turn
    (14.30, 1.50),
    (22.40, 0.80),   # follows a 3.90s dead-air gap after the agent's 17.00 turn
]

# What the analyzer should recover.
EXPECT = {
    "latencies_ms": [750.0, 600.0, 1200.0, 300.0],
    "barge_in_at": 8.00,
    "barge_in_stop_ms": 280.0,     # agent keeps talking 280ms after being cut into
    "dead_air_s": 3.90,
    "caller_turns": 4,
    "agent_turns": 5,
}
TOL_MS = 60.0      # one VAD frame of slop on each edge, plus onset ramp
TOL_S = 0.15


def _render(path: Path) -> None:
    dur = 26.0
    n = int(dur * SR)
    agent = np.zeros(n, np.float32)
    caller = np.zeros(n, np.float32)

    def put(buf, start, d, f0, seed):
        s = int(start * SR)
        x = _voice(d, f0, seed)
        buf[s:s + len(x)] += x

    for i, (s, d) in enumerate(AGENT_TURNS):
        # The second agent turn is truncated by the barge-in.
        if abs(s - 6.35) < 1e-6:
            d = (8.00 + EXPECT["barge_in_stop_ms"] / 1000.0) - s
        put(agent, s, d, 130, 10 + i)
    for i, (s, d) in enumerate(CALLER_TURNS):
        put(caller, s, d, 210, 50 + i)

    noise = np.random.default_rng(9).normal(0, 0.0015, n).astype(np.float32)
    stereo = np.clip(np.stack([caller + noise, agent + noise], axis=1), -1, 1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((stereo * 32767).astype("<i2").tobytes())


IMPAIRMENTS = [
    ("clean", {}),
    ("babble @ 6dB SNR", {"noise": "babble", "snr_db": 6}),
    ("television @ 6dB SNR", {"noise": "tv", "snr_db": 6}),
    ("road noise @ 0dB SNR", {"noise": "road", "snr_db": 0}),
    ("g726 at 16kbps", {"codec": "g726:16000"}),
    ("15% packet loss", {"packet_loss": 0.15}),
    ("30% jitter", {"jitter": 0.30}),
    ("babble 8dB + g726 + 8% loss",
     {"noise": "babble", "snr_db": 8, "codec": "g726:16000", "packet_loss": 0.08}),
]
TRUE_OFFSET = 2.90
TRUE_LATENCY_MS = 750.0


def run_impaired(verbose: bool = True) -> bool:
    """The claim this guards: once the caller's channel is full of babble, the
    measurements still hold - because the harness baked the audio and therefore
    knows the timeline, and because alignment correlates onset envelopes that
    survive impairment a waveform correlation does not.

    If this regresses, every noise and network number in every report is wrong.
    """
    import json
    import numpy as np

    from . import impair
    from .audio import read_wav, analyze
    from .bake import render_scenario
    from .battery import load as load_battery

    b = load_battery("robustness")
    scenario = b.get("N00")
    clean, timeline = render_scenario(scenario, sr=SR)

    ok_all = True
    if verbose:
        print(bold("earshot selftest - measurement under impairment"))
        print(dim(f"  true offset {TRUE_OFFSET}s, true latency "
                  f"{TRUE_LATENCY_MS:.0f}ms, {len(timeline)} turns offered"))
        print()

    with tempfile.TemporaryDirectory() as td:
        for label, cond in IMPAIRMENTS:
            played = impair.apply_condition(clean, SR, cond, seed=7) if cond else clean
            ref = Path(td) / "played.wav"
            _write_mono(ref, played)

            n = int((TRUE_OFFSET + len(played) / SR + 8) * SR)
            caller = np.zeros(n, np.float32)
            i = int(TRUE_OFFSET * SR)
            caller[i:i + len(played)] += played[:n - i]

            at = [0.4] + [t["end"] + TRUE_OFFSET + TRUE_LATENCY_MS / 1000.0
                          for t in timeline]
            agent = np.zeros(n, np.float32)
            rng = np.random.default_rng(3)
            for st in at:
                x = _voice(2.0, 125, int(st * 100))
                j = int(st * SR)
                agent[j:j + len(x)] += x
            noise = rng.normal(0, 0.002, n).astype(np.float32)
            stereo = np.clip(np.stack([agent + noise, caller + noise], axis=1), -1, 1)
            rec = Path(td) / "rec.wav"
            with wave.open(str(rec), "wb") as w:
                w.setnchannels(2)
                w.setsampwidth(2)
                w.setframerate(SR)
                w.writeframes((stereo * 32767).astype("<i2").tobytes())

            m = analyze(rec, td, agent_channel=0, reference=ref, timeline=timeline)
            lat = (float(np.median(m.response_latency_ms))
                   if m.response_latency_ms else None)
            ok = (m.alignment_offset_s is not None
                  and abs(m.alignment_offset_s - TRUE_OFFSET) <= 0.05
                  and (m.alignment_confidence or 0) >= 4.0
                  and m.turns_answered == len(timeline)
                  and lat is not None and abs(lat - TRUE_LATENCY_MS) <= 80
                  and (m.false_triggers or 0) == 0)
            ok_all &= ok
            if verbose:
                mark = green("PASS") if ok else red("FAIL")
                print(f"  {mark}  {label:<30} align {m.alignment_offset_s:>5.2f}s"
                      f" @ {m.alignment_confidence:>4.1f}\u03c3   "
                      f"answered {m.turns_answered}/{m.turns_offered}   "
                      f"latency {lat:>6,.0f}ms" if lat else f"  {mark}  {label}")
    if verbose:
        print()
        print(green("measurement survives impairment") if ok_all
              else red("IMPAIRED MEASUREMENT IS BROKEN - noise and network "
                       "numbers cannot be trusted"))
    return ok_all


def _write_mono(path: Path, x) -> None:
    import numpy as np
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())


def run(verbose: bool = True) -> bool:
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "selftest.wav"
        _render(wav)
        m = analyze(wav, td, agent_channel=1)

        checks = []

        def chk(name, got, want, tol, unit=""):
            ok = got is not None and abs(got - want) <= tol
            checks.append((name, ok, got, want, unit))
            return ok

        lat = sorted(m.response_latency_ms)
        want_lat = sorted(EXPECT["latencies_ms"])
        checks.append(("response latency count", len(lat) == len(want_lat),
                       len(lat), len(want_lat), ""))
        for i, w in enumerate(want_lat):
            g = lat[i] if i < len(lat) else None
            chk(f"  latency #{i + 1}", g, w, TOL_MS, "ms")

        b = m.barge_ins[0] if m.barge_ins else None
        checks.append(("barge-in detected", b is not None, len(m.barge_ins), 1, ""))
        if b:
            chk("  barge-in onset", b.at, EXPECT["barge_in_at"], TOL_S, "s")
            chk("  barge-in stop latency", b.stop_ms,
                EXPECT["barge_in_stop_ms"], TOL_MS, "ms")

        chk("longest dead air", m.longest_dead_air_s, EXPECT["dead_air_s"], TOL_S, "s")
        checks.append(("caller turns", m.n_caller_turns == EXPECT["caller_turns"],
                       m.n_caller_turns, EXPECT["caller_turns"], ""))
        checks.append(("agent turns", m.n_agent_turns == EXPECT["agent_turns"],
                       m.n_agent_turns, EXPECT["agent_turns"], ""))

        passed = all(ok for _, ok, *_ in checks)
        if verbose:
            print(bold("earshot selftest - analyzer calibration"))
            print(dim("  synthetic call with known turn boundaries, "
                      f"tolerance {TOL_MS:.0f}ms / {TOL_S:.2f}s"))
            print()
            for name, ok, got, want, unit in checks:
                mark = green("PASS") if ok else red("FAIL")
                g = "n/a" if got is None else (
                    f"{got:.0f}{unit}" if isinstance(got, float) else f"{got}{unit}")
                w = f"{want:.0f}{unit}" if isinstance(want, float) else f"{want}{unit}"
                print(f"  {mark}  {name:<26} got {g:>8}   want {w:>8}")
            print()
            print(green("analyzer is calibrated") if passed
                  else red("analyzer is OUT OF CALIBRATION - do not trust reports"))
        return passed
