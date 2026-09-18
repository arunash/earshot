"""The LLM judge: turns measured numbers + transcripts into a scored report card.

The judge never sees phone numbers or vendor names - only blind letters. The
measured numbers are computed before the model is called, so the model grades
against facts it cannot fudge.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import rubric
from .util import die, info, data_dir, warn

MODEL = "claude-opus-5"

DIM_KEYS = [d.key for d in rubric.DIMENSIONS]

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "ranking", "systems", "head_to_head",
                 "failure_taxonomy", "methodology_gaps", "blind_guess"],
    "properties": {
        "verdict": {"type": "string"},
        "ranking": {"type": "array", "items": {"type": "string"}},
        "systems": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "sketch", "dimensions", "strengths",
                             "weaknesses", "worst_moment", "deployability"],
                "properties": {
                    "id": {"type": "string"},
                    "sketch": {"type": "string"},
                    "dimensions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["key", "score", "rationale", "evidence"],
                            "properties": {
                                "key": {"type": "string", "enum": DIM_KEYS},
                                "score": {"type": ["number", "null"],
                                          "minimum": 0, "maximum": 5},
                                "rationale": {"type": "string"},
                                "evidence": {"type": "array",
                                             "items": {"type": "string"}},
                            },
                        },
                    },
                    "strengths": {"type": "array", "items": {"type": "string"}},
                    "weaknesses": {"type": "array", "items": {"type": "string"}},
                    "worst_moment": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["scenario", "quote", "why"],
                        "properties": {
                            "scenario": {"type": "string"},
                            "quote": {"type": "string"},
                            "why": {"type": "string"},
                        },
                    },
                    "deployability": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["status", "guardrails"],
                        "properties": {
                            "status": {"type": "string",
                                       "enum": ["ship", "ship_with_guardrails",
                                                "not_ready"]},
                            "guardrails": {"type": "array",
                                           "items": {"type": "string"}},
                        },
                    },
                },
            },
        },
        "head_to_head": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["measure", "values", "comment"],
                "properties": {
                    "measure": {"type": "string"},
                    "values": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["system", "value"],
                            "properties": {"system": {"type": "string"},
                                           "value": {"type": "string"}},
                        },
                    },
                    "comment": {"type": "string"},
                },
            },
        },
        "failure_taxonomy": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["root_cause", "failure", "systems", "fixable", "severity"],
                "properties": {
                    "root_cause": {"type": "string",
                                   "enum": ["hearing", "turn_taking", "reasoning",
                                            "voice_output", "policy"]},
                    "failure": {"type": "string"},
                    "systems": {"type": "array", "items": {"type": "string"}},
                    "fixable": {"type": "string",
                                "enum": ["prompt_or_config", "inherent", "unclear"]},
                    "severity": {"type": "string",
                                 "enum": ["critical", "major", "minor"]},
                },
            },
        },
        "methodology_gaps": {"type": "array", "items": {"type": "string"}},
        "blind_guess": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["system", "guess", "reasoning", "confidence"],
                "properties": {
                    "system": {"type": "string"},
                    "guess": {"type": "string"},
                    "reasoning": {"type": "string"},
                    "confidence": {"type": "string",
                                   "enum": ["low", "medium", "high"]},
                },
            },
        },
    },
}


def build_prompt(payload: Dict[str, Any], profile: str = "default") -> str:
    base = (data_dir() / "prompts" / "judge.md").read_text(encoding="utf-8")
    w = rubric.weights(profile)
    dims = "\n".join(
        f"- `{d.key}` **{d.name}** - weight {w[d.key]} - {d.what_it_measures}"
        for d in rubric.DIMENSIONS
    )
    return "\n\n".join([
        base,
        f"# Rubric dimensions and weights (profile: {profile})\n\n{dims}",
        "# BATTERY\n\n```json\n"
        + json.dumps(payload["battery"], indent=2, ensure_ascii=False) + "\n```",
        "# MEASURED\n\n```json\n"
        + json.dumps(payload["measured"], indent=2, ensure_ascii=False) + "\n```",
        "# TRANSCRIPTS AND TESTER NOTES\n\n" + payload["transcripts"],
    ])


def judge(payload: Dict[str, Any], profile: str = "default",
          model: str = MODEL, effort: str = "high") -> Dict[str, Any]:
    try:
        import anthropic
    except ImportError:
        die("The judge needs the Anthropic SDK: pip install 'earshot[judge]'")

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        warn("No ANTHROPIC_API_KEY set; relying on an `ant auth login` profile.")

    prompt = build_prompt(payload, profile)
    client = anthropic.Anthropic()
    info(f"judging with {model} (effort={effort}) - this takes a few minutes")

    with client.messages.stream(
        model=model,
        max_tokens=64000,
        thinking={"type": "adaptive"},
        output_config={
            "effort": effort,
            "format": {"type": "json_schema", "schema": SCHEMA},
        },
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        message = stream.get_final_message()

    if message.stop_reason == "refusal":
        die(f"the judge declined: {getattr(message.stop_details, 'explanation', '')}")

    text = next((b.text for b in message.content if b.type == "text"), None)
    if not text:
        die("judge returned no text block")
    result = json.loads(text)
    result["_meta"] = {
        "model": message.model,
        "profile": profile,
        "effort": effort,
        "input_tokens": message.usage.input_tokens,
        "output_tokens": message.usage.output_tokens,
    }
    return result


def apply_scores(result: Dict[str, Any], profile: str = "default") -> Dict[str, Any]:
    """Compute weighted totals and grades from the judge's per-dimension scores.

    The arithmetic is done here, in code, rather than asked of the model - so the
    ranking cannot drift from the scores that justify it.
    """
    for sysd in result.get("systems", []):
        scores = {d["key"]: d["score"] for d in sysd.get("dimensions", [])
                  if d.get("score") is not None}
        total = rubric.weighted_total(scores, profile)
        sysd["total"] = round(total, 1)
        sysd["grade"] = rubric.grade(total)
        sysd["scored_dimensions"] = len(scores)
        sysd["missing_dimensions"] = [
            d.key for d in rubric.DIMENSIONS if d.key not in scores
        ]
    result["systems"].sort(key=lambda s: s.get("total", 0), reverse=True)
    result["ranking"] = [s["id"] for s in result["systems"]]
    return result
