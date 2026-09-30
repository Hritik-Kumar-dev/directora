---
name: directora
description: Add professional voiceover narration to a video. Point at a video file, optionally describe what it should emphasise, and this watches the footage frame by frame, writes a narration script timed to the visual beats, speaks it in a neural voice, and masters the result into a new MP4 plus an .srt. Use whenever asked to voice over, narrate, dub, add a voiceover, add commentary, add narration, or explain over a screen recording, demo, walkthrough, tutorial, clip or any other video.
---

# Directora — narrated video

You are the writer. The `directora` CLI is the camera, the voice and the mix
desk. It cannot understand what is on screen; **you** can, because you can read
images. Your job is to look at the footage and write the words. The tool handles
timing, synthesis and mastering.

## The one thing to understand

`directora` never invents narration. Shot detection, speech detection and
timing budgets are automatic, but the **words are yours to write**. If you skip
watching the keyframes, the output will be generic and wrong.

## Requirements

- `ffmpeg` and `ffprobe` on `PATH`
- The `directora` CLI

## Install / bootstrap

If `directora` is not on `PATH`:

```bash
git clone https://github.com/Hritik-Kumar-dev/directora.git
~/work/projects/directora/run.sh config      # builds the venv on first run
```

If `directora: command not found`, install a launcher:

```bash
mkdir -p ~/.local/bin
printf '#!/usr/bin/env bash\nexec "$HOME/work/projects/directora/run.sh" "$@"\n' \
  > ~/.local/bin/directora
chmod +x ~/.local/bin/directora
```

A `run.sh` at the project root always works, even without the launcher.

---

## Workflow

### 1. Look at the video

```bash
directora watch VIDEO.mp4 --format compact --template /tmp/narration.json
```

This prints one line per shot with its time range, motion, edge density and
the path to a keyframe JPEG. Read that output.

### 2. Actually view the keyframes

Open every keyframe path with your image-reading tool. **Do not skip this.**
`/tmp/narration.json` also contains `_hint` (any speech detected during that
shot) and `_stats` per line.

For long videos, the keyframes are one image per shot and a single
`fps`-independent sample each — enough to understand the whole piece.

### 3. Write the script

Fill in the `"text"` field of each line in `/tmp/narration.json`. Respect
`"max_words"`, which is the real budget derived from the slot length.

Rules that produce good narration:

- **Describe what the viewer is seeing**, in present tense. "The crop frame
  snaps to the passport ratio" — not "this video shows cropping".
- **Lead with the point.** Openers should state the value of the thing being
  shown, not narrate the loading screen.
- **Cover the whole timeline.** The first meaningful shot and the last
  meaningful shot both need a line.
- **Vary sentence length.** Do not start consecutive lines the same way.
- **Skip dead air.** Black frames, loading spinners and idle cursors get no
  line.
- Never write a description *of the frame itself* ("a dark UI with panels").
  Write what the software *does*.

**Short shots cannot carry a line.** If a shot is under ~2s it has a budget of
3–4 words, which sounds clipped. Use explicit windows instead:

```json
{"start": 28.4, "end": 48.6, "text": "For the fight scenes ..."}
```

A bare array of sentences also works — lines map onto shots in order.

### 4. Check the timing — always do this

```bash
directora check VIDEO.mp4 --script /tmp/narration.json --strict
```

This renders nothing. It flags lines that overrun their slot and tells you how
many words to cut. Fix and re-run until it reports **no timing problems**.
Rendering first and discovering an overrun afterwards wastes a synthesis pass.

### 5. Render

```bash
directora narrate VIDEO.mp4 --script /tmp/narration.json -o VIDEO_voiced.mp4
```

Always pass `-o`. Never let the tool overwrite the user's source video.

### 6. Report

Tell the user:
- the output path and the subtitle path
- the narration voice and pace used
- the measured loudness
- anything you had to guess, and what you would change

If the user gave you a brief (what to emphasise, tone, audience), re-read your
script against it before rendering.

---

## Ground rules

- **Never write to the source video.** Always `-o`.
- **Never skip the keyframes.** The tool cannot see; you can.
- **Never skip `check`.** It is free and instant.
- **Honour the user's brief.** If they described the intent, let it override
  what you would otherwise write.
- **Do not overwrite the script file they gave you.** Write to `/tmp` unless
  they asked otherwise.
- **If you cannot see the footage** (no image tool, extraction failed), say so
  plainly instead of writing generic filler.
- **Report honestly.** If the narration is generic because you could not view
  the frames, say that.

## Choosing a voice

Default `en-GB-RyanNeural` is a calm British male, good for product material.
For other needs:

```bash
directora voices --locale en-IN      # Indian English
directora voices --locale hi         # Hindi
directora voices --locale ne         # Nepali
directora narrate VIDEO.mp4 --script s.json --voice en-IN-NeerjaNeural -o out.mp4
```

Roughly 140 locales are available. Set a slower or faster pace with
`--rate '-5%'` or `--rate '+5%'`.

If the source already has a real human narrator, `directora` transcribes it,
reports their words-per-minute, and you can match that pace. If it reports
**"low confidence"**, the audio was film or music rather than narration — do
not rewrite from it.

## Command reference

| | |
|---|---|
| `watch VIDEO` | analyse; `--format human\|json\|compact`, `--template FILE` |
| `template VIDEO` | script skeleton; `-o FILE` |
| `check VIDEO --script F` | validate timing only; `--strict` |
| `narrate VIDEO` | the whole job; `--script F`, `-o OUT`, `--voice`, `--rate`, `--language`, `--no-original`, `--no-srt` |
| `voices` | list voices; `--locale CODE` |
| `config` | show settings; `--set key=value` |

## Troubleshooting

**`directora: command not found`** — install the `~/.local/bin` launcher above.

**`narrate` refuses with "nothing can write the narration"** — there is no
script and no vision backend. Write a script and pass `--script`. That is the
correct path when you are the writer.

**Transcription is garbled** — non-English audio auto-detected as English.
Pass `--language ne` (or `hi`, `en`).

**A line overruns its slot** — `check` told you how many words to cut. Do that
rather than shortening the gap.

**Scene detection finds nothing** — normal for screen recordings; motion-spike
detection handles it. If shots are still very short, use explicit windows.

## Optional: letting an API write the script instead

If the user has an Anthropic API key, the vision backend can watch the
keyframes and write the narration itself, making this a one-command operation.
Only the vision step costs anything; transcription, TTS and mixing stay free.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
directora config --set vision_backend=claude
directora narrate VIDEO.mp4 -o VIDEO_voiced.mp4
```