#!/usr/bin/env bash
# Launch Directora using the project's virtualenv.
#
# Creates the venv on first run, and re-syncs dependencies whenever
# requirements.txt changes, so `git pull` followed by a normal run just works.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
VENV="$HERE/.venv"
STAMP="$VENV/.requirements.sha"
REQ="$HERE/requirements.txt"

setup() {
  if [ ! -d "$VENV" ]; then
    echo "Creating virtualenv in $VENV ..." >&2
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet --upgrade pip
  fi
  echo "Installing dependencies ..." >&2
  "$VENV/bin/pip" install --quiet -r "$REQ"
  # the vision backend is optional; do not fail the whole install without it
  "$VENV/bin/pip" install --quiet -e "$HERE" || \
    "$VENV/bin/pip" install --quiet -e "$HERE" --no-deps
  sha256sum "$REQ" > "$STAMP"
  echo "Setup complete." >&2
}

if   [ ! -x "$VENV/bin/directora" ];     then setup
elif [ ! -f "$STAMP" ];                  then setup
elif [ "$REQ" -nt "$STAMP" ];            then setup
fi

exec "$VENV/bin/directora" "$@"