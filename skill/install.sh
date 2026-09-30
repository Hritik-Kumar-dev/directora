#!/usr/bin/env bash
# Install the Directora skill into whichever agent(s) you use.
#
#   ./skill/install.sh              # auto-detect
#   ./skill/install.sh opencode     # ~/.config/opencode/skills
#   ./skill/install.sh claude       # ~/.claude/skills
#   ./skill/install.sh --list       # show what is installed
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "$0")" && pwd)/directora"
NAME="directora"

if [ ! -f "$SKILL_DIR/SKILL.md" ]; then
  echo "error: $SKILL_DIR/SKILL.md not found" >&2
  exit 1
fi

show() {
  echo "installed skills:"
  for base in "$HOME/.config/opencode/skills" "$HOME/.claude/skills"; do
    if [ -d "$base/$NAME" ]; then
      echo "  ✓ $base/$NAME"
    fi
  done
  echo
}

case "${1:-}" in
  --list|-l)
    show
    exit 0
    ;;
  opencode) TARGETS=("$HOME/.config/opencode/skills") ;;
  claude)   TARGETS=("$HOME/.claude/skills") ;;
  all|"")   TARGETS=("$HOME/.config/opencode/skills" "$HOME/.claude/skills") ;;
  *) echo "usage: $0 [opencode|claude|all|--list]" >&2; exit 2 ;;
esac

installed=0
for target in "${TARGETS[@]}"; do
  # only install for agents that actually exist
  agent_root="$(dirname "$target")"
  [ -d "$agent_root" ] || continue
  [ -d "$target" ] || mkdir -p "$target"
  rm -rf "${target:?}/$NAME"
  cp -R "$SKILL_DIR" "$target/$NAME"
  echo "installed $NAME -> $target/$NAME"
  installed=$((installed + 1))
done

if [ "$installed" -eq 0 ]; then
  echo "No known agent skill directory found. Copy it manually:"
  echo "  cp -R $SKILL_DIR ~/.claude/skills/$NAME"
  exit 1
fi

echo
echo "Restart your agent to pick up the new skill."
show