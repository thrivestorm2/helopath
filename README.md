# helopath — Air Medical Tracker

An open-source tool that studies **air medical helicopter activity from public ADS-B data**. It ingests open
ADS-B history from [adsb.lol](https://adsb.lol), discovers which helicopters use hospital helipads in Illinois and
the border receiving areas, and infers their legs, landings, missions and patient transfers between hospitals.

- **Inferred, not observed.** Missions and patient flows are worked out from aircraft positions. Nothing here
  asserts that a patient was on board, and the app holds **no patient information** of any kind.
- **Private by design.** The running app requires a login; this repository contains the code only — no data, no
  credentials and no deployment secrets (they live in the hosting provider's encrypted settings).
- The headline measure is the **resolved-landing rate**: of the legs ending inside the study area, those ending at an
  identified site (or, when the signal was lost, whose aircraft is next seen departing a site within 2 hours) ÷ all
  of them.

Licensed under the [GNU Affero General Public License v3.0](LICENSE). Security issues: see [SECURITY.md](SECURITY.md).

Stack: Django 5 · PostgreSQL in production (SQLite locally) · server-rendered templates + HTMX + Leaflet · Gunicorn ·
DigitalOcean App Platform (web service + background worker + managed PostgreSQL).

**To put it online, follow [docs/DEPLOY.md](docs/DEPLOY.md).** DigitalOcean watches `main` and deploys every merge;
pull requests run the test suite (`.github/workflows/tests.yml`) so you can see a ✓ before merging.

## Layout

| Path | What |
|---|---|
| `tracker/thresholds.py` | Every tunable number (bbox, segmentation, landing, confidence, discovery, clusters). |
| `tracker/sources/` | Source adapters. `adsblol.py` is the streaming tar scanner; `adsbx.py` is the milestone 8 stub. |
| `tracker/inference/legs.py` | Leg segmentation, end state, confidence, qualifying visits. |
| `tracker/inference/pipeline.py` | Discovery, inferred base, landing clusters, missions, aggregates, rebuild, purge. |
| `tracker/registry.py`, `tracker/seed.py` | FAA registry loader (candidate pool) and FAA facility site seed. |
| `tracker/views.py`, `tracker/templates/` | Pages; `tracker/admin.py` is Django admin with CSV import/export. |
| `Dockerfile`, `.do/app.yaml` | Production image and App Platform spec (web + worker + managed Postgres). |
| `.github/workflows/tests.yml` | Tests on every pull request (SQLite and PostgreSQL) plus an image build. |
| `tracker/ops.py`, `run_worker` command | Background worker: nightly ingest and Operations-page tasks. |
| `tracker/rawstore.py` | Retained raw traces (gzip blobs in the `RawTrace` table). |
| `deploy/droplet/` | Unused fallback: the same app on a plain $6 droplet. |
| `docs/format-notes.md` | Verified archive format and the measured full-day scan. |

## Local development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate                 # also creates the Viewer and Admin groups
python manage.py createsuperuser
python manage.py test tracker            # set DATABASE_URL to run them on PostgreSQL
python manage.py runserver
```

## Operating it

In production, use the **Operations** page buttons; the commands below are the same steps for local use.

```bash
# 1. Candidate pool: FAA registry rotorcraft (monthly). Downloads ReleasableAircraft.zip if --zip omitted.
python manage.py load_registry [--zip ReleasableAircraft.zip]

# 2. Sites: FAA NASR APT_BASE.csv (from the 28-day NASR subscription CSV zip), filtered to the bbox.
python manage.py seed_sites APT_BASE.csv
#    Then review hospital helipads in /admin/ — the highest-value manual work in the project.

# 3. Backfill 90 days (resumable: days with a successful IngestRun are skipped). ~8-9 h.
python manage.py backfill --days 90

# Nightly: ingest missing recent days, process queued rebuilds, 120-day purge.
python manage.py nightly

# Background worker (production): runs nightly at 18:30 UTC plus Operations-page tasks.
python manage.py run_worker

# Full rebuild by hand (site edits apply on their own; threshold changes need this):
python manage.py rebuild --source adsblol

# Accounts (no self-signup):
python manage.py create_account alice --role viewer && python manage.py changepassword alice

# Optional milestone 8: ADS-B Exchange first-of-month samples, evaluation only.
python manage.py ingest_day 2026-09-01 --source adsbx
python manage.py coverage_report --day 2026-09-01
```

`ingest_day DAY --local-part file.tar.aa --local-part file.tar.ab` scans already-downloaded parts.

## Deploying

See [docs/DEPLOY.md](docs/DEPLOY.md) (App Platform, about $25/month, deploys on every merge to `main`). The cheaper hands-on
droplet setup from the original spec (about $7–8/month) is kept in `deploy/droplet/`.

## How the guardrails are enforced

1. **Login everywhere.** Django's `LoginRequiredMiddleware` covers every view; only the login forms are exempt.
2. **Provenance.** `Leg`, `Mission`, `LandingCluster`, `MonthlyAggregate` carry `source`; every build filters
   by one source. Evaluation sources (`adsbx`) get legs and `CoverageComparison` rows, never `Aircraft`.
3. **Rebuildable.** `rebuild --source` drops and re-derives everything from the retained
   `RawTrace` rows (the gzip trace files exactly as downloaded).
   Aircraft `status`, `operator`, `program`, `base`, `notes` and cluster dismissals (kept by location) survive.
   Aircraft rows are never removed automatically, even if one no longer meets the discovery rule.
4. **Attribution.** ODbL footer on every page, the admin, and the CSV export.
5. **No patient information.** No patient fields exist; notes fields carry a warning; missions are "inferred".
6. **No employer-derived data.** Fleet from discovery, sites from FAA data, nothing else imported.
7. **Aggregates out.** The only general export is `/flows/export.csv` (MonthlyAggregate rows). Legs are not in the
   admin. Owner-approved exception: admins can export unresolved legs (`/quality/unresolved/export.csv`) to
   diagnose why landings do not match a site.
8. **Medical helicopters only.** Only `discovered`/`confirmed` aircraft are shown; the registry table is not in
   the UI; undiscovered traces and their legs are purged after 120 days.

## Interpretations of the spec worth knowing

- Hosting moved from a $6 droplet to App Platform at the owner's request (push-to-deploy, no server admin). App
  Platform has no persistent disk, so retained raw traces live in PostgreSQL as gzip blobs (`RawTrace`) rather
  than as files, and systemd timers are replaced by the `run_worker` background worker.

- `landed_observed` also covers an observed touchdown with **no** matching site (end_site empty). Without that,
  scene landings and unknown bases could not feed missions or the cluster queue. The resolved-landing rate counts
  legs ending inside the study area only (legs ending outside it are listed as "Ended outside the study area"); a
  lost-signal leg counts as resolved when the aircraft is next seen departing a site within
  `MISSION_LOST_SIGNAL_BRIDGE_HOURS`, the same rule missions use. It is computed over discovered + confirmed
  aircraft (toggle for confirmed). The map's "Signal lost" layer shows where aircraft lose signal at altitude: the
  places where another adsb.lol receiver would help most.
- `TracePoint` carries three extra fields beyond the spec's list: `new_leg` (flag 2), `stale` (flag 1) and
  `callsign`. Segmentation and hints need them.
- When the inferred base is not a Site, the base is kept as coordinates and mission chaining uses a 300 m circle
  around it; the location also surfaces in the cluster queue.
- Elevated pads: many hospital helipads are on rooftops, where transponders rarely report "ground" and coverage
  drops on short final. If the signal is lost while the aircraft is slow (< 40 kt) and low (< 500 ft above the
  site's elevation) within 1 km of a site, the leg is `landing_inferred` at that site, confidence 0.85 minus
  up to 0.2 for distance and 0.2 if another site is within 1 km. Speed and altitude are the most recent values
  in the last 60 s. Thresholds: `LOW_SLOW_*` in `tracker/thresholds.py`.
- Coverage gaps mid-flight are rejoined: when signal is lost while cruising and regained within 30 min, and
  the jump across the gap matches the heading (within 45°) and speed (0.7–1.3×) flown on both sides, the two
  pieces become one leg (`Leg.gaps_bridged` counts the joins). An unseen stop shows up as a heading change or a
  slow jump, so those stay separate. Thresholds: `STITCH_*` in `tracker/thresholds.py`. On the real 2026-10-06
  sample, 26 gaps were rejoined (signal-lost legs 485 → 459); every join matched cruise speed within ~8%.
- Busy landing clusters (more than 20 landings) get name suggestions from OpenStreetMap: the worker asks the
  Overpass API for named hospitals and helipads within 500 m, a few per idle tick, caches answers by ~100 m
  location, and retries failures after an hour. The Cluster queue shows the best match; Create site (or Use on
  another nearby match) pre-fills its name, city and state (the feature's address, else the city and state
  containing the cluster) as a Hospital helipad. Suggestions only; an admin creates the site.
- A leg that ends in `signal_lost` is bridged to the next leg's departure (within 2 h), so a hospital landing
  below coverage still counts if the aircraft is seen lifting off from that pad.
- Weather hangars: a base can name the site (usually an airport) where its aircraft shelter in bad weather. Aircraft
  based there treat it as a second home: base ↔ hangar flights are repositions, missions flown from the hangar count
  normally, and days started in the hangar count toward the base when the base is inferred. The base's page suggests
  airports where its aircraft often start the day (`HANGAR_SUGGEST_MIN_DAYS`). Changes apply after a rebuild.
- Hospital bases: when the base is a hospital helipad, landing back there after a scene or after one other hospital is
  counted as the drop-off (scene → base hospital, or interfacility other hospital → base hospital), at no more than
  0.6 confidence (`HOSPITAL_BASE_DELIVERY_CONF`), since a flight cancelled en route looks the same. A
  there-and-back to another hospital (base → B → base) looks the same whichever way the patient went, so the patient
  is assumed to go to the busier hospital: landings by medical aircraft based at neither hospital decide it. If B is
  busier it counts as base → B (rule ends "(to the busier hospital)"); otherwise, and on a tie, as B → base.
- Routes whose start or end is a scene / unknown link to a map of the landings away from known sites on that route
  (`/routes/map/`); a lost signal is plotted where the aircraft was last seen.
- Programs are the service name on the aircraft (often hospital-branded), separate from the operator that flies
  it. They are records of their own: add them in the admin or by typing a name in a Review queue card (a new one
  takes that aircraft's operator), link aircraft to them, and open a program's page for its aircraft, missions and
  routes. Program pages count missions directly; flow aggregates and the CSV export stay per operator.
- Rebuilds come in three kinds (`tracker/rebuild_flag.py`, run by `tracker/rebuilds.py`):
  - **Recount** (no legs re-derived): after a weather hangar, service period, or a site's type, reviewed flag or
    FAA ID changes.
  - **Partial**: after a site is added, moved, re-sized, re-elevated or deactivated, legs are re-derived only for
    aircraft with a leg starting or ending within about 5 km of its old or new location (how a track is split into
    legs never depends on sites; only the first and last points are matched to sites, within those distances), then
    the recount. A test checks it gives exactly what a full rebuild gives.
  - **Full**: everything from the raw traces.

  Recounts and partials run on their own `QUICK_UPDATE_DELAY_S` (2 minutes) after the last edit, so a burst of edits
  shares one run. Full rebuilds run on their own only overnight (`FULL_REBUILD_LOCAL_HOUR`, 2 am Chicago): after a
  deploy with new counting rules, after FAA sites are seeded, when a partial would touch more than
  `PARTIAL_MAX_SHARE` (40%) of aircraft, and every Sunday as a safety net. Admins see what is pending in a banner
  (with **Run now** for a full rebuild), and each run with its step timings on the Operations page. Edits that change
  no figures (a site's name, city, notes, social pages) need nothing. **Bump `PIPELINE_VERSION` in
  `tracker/inference/pipeline.py` with any change to legs, discovery, missions or aggregates.**
- Confirm/reject refreshes missions and aggregates immediately.

## Not verified from the build environment

The FAA NASR download URL used by **Seed FAA sites** (`tracker/nasr.py`) is unverified too. There is an upload
fallback on the Operations page. `registry.faa.gov` and `faa.gov` were blocked from the sandbox this was built in. The registry loader follows the
FAA's `ardata.pdf` column names and the site seed follows NASR `APT_BASE.csv` column names, both unit-tested on
hand-made files. Check both on first real run. The ADS-B Exchange sample URL layout is also unverified.

## Data sources and credits

| Data | Source | Licence |
|---|---|---|
| Aircraft positions (ADS-B) | [adsb.lol](https://adsb.lol) | [ODbL](https://opendatacommons.org/licenses/odbl/) — attribution shown in the app footer |
| Airports, heliports, aircraft registry | U.S. FAA (NASR, Releasable Aircraft) | Public domain (U.S. Government work) |
| Map tiles, place names, city/state | [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors (Overpass, Nominatim) | ODbL |
| Map library | [Leaflet](https://leafletjs.com) (vendored in `static/vendor/`) | BSD-2-Clause |

The repository includes one sample adsb.lol trace for the tests (`tracker/tests/fixtures/`); see the README there.

## Licence

Copyright © thrivestorm2 and contributors. This program is free software: you can redistribute it and/or modify it
under the terms of the GNU Affero General Public License as published by the Free Software Foundation, version 3.
If you run a modified version for others over a network, you must offer them its source (section 13); the app's
footer links to the source for this reason.
