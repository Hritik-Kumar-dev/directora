"""Narration synthesis via Microsoft Edge's neural voices (edge-tts)."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Callable, Iterable

from ..models import Line

Progress = Callable[[str], None]


class TTSError(RuntimeError):
    pass


VOICES: dict[str, list[str]] = {
    "British (en-GB)": [
        "en-GB-RyanNeural", "en-GB-SoniaNeural",
        "en-GB-LibbyNeural", "en-GB-ThomasNeural", "en-GB-MaisieNeural",
    ],
    "American (en-US)": [
        "en-US-GuyNeural", "en-US-JennyNeural", "en-US-AriaNeural",
        "en-US-ChristopherNeural", "en-US-AnaNeural", "en-US-EricNeural",
    ],
    "Indian (en-IN)": [
        "en-IN-AaravNeural", "en-IN-NeerjaNeural",
        "en-IN-PrabhatNeural", "en-IN-RaveenaNeural",
    ],
    "Australian (en-AU)": ["en-AU-WilliamNeural", "en-AU-NatashaNeural"],
    "Irish (en-IE)": ["en-IE-EmilyNeural", "en-IE-ConnorNeural"],
}


def _edge():
    try:
        import edge_tts
    except ImportError as exc:
        raise TTSError(
            "edge-tts is required:  pip install edge-tts"
        ) from exc
    return edge_tts


def _run(coro):
    """Run a coroutine from anywhere -- including inside a running loop.

    The TUI calls these helpers while Textual's event loop is already running,
    where a bare ``asyncio.run`` raises. In that case we hop onto a worker
    thread that owns a loop of its own.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def list_voices(locale: str | None = None) -> list[str]:
    """Enumerate voices available from the service."""
    mod = _edge()

    async def go() -> list[str]:
        voices = await mod.list_voices()
        out = []
        for v in voices:
            if locale and not v.get("ShortName", "").startswith(locale):
                continue
            out.append(v.get("ShortName", ""))
        return sorted(x for x in out if x)

    try:
        return _run(go())
    except Exception as exc:                                    # noqa: BLE001
        raise TTSError(f"Could not list voices: {exc}") from exc


def available_locales() -> list[str]:
    """Locales the service can speak, e.g. en-GB, en-IN, hi, ne."""
    mod = _edge()

    async def go() -> list[str]:
        # list_voices() is a coroutine returning a list, not an async generator
        voices = await mod.list_voices()
        return sorted({(v.get("Locale") or "")[:5] for v in voices
                       if (v.get("Locale") or "")[:5]})

    try:
        out = _run(go())
        return out or ["en-GB", "en-US", "en-IN", "en-AU", "en-IE"]
    except Exception:                                           # noqa: BLE001
        return ["en-GB", "en-US", "en-IN", "en-AU", "en-IE"]


# --------------------------------------------------------------------------- #
# synthesis
# --------------------------------------------------------------------------- #
_SSML_STRIP = re.compile(r"<[^>]+>")


def _duration_of(path: Path) -> float:
    """Duration in seconds of a rendered audio file."""
    import subprocess
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True)
    try:
        return float(p.stdout.strip())
    except ValueError:
        return 0.0


def speak(text: str, voice: str, rate: str, pitch: str, out: Path) -> float:
    """Render one line to ``out`` (mp3). Returns duration in seconds."""
    mod = _edge()
    clean = _SSML_STRIP.sub("", text).strip()
    if not clean:
        raise TTSError("Nothing to synthesise (empty line)")

    out.parent.mkdir(parents=True, exist_ok=True)

    async def go() -> None:
        comm = mod.Communicate(clean, voice, rate=rate, pitch=pitch)
        await comm.save(str(out))

    try:
        _run(go())
    except Exception as exc:                                    # noqa: BLE001
        raise TTSError(f"TTS failed for {voice}: {exc}") from exc

    return round(_duration_of(out), 3)


def render_script(lines: Iterable[Line], outdir: Path, voice: str, rate: str,
                  pitch: str, progress: Progress | None = None) -> int:
    """Render every non-empty line of a script. Returns how many succeeded.

    Failures are reported rather than swallowed -- a silent zero here used to
    look like a successful run right up until the mux stage failed.
    """
    outdir.mkdir(parents=True, exist_ok=True)
    todo = [l for l in lines if l.text.strip()]
    skipped = len(list(lines)) - len(todo)
    if skipped and progress:
        progress(f"{skipped} blank line(s) skipped — fill them in first")

    ok = 0
    failures: list[str] = []
    for i, line in enumerate(todo, 1):
        if progress:
            progress(f"Synthesising {i}/{len(todo)}: {line.text[:48]}...")
        path = outdir / f"line{line.index:03d}.mp3"
        try:
            dur = speak(line.text, voice, rate, pitch, path)
        except TTSError as exc:
            failures.append(f"line {line.index + 1}: {exc}")
            if progress:
                progress(f"[red]failed:[/red] {exc}")
            continue
        line.clip = path
        line.clip_dur = dur
        line.voice = voice
        ok += 1

    if not todo:
        raise TTSError("Every line is blank — write the narration first.")
    if ok == 0:
        detail = failures[0] if failures else "unknown error"
        raise TTSError(f"All {len(todo)} line(s) failed to synthesise ({detail})")
    if failures and progress:
        progress(f"[yellow]{len(failures)} line(s) failed; {ok} rendered[/yellow]")
    return ok


def render_one(line: Line, outdir: Path, voice: str, rate: str, pitch: str) -> float:
    """Render a single line (used by the TUI's preview button)."""
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / f"line{line.index:03d}.mp3"
    dur = speak(line.text, voice, rate, pitch, path)
    line.clip = path
    line.clip_dur = round(dur, 3)
    line.voice = voice
    return dur


def suggest_rate(detected_wpm: float, target_wpm: int) -> str:
    """Nudge edge-tts rate so the new VO matches the speaker's natural pace."""
    if detected_wpm <= 1:
        return "+0%"
    ratio = (detected_wpm / max(target_wpm, 1)) ** -1.0
    pct = int(round((ratio - 1.0) * 100))
    return f"{pct:+d}%"