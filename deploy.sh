#!/usr/bin/env bash
# Copy the bot to the Pi, install dependencies, and (re)start it as a systemd user service.
# Usage: ./deploy.sh            (default target: shawn@jarvis.local)
#        PI=me@otherhost ./deploy.sh
set -euo pipefail

PI="${PI:-shawn@jarvis.local}"
REMOTE_DIR="${REMOTE_DIR:-calorie-bot}"   # relative to the Pi user's home

cd "$(dirname "$0")"

echo "==> Syncing code to $PI:~/$REMOTE_DIR"
ssh "$PI" "mkdir -p ~/$REMOTE_DIR/data"
rsync -az --delete \
  --exclude '.venv' --exclude 'data' --exclude '__pycache__' --exclude '*.db' \
  --exclude '.git' --exclude 'tests' --exclude '.env' \
  ./ "$PI:~/$REMOTE_DIR/"
# .env holds secrets and lives on the Pi. Copy it up only if you keep one here.
if [[ -f .env ]]; then
  echo "==> Copying local .env"
  scp -q .env "$PI:~/$REMOTE_DIR/.env"
fi

ssh "$PI" bash -s -- "$REMOTE_DIR" <<'REMOTE'
set -euo pipefail
DIR="$HOME/$1"
cd "$DIR"

echo "==> Installing Python dependencies"
python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

echo "==> Installing the systemd user service"
mkdir -p ~/.config/systemd/user
sed -e "s|__DIR__|$DIR|g" deploy/calorie-bot.service > ~/.config/systemd/user/calorie-bot.service
systemctl --user daemon-reload
systemctl --user enable calorie-bot >/dev/null

if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]]; then
  echo "!!  Lingering is off, so the bot stops when you log out and won't start at boot."
  echo "!!  Fix once with:  sudo loginctl enable-linger $USER"
fi

if [[ ! -f .env ]] || ! grep -Eq '^TELEGRAM_BOT_TOKEN=[0-9]+:' .env; then
  [[ -f .env ]] || cp .env.example .env
  chmod 600 .env
  echo "!!  Put your bot token in $DIR/.env, then run: systemctl --user restart calorie-bot"
  exit 0
fi
chmod 600 .env

echo "==> (Re)starting"
systemctl --user restart calorie-bot
sleep 4
systemctl --user --no-pager --lines=15 status calorie-bot || true
REMOTE

echo "==> Done. Logs: ssh $PI journalctl --user-unit calorie-bot -f"
