#!/usr/bin/env bash
#
# Directora's installer. Installs the `directora` CLI and the agent skill.
#
#   curl -fsSL https://raw.githubusercontent.com/Hritik-Kumar-dev/directora/main/install.sh | bash
#
# Written to be piped into bash, so it must not assume anything about its own
# location: it downloads the source tree itself.
#
set -euo pipefail

REPO="Hritik-Kumar-dev/directora"
API="https://api.github.com/repos/$REPO"
CODELOAD="https://codeload.github.com/$REPO/tar.gz"

PREFIX="${DIRECTORA_PREFIX:-$HOME/.local/share/directora}"
BIN_DIR="${DIRECTORA_BIN:-$HOME/.local/bin}"
VENV="$PREFIX/.venv"

WANT_ASR=0
WANT_CLAUDE=0
AGENT_MODE="auto"      # auto | opencode | claude | all
REF=""
LOCAL_SRC="${DIRECTORA_FROM:-}"
SKIP_FFMPEG_CHECK=0
TMP=""

# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  B=$'\033[1m'; DIM=$'\033[2m'; G=$'\033[32m'; R=$'\033[31m'; Y=$'\033[33m'; N=$'\033[0m'
else
  B=""; DIM=""; G=""; R=""; Y=""; N=""
fi
step() { printf '%s==>%s %s%s%s\n' "$B" "$N" "$B" "$1" "$N"; }
info() { printf '    %s%s%s\n' "$DIM" "$1" "$N"; }
ok()   { printf '    %sok%s  %s\n' "$G" "$N" "$1"; }
warn() { printf '    %swarn%s %s\n' "$Y" "$N" "$1" >&2; }
die()  { printf '%serror:%s %s\n' "$R" "$N" "$1" >&2; exit 1; }

cleanup() {
  # must always succeed: the exit trap's status becomes the script's status
  if [ -n "$TMP" ] && [ -d "$TMP" ]; then rm -rf "$TMP"; fi
  return 0
}
trap cleanup EXIT

usage() {
  cat <<'EOF'
Directora installer

  curl -fsSL https://raw.githubusercontent.com/Hritik-Kumar-dev/directora/main/install.sh | bash

Options (pass after the URL, or set the matching environment variable):

  --asr              also install speech-to-text (large: ~200MB + a model)
  --claude           also install the Anthropic SDK for the vision backend
  --full             both of the above
  --agent WHICH      auto (default) | opencode | claude | all
  --version REF      pin to a tag or commit, e.g. v0.2.0
  --from DIR         install from a local checkout instead of downloading
  --prefix DIR       install the CLI here   (default: ~/.local/share/directora)
  --bin-dir DIR      put the launcher here  (default: ~/.local/bin)
  --skip-ffmpeg      install even without ffmpeg on PATH
  --uninstall        remove the CLI and the skill
  -h, --help         this text

Environment: DIRECTORA_VERSION, DIRECTORA_PREFIX, DIRECTORA_BIN,
             DIRECTORA_FROM, NO_COLOR

Uninstall is always available afterwards:
  curl -fsSL <this url> | bash -s -- --uninstall
EOF
}

# --------------------------------------------------------------------------- #
# arguments
# --------------------------------------------------------------------------- #
while [ $# -gt 0 ]; do
  case "$1" in
    --asr)       WANT_ASR=1 ;;
    --claude)    WANT_CLAUDE=1 ;;
    --full)      WANT_ASR=1; WANT_CLAUDE=1 ;;
    --agent)     shift; AGENT_MODE="${1:-auto}" ;;
    --agent=*)   AGENT_MODE="${1#*=}" ;;
    --version)   shift; REF="${1:-}" ;;
    --from)      shift; LOCAL_SRC="${1:-}" ;;
    --prefix)    shift; PREFIX="${1:-}" ;;
    --bin-dir)   shift; BIN_DIR="${1:-}" ;;
    --skip-ffmpeg) SKIP_FFMPEG_CHECK=1 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help)   usage; exit 0 ;;
    *)           die "unknown option: $1  (try --help)" ;;
  esac
  shift
done
UNINSTALL="${UNINSTALL:-0}"
REF="${DIRECTORA_VERSION:-$REF}"

# --------------------------------------------------------------------------- #
# preflight
# --------------------------------------------------------------------------- #
command -v curl >/dev/null 2>&1 || die "curl is required."
command -v tar  >/dev/null 2>&1 || die "tar is required."

if [ "$UNINSTALL" -eq 1 ]; then
  step "Removing Directora"
  rm -rf "$PREFIX"
  for b in "$BIN_DIR/directora"; do
    [ -e "$b" ] && rm -f "$b" && ok "removed $b"
  done
  for base in "$HOME/.config/opencode/skills" "$HOME/.claude/skills"; do
    [ -d "$base/directora" ] && rm -rf "$base/directora" && ok "removed $base/directora"
  done
  ok "done"
  exit 0
fi

step "Checking prerequisites"

PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
      PY="$cand"; break
    fi
    PY="$PY$PY,${cand:-none}"
  fi
done
[ -n "$PY" ] || die "Python 3.10+ is required and was not found (tried: ${PY:-none})."
ok "python: $("$PY" --version 2>&1)"

if [ "$SKIP_FFMPEG_CHECK" -eq 0 ]; then
  if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
    ok "ffmpeg: found"
  else
    MISSING=""
    command -v ffmpeg  >/dev/null 2>&1 || MISSING="$MISSING ffmpeg"
    command -v ffprobe >/dev/null 2>&1 || MISSING="$MISSING ffprobe"
    warn "missing:$MISSING  -- required, and not installed automatically"
    echo
    case "$(uname -s)" in
      Darwin) echo "    brew install ffmpeg" ;;
      Linux)
        if command -v apt-get >/dev/null 2>&1; then echo "    sudo apt-get update && sudo apt-get install -y ffmpeg"
        elif command -v dnf >/dev/null 2>&1; then echo "    sudo dnf install ffmpeg"
        elif command -v pacman >/dev/null 2>&1; then echo "    sudo pacman -S ffmpeg"
        else echo "    install ffmpeg with your package manager"; fi ;;
      *) echo "    install ffmpeg with your package manager" ;;
    esac
    echo
    echo "    Then re-run this command, or pass --skip-ffmpeg to install anyway."
    exit 1
  fi
fi

# --------------------------------------------------------------------------- #
# resolve the source
# --------------------------------------------------------------------------- #
step "Fetching source"
if [ -n "$LOCAL_SRC" ]; then
  [ -f "$LOCAL_SRC/pyproject.toml" ] \
    || die "--from must point at a checkout (no pyproject.toml in $LOCAL_SRC)"
  EXTRACT="$(cd "$LOCAL_SRC" && pwd)"
  ok "using local source at $EXTRACT"
elif [ -z "$REF" ]; then
  REF="$(curl -fsSL --max-time 20 "$API/releases/latest" 2>/dev/null \
        | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
        | head -1 || true)"
  if [ -n "$REF" ]; then
    ok "latest release: $REF"
  else
    REF="main"
    warn "no published release found; installing from main"
  fi
else
  ok "pinned to: $REF"
fi

if [ -z "$LOCAL_SRC" ]; then
  TMP="$(mktemp -d)"
  ARCHIVE="$TMP/directora.tar.gz"
  # codeload wants an explicit namespace; a bare ref is ambiguous
  case "$REF" in
    refs/tags/*|refs/heads/*) URL="$CODELOAD/$REF" ;;
    v[0-9]*)                  URL="$CODELOAD/refs/tags/$REF" ;;
    main|master|dev)          URL="$CODELOAD/refs/heads/$REF" ;;
    *)                        URL="$CODELOAD/$REF" ;;   # a commit sha
  esac

  curl -fsSL --max-time 180 -o "$ARCHIVE" "$URL" \
    || die "could not download $URL
  Check the ref is right:  --version v0.2.0  /  --version main  /  --version <sha>"
  [ -s "$ARCHIVE" ] || die "downloaded archive is empty"

  tar -xzf "$ARCHIVE" -C "$TMP" || die "could not unpack the download"
  EXTRACT="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -1)"
  [ -n "$EXTRACT" ] && [ -f "$EXTRACT/pyproject.toml" ] \
    || die "the download did not look like the Directora source tree"
fi

# --------------------------------------------------------------------------- #
# install the CLI
# --------------------------------------------------------------------------- #
step "Installing the CLI into $PREFIX"
SRC="$PREFIX/src"
rm -rf "$SRC"
mkdir -p "$SRC"
for item in pyproject.toml README.md LICENSE requirements.txt run.sh; do
  [ -e "$EXTRACT/$item" ] && cp -R "$EXTRACT/$item" "$SRC/"
done
cp -R "$EXTRACT/directora" "$SRC/"
[ -d "$EXTRACT/skill" ] && cp -R "$EXTRACT/skill" "$SRC/"
true

if [ ! -x "$VENV/bin/python" ]; then
  info "creating a virtualenv (first run takes a moment)"
  "$PY" -m venv "$VENV" || die "could not create a virtualenv at $VENV"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip

EXTRAS=""
[ "$WANT_ASR" -eq 1 ] && EXTRAS="$EXTRAS,asr"
[ "$WANT_CLAUDE" -eq 1 ] && EXTRAS="$EXTRAS,claude"
if [ -n "$EXTRAS" ]; then
  info "installing with extras:${EXTRAS#,}"
  "$VENV/bin/python" -m pip install --quiet "$SRC[${EXTRAS#,}]" \
    || die "dependency install failed"
else
  info "installing the core package"
  "$VENV/bin/python" -m pip install --quiet "$SRC" \
    || die "dependency install failed"
fi
ok "installed into $VENV"

# --------------------------------------------------------------------------- #
# launcher
# --------------------------------------------------------------------------- #
step "Creating the launcher"
mkdir -p "$BIN_DIR"
LAUNCHER="$BIN_DIR/directora"
cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
# Generated by the Directora installer. Edit the installer, not this file.
set -euo pipefail
VENV="$VENV"
if [ ! -x "\$VENV/bin/directora" ]; then
  echo "directora: install looks incomplete (\$VENV missing)." >&2
  echo "Re-run the installer: curl -fsSL https://raw.githubusercontent.com/$REPO/main/install.sh | bash" >&2
  exit 1
fi
exec "\$VENV/bin/directora" "\$@"
EOF
chmod +x "$LAUNCHER"
ok "$LAUNCHER"

case ":$PATH:" in
  *":$BIN_DIR:"*) : ;;
  *) warn "$BIN_DIR is not on your PATH -- add this to your shell profile:"
     echo "        export PATH=\"$BIN_DIR:\$PATH\"" ;;
esac

# --------------------------------------------------------------------------- #
# skill
# --------------------------------------------------------------------------- #
step "Installing the agent skill"
install_skill() {
  target="$1"
  mkdir -p "$target/directora"
  cp "$SRC/skill/directora/SKILL.md" "$target/directora/SKILL.md"
  ok "$target/directora"
}

AGENTS=""
case "$AGENT_MODE" in
  all)      AGENTS="$HOME/.config/opencode/skills $HOME/.claude/skills" ;;
  opencode) AGENTS="$HOME/.config/opencode/skills" ;;
  claude)   AGENTS="$HOME/.claude/skills" ;;
  auto)
    # only touch agents that are actually present, so we never litter a
    # machine with config for software the user does not have
    [ -d "$HOME/.config/opencode" ] && AGENTS="$AGENTS $HOME/.config/opencode/skills"
    [ -d "$HOME/.claude" ]         && AGENTS="$AGENTS $HOME/.claude/skills"
    ;;
  *) die "--agent must be auto, opencode, claude or all (got '$AGENT_MODE')" ;;
esac

INSTALLED=0
for a in $AGENTS; do
  install_skill "$a"
  INSTALLED=$((INSTALLED + 1))
done

if [ "$INSTALLED" -eq 0 ]; then
  warn "no agent found (looked for ~/.config/opencode and ~/.claude)"
  echo "    Install the skill manually with:"
  echo "      mkdir -p ~/.claude/skills/directora"
  echo "      cp $SRC/skill/directora/SKILL.md ~/.claude/skills/directora/"
  echo "    or re-run with --agent claude  /  --agent opencode  /  --agent all"
fi

# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #
step "Verifying"
if ! "$LAUNCHER" config >/dev/null 2>&1; then
  die "installed, but 'directora config' failed -- try running it directly to see why"
fi
ok "directora runs"

# --------------------------------------------------------------------------- #
# summary
# --------------------------------------------------------------------------- #
echo
printf '%sDirectora is installed.%s\n' "$G$B" "$N"
echo
echo "  Try it on a screen recording:"
echo
echo "      $LAUNCHER narrate demo.mp4 --script script.json -o demo_voiced.mp4"
echo
if [ "$WANT_ASR" -eq 0 ]; then
  echo "  Not installed: speech-to-text."
  echo "  It'll narrate videos that have no existing narration, but it ignores"
  echo "  narration already in the recording. To add it (~200MB plus a model"
  echo "  download on first use):"
  echo
  echo "      curl -fsSL https://raw.githubusercontent.com/$REPO/main/install.sh | bash -s -- --asr"
  echo
fi
if [ "$WANT_CLAUDE" -eq 0 ]; then
  echo "  Not installed: the Anthropic SDK for the vision backend."
  echo "  You can still write the script yourself, or with your own agent. To add it:"
  echo
  echo "      curl -fsSL https://raw.githubusercontent.com/$REPO/main/install.sh | bash -s -- --claude"
  echo
fi
if [ "$INSTALLED" -gt 0 ]; then
  echo "  Restart your agent to pick up the skill, then just ask it to"
  echo "  \"voice over demo.mp4\"."
  echo
fi
echo "  Uninstall:  curl -fsSL https://raw.githubusercontent.com/$REPO/main/install.sh | bash -s -- --uninstall"
echo