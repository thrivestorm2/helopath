#!/usr/bin/env bash
# One-time setup of an Ubuntu 24.04 droplet (spec section 4). Run as root.
# Usage: DOMAIN=tracker.example.com DEPLOY_USER=deploy bash server-setup.sh
set -euo pipefail
: "${DOMAIN:?set DOMAIN}"
DEPLOY_USER="${DEPLOY_USER:-deploy}"
APP_DIR=/srv/helopath

apt-get update
apt-get -y upgrade
apt-get -y install python3 python3-venv python3-pip sqlite3 git ufw unattended-upgrades tmux \
  debian-keyring debian-archive-keyring apt-transport-https curl gnupg

# Unattended security upgrades.
dpkg-reconfigure -f noninteractive unattended-upgrades

# Firewall: SSH, HTTP, HTTPS only.
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

# 1 GB swapfile (try this before upgrading to the 2 GB droplet).
if [ ! -f /swapfile ]; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# Non-root deploy user, SSH keys only.
id "$DEPLOY_USER" >/dev/null 2>&1 || adduser --disabled-password --gecos "" "$DEPLOY_USER"
mkdir -p /home/$DEPLOY_USER/.ssh
[ -f /root/.ssh/authorized_keys ] && cp -n /root/.ssh/authorized_keys /home/$DEPLOY_USER/.ssh/
chown -R $DEPLOY_USER:$DEPLOY_USER /home/$DEPLOY_USER/.ssh && chmod 700 /home/$DEPLOY_USER/.ssh
sed -i -E 's/^#?PasswordAuthentication .*/PasswordAuthentication no/; s/^#?PermitRootLogin .*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
systemctl reload ssh

# Caddy (automatic Let's Encrypt TLS).
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
apt-get update && apt-get -y install caddy

mkdir -p $APP_DIR /var/lib/helopath /var/backups/helopath
chown -R $DEPLOY_USER:$DEPLOY_USER $APP_DIR /var/lib/helopath /var/backups/helopath
echo "Now, as $DEPLOY_USER: clone the repo into $APP_DIR and run deploy/droplet/deploy.sh"
echo "Then as root: DOMAIN=$DOMAIN bash $APP_DIR/deploy/droplet/install-services.sh"
