"""Host baked audio on Twilio itself, so automated noise testing needs no tunnel.

<Play> requires a public URL, which is the one thing inline TwiML cannot supply.
The usual answer is a tunnel, and a tunnel is a bad dependency for a benchmark:
it is another moving part between you and a number, it fails in ways that look
like the system under test failing, and on a quick tunnel it can simply never
route - which is exactly what happened the first time this ran.

Twilio Serverless Assets removes it. The files are uploaded once to Twilio's own
CDN, get permanent https URLs, and every later call just references them. No
tunnel, no propagation wait, no local server.
"""

from __future__ import annotations

import mimetypes
import os
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from .util import die, dim, info, warn

UPLOAD_HOST = "https://serverless-upload.twilio.com/v1"
SERVICE_NAME = "earshot-audio"


def _creds() -> tuple:
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    tok = os.environ.get("TWILIO_AUTH_TOKEN")
    if not (sid and tok):
        die("Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN.")
    return sid, tok


def _multipart(fields: Dict[str, str], filename: str, content: bytes,
               content_type: str) -> tuple:
    """Build a multipart/form-data body without pulling in `requests`."""
    boundary = uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
        out += f"{v}\r\n".encode()
    out += f"--{boundary}\r\n".encode()
    out += (f'Content-Disposition: form-data; name="Content"; '
            f'filename="{filename}"\r\n').encode()
    out += f"Content-Type: {content_type}\r\n\r\n".encode()
    out += content + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def _upload_version(service_sid: str, asset_sid: str, path: Path,
                    remote_name: Optional[str] = None) -> str:
    """POST the file bytes to the upload host; returns the AssetVersion SID."""
    import base64
    import json

    sid, tok = _creds()
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    name = remote_name or path.name
    body, content_type = _multipart(
        {"Path": f"/{name}", "Visibility": "public"},
        name, path.read_bytes(), ctype)
    url = f"{UPLOAD_HOST}/Services/{service_sid}/Assets/{asset_sid}/Versions"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", content_type)
    req.add_header("Authorization", "Basic " + base64.b64encode(
        f"{sid}:{tok}".encode()).decode())
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())["sid"]


def content_name(path: Path) -> str:
    """`N00.wav` -> `N00-3f9a1c22.wav`, keyed to the bytes.

    Re-publishing the same path is not safe: Twilio's CDN served a cached copy
    of a previous bake for minutes afterwards, and a run placed in that window
    plays the wrong script while every check passes. A content-addressed name
    means new audio is always a new URL, so there is no cache to be stale.
    """
    import hashlib
    h = hashlib.sha1(path.read_bytes()).hexdigest()[:8]
    return f"{path.stem}-{h}{path.suffix}"


def publish(files: List[Path], environment: str = "earshot",
            verbose: bool = True) -> Dict[str, str]:
    """Upload, build and deploy. Returns {local filename: public url}.

    Re-running reuses the service, so re-baking does not accumulate services.
    Asset paths are content-addressed - see content_name.
    """
    try:
        from twilio.rest import Client
    except ImportError:
        die("Needs the Twilio SDK: pip install 'earshot[twilio]'")
    sid, tok = _creds()
    client = Client(sid, tok)
    sl = client.serverless

    svc = next((s for s in sl.services.list(limit=50)
                if s.unique_name == SERVICE_NAME), None)
    if svc is None:
        svc = sl.services.create(unique_name=SERVICE_NAME,
                                 friendly_name="earshot baked call audio",
                                 include_credentials=False)
        if verbose:
            info(f"created Twilio Serverless service {svc.sid}")
    service = sl.services(svc.sid)

    existing = {a.friendly_name: a for a in service.assets.list(limit=100)}
    versions: List[str] = []
    names: Dict[str, str] = {}
    for i, f in enumerate(files, 1):
        remote = content_name(f)
        names[f.name] = remote
        asset = existing.get(remote) or service.assets.create(friendly_name=remote)
        versions.append(_upload_version(svc.sid, asset.sid, f, remote))
        if verbose:
            info(f"[{i}/{len(files)}] uploaded {remote}")

    build = service.builds.create(asset_versions=versions)
    if verbose:
        info("building...")
    for _ in range(90):
        b = service.builds(build.sid).fetch()
        if b.status == "completed":
            break
        if b.status == "failed":
            die(f"Twilio build failed: {b.sid}")
        time.sleep(2)
    else:
        die("Twilio build did not complete in time")

    env = next((e for e in service.environments.list(limit=50)
                if e.unique_name == environment), None)
    if env is None:
        env = service.environments.create(unique_name=environment,
                                          domain_suffix=environment[:16])
    service.environments(env.sid).deployments.create(build_sid=build.sid)
    if verbose:
        info(f"deployed to https://{env.domain_name}")

    # The CDN needs a moment after a deployment before the first fetch works.
    time.sleep(5)
    return {f.name: f"https://{env.domain_name}/{names[f.name]}" for f in files}


def verify(url: str, expect_bytes: Optional[int] = None,
           timeout: float = 30.0) -> int:
    """HTTP status for a published asset, and optionally that it is the RIGHT one.

    Status alone is not enough. A stale cached copy returns 200 while serving a
    previous bake, which is how a run silently plays the wrong script. Pass the
    local file's size and a mismatch is reported as 409 rather than 200.
    """
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            if expect_bytes is not None and len(body) != expect_bytes:
                return 409
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0
