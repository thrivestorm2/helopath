#!/usr/bin/env bash
# Deploy or update the app. Run as the deploy user from /srv/helopath.
set -euo pipefail
cd /srv/helopath
git pull --ff-only
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
[ -f .env ] || { echo "create /srv/helopath/.env first (see deploy/env.example)"; exit 1; }
set -a; . ./.env; set +a
.venv/bin/python manage.py migrate --noinput
.venv/bin/python manage.py collectstatic --noinput -v0
.venv/bin/python manage.py check --deploy || true
sudo -n systemctl restart helopath-web.service 2>/dev/null || echo "restart helopath-web.service as root"
