#!/usr/bin/env bash
# Install systemd units and the Caddyfile. Run as root after deploy.sh.
set -euo pipefail
: "${DOMAIN:?set DOMAIN}"
DEPLOY_USER="${DEPLOY_USER:-deploy}"
APP_DIR=/srv/helopath
for unit in helopath-web.service helopath-nightly.service helopath-nightly.timer helopath-backup.service helopath-backup.timer; do
  sed "s/__USER__/$DEPLOY_USER/g" "$APP_DIR/deploy/systemd/$unit" > /etc/systemd/system/$unit
done
sed "s/__DOMAIN__/$DOMAIN/g" "$APP_DIR/deploy/Caddyfile" > /etc/caddy/Caddyfile
systemctl daemon-reload
systemctl enable --now helopath-web.service helopath-nightly.timer helopath-backup.timer
systemctl reload caddy
systemctl status helopath-web.service --no-pager | head -5
