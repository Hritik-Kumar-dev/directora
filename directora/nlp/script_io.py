"""Reading, writing and validating agent-authored narration scripts.

The point of this module is that the script does not have to come from a
text editor or from this tool. An agent (or a human, or a diff) can write a
small JSON file describing what to say, and Directora will place it on the
timeline, fit it, speak it and mux it.

A script file looks like this::

    {
      "voice": "en-GB-RyanNeural",
      "rate": "-8%",
      "notes": "optional free text",
      "lines": [
        {"shot": 0, "text": "This covers the opening screen."},
        {"shot": 3, "text": "Times are inherited from shot 3."},
        {"start": 62.0, "end": 71.0, "text": "Explicit window, ignores shots."},
        {"start": 96.5, "text": "Start given, end estimated from the pace."}
      ]
    }

Each line needs ``text`` plus at least one of ``shot``, ``start`` or ``end``.
You may mix the three styles in a single file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..config import Settings
from ..models import Line, Script, Session, Shot
from .script_gen import capacity_words, plan_slots, speech_seconds


class ScriptError(ValueError):
    """The supplied script file is not usable."""


# --------------------------------------------------------------------------- #
# authoring helpers
# --------------------------------------------------------------------------- #
def template(session: Session, s: Settings) -> dict[str, Any]:
    """A skeleton an agent can fill in, with per-shot hints kept as notes."""
    shots = {sh.index: sh for sh in session.shots}
    slots = plan_slots(session.shots, s)
    lines = []
    for sh, start, end in slots:
        hint = sh.description or sh.transcript or ""
        lines.append({
            "shot": sh.index,
            "start": round(start, 2),
            "end": round(end, 2),
            "max_words": int(capacity_words(end - start, s)),
            "text": "",
            "_hint": hint,
            "_stats": {
                "type": sh.kind,
                "motion": round(sh.motion, 4),
                "edge_density": round(sh.edge_density, 4),
                "brightness": round(sh.brightness, 3),
            },
        })
    return {
        "voice": s.voice,
        "rate": s.rate,
        "pitch": s.pitch,
        "style": "product-demo",
        "notes": session.script.notes if session.script else "",
        "video_duration": round(session.info.duration, 2) if session.info else None,
        "speech_seen": session.has_speech,
        "lines": lines,
    }


def dump(path: Path, data: dict[str, Any]) -> Path:
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    return path


def manifest(session: Session, keyframe_dir: Path | None = None) -> dict[str, Any]:
    """Everything an agent needs to *see* the video without decoding it."""
    shots = []
    for sh in session.shots:
        frame = None
        if sh.frame:
            p = Path(sh.frame)
            if keyframe_dir:
                try:
                    p = p.relative_to(p.parents[2])
                except ValueError:
                    pass
            frame = str(p) if p.exists() else None
        shots.append({
            "index": sh.index,
            "start": round(sh.start, 3),
            "end": round(sh.end, 3),
            "duration": round(sh.duration, 3),
            "keyframes": frame,
            "type": sh.kind,
            "motion": round(sh.motion, 4),
            "edge_density": round(sh.edge_density, 4),
            "brightness": round(sh.brightness, 3),
            "colorfulness": round(sh.colorfulness, 2),
            "spoken_during_shot": sh.transcript or None,
            "analysed_by": sh.analysed_by or None,
            "description": sh.description or None,
        })
    return {
        "video": str(session.video),
        "duration": round(session.info.duration, 3) if session.info else None,
        "resolution": session.info.resolution if session.info else None,
        "fps": session.info.fps if session.info else None,
        "has_audio": session.info.has_audio if session.info else None,
        "speech_detected": session.has_speech,
        "transcript": session.transcript or None,
        "speech_wpm": session.detected_wpm or None,
        "shot_count": len(session.shots),
        "shots": shots,
    }


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #
def _num(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def load(path: Path, session: Session, s: Settings,
         default_voice: str | None = None) -> tuple[Script, dict[str, Any]]:
    """Parse a script file into a :class:`Script` positioned on the timeline."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ScriptError(f"No such script file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ScriptError(f"{path.name} is not valid JSON: {exc}") from exc

    if isinstance(raw, list):
        raw = {"lines": raw}
    if not isinstance(raw, dict):
        raise ScriptError("Script must be a JSON object, or a list of lines.")

    items = raw.get("lines") or raw.get("script") or []
    if not isinstance(items, list):
        raise ScriptError("'lines' must be a list.")

    shots: dict[int, Shot] = {sh.index: sh for sh in session.shots}
    duration = session.info.duration if session.info else 0.0
    margin = s.speech_margin
    problems: list[str] = []

    # Lines that carry no timing of their own are mapped onto shots in
    # document order, so `["First sentence.", "Second sentence."]` works and
    # a bare string is never silently dropped.
    normalised: list[Any] = [
        {"text": item} if isinstance(item, str) else item for item in items
    ]
    usable = [sh for sh, _, _ in plan_slots(session.shots, s)]
    cursor = 0
    for i, item in enumerate(normalised):
        if not isinstance(item, dict):
            continue
        timed = (item.get("shot", item.get("shot_index")) is not None
                 or _num(item.get("start")) is not None)
        if not timed and cursor < len(usable):
            item = dict(item)
            item["shot"] = usable[cursor].index
            normalised[i] = item
        cursor += 1

    # default window when a line only supplies "start"
    def estimate(start: float) -> float:
        return min(duration - 0.05 if duration else start + 6.0,
                   start + 30.0 / s.target_wpm * 60.0 * 1.6 + 1.2)

    lines: list[Line] = []

    for i, item in enumerate(normalised):
        if not isinstance(item, dict):
            problems.append(f"line {i + 1}: expected an object, got "
                            f"{type(item).__name__}")
            continue

        text = str(item.get("text") or "").strip()
        if not text:
            problems.append(f"line {i + 1}: empty 'text' -- skipped")
            continue

        shot_no = item.get("shot", item.get("shot_index"))
        start = _num(item.get("start"))
        end = _num(item.get("end"))

        shot: Shot | None = None
        if shot_no is not None:
            try:
                shot = shots[int(shot_no)]
            except (ValueError, KeyError):
                problems.append(f"line {i + 1}: unknown shot {shot_no!r} -- "
                                f"times must be given explicitly")
                shot = None

        if start is None and shot is not None:
            start = shot.start + margin
        if end is None and shot is not None:
            end = max(start + 0.3, shot.end - margin)
        if start is None:
            problems.append(f"line {i + 1}: needs 'shot' or 'start' -- skipped")
            continue
        if end is None:
            end = estimate(start)
        if end <= start:
            problems.append(f"line {i + 1}: end {end:.2f} <= start {start:.2f} "
                            f"-- set to 1.2s")
            end = start + 1.2

        lines.append(Line(
            index=len(lines),
            start=round(start, 3),
            end=round(end, 3),
            text=text,
            shot_index=shot.index if shot else -1,
            note=str(item.get("note") or (shot.description if shot else "") or ""),
        ))

    if not lines:
        raise ScriptError("No usable lines in the script.\n  " +
                          ("\n  ".join(problems) if problems else
                           "Every line needs non-empty 'text'."))

    # keep the timeline monotonic so the mix stage never collides
    lines.sort(key=lambda l: (l.start, l.index))
    cursor = 0.0
    for l in lines:
        if l.start < cursor:
            problems.append(f"line {l.index + 1}: overlaps the previous line "
                            f"(moved to {cursor:.2f}s)")
            l.start = round(cursor, 3)
        if l.end <= l.start:
            l.end = round(l.start + 1.2, 3)
        cursor = l.end
    for i, l in enumerate(lines):
        l.index = i

    script = Script(
        lines=lines,
        style=str(raw.get("style") or "product-demo"),
        notes=str(raw.get("notes") or ""),
        source="agent",
    )

    # return any voice overrides so the caller can honour them
    opts = {
        "voice": raw.get("voice") or default_voice,
        "rate": raw.get("rate"),
        "pitch": raw.get("pitch"),
    }
    return script, opts


# --------------------------------------------------------------------------- #
# checking
# --------------------------------------------------------------------------- #
def _tc(t: float) -> str:
    m, s = divmod(max(0.0, t), 60)
    return f"{int(m):02d}:{s:04.1f}"


def check(script: Script, session: Session, s: Settings) -> list[str]:
    """Human-readable warnings about a loaded script."""
    out: list[str] = []
    if not session.info:
        return out
    dur = session.info.duration

    for line in script.lines:
        cap = capacity_words(line.duration, s)
        if line.words > cap:
            out.append(
                f"line {line.index + 1} ({_tc(line.start)}): {line.words} words "
                f"but only ~{int(cap)} fit in {line.duration:.1f}s — "
                f"cut about {max(1, line.words - int(cap))}")
        if line.end > dur:
            out.append(f"line {line.index + 1}: ends at {line.end:.1f}s, past the "
                       f"end of the video ({dur:.1f}s)")

    est = speech_seconds(script.words, s)
    if est > dur:
        over = est - dur
        note = "expect to talk over the visuals" if over < dur * 0.35 else \
               "this is far too long -- cut roughly " \
               f"{max(1, round(script.words * (1 - dur / est)))} words"
        out.append(f"total script runs about {est:.0f}s over a {dur:.0f}s video "
                   f"({over:.0f}s over) -- {note}")

    for a, b in zip(script.lines, script.lines[1:]):
        gap = b.start - a.end
        if gap < 0:
            out.append(f"lines {a.index + 1} and {b.index + 1} overlap")
        elif gap > 6.0:
            out.append(f"long silence ({gap:.0f}s) before line {b.index + 1}")

    return out