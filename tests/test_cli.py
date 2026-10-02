"""Tests for the CLI, the agent script format, and the mix pipeline."""

from __future__ import annotations

import json

import pytest

from directora.config import Settings
from directora.models import Line, MediaInfo, Script, Session, Shot
from directora.nlp import script_gen, script_io


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _session(tmp_path) -> Session:
    info = MediaInfo(path=tmp_path / "demo.mp4", duration=25.0, width=1920,
                     height=1080, fps=60.0, has_audio=True, audio_codec="aac",
                     audio_rate=48000, audio_channels=2, video_codec="h264")
    shots = [
        Shot(index=0, start=0.0, end=8.0, description="crop editor"),
        Shot(index=1, start=8.0, end=16.0, description="gallery grid"),
        Shot(index=2, start=16.0, end=25.0, description="export"),
    ]
    return Session(video=tmp_path / "demo.mp4", info=info, shots=shots,
                   script=Script(lines=[], source="heuristic"))


# --------------------------------------------------------------------------- #
# pacing maths
# --------------------------------------------------------------------------- #
def test_capacity_words_respects_fill_ratio():
    s = Settings()
    slot = 10.0
    cap = script_gen.capacity_words(slot, s)
    # 10s slot, 145 wpm, filling 82% -> ~19.8 words
    assert 19.0 < cap < 20.5
    # a full-speed line would be ~24 words, so fill must actually reduce it
    assert cap < slot / 60.0 * s.target_wpm


def test_speech_seconds_roundtrip():
    s = Settings()
    assert script_gen.speech_seconds(145, s) == pytest.approx(60.0, abs=0.1)


def test_overlong_detection_and_trim():
    s = Settings()
    long_line = Line(index=0, start=0, end=2.0, text=" ".join(["word"] * 60))
    assert script_gen.overlong_lines(Script(lines=[long_line]), s)
    trimmed = script_gen.suggest_trim(long_line, s)
    assert len(trimmed.split()) <= int(script_gen.capacity_words(2.0, s)) + 1


def test_pacing_report_ignores_blank_lines():
    script = Script(lines=[
        Line(index=0, start=0, end=10, text="one two three"),
        Line(index=1, start=10, end=20, text=""),
    ])
    rep = script_gen.pacing_report(script, 60.0, Settings())
    assert rep["words"] == 3
    assert rep["lines"] == 2


def test_transcript_cleanup():
    from directora.nlp.script_gen import clean_transcript
    out = clean_transcript("um so basically we just uh click the button")
    assert "um" not in out.lower()
    assert out.endswith(".")


# --------------------------------------------------------------------------- #
# agent script format
# --------------------------------------------------------------------------- #
def test_template_has_one_line_per_shot(tmp_path):
    tpl = script_io.template(_session(tmp_path), Settings())
    assert len(tpl["lines"]) == 3
    assert tpl["lines"][0]["shot"] == 0
    assert tpl["lines"][0]["text"] == ""
    assert tpl["lines"][0]["max_words"] > 0
    assert "_hint" in tpl["lines"][0]


def _write(tmp_path, data):
    p = tmp_path / "script.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_load_inherits_times_from_shot(tmp_path):
    sess = _session(tmp_path)
    p = _write(tmp_path, {"lines": [{"shot": 1, "text": "Gallery view here."}]})
    script, _ = script_io.load(p, sess, Settings())
    line = script.lines[0]
    assert line.shot_index == 1
    assert sess.shots[1].start < line.start < sess.shots[1].end
    assert line.note == "gallery grid"


def test_load_explicit_times_win_over_shot(tmp_path):
    sess = _session(tmp_path)
    p = _write(tmp_path, {"lines": [
        {"shot": 0, "start": 1.0, "end": 5.0, "text": "Explicit window."}]})
    script, _ = script_io.load(p, sess, Settings())
    assert script.lines[0].start == 1.0
    assert script.lines[0].end == 5.0


def test_load_estimates_end_from_start(tmp_path):
    sess = _session(tmp_path)
    p = _write(tmp_path, {"lines": [{"start": 2.0, "text": "Just a start."}]})
    script, _ = script_io.load(p, sess, Settings())
    assert script.lines[0].start == 2.0
    assert script.lines[0].end > 2.0


def test_load_accepts_bare_list_and_strings(tmp_path):
    sess = _session(tmp_path)
    p = _write(tmp_path, ["A plain string line.", {"start": 10.0,
                                                    "text": "Second."}])
    script, _ = script_io.load(p, sess, Settings())
    assert len(script.lines) == 2
    assert script.lines[0].text.startswith("A plain string")


def test_load_sorts_and_deoverlaps(tmp_path):
    sess = _session(tmp_path)
    p = _write(tmp_path, {"lines": [
        {"start": 10.0, "text": "Second in time."},
        {"start": 1.0, "text": "First in time."},
        {"start": 2.0, "text": "Overlapping badly here."},
    ]})
    script, _ = script_io.load(p, sess, Settings())
    starts = [l.start for l in script.lines]
    assert starts == sorted(starts)
    for a, b in zip(script.lines, script.lines[1:]):
        assert b.start >= a.end


def test_load_rejects_unusable_script(tmp_path):
    sess = _session(tmp_path)
    p = _write(tmp_path, {"lines": [{"shot": 0, "text": "   "}]})
    with pytest.raises(script_io.ScriptError):
        script_io.load(p, sess, Settings())


def test_load_reports_unknown_shot(tmp_path):
    sess = _session(tmp_path)
    p = _write(tmp_path, {"lines": [{"shot": 99, "text": "Nope."}]})
    with pytest.raises(script_io.ScriptError):
        script_io.load(p, sess, Settings())


def test_load_honours_voice_overrides(tmp_path):
    sess = _session(tmp_path)
    p = _write(tmp_path, {
        "voice": "en-IN-NeerjaNeural", "rate": "-4%",
        "lines": [{"shot": 0, "text": "Indian English please."}]})
    _, opts = script_io.load(p, sess, Settings())
    assert opts["voice"] == "en-IN-NeerjaNeural"
    assert opts["rate"] == "-4%"


def test_check_flags_overlong_lines(tmp_path):
    sess = _session(tmp_path)
    script = Script(lines=[
        Line(index=0, start=0.0, end=2.0, text=" ".join(["word"] * 40))])
    warnings = script_io.check(script, sess, Settings())
    assert any("fit in" in w for w in warnings)


def test_check_flags_running_past_the_end(tmp_path):
    sess = _session(tmp_path)
    script = Script(lines=[
        Line(index=0, start=20.0, end=99.0, text="Way too late.")])
    warnings = script_io.check(script, sess, Settings())
    assert any("past the end" in w for w in warnings)


def test_check_clean_script_has_no_warnings(tmp_path):
    sess = _session(tmp_path)
    script = Script(lines=[
        Line(index=0, start=0.5, end=6.0, text="Short and fine."),
        Line(index=1, start=9.0, end=14.0, text="Also fine here.")])
    assert script_io.check(script, sess, Settings()) == []


def test_manifest_exposes_keyframes_and_transcript(tmp_path):
    sess = _session(tmp_path)
    sess.transcript = "hello there"
    sess.transcript_segments = [{"start": 0.0, "end": 1.0, "text": "hello there"}]
    sess.shots[0].transcript = "hello there"
    sess.has_speech = True
    m = script_io.manifest(sess)
    assert m["shot_count"] == 3
    assert m["speech_detected"] is True
    assert m["shots"][0]["spoken_during_shot"] == "hello there"
    assert m["duration"] == 25.0


# --------------------------------------------------------------------------- #
# session persistence
# --------------------------------------------------------------------------- #
def test_session_roundtrip(tmp_path):
    sess = _session(tmp_path)
    sess.script = Script(lines=[
        Line(index=0, start=0.0, end=5.0, text="Persisted line.",
             shot_index=0, note="a hint")])
    p = tmp_path / "s.json"
    sess.save(p)
    back = Session.load(p)
    assert back.info.duration == 25.0
    assert back.script.lines[0].text == "Persisted line."
    assert back.script.lines[0].note == "a hint"
    assert back.script.source == "heuristic"


# --------------------------------------------------------------------------- #
# misc
# --------------------------------------------------------------------------- #
def test_srt_timestamps():
    from directora.media.mix import srt_timestamp
    assert srt_timestamp(0) == "00:00:00,000"
    assert srt_timestamp(3661.5) == "01:01:01,500"


def test_config_env_override(monkeypatch):
    monkeypatch.setenv("DIRECTORA_TARGET_WPM", "120")
    monkeypatch.setenv("DIRECTORA_KEEP_ORIGINAL_AUDIO", "false")
    s = Settings.load()
    assert s.target_wpm == 120
    assert s.keep_original_audio is False


def test_cli_rejects_narrate_without_a_script_source(monkeypatch, tmp_path):
    """The offline backend must not be allowed to invent narration."""
    from directora.cli import main
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("DIRECTORA_VISION_BACKEND", "heuristic")
    rc = main(["narrate", str(tmp_path / "missing.mp4")])
    assert rc == 2


def test_tts_rate_suggestion():
    from directora.tts.engine import suggest_rate
    assert suggest_rate(190.0, 145).startswith("-")
    assert suggest_rate(100.0, 145).startswith("+")


# --------------------------------------------------------------------------- #
# vision backend
# --------------------------------------------------------------------------- #
def test_payload_reports_real_resolution_not_a_shot_index():
    """Regression: the prompt once said "Resolution: 3", a shot index."""
    from pathlib import Path

    from directora.nlp.vision import ClaudeAnalyzer
    a = object.__new__(ClaudeAnalyzer)
    a.s = Settings()
    info = MediaInfo(path=Path("x.mp4"), duration=30.0, width=1920,
                     height=1080, fps=30.0, has_audio=True)
    shots = [Shot(index=3, start=0.0, end=10.0, description="d")]
    text = a._payload(info, shots, "")[0]["text"]
    assert "Resolution: 1920x1080 (landscape)" in text
    assert "Resolution: 3" not in text
    assert "30.00 fps" in text


def test_payload_flags_portrait_video():
    from pathlib import Path

    from directora.nlp.vision import ClaudeAnalyzer
    a = object.__new__(ClaudeAnalyzer)
    a.s = Settings()
    info = MediaInfo(path=Path("x.mp4"), duration=5.0, width=1080,
                     height=1920, fps=25.0, has_audio=False)
    text = a._payload(info, [Shot(index=0, start=0.0, end=5.0)], "")[0]["text"]
    assert "1080x1920 (portrait)" in text


@pytest.mark.parametrize("ids,expected", [
    # a dated snapshot sorts before a newer name lexically: '3' < 's'
    (["claude-3-5-sonnet-20241022", "claude-sonnet-4-5"], "claude-sonnet-4-5"),
    (["claude-3-5-sonnet-20241022", "claude-3-5-sonnet-20240620"],
     "claude-3-5-sonnet-20241022"),
    (["claude-sonnet-4-20250514", "claude-sonnet-4-5-20250929"],
     "claude-sonnet-4-5-20250929"),
])
def test_model_resolution_prefers_newest(ids, expected):
    from directora.nlp.vision import ClaudeAnalyzer
    assert ClaudeAnalyzer._best(ids) == expected


# --------------------------------------------------------------------------- #
# tts cache
# --------------------------------------------------------------------------- #
def test_fingerprint_tracks_every_input():
    from directora.tts.engine import fingerprint
    base = fingerprint("Hello there.", "en-GB-RyanNeural", "-8%", "+0Hz")
    assert base == fingerprint("Hello there.", "en-GB-RyanNeural", "-8%", "+0Hz")
    # wording changes must invalidate
    assert base != fingerprint("Hello there!", "en-GB-RyanNeural", "-8%", "+0Hz")
    # as must voice, rate and pitch
    assert base != fingerprint("Hello there.", "en-GB-SoniaNeural", "-8%", "+0Hz")
    assert base != fingerprint("Hello there.", "en-GB-RyanNeural", "+0%", "+0Hz")
    assert base != fingerprint("Hello there.", "en-GB-RyanNeural", "-8%", "+5Hz")
    # whitespace and stray markup should not
    assert base == fingerprint("  Hello there.  ", "en-GB-RyanNeural", "-8%", "+0Hz")


def test_cached_speak_reuses_an_existing_clip(tmp_path, monkeypatch):
    """A second render of identical text must not hit the network."""
    from directora.tts import engine

    calls = []

    def fake_speak(text, voice, rate, pitch, out):
        calls.append(text)
        out.write_bytes(b"fake-mp3")
        return 1.25

    monkeypatch.setattr(engine, "speak", fake_speak)

    first, d1, reused1 = engine.cached_speak(
        "Reuse me.", "v", "-8%", "+0Hz", tmp_path / "clip.mp3")
    second, d2, reused2 = engine.cached_speak(
        "Reuse me.", "v", "-8%", "+0Hz", tmp_path / "clip.mp3")

    assert len(calls) == 1, "second render should not re-synthesise"
    assert reused1 is False and reused2 is True
    assert first == second and d1 == d2 == 1.25


def test_cached_speak_re_synthesises_when_text_changes(tmp_path, monkeypatch):
    from directora.tts import engine
    calls = []
    monkeypatch.setattr(engine, "speak",
                        lambda t, v, r, p, o: (calls.append(t),
                                                o.write_bytes(b"x"), 1.0)[-1])
    engine.cached_speak("One.", "v", "-8%", "+0Hz", tmp_path / "clip.mp3")
    engine.cached_speak("Two.", "v", "-8%", "+0Hz", tmp_path / "clip.mp3")
    assert calls == ["One.", "Two."]


def test_cached_speak_rejects_blank_text(tmp_path):
    from directora.tts import engine
    with pytest.raises(engine.TTSError):
        engine.cached_speak("   ", "v", "-8%", "+0Hz", tmp_path / "clip.mp3")


# --------------------------------------------------------------------------- #
# mix: the video stream must be byte-identical
# --------------------------------------------------------------------------- #
def _have_ffmpeg() -> bool:
    import shutil
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _stream_md5(path) -> str:
    import subprocess
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path),
                        "-map", "0:v", "-c", "copy", "-f", "md5", "-"],
                       capture_output=True, text=True)
    return p.stdout.strip()


@pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg/ffprobe not installed")
def test_mux_stream_copies_the_video_exactly(tmp_path):
    """The README promises the picture is never re-encoded. Prove it."""
    import subprocess

    from directora.media import mix

    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=320x180:rate=15:duration=3",
         "-f", "lavfi", "-i", "sine=frequency=300:duration=3",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", str(src)],
        check=True, capture_output=True)

    # a stand-in for the normalised narration track
    voice = tmp_path / "voice.wav"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", "sine=frequency=660:duration=3", str(voice)],
        check=True, capture_output=True)

    out = tmp_path / "out.mp4"
    mix.mux(src, voice, out, keep_original=True)

    assert out.exists()
    assert _stream_md5(src) == _stream_md5(out)


@pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg/ffprobe not installed")
def test_write_srt_clamps_to_the_rendered_clip(tmp_path):
    """A line whose audio is longer than its slot must not over-run in the srt."""
    from directora.media.mix import write_srt
    line = Line(index=0, start=10.0, end=11.0, text="Short slot.",
                clip=tmp_path / "c.mp3", clip_dur=3.5)
    p = write_srt([line], tmp_path / "o.srt")
    body = p.read_text(encoding="utf-8")
    assert "00:00:10,000 --> 00:00:11,000" in body


# --------------------------------------------------------------------------- #
# cli surface
# --------------------------------------------------------------------------- #
def test_cli_has_the_documented_commands():
    from directora.cli import build_parser
    parser = build_parser()
    actions = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
    names = set()
    for a in actions:
        if isinstance(a.choices, dict):
            names |= set(a.choices)
    assert {"watch", "template", "check", "narrate", "voices", "config",
            "preview", "batch", "scan"} <= names


def test_batch_refuses_without_a_narration_source(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("DIRECTORA_VISION_BACKEND", "heuristic")
    (tmp_path / "demo.mp4").write_bytes(b"not really a video")
    from directora.cli import main
    assert main(["batch", str(tmp_path)]) == 2


def test_batch_reports_no_videos(tmp_path, monkeypatch):
    monkeypatch.setattr("directora.cli.find_videos", lambda *a, **k: [])
    from directora.cli import main
    assert main(["batch", str(tmp_path)]) == 1


def test_config_file_is_not_world_readable(tmp_path, monkeypatch):
    """The config can hold an API key, so it must not be 0644."""
    import stat

    from directora import cli
    monkeypatch.setattr(cli, "CONFIG_FILE", tmp_path / "config.toml")
    s = Settings()
    s.extra.setdefault("_keys", {})["anthropic_api_key"] = "sk-ant-test"
    cli._write_config(s)
    mode = stat.S_IMODE((tmp_path / "config.toml").stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"
