"""Timeline placement, loudness normalisation and the final mux.

This mirrors the pipeline validated by hand on Rapid_Aurora.mp4:
two-pass loudnorm to -16 LUFS / -1.5 dBTP, side-chain ducking of the
original track, and a stream-copy of the video so picture quality is never
touched.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ..models import Line

FFMPEG = "ffmpeg"


class MixError(RuntimeError):
    pass


def _run(cmd: list[str], timeout: int = 7200) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        tail = (p.stderr or "")[-800:]
        raise MixError(f"ffmpeg failed:\n{tail}")
    return p


def _ms(t: float) -> int:
    return int(round(t * 1000))


def build_voice_track(lines: list[Line], work: Path) -> Path:
    """Place each clip at its start time and sum into one narration track."""
    clips = [l for l in lines if l.clip and l.clip.exists()]
    if not clips:
        raise MixError("No rendered narration clips found. Generate the voice first.")

    parts: list[str] = []
    for i, l in enumerate(clips):
        ms = _ms(l.start)
        parts.append(
            f"[{i}]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
            f"adelay={ms}|{ms}[a{i}]"
        )
    labels = "".join(f"[a{i}]" for i in range(len(clips)))
    fc = ";".join(parts) + f";{labels}amix=inputs={len(clips)}:normalize=0[vox]"

    raw = work / "voice_raw.wav"
    cmd = [FFMPEG, "-hide_banner", "-v", "error", "-y"]
    for l in clips:
        cmd += ["-i", str(l.clip)]
    cmd += ["-filter_complex", fc, "-map", "[vox]", str(raw)]
    _run(cmd)
    return raw


def normalise(src: Path, dst: Path, target_lufs: float, true_peak: float) -> Path:
    """Two-pass loudnorm so a whisper-quiet bed reaches broadcast level."""
    meas_cmd = [
        FFMPEG, "-hide_banner", "-i", str(src), "-af",
        f"loudnorm=I={target_lufs}:TP={true_peak}:LRA=11:print_format=json",
        "-f", "null", "-",
    ]
    p = subprocess.run(meas_cmd, capture_output=True, text=True, timeout=1800)
    blob = p.stderr or ""
    start, end = blob.find("{"), blob.rfind("}")
    if start == -1 or end == -1:
        raise MixError(f"loudnorm returned no measurements:\n{blob[-400:]}")
    data = json.loads(blob[start:end + 1])

    filt = (
        f"loudnorm=I={target_lufs}:TP={true_peak}:LRA=11"
        f":measured_I={data['input_i']}:measured_TP={data['input_tp']}"
        f":measured_LRA={data['input_lra']}:measured_thresh={data['input_thresh']}"
        f":linear=true:print_format=summary"
    )
    _run([FFMPEG, "-hide_banner", "-v", "error", "-y", "-i", str(src),
          "-af", filt, "-ar", "48000", "-ac", "2", str(dst)])
    return dst


def mux(video: Path, voice: Path, out: Path, *,
        keep_original: bool = True,
        duck_threshold: float = 0.020,
        duck_ratio: int = 10,
        audio_bitrate: str = "192k") -> Path:
    """Combine video + narration (+ ducked original) into ``out``."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=index", "-of", "csv=p=0", str(video)],
        capture_output=True, text=True)
    has_orig = bool((probe.stdout or "").strip())

    if keep_original and has_orig:
        fc = (
            "[0:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
            "highpass=f=90[orig];"
            f"[orig][1:a]sidechaincompress=threshold={duck_threshold}:"
            f"ratio={duck_ratio}:attack=8:release=450:makeup=1[ducked];"
            "[1:a]volume=1.0[vox];"
            "[vox][ducked]amix=inputs=2:normalize=0:duration=first[mix]"
        )
        cmd = [FFMPEG, "-hide_banner", "-v", "error", "-y",
               "-i", str(video), "-i", str(voice),
               "-filter_complex", fc, "-map", "0:v", "-map", "[mix]",
               "-c:v", "copy", "-c:a", "aac", "-b:a", audio_bitrate,
               "-ar", "48000", "-movflags", "+faststart", str(out)]
    else:
        cmd = [FFMPEG, "-hide_banner", "-v", "error", "-y",
               "-i", str(video), "-i", str(voice),
               "-map", "0:v", "-map", "1:a",
               "-c:v", "copy", "-c:a", "aac", "-b:a", audio_bitrate,
               "-ar", "48000", "-movflags", "+faststart", str(out)]

    _run(cmd)
    return out


def overruns(lines: list[Line], tolerance: float = 0.05) -> list[Line]:
    """Lines whose rendered audio is longer than its slot.

    Word-count estimates can be optimistic -- this is the ground truth, taken
    from the audio that was actually synthesised.
    """
    return [l for l in lines if l.clip and l.clip_dur > l.duration + tolerance]


def srt_timestamp(t: float, sep: str = ",") -> str:
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    if ms == 1000:
        ms, s = 0, s + 1
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def write_srt(lines: list[Line], out: Path) -> Path:
    """Sidecar subtitles derived from the final timings."""
    rows = []
    for i, l in enumerate(lines, 1):
        end = l.end
        if l.clip_dur:
            end = min(l.end, l.start + l.clip_dur)
        if end <= l.start:
            end = l.start + 1.2
        rows.append(
            f"{i}\n{srt_timestamp(l.start)} --> {srt_timestamp(end)}\n{l.text}\n"
        )
    out.write_text("\n".join(rows), encoding="utf-8")
    return out


def render(video: Path, lines: list[Line], out: Path, work: Path, *,
           target_lufs: float = -16.0, true_peak: float = -1.5,
           keep_original: bool = True, duck_threshold: float = 0.020,
           duck_ratio: int = 10, also_srt: bool = True) -> dict:
    """Full render: voice track -> normalise -> mux -> (srt)."""
    raw = build_voice_track(lines, work)
    norm = normalise(raw, work / "voice.wav", target_lufs, true_peak)
    mux(video, norm, out, keep_original=keep_original,
        duck_threshold=duck_threshold, duck_ratio=duck_ratio)
    made = {"output": out}
    if also_srt:
        made["srt"] = write_srt(lines, out.with_suffix(".srt"))
    return made