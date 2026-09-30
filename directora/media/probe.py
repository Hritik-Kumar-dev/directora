"""Thin, well-behaved wrappers around ffprobe."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from ..models import MediaInfo


class MediaError(RuntimeError):
    pass


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _fraction(text: str) -> float:
    """Parse ffprobe's '60/1' rationals safely."""
    try:
        if "/" in text:
            num, den = text.split("/", 1)
            den_f = float(den)
            return float(num) / den_f if den_f else 0.0
        return float(text)
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe(path: Path) -> MediaInfo:
    """Read stream/format metadata for ``path``."""
    if not path.exists():
        raise MediaError(f"No such file: {path}")
    if not have_ffmpeg():
        raise MediaError("ffmpeg/ffprobe not found on PATH. Install ffmpeg first.")

    p = _run([
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ])
    if p.returncode != 0:
        raise MediaError(f"ffprobe failed on {path.name}:\n{p.stderr.strip()[:400]}")

    data = json.loads(p.stdout or "{}")
    streams = data.get("streams", [])
    fmt = data.get("format", {})

    vs = next((s for s in streams if s.get("codec_type") == "video"), None)
    aus = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if vs is None:
        raise MediaError(f"{path.name} has no video stream.")

    duration = 0.0
    for cand in (fmt.get("duration"), vs.get("duration")):
        try:
            if cand:
                duration = float(cand)
                break
        except (TypeError, ValueError):
            continue

    fps = _fraction(vs.get("avg_frame_rate") or vs.get("r_frame_rate") or "0/1") or 25.0

    return MediaInfo(
        path=Path(path),
        duration=round(duration, 3),
        width=int(vs.get("width") or 0),
        height=int(vs.get("height") or 0),
        fps=round(fps, 3),
        has_audio=aus is not None,
        audio_codec=(aus or {}).get("codec_name"),
        audio_rate=int((aus or {}).get("sample_rate") or 0) or None,
        audio_channels=int((aus or {}).get("channels") or 0) or None,
        video_codec=vs.get("codec_name"),
        size_bytes=int(fmt.get("size") or 0),
    )