#!/usr/bin/env bash
# Install or update the calorie bot on this machine (a Raspberry Pi or any Linux box with
# systemd). Run it from the folder you cloned:
#
#   ./install.sh            first time: asks a few questions, then starts the bot
#   ./install.sh --update   no questions: reinstall dependencies and restart (deploy.sh uses this)
#
# It runs as your own user. Nothing needs root, except possibly one `loginctl` command it
# tells you about.
set -euo pipefail

UPDATE=0
[[ "${1:-}" == "--update" ]] && UPDATE=1

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
SERVICE="${SERVICE_NAME:-calorie-bot}"   # set SERVICE_NAME to run a second copy

say()  { printf '\n==> %s\n' "$*"; }
warn() { printf '!!  %s\n' "$*"; }
ask()  { local prompt="$1" default="${2:-}" reply; read -r -p "$prompt" reply </dev/tty; echo "${reply:-$default}"; }
ask_secret() { local reply; read -r -s -p "$1" reply </dev/tty; echo >&2; echo "$reply"; }
set_env() {  # set_env KEY VALUE: replace the line (commented out or not) or append it
  local key="$1" value="$2"
  if grep -Eq "^#? *${key}=" .env; then
    python3 - "$key" "$value" <<'PY'
import re, sys
key, value = sys.argv[1], sys.argv[2]
text = open(".env").read()
text = re.sub(rf"^# *{key}=.*$|^{key}=.*$", f"{key}={value}", text, count=1, flags=re.M)
open(".env", "w").write(text)
PY
  else
    printf '%s=%s\n' "$key" "$value" >> .env
  fi
}
env_value() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- || true; }

# ---- Python -----------------------------------------------------------------------
say "Checking Python"
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
  warn "Python 3.9 or newer is needed (found: $(python3 --version 2>&1 || echo none))."
  exit 1
fi
if ! python3 -c 'import venv, ensurepip' 2>/dev/null; then
  warn "Python's venv module is missing. Install it with:  sudo apt install python3-venv"
  exit 1
fi
python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
mkdir -p data

# ---- Settings (.env) --------------------------------------------------------------
if [[ ! -f .env ]]; then
  cp .env.example .env
fi
chmod 600 .env

if [[ $UPDATE -eq 0 ]]; then
  say "Settings"
  if ! grep -Eq '^TELEGRAM_BOT_TOKEN=[0-9]+:[A-Za-z0-9_-]{30,}$' .env; then
    echo "Create a bot first: in Telegram, message @BotFather, send /newbot, and copy the token."
    while true; do
      token="$(ask_secret 'Paste your bot token (hidden as you paste): ')"
      if [[ ! "$token" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]]; then
        warn "That doesn't look like a bot token (it looks like 123456789:AAH...)."; continue
      fi
      name="$(curl -fsS "https://api.telegram.org/bot${token}/getMe" 2>/dev/null \
              | python3 -c 'import sys, json; print(json.load(sys.stdin)["result"]["username"])' 2>/dev/null || true)"
      if [[ -n "$name" ]]; then echo "Telegram knows that token: @$name"; break; fi
      warn "Telegram didn't accept that token. Check it and try again."
    done
    set_env TELEGRAM_BOT_TOKEN "$token"
  fi

  if [[ -z "$(env_value TIMEZONE)" ]]; then
    systz="$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo UTC)"
    tz="$(ask "Your timezone, which decides when your day starts [$systz]: " "$systz")"
    set_env TIMEZONE "$tz"
  fi

  if ! grep -Eq '^ESTIMATOR=' .env || [[ "$(env_value ESTIMATOR)" == "claude-code" && ! -f data/.estimator-chosen ]]; then
    echo
    echo "How should calories be estimated?"
    echo "  1) Claude Code on this Pi, using your own Claude Pro/Max subscription (no per-meal cost)"
    echo "  2) The Anthropic API with an API key (about 1-2 cents per meal)"
    choice="$(ask 'Pick 1 or 2 [1]: ' 1)"
    if [[ "$choice" == "2" ]]; then
      set_env ESTIMATOR api
      key="$(ask_secret 'Paste your Anthropic API key from platform.claude.com (hidden): ')"
      set_env ANTHROPIC_API_KEY "$key"
    else
      set_env ESTIMATOR claude-code
    fi
    touch data/.estimator-chosen
  fi
fi

# ---- Claude Code check (only for the subscription option) -------------------------
if [[ "$(env_value ESTIMATOR)" != "api" ]]; then
  CLAUDE_BIN="$(env_value CLAUDE_BIN)"
  CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude || echo "$HOME/.local/bin/claude")}"
  if [[ ! -x "$CLAUDE_BIN" ]]; then
    warn "Claude Code isn't installed. Install it, log in, then run ./install.sh again:"
    warn "  curl -fsSL https://claude.ai/install.sh | bash"
    warn "  claude        # log in with your Claude account"
    exit 1
  fi
  if ! "$CLAUDE_BIN" auth status 2>/dev/null | grep -q '"loggedIn": *true' \
     && [[ -z "$(env_value CLAUDE_CODE_OAUTH_TOKEN)" ]]; then
    warn "Claude Code isn't logged in. Run \`claude\` once and log in, or run"
    warn "\`claude setup-token\` and put the token in .env as CLAUDE_CODE_OAUTH_TOKEN."
    exit 1
  fi
  echo "Claude Code found at $CLAUDE_BIN and logged in."
fi

# ---- systemd user service ---------------------------------------------------------
say "Installing the $SERVICE service"
mkdir -p ~/.config/systemd/user
sed -e "s|__DIR__|$DIR|g" deploy/calorie-bot.service > ~/.config/systemd/user/$SERVICE.service
systemctl --user daemon-reload
systemctl --user enable $SERVICE >/dev/null

if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]]; then
  # Lingering keeps user services running after logout and starts them at boot.
  if ! loginctl enable-linger "$USER" 2>/dev/null; then
    warn "To keep the bot running after you log out and start it at boot, run once:"
    warn "  sudo loginctl enable-linger $USER"
  fi
fi

if ! grep -Eq '^TELEGRAM_BOT_TOKEN=[0-9]+:' .env; then
  warn "No bot token in $DIR/.env yet. Add it, then run ./install.sh again."
  exit 0
fi

systemctl --user restart $SERVICE
sleep 4
if ! systemctl --user is-active --quiet $SERVICE; then
  warn "The bot didn't start. Recent logs:"
  journalctl --user-unit $SERVICE --no-pager -n 20 -o cat || true
  exit 1
fi
echo "The bot is running."

# ---- Lock the bot to its owner ----------------------------------------------------
if [[ $UPDATE -eq 0 && -z "$(env_value ALLOWED_USER_IDS)" ]]; then
  say "Locking the bot to you"
  echo "Open your bot in Telegram and send it any message now. Waiting up to 3 minutes..."
  since="$(date '+%Y-%m-%d %H:%M:%S')"
  uid=""
  for _ in $(seq 1 90); do
    uid="$(journalctl --user-unit $SERVICE --since "$since" --no-pager -o cat 2>/dev/null \
           | grep -oE 'unauthorized user [0-9]+' | tail -1 | grep -oE '[0-9]+$' || true)"
    [[ -n "$uid" ]] && break
    sleep 2
  done
  if [[ -n "$uid" ]]; then
    set_env ALLOWED_USER_IDS "$uid"
    systemctl --user restart $SERVICE
    echo "Locked to Telegram user $uid. Message the bot again: it's yours now."
  else
    warn "No message arrived. Message the bot: it replies with your user id. Put it in"
    warn ".env as ALLOWED_USER_IDS=<id>, then run: systemctl --user restart $SERVICE"
  fi
fi

say "Done"
echo "Logs:     journalctl --user-unit $SERVICE -f"
echo "Restart:  systemctl --user restart $SERVICE"
echo "Update:   git pull && ./install.sh --update"
