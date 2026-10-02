# ⚡ Directora

**Add professional narration to screen recordings. Built for agents.**

Point it at a video. It works out where the shots are, works out what to say,
speaks it in a neural voice, and masters the result — all from the command
line, unattended.

```bash
directora narrate demo.mp4 -o demo_voiced.mp4
```

---

## ◈ Two ways to run it

### 1. Fully autonomous

With your own Claude key, the agent watches the footage and writes the script:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
directora config --set vision_backend=claude
directora narrate demo.mp4 -o demo_voiced.mp4
```

One command. Done.

### 2. Agent-in-the-loop

For when you want to see the reasoning, or when you want the script to come
from *your* agent rather than an API call at render time:

```bash
# step 1 — the agent looks at the video
directora watch demo.mp4 --format json --template script.json

#   → shot boundaries, keyframe paths, visual stats, transcript,
#     and a fill-in-the-blanks template with a per-shot word budget

# step 2 — the agent (or you) writes the narration
#   "text": ""  →  "text": "Pick an aspect preset and the frame snaps to fit."

# step 3 — check the timing before spending money or time
directora check demo.mp4 --script script.json --strict

# step 4 — speak it and mux it
directora narrate demo.mp4 --script script.json -o demo_voiced.mp4
```

This is what I used to build the tool against my own veltrix walkthrough:
read the manifest, open the keyframes it points at, write the lines, let
`check` tell me which ones were too long, trim, render.

---

## ◈ Install

Requires **ffmpeg** on your `PATH` (`sudo apt install ffmpeg`).

```bash
git clone git@github.com:Hritik-Kumar-dev/directora.git
cd directora
./run.sh narrate ~/Videos/demo.mp4          # builds .venv on first run
```

Nothing is installed system-wide. Python 3.10+.

### Making `directora` available everywhere

`./run.sh` works from inside the project directory. To call it from anywhere,
put a launcher on your `PATH` — `~/.local/bin` is already on `PATH` in most
setups:

```bash
mkdir -p ~/.local/bin
cat > ~/.local/bin/directora <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
PROJECT="$HOME/work/projects/directora"
[ -x "$PROJECT/run.sh" ] || { echo "directora: project missing at $PROJECT" >&2; exit 1; }
exec "$PROJECT/run.sh" "$@"
EOF
chmod +x ~/.local/bin/directora
```

`run.sh` creates the virtualenv on first run and re-syncs dependencies whenever
`requirements.txt` changes, so `git pull` followed by a normal run just works.

---

## ◈ Commands

| | |
|---|---|
| `watch VIDEO` | analyse and emit a manifest; `--format json\|compact`, `--template FILE` |
| `template VIDEO` | emit a fill-in-the-blanks script skeleton |
| `check VIDEO --script F` | validate timing, render nothing; `--strict` exits non-zero |
| `narrate VIDEO` | the whole job: analyse → script → speak → mux |
| `preview VIDEO N` | speak **one** line and report its real length |
| `batch DIR` | narrate every video in a folder |
| `voices` | list narration voices (`--locale en-IN`, `hi`, …) |
| `scan DIR` | list videos in a folder |
| `config` | show or change settings (`--set key=value`) |

Shared flags: `--backend`, `--voice`, `--rate`, `--pitch`, `--wpm`, `--fill`,
`--whisper`, `--language`, `-q`.

---

## ◈ Iterating without re-paying

Narration is the expensive part of a run, so clips are **content-addressed**:
each one is keyed by a hash of its text, voice, rate and pitch. Re-running after
a small edit re-synthesises only the lines that actually changed.

```
$ directora narrate demo.mp4 --script script.json -o out.mp4
  ·   cached: 5290fd111968ff53
  · Synthesised 1/2: This is the crop editor....
  · reused 0 of 2 cached clip(s)

$ # ...you tighten line 2 and run it again
  · Synthesised 2/2: Pick a preset and it snaps to fit.
  · reused 1 of 2 cached clip(s)
```

To hear a line before committing to a render, use `preview` — it synthesises
just that one line and tells you how it sits in its slot:

```
$ directora preview demo.mp4 2 --script script.json
  line 2  00:24-00:31
  text    : Pick a preset and the frame snaps to fit it exactly.
  voice   : en-GB-RyanNeural @ -8%
  length  : 4.11s of audio in a 7.00s slot (~17 words fit)
  audio  : ~/.config/directora/work/demo-1a2b3c/tts/fd04233f24701c5e.mp3
```

That is far cheaper than a full render when you are choosing between phrasings.

---

## ◈ Batch

Docs teams re-record walkthroughs in batches. `batch` walks a folder and
narrates everything it finds, matching `<stem>.json` scripts by filename:

```bash
# one script per video, named after it
directora batch ~/Videos/walkthroughs --script-dir ~/Videos/scripts \
             --out-dir ~/Videos/narrated
```

```
  3 video(s) under /home/you/Videos/walkthroughs

  [1/3] install.mp4
      -> /home/you/Videos/narrated/install_voiced.mp4
  ...
  2/3 succeeded
    ok   install.mp4: /home/you/Videos/narrated/install_voiced.mp4
    ok   setup.mp4: /home/you/Videos/narrated/setup_voiced.mp4
    FAIL broken.mp4: FAILED: ffprobe failed on broken.mp4: moov atom not found
```

A bad file does not stop the rest. The exit code is non-zero if **any** video
failed, so it works as a CI gate.

---

## ◈ The script format

Deliberately small. An agent can produce it without reading this file twice.

```json
{
  "voice": "en-GB-RyanNeural",
  "rate": "-6%",
  "lines": [
    { "shot": 0, "text": "Inherits its time window from shot 0." },
    { "start": 62.0, "end": 71.0, "text": "Explicit window, ignores shots." },
    { "start": 96.5, "text": "Start given, end estimated from the pace." },
    "A bare string is fine too."
  ]
}
```

Every line needs `text` plus at least one of `shot`, `start` or `end`. A bare
array of sentences works as well — lines are mapped onto shots in order:

```json
["First sentence.", "Second sentence.", "Third sentence."]
```

Overlapping lines are pushed apart, unknown shot numbers are reported rather
than crashing, and blank entries are skipped with a warning.

---

## ◈ How it watches your video

Two very different things are happening here, and the tool is deliberate about
which one you are getting.

### Shot detection — always local

Screen recordings almost never contain hard cuts. A window scrolling or a
panel opening is a smooth interpolation far below ffmpeg's scene threshold, so
`scene=0.3` returns **nothing** on a typical screen capture.

Frame-difference curves, though, are **spiky**: long quiet stretches punctuated
by brief bursts exactly when the UI changes. Those bursts are the real
boundaries. So we use both:

| Signal | Finds |
|---|---|
| ffmpeg `select=gt(scene,T)` | real editorial cuts, slides, hard transitions |
| frame-diff peaks (median + 4·MAD) | UI state changes, panel opens, page loads |

Editorial cuts win when plentiful; motion spikes fill in otherwise; even
slicing is the last resort. On a real 137s walkthrough this lands on
**22.5s, 62s, 96s, 117s, 130s** — matching the visual beats exactly.

Motion per shot is the **median** consecutive difference. An earlier version
averaged first-frame-vs-last-frame, which let a single transition dominate and
mislabelled a calm recording as "fast".

### Understanding content — pluggable

| Backend | Needs | Result |
|---|---|---|
| `heuristic` *(default)* | nothing, offline | structure, timing, stats, cleaned transcript. **Will not invent narration.** |
| `claude` | your `ANTHROPIC_API_KEY` | real keyframes → vision model → genuine narration |

The offline backend measures what it can and stops there. Speaking a
description like *"a steady shot; dense with interface elements"* would be
nonsense, so `narrate` without a script and without a key **refuses to run**
rather than emitting garbage:

```
error: nothing can write the narration for you.
  Pick one:
    --script FILE     supply an agent-written script
    export ANTHROPIC_API_KEY=sk-ant-...
```

The model id defaults to `auto`, which asks your key which Claude models it can
use and prefers Sonnet → Opus → Haiku. Pin it with
`directora config --set claude_model=<id>`.

> Only `ANTHROPIC_API_KEY` / `DIRECTORA_ANTHROPIC_API_KEY` are ever read — from
> your environment or Directora's own config. It never touches agent or session
> credentials.

---

## ◈ Existing narration

If the recording already has speech, `faster-whisper` transcribes it (CPU,
`int8`) and each shot adopts the words spoken during it, lightly cleaned:
fillers removed (*um, uh, basically, like…*), punctuation normalised, words
attributed to the shot they fall inside.

You keep your wording and meaning and get a clean baseline. The detected
speaking rate is reported and used to suggest a matching `edge-tts` rate, so
the new voiceover sits near your natural pace.

---

## ◈ Timing that actually constrains you

The word budget is real arithmetic, not a suggestion:

```
capacity_words = slot_seconds / 60 × target_wpm × fill_ratio
```

So a 6.5s slot at 145 wpm filling 82% holds **~12 words**, not 19. `check`
flags overruns and tells you roughly how many words to cut:

```
!! 00:00-00:06   18w of ~ 12  This is the crop editor. Pick an aspect preset...
     00:07-00:14   12w of ~ 13  Here a scanned form is straightened and cropped...
   1 warning(s):
     - line 1 (00:00.3): 18 words but only ~12 fit in 6.5s — cut about 6
```

Use `--strict` to fail instead of warn, which is what you want in a pipeline.

---

## ◈ The mix

- **Video is stream-copied.** No re-encode, MD5-identical to the source.
  There is a test that renders a clip and compares the video-stream MD5
  against the original, so this claim cannot quietly rot.
- **Two-pass `loudnorm`** to −16 LUFS / −1.5 dBTP.
- **Original audio is ducked, not deleted** — side-chain compressed under the
  narration so clicks survive as texture. `--no-original` to drop it instead.
- High-pass at 90 Hz.
- `.srt` written from the final timings.

---

## ◈ The agent skill

The workflow above is packaged as a skill, so any agent can run it without
being told how.

```bash
./skill/install.sh              # auto-detects opencode / claude
./skill/install.sh opencode     # or target one
./skill/install.sh --list       # show what's installed
```

Then just point your agent at a video:

> *voice over ~/Videos/demo.mp4, and make sure it emphasises the batch export step*

The skill encodes the lessons that took the most iterations to learn:

- **Always open the keyframes.** The tool measures structure; the agent supplies
  meaning. A skill that skips this produces generic filler.
- **Always run `check` before rendering.** It is free and instant, and it tells
  you exactly how many words to cut.
- **Never write to the source video.** Always `-o`.
- **Shots under ~2s cannot carry a line** — use explicit time windows instead.
- **Write what the software does**, not what the frame looks like.
- **Low-confidence transcription means film or music**, not narration to rewrite.

Install path for a fresh machine:

```bash
git clone https://github.com/Hritik-Kumar-dev/directora.git
cd directora && ./skill/install.sh
```

It installs to `~/.config/opencode/skills/directora` and
`~/.claude/skills/directora`, and prints manual instructions if neither exists.

---

## ◈ Configuration

`~/.config/directora/config.toml`, or `directora config --set key=value`.
Every key also honours a `DIRECTORA_<KEY>` environment variable.
The file is written `0600`, since it can hold an API key.

| Key | Default | Meaning |
|---|---|---|
| `vision_backend` | `heuristic` | `heuristic` or `claude` |
| `claude_model` | `auto` | resolved via the API |
| `max_frames` | `40` | keyframes sent to the vision model |
| `scene_threshold` | `0.30` | ffmpeg scene sensitivity |
| `min_shot_seconds` | `1.2` | shortest narratable shot |
| `whisper_model` | `base` | `tiny`/`base`/`small`/`medium` |
| `target_wpm` | `145` | narration pace |
| `fill_ratio` | `0.82` | share of a slot the voice occupies |
| `speech_margin` | `0.35` | silence kept before/after a line |
| `voice` | `en-GB-RyanNeural` | narration voice |
| `rate` | `-8%` | edge-tts rate |
| `target_lufs` | `-16.0` | master loudness |
| `keep_original_audio` | `true` | duck vs. drop |

---

## ◈ Layout

```
directora/
├── cli.py                  argparse entry point
├── config.py               settings, env overrides, key lookup
├── models.py               Shot / Line / Script / Session + serialisation
├── pipeline.py             stage orchestration
├── media/
│   ├── probe.py            ffprobe wrapper
│   ├── shots.py            cut detection, motion spikes, visual stats
│   └── mix.py              timeline, loudnorm, ducking, mux, srt
├── nlp/
│   ├── asr.py              faster-whisper transcription + VAD
│   ├── vision.py           HeuristicAnalyzer / ClaudeAnalyzer
│   ├── script_gen.py       slot planning, pacing budgets, transcript cleanup
│   └── script_io.py        agent script format: load / validate / template
└── tts/engine.py           edge-tts synthesis, voice catalogue, rate matching

.github/workflows/ci.yml    tests, lint, dependency resolve, skill validity
skill/directora/SKILL.md    the workflow packaged as an agent skill
```

---

## ◈ Tests

```bash
.venv/bin/python -m pytest tests/
```

38 tests: pacing maths, the agent script format (shot inheritance, explicit
times, bare lists, overlap resolution, unknown shots, voice overrides), session
round-tripping, srt timing, config overrides, refusal to invent narration
without a script or key, the TTS content cache, Claude model-id selection, and
a real ffmpeg render asserting the video stream is byte-identical.

Several exist because they caught real bugs during the build — including a
function-local import that shadowed an asyncio helper and made every line
silently fail to synthesise, and `capacity_words` dividing by `fill_ratio`
instead of multiplying it, which made every overrun check far too lenient.

The suite deliberately needs only **numpy** and **pillow**; `edge-tts`,
`faster-whisper` and `anthropic` are all imported lazily, so CI installs three
packages and runs in about a second.

### CI

`.github/workflows/ci.yml` runs four jobs on every push:

| job | what it guards |
|---|---|
| `test` | the suite on Python 3.10 (the declared floor) and 3.13 |
| `lint` | `ruff check`, configured in `pyproject.toml` |
| `deps` | that `requirements.txt` still resolves and every module imports |
| `skill` | that the skill's front matter is valid and `install.sh` parses |

---

## ◈ Known limits

- The offline backend cannot read UI labels — no `tesseract`, no GPU. It knows
  structure, not semantics. That is why it refuses rather than guesses.
- No GPU means no local VLM; a CPU-only vision model would be too slow to be
  interactive.
- Video is never re-encoded, so a bad source recording can't be fixed here —
  no denoising, de-stuttering or reframing.

<div align="center">

### ◈ Built for the walkthrough. Built for the privacy. ◈

*Directora — your files never leave the room.*

</div>