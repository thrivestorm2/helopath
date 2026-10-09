# Fallback: a plain DigitalOcean droplet (~$7–8/month)

Not used by default. The app deploys to App Platform via `.github/workflows/deploy.yml` (see `docs/DEPLOY.md`).
These scripts run the same app on a single $6 droplet with SQLite, systemd timers and Caddy, for a cheaper,
hands-on setup. On a droplet, run `manage.py run_worker` as a service instead of the nightly timer if you want
the Operations page buttons to work.
