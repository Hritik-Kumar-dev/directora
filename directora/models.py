"""Core data structures shared across the whole pipeline."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


# --------------------------------------------------------------------------- #
# Media probing
# --------------------------------------------------------------------------- #
@dataclass
class MediaInfo:
    """Everything ffprobe tells us about the input file."""

    path: Path
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    audio_codec: str | None = None
    audio_rate: int | None = None
    audio_channels: int | None = None
    video_codec: str | None = None
    size_bytes: int = 0

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0

    @property
    def portrait(self) -> bool:
        return self.height > self.width


# --------------------------------------------------------------------------- #
# Shot / scene detection
# --------------------------------------------------------------------------- #
@dataclass
class Shot:
    """A contiguous visual unit of the video, bounded by scene cuts."""

    index: int
    start: float
    end: float
    frame: Path | None = None
    scene_score: float = 0.0

    # Visual measurements (local, no ML required)
    brightness: float = 0.0          # 0..1
    colorfulness: float = 0.0        # Hasler-Susstrunk, 0..~150
    motion: float = 0.0              # mean abs frame-diff, 0..1
    edge_density: float = 0.0        # Canny-ish edge fraction, 0..1

    # What the analyser concluded
    description: str = ""
    keywords: list[str] = field(default_factory=list)
    transcript: str = ""             # words spoken *during* this shot
    analysed_by: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def kind(self) -> str:
        """Coarse structural classification from local signals alone."""
        if self.motion < 0.004 and self.edge_density < 0.02:
            return "static"
        if self.motion > 0.035:
            return "fast"
        return "normal"


# --------------------------------------------------------------------------- #
# Script
# --------------------------------------------------------------------------- #
@dataclass
class Line:
    """One narration line, pinned to a time window.

    ``text`` is what gets spoken. ``note`` is guidance shown in the editor
    (what the analyser saw at that moment) and is never synthesised.
    """

    index: int
    start: float
    end: float
    text: str
    shot_index: int = -1
    note: str = ""
    clip: Path | None = None       # rendered TTS audio
    clip_dur: float = 0.0
    voice: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def words(self) -> int:
        return len(self.text.split())

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()

    def fits(self) -> bool:
        """Does the rendered audio fit inside its slot?"""
        return self.clip_dur <= self.duration + 0.05

    def tc(self, sep: str = ".") -> str:
        """Timecode, e.g. 01:23.456"""
        m, s = divmod(self.start, 60)
        return f"{int(m):02d}{sep}{s:06.3f}"


@dataclass
class Script:
    lines: list[Line] = field(default_factory=list)
    style: str = "product-demo"
    notes: str = ""
    source: str = "heuristic"      # heuristic | claude | transcript-rewrite

    @property
    def words(self) -> int:
        return sum(l.words for l in self.lines)

    @property
    def speech_seconds(self) -> float:
        return sum(l.clip_dur for l in self.lines if l.clip)

    def reindex(self) -> None:
        for i, line in enumerate(self.lines):
            line.index = i


# --------------------------------------------------------------------------- #
# Session / project state
# --------------------------------------------------------------------------- #
@dataclass
class Session:
    """Everything produced for one video. Persisted as JSON next to the app."""

    video: Path
    info: MediaInfo | None = None
    shots: list[Shot] = field(default_factory=list)
    transcript: str = ""
    transcript_segments: list[dict[str, Any]] = field(default_factory=list)
    speech_seconds: float = 0.0
    detected_wpm: float = 0.0
    has_speech: bool = False
    transcript_reliable: bool = True
    mean_logprob: float | None = None
    script: Script = field(default_factory=Script)
    output: Path | None = None
    created: float = field(default_factory=time.time)

    # ---- serialisation ----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["video"] = str(self.video)
        d["info"] = asdict(self.info) if self.info else None
        if d["info"]:
            d["info"]["path"] = str(self.info.path)
        d["output"] = str(self.output) if self.output else None
        for s in d["shots"]:
            s["frame"] = str(s["frame"]) if s["frame"] else None
        for l in d["script"]["lines"]:
            l["clip"] = str(l["clip"]) if l["clip"] else None
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Session:
        d = dict(d)
        d["video"] = Path(d["video"])

        info = d.get("info")
        if isinstance(info, dict):
            info = dict(info)
            info["path"] = Path(info["path"])
            d["info"] = MediaInfo(**info)

        # Shots: keep only real dataclass fields (asdict can carry extras)
        shot_fields = {f for f in Shot.__dataclass_fields__}
        shots = []
        for raw in d.get("shots", []) or []:
            raw = dict(raw)
            raw["frame"] = Path(raw["frame"]) if raw.get("frame") else None
            shots.append(Shot(**{k: v for k, v in raw.items()
                                 if k in shot_fields}))
        d["shots"] = shots

        # Script + Lines must be rebuilt, not passed through as plain dicts
        raw_script = d.get("script") or {}
        raw_script = dict(raw_script)
        line_fields = {f for f in Line.__dataclass_fields__}
        lines = []
        for raw in raw_script.get("lines", []) or []:
            raw = dict(raw)
            raw["clip"] = Path(raw["clip"]) if raw.get("clip") else None
            lines.append(Line(**{k: v for k, v in raw.items()
                                 if k in line_fields}))
        d["script"] = Script(
            lines=lines,
            style=raw_script.get("style", "product-demo"),
            notes=raw_script.get("notes", ""),
            source=raw_script.get("source", "heuristic"),
        )

        out = d.get("output")
        d["output"] = Path(out) if out else None

        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Session:
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
