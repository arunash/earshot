from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------- tty --

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _COLOR else s


def bold(s: str) -> str:
    return _c("1", s)


def dim(s: str) -> str:
    return _c("2", s)


def green(s: str) -> str:
    return _c("32", s)


def yellow(s: str) -> str:
    return _c("33", s)


def red(s: str) -> str:
    return _c("31", s)


def cyan(s: str) -> str:
    return _c("36", s)


def info(msg: str) -> None:
    print(f"{cyan('::')} {msg}")


def warn(msg: str) -> None:
    print(f"{yellow('!!')} {msg}", file=sys.stderr)


def die(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[valid-type]
    print(f"{red('xx')} {msg}", file=sys.stderr)
    raise SystemExit(code)


# ------------------------------------------------------------------- files --


def read_json(path: "str | Path") -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: "str | Path", obj: Any) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def data_dir() -> Path:
    """Directory holding batteries/ and prompts/, installed or in-tree."""
    here = Path(__file__).resolve().parent
    for cand in (here, here.parent):
        if (cand / "batteries").is_dir() and (cand / "prompts").is_dir():
            return cand
    return here


# Kept for callers that predate the rename.
repo_root = data_dir


# ------------------------------------------------------------------ ffmpeg --


def have(binary: str) -> bool:
    return shutil.which(binary) is not None


def require_ffmpeg() -> None:
    if not have("ffmpeg"):
        die("ffmpeg not found. Install it (brew install ffmpeg) - Earshot needs it "
            "to normalize call recordings.")


def ffprobe_channels(path: "str | Path") -> int:
    """Number of audio channels, or 0 if unreadable."""
    if not have("ffprobe"):
        return 0
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=channels", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return int(out.splitlines()[0]) if out else 0
    except Exception:
        return 0


def to_pcm_wav(src: "str | Path", dst: "str | Path", sample_rate: int = 16000,
               channels: "int | None" = None) -> Path:
    """Normalize any input to signed-16 PCM WAV without touching levels.

    Levels are deliberately NOT normalized: relative loudness between the two
    legs of a call carries information the VAD uses.
    """
    require_ffmpeg()
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
           "-acodec", "pcm_s16le", "-ar", str(sample_rate)]
    if channels:
        cmd += ["-ac", str(channels)]
    cmd.append(str(dst))
    subprocess.run(cmd, check=True)
    return dst


def fmt_ms(v: "float | None") -> str:
    if v is None:
        return "  n/a"
    return f"{v:5.0f}"
