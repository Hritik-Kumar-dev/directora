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


def _write(tmp_path, data) -> Path:
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


def test_cli_has_the_documented_commands():
    from directora.cli import build_parser
    parser = build_parser()
    actions = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
    names = set()
    for a in actions:
        if isinstance(a.choices, dict):
            names |= set(a.choices)
    assert {"watch", "template", "check", "narrate", "voices", "config"} <= names


def test_tts_rate_suggestion():
    from directora.tts.engine import suggest_rate
    assert suggest_rate(190.0, 145).startswith("-")
    assert suggest_rate(100.0, 145).startswith("+")