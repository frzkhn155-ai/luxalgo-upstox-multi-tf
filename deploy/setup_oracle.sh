#!/usr/bin/env bash
# One-shot installer for the LuxAlgo multi-TF bot on an Oracle Cloud VM (Ubuntu or Oracle Linux).
#
# Usage (on the VM, as your normal user, NOT root):
#   curl -fsSL https://raw.githubusercontent.com/frzkhn155-ai/luxalgo-upstox-multi-tf/main/deploy/setup_oracle.sh | bash
#   (or copy this file to the VM and run: bash setup_oracle.sh)
#
# What it does:
#   1. installs python/git, sets the clock to IST
#   2. clones the repo to ~/luxalgo and builds a virtualenv
#   3. creates ~/luxalgo/.env (token + mode + qty), chmod 600
#   4. installs a systemd service + timer: starts Mon-Fri 09:10 IST, stops itself at ~15:35 IST
#   5. gives you helper commands: set_token.sh, update.sh
#
# It starts in PAPER mode. To go live later, edit LUX_MODE in ~/luxalgo/.env.
set -euo pipefail

REPO_URL="https://github.com/frzkhn155-ai/luxalgo-upstox-multi-tf.git"
APP_DIR="$HOME/luxalgo"
RUN_USER="$(id -un)"

if [ "$(id -u)" -eq 0 ]; then
  echo "Run this as your normal user (e.g. ubuntu / opc), not root. It uses sudo where needed." >&2
  exit 1
fi

echo "==> Installing system packages"
if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update -y
  sudo apt-get install -y python3 python3-venv python3-pip git
elif command -v dnf >/dev/null 2>&1; then
  sudo dnf install -y python3 python3-pip git
else
  echo "Unsupported OS: need apt-get or dnf" >&2
  exit 1
fi

echo "==> Setting timezone to Asia/Kolkata"
sudo timedatectl set-timezone Asia/Kolkata

echo "==> Fetching code into $APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$APP_DIR"
fi

echo "==> Creating virtualenv and installing requirements"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "==> Writing $APP_DIR/.env (kept if it already exists)"
if [ ! -f "$APP_DIR/.env" ]; then
  cat > "$APP_DIR/.env" <<EOF
UPSTOX_TOKEN=
LUX_MODE=paper
LUX_QTY=65
LUX_INTERVALS=3,5,15
TZ=Asia/Kolkata
EOF
fi
chmod 600 "$APP_DIR/.env"

echo "==> Writing helper scripts"
cat > "$APP_DIR/set_token.sh" <<'EOF'
#!/usr/bin/env bash
# Paste today's Upstox access token (input is hidden). Run this every morning before 09:10.
set -euo pipefail
ENV_FILE="$(dirname "$(readlink -f "$0")")/.env"
read -rsp "Paste Upstox access token: " TOKEN; echo
[ -n "$TOKEN" ] || { echo "Empty token, nothing changed." >&2; exit 1; }
if grep -q '^UPSTOX_TOKEN=' "$ENV_FILE"; then
  # use a different delimiter and avoid sed escaping problems with the token
  python3 - "$ENV_FILE" "$TOKEN" <<'PY'
import sys
path, tok = sys.argv[1], sys.argv[2]
lines = open(path).read().splitlines()
lines = [("UPSTOX_TOKEN=" + tok) if l.startswith("UPSTOX_TOKEN=") else l for l in lines]
open(path, "w").write("\n".join(lines) + "\n")
PY
else
  printf 'UPSTOX_TOKEN=%s\n' "$TOKEN" >> "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"
echo "Token saved."
# If the bot is already running (e.g. you set the token late), restart it with the new token.
if systemctl is-active --quiet luxalgo.service; then
  sudo systemctl restart luxalgo.service && echo "Bot restarted with the new token."
fi
EOF
chmod +x "$APP_DIR/set_token.sh"

cat > "$APP_DIR/update.sh" <<'EOF'
#!/usr/bin/env bash
# Pull the latest bot code and refresh dependencies.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
git pull --ff-only
./venv/bin/pip install -r requirements.txt
echo "Updated. Takes effect at the next start (or: sudo systemctl restart luxalgo)."
EOF
chmod +x "$APP_DIR/update.sh"

echo "==> Installing systemd service and timer"
sudo tee /etc/systemd/system/luxalgo.service >/dev/null <<EOF
[Unit]
Description=LuxAlgo multi-timeframe bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/venv/bin/python -u luxalgo_upstox_multi_tf.py --mode \${LUX_MODE} --qty \${LUX_QTY} --intervals \${LUX_INTERVALS}
# Restart after a crash, but NOT after the bot's deliberate halt (exit code 2: unknown order state)
Restart=on-failure
RestartSec=10
RestartPreventExitStatus=2
# 09:10 start + 6h25m = ~15:35 IST: the service stops itself after the market closes
RuntimeMaxSec=23100
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/luxalgo.timer >/dev/null <<'EOF'
[Unit]
Description=Start LuxAlgo bot on trading days

[Timer]
OnCalendar=Mon..Fri 09:10:00
Persistent=false
Unit=luxalgo.service

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now luxalgo.timer

cat <<EOF

=====================================================================
 Install complete. Mode: PAPER.
 Next steps:
   1. Paste today's token:        $APP_DIR/set_token.sh
   2. Start now (optional):       sudo systemctl start luxalgo
      (otherwise it starts by itself Mon-Fri 09:10 IST)
   3. Watch the live log:         journalctl -u luxalgo -f
      or the log file:            tail -f $APP_DIR/luxalgo_live_production_multi.log
   4. Check status / next start:  systemctl status luxalgo ; systemctl list-timers luxalgo.timer
   5. Stop it:                    sudo systemctl stop luxalgo
   6. Update the code later:      $APP_DIR/update.sh

 Upstox rules you must handle:
   - Register this VM's PUBLIC reserved IP in your Upstox app (live orders only).
     Find it with:  curl -s https://api.ipify.org ; echo
   - The access token expires daily: run set_token.sh each morning before 09:10.
   - Go live only after clean paper days: edit LUX_MODE=live in $APP_DIR/.env,
     then:  sudo systemctl restart luxalgo
=====================================================================
EOF
