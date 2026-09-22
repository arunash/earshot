from __future__ import annotations

import os
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .util import die, data_dir

# Friendly voice aliases -> Twilio <Say> voices, chosen so S04 covers a real
# accent spread. Override per-turn with an explicit Twilio voice name.
VOICES = {
    "default":         "Polly.Joanna-Neural",
    "us_female":       "Polly.Joanna-Neural",
    "us_male":         "Polly.Matthew-Neural",
    "indian_english":  "Polly.Kajal-Neural",
    "british_english": "Polly.Amy-Neural",
    "nigerian_english": "Polly.Ayanda-Neural",
    "spanish_english": "Polly.Lupe-Neural",
    "australian":      "Polly.Olivia-Neural",
}


@dataclass
class Turn:
    say: Optional[str] = None
    play: Optional[str] = None
    wait: str = "after_agent"       # after_agent | during_agent | fixed
    offset_ms: int = 0
    gap_ms: Optional[int] = None
    voice: str = "default"
    rate: Optional[str] = None      # fast | slow
    volume: Optional[str] = None    # quiet | loud
    tone: Optional[str] = None      # angry | distressed | flat | rushed | hesitant

    @property
    def is_barge_in(self) -> bool:
        return self.wait == "during_agent"


@dataclass
class Scenario:
    id: str
    name: str
    intent: str = ""
    dimensions: List[str] = field(default_factory=list)
    setup: Dict[str, Any] = field(default_factory=dict)
    barge_expectation: str = "none"
    # Impairment applied to the harness's own audio when the scenario is baked:
    # {noise, snr_db, codec, packet_loss, jitter, dropouts, clip_db}
    condition: Dict[str, Any] = field(default_factory=dict)
    turns: List[Turn] = field(default_factory=list)
    tester_script: str = ""
    pass_signals: List[str] = field(default_factory=list)
    fail_signals: List[str] = field(default_factory=list)

    def summary(self) -> Dict[str, Any]:
        """What the judge is told about this scenario. No scores, no leading."""
        return {
            "id": self.id,
            "name": self.name,
            "intent": self.intent,
            "dimensions": self.dimensions,
            "setup": self.setup,
            "barge_expectation": self.barge_expectation,
            "condition": self.condition,
            "pass_signals": self.pass_signals,
            "fail_signals": self.fail_signals,
        }


@dataclass
class Battery:
    id: str
    name: str
    version: int
    runs_per_scenario: int
    default_gap_ms: int
    context: str
    scenarios: List[Scenario]
    # A digit string the agent is expected to repeat back, e.g. a PIN. When the
    # agent echoes what it heard, hearing accuracy stops being a judgement call
    # and becomes a string comparison - which is the strongest kind of evidence
    # a noise ladder can produce.
    expect_echo: Optional[str] = None

    def get(self, sid: str) -> Scenario:
        for s in self.scenarios:
            if s.id.lower() == sid.lower():
                return s
        die(f"no scenario {sid!r} in battery {self.id!r}")

    def select(self, only: Optional[List[str]] = None,
               skip: Optional[List[str]] = None) -> List[Scenario]:
        out = self.scenarios
        if only:
            want = {s.lower() for s in only}
            out = [s for s in out if s.id.lower() in want]
        if skip:
            drop = {s.lower() for s in skip}
            out = [s for s in out if s.id.lower() not in drop]
        return out

    def plan(self, systems: List[str], only=None, skip=None,
             runs: Optional[int] = None, shuffle: bool = True,
             seed: Optional[int] = None) -> List[Dict[str, Any]]:
        """Ordered list of calls to place.

        Scenario order is shuffled INDEPENDENTLY per system, so tester fatigue and
        any time-of-day effect on one provider cannot line up with one position in
        the script. Systems are interleaved for the same reason.
        """
        rng = random.Random(seed)
        n = runs or self.runs_per_scenario
        per_system = {}
        for sysid in systems:
            items = [(s.id, r + 1) for s in self.select(only, skip) for r in range(n)]
            if shuffle:
                rng.shuffle(items)
            per_system[sysid] = items

        plan = []
        for i in range(max(len(v) for v in per_system.values())):
            for sysid in systems:
                items = per_system[sysid]
                if i < len(items):
                    sid, run = items[i]
                    plan.append({"system": sysid, "scenario": sid, "run": run,
                                 "index": len(plan) + 1})
        return plan


def _interp_turn(d: Any, vals: Dict[str, str]) -> Any:
    if isinstance(d, str):
        return _interp(d, vals)
    out = dict(d)
    for k in ("say", "play"):
        if k in out:
            out[k] = _interp(out[k], vals)
    return out


def _turn(d: Any, default_gap: int) -> Turn:
    if isinstance(d, str):
        return Turn(say=d)
    t = Turn(
        say=d.get("say"),
        play=d.get("play"),
        wait=d.get("wait", "after_agent"),
        offset_ms=int(d.get("offset_ms", 0)),
        gap_ms=int(d["gap_ms"]) if "gap_ms" in d else default_gap,
        voice=d.get("voice", "default"),
        rate=d.get("rate"),
        volume=d.get("volume"),
        tone=d.get("tone"),
    )
    if t.wait not in ("after_agent", "during_agent", "fixed"):
        die(f"bad turn.wait {t.wait!r} (after_agent | during_agent | fixed)")
    return t


def _placeholders() -> Dict[str, str]:
    """Values a battery can interpolate into its script.

    `from_last4` exists because some lines derive a caller's PIN from their
    number. Writing the digits into the YAML would silently produce an invalid
    PIN for anyone whose Twilio number differs - and an invalid PIN, on the
    agents this was built against, means the call is cut off before most of the
    script plays.
    """
    num = re.sub(r"\D", "", os.environ.get("TWILIO_FROM_NUMBER", ""))
    last4 = num[-4:] if len(num) >= 4 else ""
    # EARSHOT_PIN overrides the derivation outright, for a line whose PIN comes
    # from somewhere else - or for finding out where it comes from.
    return {"from_last4": os.environ.get("EARSHOT_PIN") or last4}


def _interp(text: Optional[str], values: Dict[str, str]) -> Optional[str]:
    if not text:
        return text
    for k, v in values.items():
        text = text.replace("{" + k + "}", v)
    return text


SPOKEN = {"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
          "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine"}


def load(path_or_id: str = "default") -> Battery:
    p = Path(path_or_id)
    if not p.exists():
        p = data_dir() / "batteries" / f"{path_or_id}.yaml"
    if not p.exists():
        die(f"battery not found: {path_or_id}")
    with open(p, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    gap = int(raw.get("default_gap_ms", 300))
    vals = _placeholders()
    # A spoken form too, so TTS says "eight four seven eight" rather than
    # "eight thousand four hundred and seventy-eight".
    vals["from_last4_spoken"] = " ".join(SPOKEN.get(c, c) for c in vals["from_last4"])
    scenarios = []
    for s in raw.get("scenarios", []):
        scenarios.append(Scenario(
            id=s["id"],
            name=s.get("name", s["id"]),
            intent=s.get("intent", ""),
            dimensions=s.get("dimensions", []),
            setup=s.get("setup", {}) or {},
            barge_expectation=s.get("barge_expectation", "none"),
            condition=s.get("condition", {}) or {},
            turns=[_turn(_interp_turn(t, vals), gap) for t in s.get("turns", [])],
            tester_script=(s.get("tester_script") or "").strip(),
            pass_signals=s.get("pass_signals", []) or [],
            fail_signals=s.get("fail_signals", []) or [],
        ))
    if not scenarios:
        die(f"battery {p} has no scenarios")
    return Battery(
        id=raw.get("id", p.stem),
        name=raw.get("name", p.stem),
        version=int(raw.get("version", 1)),
        runs_per_scenario=int(raw.get("runs_per_scenario", 3)),
        default_gap_ms=gap,
        context=(raw.get("context") or "").strip(),
        scenarios=scenarios,
        expect_echo=(_interp(str(raw["expect_echo"]), vals)
                     if raw.get("expect_echo") else None),
    )
