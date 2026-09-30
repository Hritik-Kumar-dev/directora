"""Turn analysed shots into a timed, editable narration script."""

from __future__ import annotations

import re

from ..config import Settings
from ..models import Line, Script, Shot
from .asr import attach_transcript_to_shots


# --------------------------------------------------------------------------- #
# slot planning
# --------------------------------------------------------------------------- #
def plan_slots(shots: list[Shot], s: Settings) -> list[tuple[Shot, float, float]]:
    """Give every narratable shot a time window, inset by ``speech_margin``.

    Windows never overlap because they live strictly inside non-overlapping
    shots, so the mix stage can place clips without collision.
    """
    out: list[tuple[Shot, float, float]] = []
    for sh in shots:
        if sh.duration <= s.speech_margin * 2 + 0.15:
            continue
        start = sh.start + s.speech_margin
        end = max(start + 0.2, sh.end - s.speech_margin)
        out.append((sh, start, end))
    return out


def capacity_words(slot: float, s: Settings) -> float:
    """How many words fit in ``slot`` seconds at the configured pace.

    ``fill_ratio`` is the share of the slot the voice actually occupies, so
    the narration may use 82% of the window and leave the rest as air.
    """
    return slot / 60.0 * s.target_wpm * s.fill_ratio


def speech_seconds(words: float, s: Settings) -> float:
    """How long ``words`` take to speak at the configured pace."""
    return words / s.target_wpm * 60.0 if s.target_wpm else 0.0


# --------------------------------------------------------------------------- #
# transcript tidying
# --------------------------------------------------------------------------- #
_FILLER = {
    "um", "uh", "er", "ah", "like", "you know", "so", "basically",
    "actually", "right", "okay", "ok", "yeah", "just", "anyway",
}


def clean_transcript(raw: str) -> str:
    """Light clean-up so a raw Whisper dump reads like a sentence."""
    text = re.sub(r"\s+", " ", raw or "").strip()
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    if not text:
        return ""
    words = [w for w in text.split() if w.lower().strip(".,!?") not in _FILLER]
    text = " ".join(words) if words else text
    text = text[0].upper() + text[1:] if text else text
    if text[-1] not in ".!?":
        text += "."
    return text


def rewrite_from_transcript(shots: list[Shot], transcript_segments: list[dict]) -> None:
    """Fallback: when we have words but no vision model, keep the words.

    Each shot adopts the speech that happened during it, lightly cleaned.
    This preserves meaning even though it cannot *improve* the wording.
    """
    attach_transcript_to_shots(shots, transcript_segments)
    for sh in shots:
        if sh.transcript:
            cleaned = clean_transcript(sh.transcript)
            sh.description = cleaned
            sh.analysed_by = "transcript"
            sh.keywords = [sh.kind, "transcript-rewrite"]


# --------------------------------------------------------------------------- #
# build the script
# --------------------------------------------------------------------------- #
def build(info, shots: list[Shot], s: Settings, *,
          transcript_segments: list[dict] | None = None,
          has_speech: bool = False,
          transcript: str = "") -> Script:
    """Assemble a Script from the analysed shots.

    Only backends that genuinely understand the footage (``claude``) or that
    are rewriting what the user actually said (``transcript-rewrite``) are
    allowed to pre-fill spoken text. The offline heuristic backend knows
    structure, not meaning -- so it leaves the words blank and puts what it
    knows in ``note`` for you to write from.
    """
    slots = plan_slots(shots, s)

    described = [sh for sh, _, _ in slots if sh.description.strip()]
    backend = described[0].analysed_by if described else ""
    if backend.startswith("claude"):
        source = "claude"
    elif has_speech and any(sh.analysed_by == "transcript" for sh in described):
        source = "transcript-rewrite"
    else:
        source = "heuristic"

    autofill = source in ("claude", "transcript-rewrite")

    lines: list[Line] = []
    for sh, start, end in slots:
        note = (sh.description or "").strip()
        if not note and not autofill:
            continue
        text = note if autofill else ""
        if autofill and not text:
            continue
        lines.append(Line(
            index=len(lines), start=round(start, 3), end=round(end, 3),
            text=text, shot_index=sh.index,
            note="" if autofill else note,
        ))

    script = Script(lines=lines, source=source)
    script.notes = summarise(info, shots, s, has_speech, source)
    return script


def summarise(info, shots: list[Shot], s: Settings,
              has_speech: bool, source: str) -> str:
    narratable = sum(1 for sh in shots if sh.description.strip())
    budget = info.duration * s.fill_ratio
    existing = "yes, rewritten" if has_speech else "none"
    return (
        f"{len(shots)} shots, {narratable} narratable. "
        f"Speech budget {budget:.0f}s of {info.duration:.0f}s "
        f"(fill {int(s.fill_ratio * 100)}% at {s.target_wpm} wpm). "
        f"Existing narration: {existing}. Source: {source}."
    )


# --------------------------------------------------------------------------- #
# pacing helpers used by the TUI
# --------------------------------------------------------------------------- #
def pacing_report(script: Script, info_duration: float, s: Settings) -> dict:
    """Numbers the TUI shows while you edit."""
    total_words = script.words
    est = total_words / s.target_wpm * 60.0 if s.target_wpm else 0.0
    return {
        "lines": len(script.lines),
        "words": total_words,
        "estimated_seconds": round(est, 1),
        "video_seconds": round(info_duration, 1),
        "fill_percent": round(est / info_duration * 100, 1) if info_duration else 0.0,
        "words_per_minute": round(total_words / (info_duration / 60), 1)
        if info_duration else 0.0,
    }


def overlong_lines(script: Script, s: Settings) -> list[Line]:
    """Lines whose text cannot possibly fit its slot at the target pace."""
    bad = []
    for l in script.lines:
        cap_words = capacity_words(l.duration, s)
        if l.words > cap_words:
            bad.append(l)
    return bad


def suggest_trim(line: Line, s: Settings) -> str:
    """A mechanically shortened version of an over-long line."""
    cap = max(3, int(capacity_words(line.duration, s)))
    words = line.text.split()
    if len(words) <= cap:
        return line.text
    kept = " ".join(words[:cap]).rstrip(",;:")
    if kept and kept[-1] not in ".!?":
        kept += "."
    return kept