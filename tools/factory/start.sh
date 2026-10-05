#!/usr/bin/env bash
# Start three external seats. Stop only the specific PIDs printed by this script.
set -euo pipefail
usage() { echo 'Usage: bash tools/factory/start.sh <mandates directory or workspace> <absolute result repo> [fallback model]'; }
if [[ ${1:-} == --help || ${1:-} == -h ]]; then usage; exit 0; fi
if (( $# < 2 || $# > 3 )); then usage >&2; exit 2; fi
WS=$1
REPO=$2
MODEL=${3:-claude-sonnet-5-5}
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
SEAT_PY="$SCRIPT_DIR/seat.py"
MANDATES=$WS
if [[ -d "$WS/mandates" ]]; then MANDATES="$WS/mandates"; fi
for SEAT in coordinator implementer reviewer; do
  if [[ ! -r "$MANDATES/$SEAT.md" ]]; then echo "Missing mandate: $MANDATES/$SEAT.md" >&2; exit 1; fi
done
if [[ ! -d "$REPO" ]]; then echo 'Result repository directory does not exist.' >&2; exit 1; fi
REPO=$(cd -- "$REPO" && pwd)
VENV=${VENV:-$HOME/band-venv}
PYTHON="$VENV/bin/python"
BAND_ENV_FILE=${BAND_ENV_FILE:-$HOME/.config/band/env}
BAND_SEATS_FILE=${BAND_SEATS_FILE:-$HOME/.config/band/seats.json}
for REQUIRED in "$PYTHON" "$SEAT_PY" "$BAND_ENV_FILE" "$BAND_SEATS_FILE"; do
  if [[ ! -r "$REQUIRED" ]]; then echo "Missing runtime or configuration file: $REQUIRED" >&2; exit 1; fi
done
if [[ ! -x "$PYTHON" ]]; then echo 'Virtual environment Python is not executable.' >&2; exit 1; fi
# Validate all identities before launching any worker; never print credentials.
"$PYTHON" - "$BAND_SEATS_FILE" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding='utf-8') as source:
        seats = json.load(source)
    for name in ('coordinator', 'implementer', 'reviewer'):
        data = seats[name]['data']
        for value in (data['agent']['id'], data['credentials']['api_key']):
            if not isinstance(value, str) or not value.strip():
                raise ValueError('missing identity')
except (OSError, ValueError, KeyError, TypeError):
    sys.exit('Invalid seats configuration; all three identities and keys are required.')
PY
set -a
. "$BAND_ENV_FILE"
set +a
export PATH="$HOME/.local/bin:$PATH"
mkdir -p "$WS/logs"
for SEAT in coordinator implementer reviewer; do
  M=$(sed -n 's/^Model: *//p' "$MANDATES/$SEAT.md" | head -n 1)
  M=${M%$'\r'}
  [[ -n "$M" ]] || M=$MODEL
  export SEAT_NAME=$SEAT MANDATE_FILE="$MANDATES/$SEAT.md" SEAT_CWD="$REPO" SEAT_MODEL=$M
  AGENT_ID=$("$PYTHON" -c 'import json,sys;print(json.load(open(sys.argv[1], encoding="utf-8"))[sys.argv[2]]["data"]["agent"]["id"])' "$BAND_SEATS_FILE" "$SEAT")
  AGENT_KEY=$("$PYTHON" -c 'import json,sys;print(json.load(open(sys.argv[1], encoding="utf-8"))[sys.argv[2]]["data"]["credentials"]["api_key"])' "$BAND_SEATS_FILE" "$SEAT")
  export AGENT_ID AGENT_KEY
  nohup "$PYTHON" "$SEAT_PY" > "$WS/logs/$SEAT.log" 2>&1 &
  echo "$SEAT pid $!"
done
