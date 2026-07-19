#!/bin/bash
set -euo pipefail

RELEASE_DIR="${1:-/home/ubuntu/wuyou-dispatch-release}"
APP_ROOT="/opt/wuyou-dispatch"
APP_DIR="$APP_ROOT/app"

if [ ! -d "$RELEASE_DIR/app" ]; then
  echo "部署包不存在: $RELEASE_DIR/app" >&2
  exit 1
fi

if ! id dispatch >/dev/null 2>&1; then
  sudo useradd --system --home-dir "$APP_ROOT" --shell /usr/sbin/nologin dispatch
fi

sudo install -d -m 0750 -o dispatch -g dispatch "$APP_ROOT" "$APP_DIR"
sudo install -d -m 0750 -o dispatch -g dispatch \
  "$APP_DIR/vendor" "$APP_DIR/media" "$APP_DIR/backups" "$APP_DIR/logs"

for file in server.py business.py service.py index.html; do
  sudo install -m 0640 -o dispatch -g dispatch "$RELEASE_DIR/app/$file" "$APP_DIR/$file"
done
sudo install -m 0640 -o dispatch -g dispatch \
  "$RELEASE_DIR/app/vendor/lucide.min.js" "$APP_DIR/vendor/lucide.min.js"

if [ -f "$RELEASE_DIR/app/data.json" ]; then
  sudo install -m 0640 -o dispatch -g dispatch "$RELEASE_DIR/app/data.json" "$APP_DIR/data.json"
fi
if [ ! -f "$APP_DIR/dispatch.db" ]; then
  sudo install -m 0640 -o dispatch -g dispatch "$RELEASE_DIR/app/dispatch.db" "$APP_DIR/dispatch.db"
fi
if [ -d "$RELEASE_DIR/app/media" ]; then
  sudo cp -a --update=none "$RELEASE_DIR/app/media/." "$APP_DIR/media/"
  sudo chown -R dispatch:dispatch "$APP_DIR/media"
fi

if [ ! -f /etc/wuyou-dispatch.env ]; then
  sudo install -m 0600 -o root -g root "$RELEASE_DIR/wuyou-dispatch.env" /etc/wuyou-dispatch.env
fi

sudo install -m 0644 "$RELEASE_DIR/wuyou-dispatch.service" /etc/systemd/system/wuyou-dispatch.service
sudo install -m 0644 "$RELEASE_DIR/nginx-wuyou-dispatch.conf" /etc/nginx/sites-available/wuyou-dispatch
sudo install -m 0644 "$RELEASE_DIR/fail2ban-sshd.local" /etc/fail2ban/jail.d/sshd.local
sudo install -m 0644 "$RELEASE_DIR/ssh-wuyou-dispatch.conf" /etc/ssh/sshd_config.d/99-wuyou-dispatch.conf
sudo ln -sfn /etc/nginx/sites-available/wuyou-dispatch /etc/nginx/sites-enabled/wuyou-dispatch
if [ -L /etc/nginx/sites-enabled/default ]; then
  sudo mv /etc/nginx/sites-enabled/default /etc/nginx/sites-available/default.disabled
fi

sudo nginx -t
sudo sshd -t
sudo timedatectl set-timezone Asia/Shanghai
sudo systemctl daemon-reload
sudo systemctl enable --now nginx
sudo systemctl reload nginx
sudo systemctl enable --now fail2ban
sudo systemctl restart fail2ban
sudo systemctl reload ssh
sudo systemctl enable wuyou-dispatch
sudo systemctl restart wuyou-dispatch
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw --force enable
