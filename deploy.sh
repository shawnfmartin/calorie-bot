#!/usr/bin/env bash
# Push your local copy to a Pi and update it there. For people who edit the code on
# another computer; if you just cloned onto the Pi, use ./install.sh there instead.
#
#   ./deploy.sh pi@raspberrypi.local
#   PI=pi@raspberrypi.local ./deploy.sh
#
# The Pi keeps its own .env and data/. Run ./install.sh on the Pi once first.
set -euo pipefail

PI="${1:-${PI:-}}"
REMOTE_DIR="${REMOTE_DIR:-calorie-bot}"   # relative to the Pi user's home
if [[ -z "$PI" ]]; then
  echo "Usage: ./deploy.sh user@your-pi.local   (or set PI=...)" >&2
  exit 1
fi

cd "$(dirname "$0")"

echo "==> Syncing code to $PI:~/$REMOTE_DIR"
ssh "$PI" "mkdir -p ~/$REMOTE_DIR"
rsync -az --delete \
  --exclude '.venv' --exclude 'data' --exclude '__pycache__' --exclude '*.db' \
  --exclude '.git' --exclude 'tests' --exclude '.env' --exclude '.pytest_cache' \
  ./ "$PI:~/$REMOTE_DIR/"

ssh "$PI" "cd ~/$REMOTE_DIR && ./install.sh --update"
echo "==> Done. Logs: ssh $PI journalctl --user-unit calorie-bot -f"
