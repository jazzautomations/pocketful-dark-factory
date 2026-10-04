#!/usr/bin/env bash
# Sobe os 3 seats headless. Uso: start.sh <dir-com-mandates/> <repo-resultado-absoluto> [modelo]
# Para parar: pkill -f seats/seat.py
set -euo pipefail
WS=$1; REPO=$2; MODEL=${3:-claude-sonnet-5-5}
VENV=${VENV:-$HOME/band-mcp-venv}
set -a; . ~/.config/band/env; set +a
export PATH=$HOME/.local/bin:$PATH
mkdir -p "$WS/logs"
for SEAT in coordinator implementer reviewer; do
  M=$(grep -m1 "^Model:" $WS/mandates/$SEAT.md | sed "s/^Model: *//"); [ -n "$M" ] || M=$MODEL
  export SEAT_NAME=$SEAT MANDATE_FILE=$WS/mandates/$SEAT.md SEAT_CWD=$REPO SEAT_MODEL=$M
  export AGENT_ID=$(python3 -c "import json;print(json.load(open('$HOME/.config/band/seats.json'))['$SEAT']['data']['agent']['id'])")
  export AGENT_KEY=$(python3 -c "import json;print(json.load(open('$HOME/.config/band/seats.json'))['$SEAT']['data']['credentials']['api_key'])")
  nohup "$VENV/bin/python" ~/band-work/seats/seat.py > "$WS/logs/$SEAT.log" 2>&1 &
  echo "$SEAT pid $!"
done
