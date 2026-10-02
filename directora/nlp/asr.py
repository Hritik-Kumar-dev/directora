"""Speech detection + transcription via faster-whisper (CPU, offline)."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from ..config import Settings

Progress = Callable[[str], None]


class ASRUnavailable(RuntimeError):
    pass


def _extract_wav(video: Path, work: Path) -> Path:
    """16 kHz mono PCM -- what Whisper actually wants."""
    out = work / "audio16k.wav"
    cmd = [
        "ffmpeg", "-hide_banner", "-v", "error", "-y", "-i", str(video),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out),
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if p.returncode != 0 or not out.exists():
        raise ASRUnavailable(f"Could not extract audio:\n{p.stderr[-300:]}")
    return out


def transcribe(video: Path, s: Settings, work: Path,
               progress: Progress | None = None) -> dict:
    """Transcribe narration.

    Returns ``{text, segments:[{start,end,text}], speech_seconds, wpm, has_speech}``.
    A missing/whisper-less install degrades gracefully to ``has_speech=False``
    rather than crashing the pipeline.
    """
    def tick(m):
        if progress:
            progress(m)

    empty = {"text": "", "segments": [], "speech_seconds": 0.0,
             "wpm": 0.0, "has_speech": False}

    if not s.detect_speech:
        tick("Speech detection disabled")
        return empty

    tick("Extracting audio track")
    try:
        wav = _extract_wav(video, work)
    except ASRUnavailable as exc:
        tick(f"No audio to transcribe ({exc})")
        return empty

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        tick("faster-whisper not installed - skipping transcription")
        return empty

    tick(f"Loading whisper model '{s.whisper_model}' (first run downloads it)")
    try:
        model = WhisperModel(s.whisper_model, device="cpu", compute_type="int8")
    except Exception as exc:                                   # noqa: BLE001
        tick(f"Could not load whisper model: {exc}")
        return empty

    tick("Transcribing")
    try:
        segments, info = model.transcribe(
            str(wav),
            language=s.language or None,     # None = auto-detect
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 400},
            beam_size=5,
            condition_on_previous_text=False,
        )
        segs = []
        logprobs = []
        for seg in segments:
            text = (seg.text or "").strip()
            if not text:
                continue
            segs.append({"start": round(float(seg.start), 3),
                         "end": round(float(seg.end), 3),
                         "text": text})
            if getattr(seg, "avg_logprob", None) is not None:
                logprobs.append(float(seg.avg_logprob))
    except Exception as exc:                                   # noqa: BLE001
        tick(f"Transcription failed: {exc}")
        return empty

    speech = sum(x["end"] - x["start"] for x in segs)
    words = sum(len(x["text"].split()) for x in segs)
    text = " ".join(x["text"] for x in segs).strip()

    if speech < 0.4 or not text:
        tick("No speech detected")
        return empty

    # A low mean logprob means the model was guessing. That usually means the
    # audio is not narration at all -- film dialogue, music, background chatter
    # -- so the transcript must not be used as a rewriting baseline.
    mean_logprob = sum(logprobs) / len(logprobs) if logprobs else None
    reliable = mean_logprob is None or mean_logprob > -0.70

    tick(f"Found {words} words over {speech:.1f}s of speech "
        f"({words / (speech / 60):.0f} wpm)")
    if not reliable:
        tick(f"low confidence (logprob {mean_logprob:.2f}) - likely film or "
            f"music rather than narration; not using it as a baseline")

    return {
        "text": text,
        "segments": segs,
        "speech_seconds": round(speech, 3),
        "wpm": round(words / (speech / 60), 2),
        "has_speech": True,
        "reliable": reliable,
        "mean_logprob": None if mean_logprob is None else round(mean_logprob, 3),
        "language": getattr(info, "language", "") or "",
    }


def attach_transcript_to_shots(shots, segments) -> None:
    """Copy each transcript fragment into the shot that contains it."""
    for seg in segments:
        mid = (seg["start"] + seg["end"]) / 2
        for sh in shots:
            if sh.start <= mid < sh.end:
                sh.transcript = (sh.transcript + " " + seg["text"]).strip()
                break
