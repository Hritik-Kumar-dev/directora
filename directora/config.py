"""Configuration: paths, settings, and the optional Claude backend."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # py3.10
    tomllib = None

APP_NAME = "directora"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME
SESSION_DIR = CONFIG_DIR / "sessions"
WORK_DIR = CONFIG_DIR / "work"
CONFIG_FILE = CONFIG_DIR / "config.toml"


def coerce(current: object, raw: object) -> object | None:
    """Convert ``raw`` to the type of the current setting.

    Returns ``None`` when the value cannot sensibly be converted, so callers
    can keep the default rather than adopt something that will fail later.
    """
    if isinstance(current, bool):
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            return raw.strip().lower() in ("1", "true", "yes", "on")
        return None
    if isinstance(current, int):
        if isinstance(raw, bool):
            return None
        if isinstance(raw, int):
            return raw
        if isinstance(raw, float) and raw.is_integer():
            return int(raw)
        if isinstance(raw, str):
            try:
                return int(raw.strip())
            except ValueError:
                return None
        return None
    if isinstance(current, float):
        if isinstance(raw, bool):
            return None
        if isinstance(raw, (int, float)):
            return float(raw)
        if isinstance(raw, str):
            try:
                return float(raw.strip())
            except ValueError:
                return None
        return None
    # strings, paths and anything else pass through untouched
    return raw


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
    def load(cls) -> Settings:
        s = cls()
        if CONFIG_FILE.exists():
            try:
                data = load_toml(CONFIG_FILE.read_text(encoding="utf-8"))
            except Exception:
                return s
            for k, v in data.items():
                if hasattr(s, k) and not isinstance(v, dict):
                    # A hand-edited config can carry the wrong type -- writing
                    # target_wpm = "145" quotes the value, and adopting it
                    # verbatim used to surface much later as a baffling
                    # "can't multiply sequence by non-int" from deep in the
                    # pipeline. Fall back to the default instead.
                    coerced = coerce(getattr(s, k), v)
                    if coerced is not None:
                        setattr(s, k, coerced)
                else:
                    s.extra[k] = v
        # environment overrides -- useful for CI / headless runs
        for k in list(vars(s)):
            env = os.environ.get(f"DIRECTORA_{k.upper()}")
            if env is None:
                continue
            coerced = coerce(getattr(s, k), env)
            if coerced is not None:
                setattr(s, k, coerced)
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
        # the file can hold an API key, so keep it owner-only
        try:
            CONFIG_FILE.chmod(0o600)
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    def claude_key(self) -> str | None:
        """Only the user's *own* key -- never a session/agent credential."""
        for var in ("DIRECTORA_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"):
            v = os.environ.get(var)
            if v and v.strip():
                return v.strip()
        if CONFIG_FILE.exists():
            try:
                data = load_toml(CONFIG_FILE.read_text(encoding="utf-8"))
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


def load_toml(text: str) -> dict:
    if tomllib is not None:
        return tomllib.loads(text)
    try:
        import tomli

        return tomli.loads(text)
    except ModuleNotFoundError:
        out = {}
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            if v in ("true", "false"):
                out[k] = (v == "true")
                continue
            try:
                import json

                out[k] = json.loads(v)
            except Exception:
                out[k] = v.strip('"')
        return out


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, SESSION_DIR, WORK_DIR):
        d.mkdir(parents=True, exist_ok=True)
