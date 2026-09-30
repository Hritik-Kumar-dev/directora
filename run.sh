#!/usr/bin/env bash
# Launch Directora using the local virtualenv.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
VENV="$HERE/.venv"
if [ ! -d "$VENV" ]; then
  echo "Creating virtualenv in $VENV ..."
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet -r "$HERE/requirements.txt"
  "$VENV/bin/pip" install --quiet -e "$HERE"
  echo "Setup complete."
fi
exec "$VENV/bin/directora" "$@"
