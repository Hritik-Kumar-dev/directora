"""Configuration: paths, settings, and the optional Claude backend."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "directora"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME
SESSION_DIR = CONFIG_DIR / "sessions"
WORK_DIR = CONFIG_DIR / "work"
CONFIG_FILE = CONFIG_DIR / "config.toml"


@dataclass
class Settings:
    # --- vision ---
    vision_backend: str = "heuristic"      # heuristic | claude
    claude_model: str = "auto"             # "auto" resolves via the API
    max_frames: int = 40                   # frames sent to the vision model
    frame_width: int = 900                 # downscale before analysis

    # --- scene detection ---
    scene_threshold: float = 0.30
    min_shot_seconds: float = 1.2
    max_shots: int = 60

    # --- speech ---
    whisper_model: str = "base"            # tiny | base | small | medium
    language: str = ""                     # force "ne"/"hi"/"en"; "" = auto
    detect_speech: bool = True

    # --- script ---
    target_wpm: int = 145                  # words per minute for a demo VO
    fill_ratio: float = 0.82               # share of the video the VO occupies
    speech_margin: float = 0.35            # silence kept before/after a line

    # --- tts ---
    voice: str = "en-GB-RyanNeural"
    rate: str = "-8%"                      # edge-tts rate string
    pitch: str = "+0Hz"

    # --- mix ---
    target_lufs: float = -16.0
    true_peak: float = -1.5
    keep_original_audio: bool = True
    duck_threshold: float = 0.020
    duck_ratio: int = 10

    # --- misc ---
    verbose: bool = False
    extra: dict = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls) -> "Settings":
        s = cls()
        if CONFIG_FILE.exists():
            try:
                data = tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            except Exception:
                return s
            for k, v in data.items():
                if hasattr(s, k) and not isinstance(v, dict):
                    setattr(s, k, v)
                else:
                    s.extra[k] = v
        # environment overrides -- useful for CI / headless runs
        for k in list(vars(s)):
            env = os.environ.get(f"DIRECTORA_{k.upper()}")
            if env is None:
                continue
            cur = getattr(s, k)
            if isinstance(cur, bool):
                setattr(s, k, env.lower() in ("1", "true", "yes", "on"))
            elif isinstance(cur, int) and not isinstance(cur, bool):
                setattr(s, k, int(env))
            elif isinstance(cur, float):
                setattr(s, k, float(env))
            else:
                setattr(s, k, env)
        return s

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        known = {k: v for k, v in vars(self).items() if k != "extra"}
        lines = ["# Directora configuration", ""]
        for k, v in known.items():
            lines.append(f"{k} = {json_dump(v)}")
        lines.append("")
        for k, v in self.extra.items():
            lines.append(f"# [extra] {k} = {v}")
        CONFIG_FILE.write_text("\n".join(lines), encoding="utf-8")

    # ------------------------------------------------------------------ #
    def claude_key(self) -> str | None:
        """Only the user's *own* key -- never a session/agent credential."""
        for var in ("DIRECTORA_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"):
            v = os.environ.get(var)
            if v and v.strip():
                return v.strip()
        if CONFIG_FILE.exists():
            try:
                data = tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                v = data.get("anthropic_api_key")
                if v:
                    return str(v).strip()
            except Exception:
                pass
        return None

    def require_claude(self) -> str:
        key = self.claude_key()
        if not key:
            raise RuntimeError(
                "The 'claude' vision backend needs your own API key.\n"
                "  export ANTHROPIC_API_KEY=sk-ant-...\n"
                "or run:  directora config --set anthropic_api_key=sk-ant-...\n"
                "The local 'heuristic' backend needs no key and works offline."
            )
        return key


def json_dump(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return '"' + v.replace('"', '\\"') + '"'
    if isinstance(v, (list, dict)):
        import json
        return json.dumps(v)
    return str(v)


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, SESSION_DIR, WORK_DIR):
        d.mkdir(parents=True, exist_ok=True)