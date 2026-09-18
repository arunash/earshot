"""Automated mode: Earshot places the calls itself via Twilio.

TwiML is delivered INLINE by default (Twilio's `twiml` parameter, 4000 char
limit - the longest scenario here is 729). That means automated calling needs no
public URL, no tunnel, and no server: credentials and a from-number are the whole
setup. `earshot serve` and EARSHOT_PUBLIC_URL remain as a fallback for scenarios
that outgrow the inline limit.

Injection is OPEN LOOP - the harness plays its lines on a fixed timeline rather
than reacting to the agent in real time. That is deliberate and it is sound,
because nothing is measured at injection time. Twilio records the call in DUAL
CHANNEL, and every number in the report is read back off that recording to
within one 10ms frame. The interruption in a barge-in scenario only has to LAND
somewhere inside the agent's turn; exactly where it landed, and how long the
agent kept talking afterwards, are recovered afterwards from the waveform.

`earshot ingest` verifies that each barge-in actually landed, and tells you which
calls to re-run if one missed.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from xml.sax.saxutils import escape as xesc

from pathlib import Path

from .battery import Battery, Scenario, VOICES
from .util import die, dim, info, warn

# How long we assume an agent turn lasts when a turn says "after_agent".
DEFAULT_AGENT_BUDGET_S = 6.0
# Twilio rejects an inline `twiml` payload over this size; fall back to a URL.
TWIML_INLINE_LIMIT = 4000
RATE = {"fast": "125%", "slow": "75%"}
VOLUME = {"quiet": "soft", "loud": "loud"}


def build_twiml(scenario: Scenario, agent_budget_s: float = DEFAULT_AGENT_BUDGET_S,
                greeting_s: float = 4.0) -> str:
    """Render one scenario as a TwiML timeline."""
    parts = [f'<Pause length="{max(1, round(greeting_s))}"/>']
    for t in scenario.turns:
        if t.wait == "after_agent":
            wait = agent_budget_s + (t.gap_ms or 300) / 1000.0
        elif t.wait == "during_agent":
            wait = t.offset_ms / 1000.0
        else:  # fixed
            wait = t.offset_ms / 1000.0
        if wait >= 0.5:
            parts.append(f'<Pause length="{max(1, round(wait))}"/>')

        if t.play:
            parts.append(f'<Play>{xesc(t.play)}</Play>')
            continue
        if not t.say:
            continue
        voice = VOICES.get(t.voice, t.voice)
        text = xesc(t.say)
        attrs = []
        if t.rate in RATE:
            attrs.append(f'rate="{RATE[t.rate]}"')
        if t.volume in VOLUME:
            attrs.append(f'volume="{VOLUME[t.volume]}"')
        if attrs:
            text = f'<prosody {" ".join(attrs)}>{text}</prosody>'
        parts.append(f'<Say voice="{xesc(voice)}">{text}</Say>')
    parts.append('<Pause length="3"/>')
    return "<?xml version='1.0' encoding='UTF-8'?>\n<Response>\n  " \
           + "\n  ".join(parts) + "\n</Response>"


# ------------------------------------------------------------------- server --


def make_app(battery: Battery, agent_budget_s: float = DEFAULT_AGENT_BUDGET_S,
             audio_dir=None):
    try:
        from flask import Flask, Response, request
    except ImportError:
        die("Automated mode needs Flask: pip install 'earshot[twilio]'")

    app = Flask("earshot")

    @app.route("/health")
    def health():
        return {"ok": True, "battery": battery.id,
                "scenarios": [s.id for s in battery.scenarios]}

    @app.route("/audio/<path:name>")
    def audio(name):
        from flask import send_from_directory
        if audio_dir is None:
            return ("no baked audio is being served; "
                    "start with `earshot serve --run <id>`", 404)
        return send_from_directory(str(Path(audio_dir).resolve()), name)

    @app.route("/twiml/<scenario>", methods=["GET", "POST"])
    def twiml(scenario):
        sc = battery.get(scenario)
        budget = float(request.args.get("budget", agent_budget_s))
        play = request.args.get("play")
        if play:
            return Response(play_twiml(play), mimetype="text/xml")
        return Response(build_twiml(sc, budget), mimetype="text/xml")

    return app


def play_twiml(url: str) -> str:
    """One <Play> of a baked file. The whole scenario is inside the audio."""
    return ("<?xml version='1.0' encoding='UTF-8'?>\n<Response>\n"
            f"  <Play>{xesc(url)}</Play>\n  <Pause length=\"3\"/>\n</Response>")


def serve(battery: Battery, host: str = "0.0.0.0", port: int = 8787,
          agent_budget_s: float = DEFAULT_AGENT_BUDGET_S, audio_dir=None) -> None:
    app = make_app(battery, agent_budget_s, audio_dir)
    info(f"TwiML server on http://{host}:{port}  "
         f"(expose it and set EARSHOT_PUBLIC_URL to the public https URL)")
    info(f"  test: curl http://127.0.0.1:{port}/twiml/{battery.scenarios[0].id}")
    if audio_dir:
        info(f"  serving baked audio from {audio_dir}")
    app.run(host=host, port=port)


# ------------------------------------------------------------------- calling --


def _client():
    try:
        from twilio.rest import Client
    except ImportError:
        die("Automated mode needs the Twilio SDK: pip install 'earshot[twilio]'")
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    tok = os.environ.get("TWILIO_AUTH_TOKEN")
    if not (sid and tok):
        die("Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN (see .env.example).")
    return Client(sid, tok), sid, tok


def place_calls(manifest: Dict[str, Any], plan: List[Dict[str, Any]],
                run_dir: "str | Path", battery: Battery,
                budget_s: float = DEFAULT_AGENT_BUDGET_S,
                pause_between_s: float = 20.0,
                max_call_s: float = 180.0,
                dry_run: bool = False) -> List[Dict[str, Any]]:
    """Place every call in the plan and download the dual-channel recordings."""
    base = os.environ.get("EARSHOT_PUBLIC_URL", "").rstrip("/")
    from_number = os.environ.get("TWILIO_FROM_NUMBER", "")
    numbers = {s["id"]: s.get("phone") for s in manifest["systems"]}

    if dry_run:
        for item in plan[:3]:
            sc = battery.get(item["scenario"])
            print(f"--- {item['system']} {sc.id} run{item['run']} -> "
                  f"{numbers.get(item['system'])}")
            print(build_twiml(sc, budget_s))
        print(dim(f"... {len(plan)} calls total (showing 3)"))
        return []

    if not from_number:
        die("Set TWILIO_FROM_NUMBER to a Twilio number you own.")

    client, acct, tok = _client()
    rec_dir = Path(run_dir) / "recordings"
    rec_dir.mkdir(parents=True, exist_ok=True)
    results: List[Dict[str, Any]] = []

    for n_done, item in enumerate(plan, 1):
        sysid, scn, rn = item["system"], item["scenario"], item["run"]
        to = numbers.get(sysid)
        if not to:
            warn(f"no phone number for system {sysid}; skipping")
            continue
        dest = rec_dir / f"{sysid}-{scn}-run{rn}.wav"
        if dest.exists():
            info(f"[{n_done}/{len(plan)}] {dest.name} exists, skipping")
            continue

        baked = Path(run_dir) / "audio" / f"{scn}.wav"
        hosted = (manifest.get("audio_urls") or {}).get(f"{scn}.wav")
        kwargs: Dict[str, Any] = {}
        if baked.exists():
            if hosted:
                markup = play_twiml(hosted)          # Twilio's own CDN
            elif base.startswith("https://"):
                markup = play_twiml(f"{base}/audio/{scn}.wav")
            else:
                die(f"{scn} has baked audio, which Twilio plays from a URL. "
                    f"Either `earshot bake <run> --publish` to host it on "
                    f"Twilio, or serve it yourself and set EARSHOT_PUBLIC_URL.")
            kwargs["twiml"] = markup
        else:
            markup = build_twiml(battery.get(scn), budget_s)
        if "twiml" in kwargs:
            pass
        elif len(markup) <= TWIML_INLINE_LIMIT:
            kwargs["twiml"] = markup          # no public URL needed
        elif base.startswith("https://"):
            kwargs["url"] = f"{base}/twiml/{scn}?budget={budget_s}"
        else:
            die(f"{scn} renders {len(markup)} chars of TwiML, over Twilio's "
                f"{TWIML_INLINE_LIMIT}-char inline limit. Run `earshot serve`, "
                f"expose it, and set EARSHOT_PUBLIC_URL.")

        info(f"[{n_done}/{len(plan)}] calling {sysid} ({to}) with {scn} run {rn}")
        call = client.calls.create(
            to=to, from_=from_number,
            record=True, recording_channels="dual",
            timeout=30, time_limit=int(max_call_s), **kwargs,
        )

        deadline = time.time() + max_call_s + 60
        status = None
        while time.time() < deadline:
            time.sleep(4)
            status = client.calls(call.sid).fetch().status
            if status in ("completed", "failed", "busy", "no-answer", "canceled"):
                break
        entry = {"system": sysid, "scenario": scn, "run": rn,
                 "call_sid": call.sid, "status": status}

        if status == "completed":
            rec = None
            for _ in range(15):
                time.sleep(3)
                recs = client.recordings.list(call_sid=call.sid, limit=1)
                if recs:
                    rec = recs[0]
                    break
            if rec:
                import base64
                import urllib.request
                media = (f"https://api.twilio.com/2010-04-01/Accounts/{acct}"
                         f"/Recordings/{rec.sid}.wav")
                req = urllib.request.Request(media)
                cred = base64.b64encode(f"{acct}:{tok}".encode()).decode()
                req.add_header("Authorization", f"Basic {cred}")
                with urllib.request.urlopen(req) as r, open(dest, "wb") as fh:
                    fh.write(r.read())
                entry["recording"] = str(dest)
                entry["channels"] = int(getattr(rec, "channels", 2) or 2)
                if entry["channels"] < 2:
                    warn("Twilio returned a MONO recording - barge-in will not be "
                         "measurable on this call.")
            else:
                warn(f"no recording appeared for {call.sid}")
        else:
            warn(f"call {call.sid} ended as {status}")

        results.append(entry)
        time.sleep(pause_between_s)

    return results
