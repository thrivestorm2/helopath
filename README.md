# helopath — Air Medical Tracker, Phase 1

Private proof of concept. It ingests open ADS-B history from adsb.lol, discovers which helicopters use hospital
helipads in Illinois and the border receiving areas, and infers their legs, landings and missions. The number
Phase 1 must produce is the **resolved-landing rate**: legs ending at an identified site ÷ all legs.

Stack: Django 5 · SQLite (WAL) · server-rendered templates + HTMX + Leaflet · Gunicorn · Caddy · systemd timers.

## Layout

| Path | What |
|---|---|
| `tracker/thresholds.py` | Every tunable number (bbox, segmentation, landing, confidence, discovery, clusters). |
| `tracker/sources/` | Source adapters. `adsblol.py` is the streaming tar scanner; `adsbx.py` is the milestone 8 stub. |
| `tracker/inference/legs.py` | Leg segmentation, end state, confidence, qualifying visits. |
| `tracker/inference/pipeline.py` | Discovery, inferred base, landing clusters, missions, aggregates, rebuild, purge. |
| `tracker/registry.py`, `tracker/seed.py` | FAA registry loader (candidate pool) and FAA facility site seed. |
| `tracker/views.py`, `tracker/templates/` | Pages; `tracker/admin.py` is Django admin with CSV import/export. |
| `deploy/` | Droplet setup, systemd units, Caddyfile, backup script. |
| `docs/format-notes.md` | Verified archive format and the measured full-day scan. |

## Local development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate                 # also creates the Viewer and Admin groups
python manage.py createsuperuser
python manage.py test tracker            # 21 tests, synthetic traces plus one real sample
python manage.py runserver
```

## Operating it

```bash
# 1. Candidate pool: FAA registry rotorcraft (monthly). Downloads ReleasableAircraft.zip if --zip omitted.
python manage.py load_registry [--zip ReleasableAircraft.zip]

# 2. Sites: FAA NASR APT_BASE.csv (from the 28-day NASR subscription CSV zip), filtered to the bbox.
python manage.py seed_sites APT_BASE.csv
#    Then review hospital helipads in /admin/ — the highest-value manual work in the project.

# 3. Backfill 90 days (resumable: days with a successful IngestRun are skipped). Run in tmux; ~8-9 h.
python manage.py backfill --days 90

# Nightly (systemd timer): ingest missing recent days, process queued rebuilds, 120-day purge.
python manage.py nightly

# After changing sites or thresholds:
python manage.py rebuild --source adsblol

# Accounts (no self-signup):
python manage.py create_account alice --role viewer && python manage.py changepassword alice

# Optional milestone 8: ADS-B Exchange first-of-month samples, evaluation only.
python manage.py ingest_day 2026-09-01 --source adsbx
python manage.py coverage_report --day 2026-09-01
```

`ingest_day DAY --local-part file.tar.aa --local-part file.tar.ab` scans already-downloaded parts.

## Deploying (DigitalOcean, ~$7–8/month)

1. Create a $6 Basic droplet (Ubuntu 24.04, 1 GB) with backups on and your SSH key; point DNS at it.
2. As root: `DOMAIN=tracker.example.com bash deploy/server-setup.sh` (ufw, unattended-upgrades, swap, deploy
   user, Caddy).
3. As `deploy`: clone into `/srv/helopath`, copy `deploy/env.example` to `.env` and fill it in, run
   `deploy/deploy.sh`.
4. As root: `DOMAIN=tracker.example.com bash /srv/helopath/deploy/install-services.sh` (gunicorn, nightly timer,
   7-day SQLite backup timer, Caddy with automatic TLS).

## How the guardrails are enforced

1. **Login everywhere.** Django's `LoginRequiredMiddleware` covers every view; only the login forms are exempt.
2. **Provenance.** `Leg`, `Mission`, `LandingCluster`, `MonthlyAggregate` carry `source`; every build filters
   by one source. Evaluation sources (`adsbx`) get legs and `CoverageComparison` rows, never `Aircraft`.
3. **Rebuildable.** `rebuild --source` drops and re-derives everything from `data/raw/<source>/YYYY/MM/DD/`.
   Aircraft `status`, `operator`, `base`, `notes` and cluster dismissals (kept by location) survive.
4. **Attribution.** ODbL footer on every page, the admin, and the CSV export.
5. **No patient information.** No patient fields exist; notes fields carry a warning; missions are "inferred".
6. **No employer-derived data.** Fleet from discovery, sites from FAA data, nothing else imported.
7. **Aggregates out.** The only export is `/flows/export.csv` (MonthlyAggregate rows). Legs are not in the admin.
8. **Medical helicopters only.** Only `discovered`/`confirmed` aircraft are shown; the registry table is not in
   the UI; undiscovered traces and their legs are purged after 120 days.

## Interpretations of the spec worth knowing

- `landed_observed` also covers an observed touchdown with **no** matching site (end_site empty). Without that,
  scene landings and unknown bases could not feed missions or the cluster queue. The resolved-landing rate is
  still "legs with an end site ÷ all legs", computed over discovered + confirmed aircraft (toggle for confirmed).
- `TracePoint` carries three extra fields beyond the spec's list: `new_leg` (flag 2), `stale` (flag 1) and
  `callsign`. Segmentation and hints need them.
- When the inferred base is not a Site, the base is kept as coordinates and mission chaining uses a 300 m circle
  around it; the location also surfaces in the cluster queue.
- A leg that ends in `signal_lost` is bridged to the next leg's departure (within 2 h), so a hospital landing
  below coverage still counts if the aircraft is seen lifting off from that pad.
- Confirm/reject refreshes missions and aggregates immediately; site edits need a rebuild (queued button,
  processed nightly, or `manage.py rebuild`).

## Not verified from the build environment

`registry.faa.gov` and `faa.gov` were blocked from the sandbox this was built in. The registry loader follows the
FAA's `ardata.pdf` column names and the site seed follows NASR `APT_BASE.csv` column names, both unit-tested on
hand-made files. Check both on first real run. The ADS-B Exchange sample URL layout is also unverified.
