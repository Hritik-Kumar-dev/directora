"""Directora command line.

The intended flow is agentic and unattended::

    # 1. the agent looks at the video
    directora watch demo.mp4 > analysis.json

    # 2. the agent writes the narration against that analysis
    #    (fill in "text" for each line, save as script.json)

    # 3. the agent hands it back to be spoken and muxed
    directora narrate demo.mp4 --script script.json -o demo_voiced.mp4

Steps 1 and 2 can be skipped entirely when a Claude key is available, in which
case ``directora narrate demo.mp4`` does the whole job by itself.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import CONFIG_FILE, SESSION_DIR, Settings, ensure_dirs
from .models import Session
from .nlp import script_gen, script_io
from .pipeline import Pipeline, find_videos, session_slug


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _human(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(int(t), 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _ticker(quiet: bool):
    def tick(msg: str) -> None:
        if not quiet:
            print(f"  · {msg}", file=sys.stderr, flush=True)
    return tick


def _settings(args) -> Settings:
    s = Settings.load()
    for attr, value in (("vision_backend", getattr(args, "backend", None)),
                        ("claude_model", getattr(args, "model", None)),
                        ("voice", getattr(args, "voice", None)),
                        ("rate", getattr(args, "rate", None)),
                        ("pitch", getattr(args, "pitch", None)),
                        ("whisper_model", getattr(args, "whisper", None))):
        if value:
            setattr(s, attr, value)
    if getattr(args, "wpm", None):
        s.target_wpm = args.wpm
    if getattr(args, "fill", None):
        s.fill_ratio = args.fill
    if getattr(args, "original", None) is False:
        s.keep_original_audio = False
    return s


def _save(session: Session) -> Path:
    ensure_dirs()
    path = SESSION_DIR / f"{session_slug(session.video)}.json"
    session.save(path)
    return path


def _summary(session: Session, s: Settings, out: Path | None,
             srt: Path | None) -> None:
    info = session.info
    print()
    print(f"  video     : {session.video}")
    print(f"  duration  : {_human(info.duration)}  ({info.resolution}, "
          f"{info.fps:.0f} fps)")
    print(f"  shots     : {len(session.shots)}")
    if session.has_speech:
        print(f"  heard     : {len(session.transcript.split())} words @ "
              f"{session.detected_wpm:.0f} wpm")
    rep = script_gen.pacing_report(session.script, info.duration, s)
    print(f"  script    : {rep['lines']} lines, {rep['words']} words, "
          f"~{rep['estimated_seconds']:.0f}s ({rep['fill_percent']:.0f}% fill) "
          f"[{session.script.source}]")
    print(f"  voice     : {s.voice} @ {s.rate}")
    if out:
        print(f"  output    : {out}")
    if srt:
        print(f"  subtitles : {srt}")
    print()


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_watch(args) -> int:
    """Analyse a video and emit a manifest for an agent to reason about."""
    s = _settings(args)
    pipe = Pipeline(s)
    video = Path(args.video).expanduser()
    session = pipe.watch(video, _ticker(args.quiet))
    _save(session)

    data = script_io.manifest(session)
    if args.format == "json":
        print(json.dumps(data, indent=1, ensure_ascii=False))
    elif args.format == "compact":
        for sh in session.shots:
            spoken = f"  speech={sh.transcript[:60]!r}" if sh.transcript else ""
            print(f"[{sh.index:02d}] {_human(sh.start)}-{_human(sh.end)} "
                  f"({sh.duration:5.1f}s) {sh.kind:6s} "
                  f"motion={sh.motion:.4f} edge={sh.edge_density:.3f} "
                  f"frame={sh.frame or '-'}{spoken}")
    else:  # human readable
        print()
        print(f"  {video.name}  ·  {session.info.resolution}  ·  "
              f"{session.info.duration:.1f}s  ·  {len(session.shots)} shots")
        if session.has_speech:
            print(f"  speech: {len(session.transcript.split())} words @ "
                  f"{session.detected_wpm:.0f} wpm")
            print(f"  \"{session.transcript[:300]}\"")
        print()
        for sh in session.shots:
            note = f"  speech: {sh.transcript[:70]}" if sh.transcript else ""
            print(f"  [{sh.index:02d}] {_human(sh.start)}-{_human(sh.end)} "
                  f"({sh.duration:5.1f}s) {sh.kind:6s} "
                  f"motion={sh.motion:.4f} edge={sh.edge_density:.3f}")
            if sh.frame:
                print(f"       frame: {sh.frame}")
            if note:
                print(f"      {note}")
        print()

    if args.template:
        tpl = script_io.template(session, s)
        out = Path(args.template)
        script_io.dump(out, tpl)
        print(f"  script template written to {out}", file=sys.stderr)
    return 0


def cmd_narrate(args) -> int:
    """The whole job: analyse, script, speak, mix."""
    s = _settings(args)

    if not args.script and s.vision_backend != "claude" and not s.claude_key():
        print("error: nothing can write the narration for you.\n"
              "\n"
              "  This video has no existing narration to rewrite, and the offline\n"
              "  backend measures structure but cannot read the screen, so it will\n"
              "  not invent words for you.\n"
              "\n"
              "  Option A - let an API write the script (one command):\n"
              "\n"
              "    1. create an API key at  https://console.anthropic.com/settings/keys\n"
              "       (the API is pay-as-you-go and needs a card on file)\n"
              "    2. export ANTHROPIC_API_KEY='sk-ant-...'\n"
              "    3. directora config --set vision_backend=claude\n"
              "    4. directora narrate FILE -o OUT.mp4\n"
              "\n"
              "  Option B - you write the script (no key, no cost):\n"
              "\n"
              "    directora template FILE -o script.json\n"
              "    # fill in each line's \"text\", then:\n"
              "    directora check   FILE --script script.json\n"
              "    directora narrate FILE --script script.json -o OUT.mp4\n"
              "\n"
              "  Check what the tool can see at any time:  directora config",
              file=sys.stderr)
        return 2

    if not args.script and s.vision_backend == "claude":
        s.vision_backend = "claude"
        try:
            s.require_claude()
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    pipe = Pipeline(s)
    video = Path(args.video).expanduser()
    out = Path(args.out).expanduser() if args.out else None
    tick = _ticker(args.quiet)

    if args.script:
        tick(f"using agent script {Path(args.script).name}")
        session = pipe.narrate(video, out, tick,
                              script_path=Path(args.script).expanduser())
    else:
        session = pipe.narrate(video, out, tick)

    srt = out.with_suffix(".srt") if out else None
    if out is None and session.output:
        srt = session.output.with_suffix(".srt")
    _save(session)
    _summary(session, s, session.output, srt if not args.no_srt else None)
    return 0


def cmd_template(args) -> int:
    """Emit a script skeleton the agent fills in."""
    s = _settings(args)
    pipe = Pipeline(s)
    video = Path(args.video).expanduser()

    session_file = SESSION_DIR / f"{session_slug(video)}.json"
    if session_file.exists() and not args.reanalyse:
        session = Session.load(session_file)
        tick = _ticker(args.quiet)
    else:
        tick = _ticker(args.quiet)
        session = pipe.watch(video, tick)
        _save(session)

    data = script_io.template(session, s)
    text = json.dumps(data, indent=1, ensure_ascii=False)
    if args.out:
        script_io.dump(Path(args.out), data)
        print(f"  template for {video.name} written to {args.out}",
              file=sys.stderr)
    else:
        print(text)
    return 0


def cmd_check(args) -> int:
    """Validate a script file without rendering anything."""
    s = _settings(args)
    video = Path(args.video).expanduser()
    session_file = SESSION_DIR / f"{session_slug(video)}.json"

    if session_file.exists():
        session = Session.load(session_file)
    else:
        session = Pipeline(s).watch(video, _ticker(args.quiet))

    pipe = Pipeline(s)
    try:
        script = pipe.apply_script(session, Path(args.script).expanduser(),
                                   _ticker(args.quiet))
    except script_io.ScriptError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    warnings = script_io.check(script, session, s)
    print()
    for line in script.lines:
        cap = script_gen.capacity_words(line.duration, s)
        flag = "  " if line.words <= cap else "!!"
        print(f"  {flag} {_human(line.start)}-{_human(line.end)}  "
              f"{line.words:3d}w of ~{int(cap):3d}  {line.text}")
    print()
    if warnings:
        print(f"  {len(warnings)} warning(s):")
        for w in warnings:
            print(f"    - {w}")
    else:
        print("  no timing problems")
    print()
    return 1 if warnings and args.strict else 0


def cmd_analyze(args) -> int:
    """Backwards-compatible alias for `watch` plus a script preview."""
    rc = cmd_watch(args)
    return rc


def cmd_voices(args) -> int:
    from .tts import engine as tts
    if args.locale:
        for v in tts.list_voices(args.locale):
            print(v)
        return 0
    print("Curated groups:")
    for group, voices in tts.VOICES.items():
        print(f"  {group}")
        for v in voices:
            print(f"    {v}")
    print(f"\nLocales available ({len(tts.available_locales())} total):")
    for loc in tts.available_locales():
        print(f"  {loc}")
    return 0


def cmd_scan(args) -> int:
    videos = find_videos(Path(args.dir).expanduser())
    if not videos:
        print("no video files found")
        return 1
    for v in videos:
        try:
            size = v.stat().st_size / 1048576
        except OSError:
            size = 0.0
        print(f"  {size:9.1f} MB  {v}")
    print(f"\n{len(videos)} video(s)")
    return 0


def cmd_config(args) -> int:
    s = Settings.load()
    if args.set:
        for pair in args.set:
            if "=" not in pair:
                print(f"  bad --set value: {pair} (use key=value)")
                continue
            k, v = pair.split("=", 1)
            k = k.strip()
            if k == "anthropic_api_key":
                s.extra.setdefault("_keys", {})
                s.extra["_keys"]["anthropic_api_key"] = v.strip()
                print(f"  stored anthropic_api_key in {CONFIG_FILE}")
                continue
            if not hasattr(s, k):
                print(f"  unknown setting: {k}")
                continue
            cur = getattr(s, k)
            try:
                if isinstance(cur, bool):
                    setattr(s, k, v.lower() in ("1", "true", "yes", "on"))
                elif isinstance(cur, int):
                    setattr(s, k, int(v))
                elif isinstance(cur, float):
                    setattr(s, k, float(v))
                else:
                    setattr(s, k, v)
            except ValueError:
                print(f"  could not parse {k}={v}")
        _write_config(s)
        print(f"  wrote {CONFIG_FILE}")

    print(f"  config file : {CONFIG_FILE}")
    print(f"  vision      : {s.vision_backend}")
    print(f"  claude model: {s.claude_model}")
    print(f"  whisper     : {s.whisper_model}")
    print(f"  voice       : {s.voice} (rate {s.rate})")
    print(f"  pace        : {s.target_wpm} wpm, fill {int(s.fill_ratio * 100)}%")
    print(f"  master      : {s.target_lufs} LUFS / {s.true_peak} dBTP")
    print(f"  api key     : {'found' if s.claude_key() else 'not set'}")
    return 0


def _write_config(s: Settings) -> None:
    from .config import json_dump
    lines = ["# Directora configuration", ""]
    for k, v in vars(s).items():
        if k == "extra":
            continue
        lines.append(f"{k} = {json_dump(v)}")
    extra = {k: v for k, v in s.extra.items() if k != "_keys"}
    for k, v in extra.items():
        lines.append(f"{k} = {json_dump(v)}")
    keys = s.extra.get("_keys") or {}
    for k, v in keys.items():
        lines.append(f"{k} = {json_dump(v)}")
    lines.append("")
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="directora",
        description="Add professional narration to screen recordings.",
        epilog=(
            "typical agent workflow:\n"
            "  directora watch  demo.mp4 --template script.json\n"
            "  # agent fills in every line's \"text\"\n"
            "  directora check  demo.mp4 --script script.json\n"
            "  directora narrate demo.mp4 --script script.json -o out.mp4\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-q", "--quiet", action="store_true",
                   help="suppress progress output")

    common = argparse.ArgumentParser(add_help=False)
    # SUPPRESS so this never clobbers the top-level -q default when omitted
    common.add_argument("-q", "--quiet", action="store_true",
                        default=argparse.SUPPRESS,
                        help="suppress progress output")
    common.add_argument("--backend", choices=["heuristic", "claude"])
    common.add_argument("--model", help="claude model id (default: auto)")
    common.add_argument("--voice")
    common.add_argument("--rate", help="edge-tts rate, e.g. -8%%")
    common.add_argument("--pitch")
    common.add_argument("--wpm", type=int, help="target narration pace")
    common.add_argument("--fill", type=float,
                        help="share of the video the voice fills, e.g. 0.8")
    common.add_argument("--whisper", help="tiny|base|small|medium")

    sub = p.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("watch", parents=[common],
                       help="analyse a video and emit a manifest (agent step 1)")
    w.add_argument("video")
    w.add_argument("--format", choices=["human", "json", "compact"],
                   default="human")
    w.add_argument("--template", metavar="FILE",
                   help="also write a fill-in-the-blanks script template here")
    w.set_defaults(func=cmd_watch)

    t = sub.add_parser("template", parents=[common],
                       help="emit a script skeleton for an agent to fill in")
    t.add_argument("video")
    t.add_argument("-o", "--out")
    t.add_argument("--reanalyse", action="store_true",
                   help="ignore any cached analysis and redo the work")
    t.set_defaults(func=cmd_template)

    c = sub.add_parser("check", parents=[common],
                       help="validate a script file's timing, render nothing")
    c.add_argument("video")
    c.add_argument("--script", required=True)
    c.add_argument("--strict", action="store_true",
                   help="exit non-zero if there are warnings")
    c.set_defaults(func=cmd_check)

    n = sub.add_parser("narrate", parents=[common],
                       help="analyse, script, speak and mux in one go")
    n.add_argument("video")
    n.add_argument("-o", "--out")
    n.add_argument("--script", metavar="FILE",
                   help="agent-written script; omit to use the vision backend")
    n.add_argument("--no-original", dest="original", action="store_false",
                   help="drop the source audio instead of ducking it")
    n.add_argument("--strict", action="store_true",
                   help="fail instead of warning on timing problems")
    n.add_argument("--no-srt", action="store_true")
    n.set_defaults(func=cmd_narrate)

    a = sub.add_parser("analyze", parents=[common],
                       help="alias for watch")
    a.add_argument("video")
    a.add_argument("--format", choices=["human", "json", "compact"],
                   default="human")
    a.add_argument("--template", metavar="FILE")
    a.set_defaults(func=cmd_analyze)

    v = sub.add_parser("voices", help="list narration voices")
    v.add_argument("--locale", help="e.g. en-GB, en-IN, hi")
    v.set_defaults(func=cmd_voices)

    sc = sub.add_parser("scan", help="list videos in a folder")
    sc.add_argument("dir")
    sc.set_defaults(func=cmd_scan)

    cf = sub.add_parser("config", help="show or change settings")
    cf.add_argument("--set", nargs="*", metavar="KEY=VALUE")
    cf.set_defaults(func=cmd_config)

    return p


def main(argv: list[str] | None = None) -> int:
    ensure_dirs()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as exc:                                    # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())