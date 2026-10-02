"""Pluggable 'watching' backends.

* ``HeuristicAnalyzer`` - offline. Uses measured brightness / motion / edge
  density plus any transcript fragments to describe each shot structurally.
  It cannot read UI labels, so it is deliberately honest about that.
* ``ClaudeAnalyzer``    - opt-in. Ships real keyframes to the Anthropic vision
  API and returns genuine narration grounded in what is actually on screen.

The Claude backend reads the *user's own* API key (``ANTHROPIC_API_KEY`` or
``directora config``). It never touches agent/session credentials.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable

from ..config import Settings
from ..models import MediaInfo, Shot

Progress = Callable[[str], None]


class VisionError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Heuristic (offline) backend
# --------------------------------------------------------------------------- #
class HeuristicAnalyzer:
    """Describe shots from cheap local signals. No network, no key."""

    name = "heuristic"
    understands_pixels = False

    def __init__(self, settings: Settings):
        self.s = settings

    def analyze(self, info: MediaInfo, shots: list[Shot],
                transcript: str = "", progress: Progress | None = None) -> list[Shot]:
        if progress:
            progress("Describing shots from local signals (no vision model)")

        fast = [sh for sh in shots if sh.kind == "fast"]
        static = [sh for sh in shots if sh.kind == "static"]

        for sh in shots:
            bits: list[str] = []

            # structural read
            if sh.kind == "static":
                bits.append("a largely static shot with little motion")
            elif sh.kind == "fast":
                bits.append("a fast-moving or rapidly changing shot")
            else:
                bits.append("a steady shot with moderate motion")

            # density read
            if sh.edge_density > 0.05:
                bits.append("dense with interface elements or fine detail")
            elif sh.edge_density > 0.02:
                bits.append("moderately detailed")
            else:
                bits.append("visually sparse, few hard edges")

            # tone read
            if sh.brightness < 0.22:
                bits.append("very dark")
            elif sh.brightness > 0.80:
                bits.append("very bright")
            if sh.colorfulness < 8:
                bits.append("near-monochrome")

            sh.description = "; ".join(bits)
            sh.analysed_by = self.name
            sh.keywords = self._keywords(sh)

        if progress:
            progress(
                f"Local pass done: {len(fast)} fast / {len(static)} static / "
                f"{len(shots)} total. Configure the 'claude' backend for true "
                f"frame understanding."
            )
        return shots

    @staticmethod
    def _keywords(sh: Shot) -> list[str]:
        kw = [sh.kind]
        if sh.edge_density > 0.05:
            kw.append("dense-ui")
        if sh.brightness < 0.22:
            kw.append("dark")
        if sh.motion > 0.035:
            kw.append("dynamic")
        if sh.transcript:
            kw.append("has-voice")
        return kw


# --------------------------------------------------------------------------- #
# Claude (vision) backend
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = """\
You are a senior voiceover writer for software product demo videos.

You will receive a sequence of shots from a screen recording, in order. Each shot
has: index, start/end time in seconds, duration, motion level, visual density,
brightness, and optionally the words spoken on screen during that shot.

Your job: write narration that a viewer would want to hear, timed to the visuals.

Rules:
- Return STRICT JSON only. No prose, no markdown fences.
- Top level: {"lines": [{"shot": <index>, "text": "<narration>"}]}
- Produce one line per shot that is worth narrating. Skip shots that are pure
  loading screens, black frames, or idle time -- those get no line.
- Each line must comfortably fit its shot's duration. Assume a comfortable
  narration pace of about 145 words per minute, so maximum words for a shot of
  D seconds is roughly (D / 60) * 145 * 0.75. Never exceed that.
- Cover the whole timeline: the FIRST non-trivial shot should get a line early,
  and there must be a closing line on the LAST meaningful shot.
- Prefer concrete, specific wording about what the viewer is seeing. Name real
  features. Never say "this video shows" or "in this screen".
- If a shot has transcript words, you may keep their meaning but rewrite for
  clarity and flow -- do not just read them out verbatim.
- Vary sentence length. Do not start consecutive lines the same way.
"""


class ClaudeAnalyzer:
    """Ships real frames to the Anthropic vision API."""

    name = "claude"
    understands_pixels = True

    # preference order when auto-resolving a model for the key we were given
    PREFERRED = ("sonnet", "opus", "haiku")

    # an undated id like "claude-sonnet-4-5" is a moving alias that tracks the
    # latest release; a dated id is pinned to one snapshot.
    _DATED = re.compile(r"-(\d{8})$")

    def __init__(self, settings: Settings):
        self.s = settings
        self._key = settings.require_claude()
        self.model = settings.claude_model

    @classmethod
    def _best(cls, candidates: list[str]) -> str:
        """Newest-looking id wins.

        Sorting these as plain strings is wrong -- "claude-3-5-sonnet-20241022"
        sorts *before* "claude-sonnet-4-5" because '3' < 's'. Prefer an
        undated alias, then the highest release date.
        """
        def key(mid: str) -> tuple[int, int, str]:
            m = cls._DATED.search(mid)
            # ascending sort, and we take the last: dated ids rank by release
            # date, undated aliases rank above all of them
            return (0, int(m.group(1)), mid) if m else (1, 0, mid)

        return sorted(candidates, key=key)[-1] if candidates else ""

    # -- model resolution -------------------------------------------------- #
    def _resolve_model(self, client) -> str:
        """Pick a real model id for this key instead of guessing one.

        Model names change often, so we ask the API what this key can use and
        prefer Sonnet, then Opus, then Haiku.
        """
        if self.model and self.model != "auto":
            return self.model
        try:
            page = client.models.list()
        except Exception as exc:                                # noqa: BLE001
            raise VisionError(
                f"Could not list models for your key: {exc}\n"
                f"Set one explicitly:  directora config --set claude_model=<id>"
            ) from exc

        available = []
        for m in getattr(page, "data", []) or []:
            mid = getattr(m, "id", "")
            if "claude" in mid.lower():
                available.append(mid)
        if not available:
            raise VisionError(
                "This API key exposes no Claude models. "
                "Set one explicitly:  directora config --set claude_model=<id>"
            )

        for want in self.PREFERRED:
            hits = [m for m in available if want in m.lower()]
            if hits:
                return self._best(hits)
        return self._best(available)

    # -- payload ---------------------------------------------------------- #
    @staticmethod
    def _b64(path) -> str:
        return base64.standard_b64encode(path.read_bytes()).decode()

    def _payload(self, info: MediaInfo, shots: list[Shot],
                 transcript: str) -> list[dict]:
        content: list[dict] = []
        notable = [s for s in shots if s.duration >= self.s.min_shot_seconds]
        # keep the budget: evenly thin the shot list if there are too many
        if len(notable) > self.s.max_frames:
            step = len(notable) / self.s.max_frames
            notable = [notable[int(i * step)] for i in range(self.s.max_frames)]

        orientation = "portrait" if info.portrait else "landscape"
        content.append({
            "type": "text",
            "text": (
                "Video analysis request.\n"
                f"Resolution: {info.resolution} ({orientation})\n"
                f"Frame rate: {info.fps:.2f} fps\n"
                f"Total duration: {sum(s.duration for s in shots):.1f}s across "
                f"{len(shots)} shots.\n"
                + (f"Existing spoken narration: {transcript[:4000]}\n"
                   if transcript else "No existing narration.\n")
                + "\nShot timeline (JSON):\n"
                + json.dumps([
                    {
                        "index": s.index,
                        "start": round(s.start, 2),
                        "end": round(s.end, 2),
                        "duration": round(s.duration, 2),
                        "motion": round(s.motion, 4),
                        "edge_density": round(s.edge_density, 4),
                        "brightness": round(s.brightness, 3),
                        "type": s.kind,
                        "spoken_during_shot": s.transcript or "",
                    } for s in notable], indent=1)
            ),
        })

        for s in notable:
            if not s.frame or not s.frame.exists():
                continue
            content.append({"type": "text",
                            "text": f"--- frame for shot {s.index} "
                                    f"(t={s.start:.1f}s, {s.duration:.1f}s) ---"})
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": self._b64(s.frame),
                },
            })
        return content

    # -- call ------------------------------------------------------------- #
    def analyze(self, info: MediaInfo, shots: list[Shot],
                transcript: str = "", progress: Progress | None = None) -> list[Shot]:
        try:
            import anthropic
        except ImportError as exc:
            raise VisionError(
                "The 'claude' backend needs the anthropic package: pip install anthropic"
            ) from exc

        client = anthropic.Anthropic(api_key=self._key)
        model = self._resolve_model(client)
        content = self._payload(info, shots, transcript)

        if progress:
            progress(f"Contacting {model} with "
                     f"{min(len(shots), self.s.max_frames)} keyframes")

        resp = client.messages.create(
            model=model,
            max_tokens=8000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        parsed = _parse_lines(text)

        if progress:
            progress(f"Claude returned {len(parsed)} narration line(s)")

        if not parsed:
            raise VisionError("Claude returned no usable narration lines")

        by_shot = {int(k): v for k, v in parsed}
        for sh in shots:
            if sh.index in by_shot:
                sh.description = by_shot[sh.index]
                sh.analysed_by = self.name
                sh.keywords = [sh.kind, "vision-described"]
        return shots


# --------------------------------------------------------------------------- #
def _parse_lines(text: str) -> list[tuple[int, str]]:
    """Tolerant JSON extraction -- models sometimes wrap output in fences."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()

    data = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                data = None

    if data is None:
        return []
    if isinstance(data, dict):
        data = data.get("lines") or data.get("script") or []

    out: list[tuple[int, str]] = []
    for i, item in enumerate(data or []):
        if not isinstance(item, dict):
            continue
        idx = item.get("shot", item.get("index", i))
        txt = str(item.get("text", "")).strip()
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            idx = i
        if txt:
            out.append((idx, txt))
    return out


def get_analyzer(settings: Settings):
    """Factory driven by ``settings.vision_backend``."""
    if settings.vision_backend == "claude":
        return ClaudeAnalyzer(settings)
    return HeuristicAnalyzer(settings)
