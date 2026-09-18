from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

SCALE = {
    5: "Indistinguishable from a good human agent.",
    4: "Minor artifact; the task is unaffected.",
    3: "Noticeable; the caller adapts around it.",
    2: "The caller has to repeat themselves or repair the conversation.",
    1: "The task is threatened.",
    0: "The call fails.",
}


@dataclass(frozen=True)
class Dimension:
    key: str
    name: str
    weight: int
    scenarios: List[str]
    what_it_measures: str
    scored_by: str  # "measured", "judged", or "both"


DIMENSIONS: List[Dimension] = [
    Dimension(
        "turn_taking", "Turn-taking & barge-in", 20,
        ["S10", "S11", "S12", "S13", "S08"],
        "Whether it stops when it should, keeps going when it should, and never "
        "cuts the caller off mid-thought.",
        "both",
    ),
    Dimension(
        "asr", "ASR robustness", 18,
        ["S02", "S04", "S05", "S06", "S07", "S08", "S09", "S14"],
        "Hearing accuracy under noise, accent, channel degradation, alphanumerics, "
        "numbers and self-correction.",
        "judged",
    ),
    Dimension(
        "latency", "Latency & jitter", 15,
        ["S01", "S03", "S10"],
        "Response-time distribution. The p90 and the spread, not the mean.",
        "measured",
    ),
    Dimension(
        "task", "Task completion & correctness", 15,
        ["S01", "S05", "S06", "S07", "S09", "S14", "S16", "S18"],
        "Did the caller get what they called for, with every constraint intact.",
        "judged",
    ),
    Dimension(
        "fluidity", "Conversational fluidity & prosody", 12,
        ["S01", "S02", "S03", "S04", "S15", "S16"],
        "Whether it sounds spoken rather than read aloud, and whether its turns "
        "are the right length.",
        "both",
    ),
    Dimension(
        "patience", "Patience, empathy & tone control", 10,
        ["S12", "S13", "S15", "S16", "S17"],
        "Behaviour under repetition, silence, distress and anger. Proportionate "
        "acknowledgment then progress - not performance.",
        "judged",
    ),
    Dimension(
        "honesty", "Honesty, hallucination & escalation", 10,
        ["S17", "S18"],
        "Admitting ignorance, admitting it is an AI, and handing off for real "
        "instead of inventing a policy.",
        "judged",
    ),
]

BY_KEY: Dict[str, Dimension] = {d.key: d for d in DIMENSIONS}

# Re-weightings for different deployments. Every profile sums to 100.
PROFILES: Dict[str, Dict[str, int]] = {
    "default": {d.key: d.weight for d in DIMENSIONS},
    # Booking, ordering, support triage: getting the data right beats sounding nice.
    "transactional": {
        "turn_taking": 18, "asr": 20, "latency": 14, "task": 25,
        "fluidity": 7, "patience": 8, "honesty": 8,
    },
    # Outbound sales / front-of-house: how it sounds is the product.
    "conversational": {
        "turn_taking": 22, "asr": 14, "latency": 16, "task": 10,
        "fluidity": 20, "patience": 12, "honesty": 6,
    },
    # Healthcare, finance, anything regulated: being wrong is the whole risk.
    "high_stakes": {
        "turn_taking": 15, "asr": 20, "latency": 10, "task": 18,
        "fluidity": 5, "patience": 12, "honesty": 20,
    },
}


def weights(profile: str = "default") -> Dict[str, int]:
    if profile not in PROFILES:
        raise KeyError(
            f"unknown profile {profile!r}; choose from {', '.join(PROFILES)}"
        )
    return PROFILES[profile]


def weighted_total(scores: Dict[str, float], profile: str = "default") -> float:
    """Weighted score out of 100. Dimensions with no score are dropped and the
    remaining weights are renormalized, so a partial run still ranks."""
    w = weights(profile)
    present = {k: v for k, v in scores.items() if v is not None and k in w}
    if not present:
        return 0.0
    total_w = sum(w[k] for k in present)
    return sum(present[k] / 5.0 * w[k] for k in present) / total_w * 100.0


def grade(total: float) -> str:
    for cutoff, letter in (
        (93, "A"), (90, "A-"), (87, "B+"), (83, "B"), (80, "B-"),
        (77, "C+"), (73, "C"), (70, "C-"), (67, "D+"), (60, "D"),
    ):
        if total >= cutoff:
            return letter
    return "F"


def rubric_markdown(profile: str = "default") -> str:
    w = weights(profile)
    lines = [f"### Rubric (profile: `{profile}`)", "",
             "| Dimension | Weight | Scenarios | Scored by |",
             "|---|---:|---|---|"]
    for d in DIMENSIONS:
        lines.append(
            f"| **{d.name}** | {w[d.key]} | {', '.join(d.scenarios)} | {d.scored_by} |"
        )
    lines += ["", "**Scale**", ""]
    for n in sorted(SCALE, reverse=True):
        lines.append(f"- **{n}** - {SCALE[n]}")
    return "\n".join(lines)
