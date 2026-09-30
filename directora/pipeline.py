"""End-to-end pipeline: video in, rendered narration out."""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import Settings, WORK_DIR, ensure_dirs
from .media import mix, probe, shots as shots_mod
from .models import Script, Session
from .nlp import asr, script_gen, vision
from .tts import engine as tts

Progress = Callable[[str], None]
VideoSuffixes = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".mpg", ".mpeg"}


def find_videos(root: Path, recursive: bool = True) -> list[Path]:
    """List playable video files under ``root``."""
    root = Path(root).expanduser()
    if root.is_file():
        return [root]
    it = root.rglob("*") if recursive else root.glob("*")
    out = [p for p in it if p.is_file() and p.suffix.lower() in VideoSuffixes]
    return sorted(out, key=lambda p: (p.stat().st_mtime, str(p)), reverse=True)


def session_slug(video: Path) -> str:
    stem = "".join(c if c.isalnum() or c in "-_" else "_" for c in video.stem)[:48]
    h = hashlib.sha1(str(video.resolve()).encode()).hexdigest()[:6]
    return f"{stem}-{h}"


@dataclass
class Stage:
    name: str
    detail: str = ""


class Pipeline:
    """Runs the stages in order, reporting progress through a callback."""

    def __init__(self, settings: Settings):
        self.s = settings

    # -- stage 1 ---------------------------------------------------------- #
    def probe_video(self, video: Path, progress: Progress | None = None) -> Session:
        ensure_dirs()
        if progress:
            progress("Probing media")
        info = probe.probe(video)
        if info.duration <= 0:
            raise probe.MediaError("Could not determine video duration.")
        return Session(video=video, info=info)

    # -- stage 2 ---------------------------------------------------------- #
    def analyse(self, session: Session, progress: Progress | None = None) -> Session:
        """Shots + optional transcript + visual description."""
        assert session.info is not None
        work = self.workdir(session)
        work.mkdir(parents=True, exist_ok=True)

        session.shots = shots_mod.analyse(session.info, self.s, work, progress)

        if session.info.has_audio:
            tr = asr.transcribe(session.video, self.s, work, progress)
            session.transcript = tr.get("text", "")
            session.transcript_segments = tr.get("segments", [])
            session.speech_seconds = tr.get("speech_seconds", 0.0)
            session.detected_wpm = tr.get("wpm", 0.0)
            session.has_speech = tr.get("has_speech", False)
            session.transcript_reliable = tr.get("reliable", True)
            session.mean_logprob = tr.get("mean_logprob")

        analyzer = self._make_analyzer(progress)
        if analyzer.understands_pixels:
            session.shots = analyzer.analyze(
                session.info, session.shots, session.transcript, progress)
        elif session.has_speech and session.transcript_reliable:
            if progress:
                progress("No vision model - preserving your spoken narration as text")
            script_gen.rewrite_from_transcript(
                session.shots, session.transcript_segments)
        else:
            session.shots = analyzer.analyze(
                session.info, session.shots, "", progress)
        return session

    def _make_analyzer(self, progress: Progress | None):
        try:
            return vision.get_analyzer(self.s)
        except Exception as exc:                                # noqa: BLE001
            if progress:
                progress(f"{exc}  -- falling back to the offline backend")
            self.s.vision_backend = "heuristic"
            return vision.HeuristicAnalyzer(self.s)

    # -- stage 3 ---------------------------------------------------------- #
    def write_script(self, session: Session, progress: Progress | None = None) -> Script:
        assert session.info is not None
        if progress:
            progress("Planning narration slots")
        session.script = script_gen.build(
            session.info, session.shots, self.s,
            transcript_segments=session.transcript_segments,
            has_speech=session.has_speech,
            transcript=session.transcript)
        return session.script

    # -- stage 4 ---------------------------------------------------------- #
    def generate_voice(self, session: Session, voice: str | None = None,
                       progress: Progress | None = None) -> int:
        work = self.workdir(session) / "tts"
        voice = voice or self.s.voice
        return tts.render_script(session.script.lines, work, voice,
                                 self.s.rate, self.s.pitch, progress)

    def preview_line(self, session: Session, index: int) -> Path | None:
        work = self.workdir(session) / "tts"
        line = next((l for l in session.script.lines if l.index == index), None)
        if line is None:
            return None
        tts.render_one(line, work, self.s.voice, self.s.rate, self.s.pitch)
        return line.clip

    # -- stage 5 ---------------------------------------------------------- #
    def render(self, session: Session, out: Path | None = None,
               progress: Progress | None = None) -> dict:
        assert session.info is not None
        work = self.workdir(session)
        out = out or session.video.with_name(
            f"{session.video.stem}_voiced{session.video.suffix}")
        if progress:
            progress("Building narration timeline")
        made = mix.render(
            session.video, session.script.lines, out, work,
            target_lufs=self.s.target_lufs, true_peak=self.s.true_peak,
            keep_original=self.s.keep_original_audio,
            duck_threshold=self.s.duck_threshold,
            duck_ratio=self.s.duck_ratio)
        session.output = out
        made["overruns"] = mix.overruns(session.script.lines)
        return made

    # -- agent-facing stages ---------------------------------------------- #
    def watch(self, video: Path, progress: Progress | None = None) -> Session:
        """Analyse without writing anything.

        This is the "agent looks at the video" stage: it produces shot
        boundaries, keyframe paths, visual statistics and any transcript, so
        an external agent can inspect the result and author a script.
        """
        session = self.probe_video(video, progress)
        session = self.analyse(session, progress)
        return session

    def apply_script(self, session: Session, script_path: Path,
                     progress: Progress | None = None,
                     strict: bool = False) -> Script:
        """Adopt an agent-written script file and place it on the timeline."""
        from .nlp import script_io

        if progress:
            progress(f"Loading script from {Path(script_path).name}")
        script, opts = script_io.load(Path(script_path), session, self.s,
                                      default_voice=self.s.voice)
        for key in ("voice", "rate", "pitch"):
            if opts.get(key):
                setattr(self.s, key, opts[key])

        warnings = script_io.check(script, session, self.s)
        if warnings:
            for w in warnings:
                if progress:
                    progress(f"warning: {w}")
            if strict:
                raise script_io.ScriptError(
                    "script rejected by --strict:\n  " + "\n  ".join(warnings))

        session.script = script
        if progress:
            progress(f"{len(script.lines)} line(s), {script.words} words")
        return script

    def narrate(self, video: Path, out: Path | None = None,
                progress: Progress | None = None,
                script_path: Path | None = None) -> Session:
        """The full agentic run.

        With ``script_path`` the agent supplies the words. Without it the
        vision backend writes them. Either way it is spoken and muxed.
        """
        session = self.probe_video(video, progress)
        session = self.analyse(session, progress)

        if script_path:
            self.apply_script(session, Path(script_path), progress)
        else:
            if self.s.vision_backend == "heuristic" and not session.has_speech:
                if progress:
                    progress(
                        "No vision model and no speech to rewrite. Set "
                        "ANTHROPIC_API_KEY with vision_backend=claude, or pass "
                        "--script with an agent-written narration.")
            self.write_script(session, progress)

        self.generate_voice(session, progress=progress)
        self.render(session, out, progress)
        return session

    # -- helpers ---------------------------------------------------------- #
    def workdir(self, session: Session) -> Path:
        d = WORK_DIR / session_slug(session.video)
        if d.exists() and self.s.verbose:
            shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
        return d

    # -- all -------------------------------------------------------------- #
    def run_all(self, video: Path, out: Path | None = None,
                progress: Progress | None = None) -> Session:
        session = self.probe_video(video, progress)
        session = self.analyse(session, progress)
        self.write_script(session, progress)
        self.generate_voice(session, progress=progress)
        self.render(session, out, progress)
        return session