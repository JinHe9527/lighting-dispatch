#!/bin/bash
set -euo pipefail

PACKAGE_DIR="${1:-/home/ubuntu/wuyou-dispatch-release}"
APP_ROOT="/opt/wuyou-dispatch"
RELEASES_DIR="$APP_ROOT/releases"
DATA_ROOT="/var/lib/wuyou-dispatch"
BACKUP_ROOT="/var/backups/wuyou-dispatch"
CONFIG_BACKUP_ROOT="/var/backups/wuyou-dispatch-config"
RELEASE_ID="$(date +%Y%m%d%H%M%S)"
TARGET_DIR="$RELEASES_DIR/$RELEASE_ID"
CURRENT_LINK="$APP_ROOT/current"
NEXT_LINK="$APP_ROOT/current.next"
OLD_TARGET="$(sudo readlink -f "$CURRENT_LINK" 2>/dev/null || true)"
CONFIG_SNAPSHOT="$CONFIG_BACKUP_ROOT/pre-$RELEASE_ID"
SERVICE_WAS_ACTIVE="$(systemctl is-active wuyou-dispatch 2>/dev/null || true)"
DEFAULT_SITE_WAS_ENABLED=0
DEPLOY_FINALIZED=0
MIGRATION_BACKUP="$BACKUP_ROOT/pre-migration-$RELEASE_ID.db"

rollback() {
  if [ "$DEPLOY_FINALIZED" -eq 1 ]; then
    return
  fi
  set +e
  if ! sudo test -f "$CONFIG_SNAPSHOT/nginx-wuyou-dispatch.conf" && sudo test -L /etc/nginx/sites-enabled/wuyou-dispatch; then
    sudo mv /etc/nginx/sites-enabled/wuyou-dispatch "$CONFIG_SNAPSHOT/wuyou-dispatch.enabled"
  fi
  for entry in \
    "wuyou-dispatch.service:/etc/systemd/system/wuyou-dispatch.service" \
    "nginx-wuyou-dispatch.conf:/etc/nginx/sites-available/wuyou-dispatch" \
    "fail2ban-sshd.local:/etc/fail2ban/jail.d/sshd.local" \
    "ssh-wuyou-dispatch.conf:/etc/ssh/sshd_config.d/99-wuyou-dispatch.conf"; do
    snapshot_name="${entry%%:*}"
    target_path="${entry#*:}"
    if sudo test -f "$CONFIG_SNAPSHOT/$snapshot_name"; then
      sudo install -m 0644 "$CONFIG_SNAPSHOT/$snapshot_name" "$target_path"
    elif sudo test -f "$target_path"; then
      sudo mv "$target_path" "$CONFIG_SNAPSHOT/$snapshot_name.failed"
    fi
  done
  if [ "$DEFAULT_SITE_WAS_ENABLED" -eq 1 ]; then
    sudo ln -sfn /etc/nginx/sites-available/default /etc/nginx/sites-enabled/default
  fi
  if [ -n "$OLD_TARGET" ] && sudo test -d "$OLD_TARGET"; then
    sudo ln -sfn "$OLD_TARGET" "$NEXT_LINK"
    sudo mv -Tf "$NEXT_LINK" "$CURRENT_LINK"
  fi
  if sudo test -f "$MIGRATION_BACKUP"; then
    sudo systemctl stop wuyou-dispatch
    sudo -u dispatch /usr/bin/python3 - "$MIGRATION_BACKUP" "$DATA_ROOT/dispatch.db" <<'PY'
import sqlite3
import sys

source = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
target = sqlite3.connect(sys.argv[2])
source.backup(target)
target.close()
source.close()
PY
  fi
  sudo systemctl daemon-reload
  sudo nginx -t && sudo systemctl reload nginx
  sudo sshd -t && sudo systemctl reload ssh
  if [ "$SERVICE_WAS_ACTIVE" = "active" ]; then
    sudo systemctl restart wuyou-dispatch
  fi
  echo "部署失败，已恢复部署前的服务配置" >&2
}

if [ ! -d "$PACKAGE_DIR/app" ]; then
  echo "部署包不存在: $PACKAGE_DIR/app" >&2
  exit 1
fi

for file in \
  server.py business.py service.py index.html manifest.webmanifest \
  app-icon.svg app-icon-180.png app-icon-192.png app-icon-512.png \
  vendor/lucide.min.js; do
  if [ ! -f "$PACKAGE_DIR/app/$file" ]; then
    echo "部署包缺少文件: app/$file" >&2
    exit 1
  fi
done
for file in wuyou-dispatch.service nginx-wuyou-dispatch.conf fail2ban-sshd.local ssh-wuyou-dispatch.conf; do
  if [ ! -f "$PACKAGE_DIR/$file" ]; then
    echo "部署包缺少文件: $file" >&2
    exit 1
  fi
done
if [ ! -f /etc/wuyou-dispatch.env ] && [ ! -f "$PACKAGE_DIR/wuyou-dispatch.env" ]; then
  echo "首次部署缺少配置文件: wuyou-dispatch.env" >&2
  exit 1
fi

if ! id dispatch >/dev/null 2>&1; then
  sudo useradd --system --home-dir "$DATA_ROOT" --shell /usr/sbin/nologin dispatch
fi

sudo install -d -m 0750 -o root -g dispatch "$APP_ROOT" "$RELEASES_DIR" "$TARGET_DIR"
sudo install -d -m 0750 -o root -g dispatch "$TARGET_DIR/app" "$TARGET_DIR/app/vendor"
sudo install -d -m 0750 -o dispatch -g dispatch \
  "$DATA_ROOT" "$DATA_ROOT/media" "$DATA_ROOT/logs" "$BACKUP_ROOT" "$BACKUP_ROOT/media"
sudo install -d -m 0700 -o root -g root "$CONFIG_BACKUP_ROOT" "$CONFIG_SNAPSHOT"

for entry in \
  "/etc/systemd/system/wuyou-dispatch.service:wuyou-dispatch.service" \
  "/etc/nginx/sites-available/wuyou-dispatch:nginx-wuyou-dispatch.conf" \
  "/etc/fail2ban/jail.d/sshd.local:fail2ban-sshd.local" \
  "/etc/ssh/sshd_config.d/99-wuyou-dispatch.conf:ssh-wuyou-dispatch.conf"; do
  source_path="${entry%%:*}"
  snapshot_name="${entry#*:}"
  if [ -f "$source_path" ]; then
    sudo install -m 0600 -o root -g root "$source_path" "$CONFIG_SNAPSHOT/$snapshot_name"
  fi
done
if sudo test -L /etc/nginx/sites-enabled/default; then
  DEFAULT_SITE_WAS_ENABLED=1
fi
trap rollback ERR INT TERM

for file in server.py business.py service.py index.html; do
  sudo install -m 0640 -o root -g dispatch "$PACKAGE_DIR/app/$file" "$TARGET_DIR/app/$file"
done
for file in manifest.webmanifest app-icon.svg app-icon-180.png app-icon-192.png app-icon-512.png; do
  sudo install -m 0644 -o root -g dispatch "$PACKAGE_DIR/app/$file" "$TARGET_DIR/app/$file"
done
sudo install -m 0640 -o root -g dispatch \
  "$PACKAGE_DIR/app/vendor/lucide.min.js" "$TARGET_DIR/app/vendor/lucide.min.js"

if [ -f "$PACKAGE_DIR/app/data.json" ]; then
  sudo install -m 0640 -o root -g dispatch "$PACKAGE_DIR/app/data.json" "$TARGET_DIR/app/data.json"
fi

sudo -u dispatch /usr/bin/python3 -c \
  'import ast,sys; [ast.parse(open(path, encoding="utf-8").read(), filename=path) for path in sys.argv[1:]]' \
  "$TARGET_DIR/app/server.py" "$TARGET_DIR/app/business.py" "$TARGET_DIR/app/service.py"

if [ "$SERVICE_WAS_ACTIVE" = "active" ]; then
  sudo systemctl stop wuyou-dispatch
fi

LEGACY_DB="$APP_ROOT/app/dispatch.db"
PACKAGE_DB="$PACKAGE_DIR/app/dispatch.db"
TARGET_DB="$DATA_ROOT/dispatch.db"
if sudo test -f "$LEGACY_DB"; then
  sudo -u dispatch /usr/bin/python3 - "$LEGACY_DB" "$TARGET_DB" "$MIGRATION_BACKUP" <<'PY'
import os
import sqlite3
import sys

source_path, target_path, backup_path = sys.argv[1:]


def business_rows(conn):
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    return sum(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("users", "teams", "orders")
        if table in names
    )


source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
    raise RuntimeError("旧数据库完整性检查失败")
source_rows = business_rows(source)

target_exists = os.path.exists(target_path)
target_rows = 0
if target_exists:
    target = sqlite3.connect(target_path)
    if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise RuntimeError("目标数据库完整性检查失败")
    target_rows = business_rows(target)
    if target_rows == 0 and source_rows > 0:
        backup = sqlite3.connect(backup_path)
        target.backup(backup)
        backup.close()
    target.close()

if not target_exists or (target_rows == 0 and source_rows > 0):
    target = sqlite3.connect(target_path)
    source.backup(target)
    target.close()
source.close()
PY
elif ! sudo test -f "$TARGET_DB" && [ -f "$PACKAGE_DB" ]; then
  sudo -u dispatch /usr/bin/python3 - "$PACKAGE_DB" "$TARGET_DB" <<'PY'
import sqlite3
import sys

source = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
target = sqlite3.connect(sys.argv[2])
source.backup(target)
target.close()
source.close()
PY
fi
sudo chown dispatch:dispatch "$TARGET_DB" 2>/dev/null || true
sudo chmod 0640 "$TARGET_DB" 2>/dev/null || true

if sudo test -d "$APP_ROOT/app/media"; then
  sudo cp -a --update=none "$APP_ROOT/app/media/." "$DATA_ROOT/media/"
fi
if [ -d "$PACKAGE_DIR/app/media" ]; then
  sudo cp -a --update=none "$PACKAGE_DIR/app/media/." "$DATA_ROOT/media/"
fi
sudo chown -R dispatch:dispatch "$DATA_ROOT" "$BACKUP_ROOT/media"

if [ ! -f /etc/wuyou-dispatch.env ]; then
  sudo install -m 0600 -o root -g root "$PACKAGE_DIR/wuyou-dispatch.env" /etc/wuyou-dispatch.env
fi
sudo install -m 0600 -o root -g root /etc/wuyou-dispatch.env "$CONFIG_BACKUP_ROOT/wuyou-dispatch.env"

sudo install -m 0644 "$PACKAGE_DIR/wuyou-dispatch.service" /etc/systemd/system/wuyou-dispatch.service
sudo install -m 0644 "$PACKAGE_DIR/nginx-wuyou-dispatch.conf" /etc/nginx/sites-available/wuyou-dispatch
sudo install -m 0644 "$PACKAGE_DIR/fail2ban-sshd.local" /etc/fail2ban/jail.d/sshd.local
sudo install -m 0644 "$PACKAGE_DIR/ssh-wuyou-dispatch.conf" /etc/ssh/sshd_config.d/99-wuyou-dispatch.conf
sudo ln -sfn /etc/nginx/sites-available/wuyou-dispatch /etc/nginx/sites-enabled/wuyou-dispatch
if [ -L /etc/nginx/sites-enabled/default ]; then
  sudo mv /etc/nginx/sites-enabled/default /etc/nginx/sites-available/default.disabled
fi

sudo nginx -t
sudo sshd -t
sudo ln -sfn "$TARGET_DIR" "$NEXT_LINK"
sudo mv -Tf "$NEXT_LINK" "$CURRENT_LINK"
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

healthy=0
for _attempt in $(seq 1 30); do
  if curl --fail --silent --show-error http://127.0.0.1:8848/api/health >/dev/null; then
    healthy=1
    break
  fi
  sleep 1
done

if [ "$healthy" -ne 1 ]; then
  sudo journalctl -u wuyou-dispatch -n 80 --no-pager >&2
  rollback
  exit 1
fi

DEPLOY_FINALIZED=1
trap - ERR INT TERM
echo "部署完成: $TARGET_DIR"
