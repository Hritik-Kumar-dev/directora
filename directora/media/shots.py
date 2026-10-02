"""Shot detection and cheap local visual analysis (no ML, fully offline).

Why this is not just "ffmpeg scene detection"
--------------------------------------------
Real screen recordings almost never contain hard cuts -- a browser window
scrolling or a panel opening is a smooth interpolation that sits far below
ffmpeg's scene threshold. Measured frame-difference curves, however, are
*spiky*: long quiet stretches punctuated by brief bursts exactly when the UI
changes. Those bursts are the real shot boundaries.

So we use both signals:
  * ffmpeg scene scores  -> genuine editorial cuts (slides, real edits)
  * frame-diff spikes     -> UI state transitions (screen recordings)
and measure "motion" as the *median* consecutive difference, which reflects
how busy a shot really is rather than being dominated by one transition.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image

from ..config import Settings
from ..models import MediaInfo, Shot

PTS_RE = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")


class ShotError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# 1. scene cuts
# --------------------------------------------------------------------------- #
def detect_cuts(video: Path, threshold: float) -> list[float]:
    """Timestamps (seconds) of hard editorial scene changes.

    NOTE: showinfo logs at INFO level, so the log level must stay at ``info``
    here -- running with ``-v error`` silently yields zero cuts.
    """
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info",
        "-i", str(video),
        "-vf", f"select='gt(scene,{threshold})',showinfo",
        "-an", "-f", "null", "-",
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    return sorted(set(round(float(m), 3) for m in PTS_RE.findall(p.stderr or "")))


# --------------------------------------------------------------------------- #
# 2. sample frames
# --------------------------------------------------------------------------- #
def _sample_frames(video: Path, duration: float, work: Path,
                   width: int = 320, target: int = 480) -> list[tuple[float, Path]]:
    rate = max(0.5, min(8.0, target / max(duration, 0.5)))
    outdir = work / "samples"
    if outdir.exists():
        shutil.rmtree(outdir)
    outdir.mkdir(parents=True)

    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-v", "error", "-y",
        "-i", str(video),
        "-vf", f"fps={rate:.4f},scale={width}:-2:flags=fast_bilinear",
        "-q:v", "6", "-f", "image2", str(outdir / "%05d.jpg"),
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    files = sorted(outdir.glob("*.jpg"))
    if not files:
        raise ShotError(f"Could not extract sample frames:\n{p.stderr[-400:]}")

    step = duration / max(len(files), 1)
    return [(round((i + 0.5) * step, 3), f) for i, f in enumerate(files)]


def _luma(path: Path) -> np.ndarray:
    with Image.open(path) as im:
        return np.asarray(im.convert("L"), dtype=np.float32) / 255.0


def _motion_curve(samples: list[tuple[float, Path]]) -> list[tuple[float, float]]:
    """Consecutive-frame difference: [(midpoint_time, diff), ...]."""
    out: list[tuple[float, float]] = []
    prev = None
    prev_t = 0.0
    for t, f in samples:
        try:
            cur = _luma(f)
        except Exception:                                      # noqa: BLE001
            continue
        if prev is not None and prev.shape == cur.shape:
            out.append(((prev_t + t) / 2.0, float(np.abs(prev - cur).mean())))
        prev, prev_t = cur, t
    return out


def _stats(img: Image.Image) -> tuple[float, float, float]:
    """(brightness 0..1, colorfulness ~0..150, edge_density 0..1)"""
    a = np.asarray(img.convert("RGB"), dtype=np.float32) / 255.0
    brightness = float(a.mean())

    rg = a[..., 0] - a[..., 1]
    yb = 0.5 * (a[..., 0] + a[..., 1]) - a[..., 2]
    std = float(np.sqrt(rg.std() ** 2 + yb.std() ** 2))
    mean = float(np.sqrt(rg.mean() ** 2 + yb.mean() ** 2))
    colorfulness = std + 0.3 * mean

    # gradient magnitude, aligned on a common (H-1, W-1) region
    g = a.mean(axis=2)
    h, w = g.shape
    if h < 2 or w < 2:
        return brightness, colorfulness, 0.0
    gx = np.abs(np.diff(g, axis=1))[: h - 1, : w - 1]
    gy = np.abs(np.diff(g, axis=0))[: h - 1, : w - 1]
    edge_density = float(((gx + gy) > 0.12).mean())
    return brightness, colorfulness, edge_density


# --------------------------------------------------------------------------- #
# 3. boundaries
# --------------------------------------------------------------------------- #
def _spike_boundaries(curve: list[tuple[float, float]],
                      duration: float, min_shot: float,
                      max_shots: int) -> list[float]:
    """Pick UI-transition peaks out of a spiky frame-difference curve.

    Robust thresholding: median + k*MAD, so a couple of huge spikes cannot
    drag the bar up and hide the smaller ones.
    """
    if len(curve) < 4:
        return []
    vals = np.array([d for _, d in curve], dtype=np.float32)
    med = float(np.median(vals))
    mad = float(np.median(np.abs(vals - med))) or 1e-6
    thr = max(med + 4.0 * mad, med * 2.0, 0.012)
    if float(vals.max()) <= thr:
        return []

    # local maxima above the threshold, strongest first, spaced by min_shot
    order = np.argsort(vals)[::-1]
    chosen: list[tuple[float, float]] = []
    for i in order:
        t, v = curve[i]
        if v < thr:
            break
        if t < min_shot or t > duration - min_shot:
            continue
        if all(abs(t - ct) >= min_shot for ct, _ in chosen):
            chosen.append((float(t), float(v)))
        if len(chosen) >= max_shots:
            break

    chosen.sort()
    return [round(t, 3) for t, _ in chosen]


def _merge_bounds(values: list[float], min_shot: float) -> list[float]:
    """Drop boundaries that sit too close together, keeping the strongest."""
    out: list[float] = []
    for v in sorted(values):
        if not out or v - out[-1] >= min_shot:
            out.append(v)
    return out


def build_shots(info: MediaInfo, cuts: list[float], s: Settings,
                work: Path) -> tuple[list[Shot], list[tuple[float, float]]]:
    """Group cut timestamps into shots and attach visual measurements."""
    dur = info.duration
    curve = None

    samples = _sample_frames(info.path, dur, work)
    curve = _motion_curve(samples)

    spikes = _spike_boundaries(curve, dur, s.min_shot_seconds, s.max_shots)

    editorial = [c for c in cuts if 0.2 < c < dur - 0.2]

    # Editorial cuts win; motion spikes only fill in when there are too few.
    if len(editorial) >= 3 or (editorial and len(editorial) >= len(spikes)):
        bounds = editorial
        method = "scene"
    elif spikes:
        bounds = spikes
        method = "motion"
    elif editorial:
        bounds = editorial
        method = "scene"
    else:
        bounds = []
        method = "even"

    if not bounds:
        n = max(1, min(s.max_shots, max(1, int(round(dur / 4.0)))))
        step = dur / n
        bounds = [round(i * step, 3) for i in range(1, n)]

    bounds = _merge_bounds([b for b in bounds if 0.15 < b < dur - 0.15],
                           s.min_shot_seconds)
    edges = [0.0] + bounds + [dur]

    shots = [Shot(index=i, start=round(edges[i], 3),
                  end=round(edges[i + 1], 3)) for i in range(len(edges) - 1)]
    shots = [sh for sh in shots if sh.duration >= 0.2]

    # ensure each shot has a minimum viable duration
    merged: list[Shot] = []
    for sh in shots:
        if merged and sh.duration < s.min_shot_seconds:
            merged[-1].end = sh.end
        else:
            merged.append(sh)
    shots = merged

    if len(shots) > s.max_shots:
        step = dur / s.max_shots
        shots = [Shot(index=i, start=round(i * step, 3),
                      end=round(min((i + 1) * step, dur), 3))
                 for i in range(s.max_shots)]

    for i, sh in enumerate(shots):
        sh.index = i
        sh.scene_score = 1.0 if method == "scene" else 0.0

    _measure(info, shots, s, work, samples, curve)
    for sh in shots:
        sh.analysed_by = f"segment:{method}"
    shots[0].keywords.append(method)
    return shots, curve


# --------------------------------------------------------------------------- #
# 4. per-shot measurements
# --------------------------------------------------------------------------- #
def _measure(info: MediaInfo, shots: list[Shot], s: Settings, work: Path,
             samples: list[tuple[float, Path]],
             curve: list[tuple[float, float]]) -> None:
    cache: dict[Path, tuple[float, float, float]] = {}

    def load(p: Path):
        if p not in cache:
            with Image.open(p) as im:
                cache[p] = _stats(im)
        return cache[p]

    for sh in shots:
        inside = [(t, f) for t, f in samples if sh.start <= t < sh.end]
        if not inside:
            inside = [(t, f) for t, f in samples
                      if abs(t - (sh.start + sh.duration / 2)) < 1.5] or samples[:1]
        mid = inside[len(inside) // 2]
        sh.brightness, sh.colorfulness, sh.edge_density = load(mid[1])

        # motion = MEDIAN difference inside the shot (typical busyness),
        # not first-vs-last which a single transition would dominate.
        diffs = [d for t, d in curve if sh.start <= t < sh.end]
        if diffs:
            arr = np.array(diffs, dtype=np.float32)
            sh.motion = float(np.median(arr))
            sh.keywords.append(f"peak={float(arr.max()):.3f}")
        else:
            sh.motion = 0.0

    _extract_keyframes(info, shots, s, work)


def _extract_keyframes(info: MediaInfo, shots: list[Shot], s: Settings,
                       work: Path) -> None:
    kdir = work / "keyframes"
    if kdir.exists():
        shutil.rmtree(kdir)
    kdir.mkdir(parents=True)

    wanted = list(shots)[: s.max_frames]
    expr = "+".join(
        f"eq(n\\,{int(round((sh.start + sh.duration / 2) * info.fps))})"
        for sh in wanted
    )
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-v", "error", "-y",
        "-i", str(info.path),
        "-vf", f"select='{expr}',scale={s.frame_width}:-2:flags=lanczos",
        "-vsync", "0", "-q:v", "4", "-f", "image2", str(kdir / "k%04d.jpg"),
    ]
    # a failure here just means fewer keyframes, which the length check below
    # already handles
    subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    produced = sorted(kdir.glob("*.jpg"))

    if len(produced) == len(wanted):
        for sh, f in zip(wanted, produced):
            sh.frame = f
        return

    # selection mismatch -- fall back to seeking per shot (slower, always right)
    for sh in shots:
        out = kdir / f"shot{sh.index:04d}.jpg"
        q = subprocess.run([
            "ffmpeg", "-hide_banner", "-v", "error", "-y",
            "-ss", f"{sh.start + sh.duration / 2:.3f}", "-i", str(info.path),
            "-frames:v", "1", "-vf", f"scale={s.frame_width}:-2",
            "-q:v", "4", str(out),
        ], capture_output=True, text=True, timeout=300)
        if q.returncode == 0 and out.exists():
            sh.frame = out


def analyse(info: MediaInfo, s: Settings, work: Path,
            progress=None) -> list[Shot]:
    """Full local analysis: cuts + motion spikes -> shots -> measurements."""
    def tick(msg):
        if progress:
            progress(msg)

    tick("Detecting editorial scene cuts")
    cuts = detect_cuts(info.path, s.scene_threshold)
    tick(f"{len(cuts)} editorial cut(s); measuring UI transitions")
    shots, _curve = build_shots(info, cuts, s, work)
    tick(f"{len(shots)} shot(s) analysed")
    return shots
