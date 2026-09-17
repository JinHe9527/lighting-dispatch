#!/usr/bin/env bash
set -euo pipefail

[[ "$EUID" -eq 0 && "$(uname -s)" == Linux ]] || { echo 'Run with sudo on Linux' >&2; exit 1; }
[[ "$#" -le 1 ]] || { echo 'Usage: host-bootstrap.sh [deploy-public-key.pub]' >&2; exit 1; }
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
app_root=/opt/wuyou-dispatch
deploy_home=/var/lib/wuyou-deploy
key_file="${1:-}"
for command in python3 systemctl useradd passwd install visudo timeout flock; do
  command -v "$command" >/dev/null || { echo "Missing command: $command" >&2; exit 1; }
done
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'
[[ ! -e "$app_root/app" ]] || { echo 'Legacy deployment requires a separate migration' >&2; exit 1; }
if [[ -n "$key_file" ]]; then
  [[ ! -e "$deploy_home/.ssh/authorized_keys" && ! -L "$deploy_home/.ssh/authorized_keys" ]] || { echo 'Key already installed; omit the key to update host scripts' >&2; exit 1; }
  read -r key_type key_data _ < "$key_file"
  [[ "$key_type" == ssh-ed25519 && "$key_data" =~ ^[A-Za-z0-9+/]+={0,3}$ ]]
  ssh-keygen -lf "$key_file" >/dev/null
fi

if ! id dispatch >/dev/null 2>&1; then
  useradd --system --user-group --home-dir /var/lib/wuyou-dispatch --no-create-home --shell /usr/sbin/nologin dispatch
fi
if ! id wuyou-deploy >/dev/null 2>&1; then
  useradd --system --user-group --home-dir "$deploy_home" --create-home --shell /bin/bash wuyou-deploy
fi
[[ "$(id -u dispatch)" -ne 0 && "$(id -u wuyou-deploy)" -ne 0 ]]
passwd --lock wuyou-deploy >/dev/null
for directory in "$app_root" "$app_root/releases" "$app_root/incoming" "$deploy_home/.ssh"; do
  [[ ! -L "$directory" ]] || { echo "Refusing directory symlink: $directory" >&2; exit 1; }
done
install -d -m 0755 -o wuyou-deploy -g wuyou-deploy "$app_root" "$app_root/releases"
install -d -m 0700 -o wuyou-deploy -g wuyou-deploy "$app_root/incoming"
[[ ! -L "$app_root/deploy.lock" ]] || { echo 'Refusing deploy lock symlink' >&2; exit 1; }
if [[ ! -e "$app_root/deploy.lock" ]]; then
  install -m 0600 -o wuyou-deploy -g wuyou-deploy /dev/null "$app_root/deploy.lock"
fi
exec 9<>"$app_root/deploy.lock"
flock -n 9 || { echo 'An application deployment is running; retry after it completes' >&2; exit 1; }
install -d -m 0750 -o dispatch -g dispatch /var/lib/wuyou-dispatch /var/backups/wuyou-dispatch
if [[ ! -e /etc/wuyou-dispatch.env ]]; then
  install -m 0600 -o root -g root /dev/null /etc/wuyou-dispatch.env
  printf '%s\n' 'DISPATCH_APP_ID=' 'DISPATCH_APP_SECRET=' \
    'DISPATCH_BASE_URL=https://wuyoupaiban.cn' 'DISPATCH_TEST_MODE=0' > /etc/wuyou-dispatch.env
fi

install -d -m 0755 /usr/local/libexec
install -m 0644 -o root -g root "$script_dir/release.py" /usr/local/libexec/wuyou-release.py
install -m 0644 -o root -g root "$script_dir/wuyou-dispatch.service" /etc/systemd/system/wuyou-dispatch.service
sudoers_file="$(mktemp)"
printf '%s\n' 'wuyou-deploy ALL=(root) NOPASSWD: /usr/bin/systemctl restart wuyou-dispatch.service, /usr/bin/systemctl stop wuyou-dispatch.service' > "$sudoers_file"
visudo -cf "$sudoers_file" >/dev/null
install -m 0440 -o root -g root "$sudoers_file" /etc/sudoers.d/wuyou-deploy
if command -v trash >/dev/null; then trash "$sudoers_file"; else echo "Temporary sudoers copy retained: $sudoers_file"; fi

if [[ -n "$key_file" ]]; then
  install -d -m 0700 -o wuyou-deploy -g wuyou-deploy "$deploy_home/.ssh"
  printf 'restrict,command="/usr/bin/python3 /usr/local/libexec/wuyou-release.py ssh" %s %s\n' \
    "$key_type" "$key_data" > "$deploy_home/.ssh/authorized_keys"
  chown wuyou-deploy:wuyou-deploy "$deploy_home/.ssh/authorized_keys"
  chmod 0600 "$deploy_home/.ssh/authorized_keys"
fi
timeout 30 systemctl daemon-reload
timeout 30 systemctl enable wuyou-dispatch.service
python3 /usr/local/libexec/wuyou-release.py digest /etc/systemd/system/wuyou-dispatch.service
echo 'Host ready; application not started. DNS, Nginx, SSH settings and firewall unchanged.'
