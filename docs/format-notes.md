# adsb.lol format notes (milestone 3: VERIFY FIRST)

Checked on 2026-10-08 against the real release `v2026.10.06-planes-readsb-prod-0`
from `adsblol/globe_history_2026`, by streaming the archive with Python `tarfile` in `r|` mode.

## Releases

- One release per UTC day. Tag: `vYYYY.MM.DD-planes-readsb-{prod|staging|mlatonly}-0`.
  The tag date is the data date (the sample's `timestamp` is 2026-10-06 00:00:00 UTC).
- Assets: `<tag>.tar.aa`, `<tag>.tar.ab`, ... Parts are split at exactly 2,000,000,000 bytes.
  2026-10-06 had three parts: `aa` and `ab` at 2.0 GB each, `ac` at 298 KB. In 2026 the daily total is about 4.0 GB, not the spec's 6.3 GiB.
  Some older days (e.g. 2025-12-14..17 `mlatonly`) ship a single unsplit `<tag>.tar`.
- `PREFERRED_RELEASES.txt` (repo root, on `main`) has one line per day: the comma-separated part URLs of the
  release to use (prod or staging). The adapter reads it from
  `https://raw.githubusercontent.com/adsblol/globe_history_2026/main/PREFERRED_RELEASES.txt` and falls back to
  probing prod then staging parts. Most days are prod; many are staging.
- Release assets download from `github.com/.../releases/download/...` with HTTP Range support (206). A 4 GB
  stream got a TCP reset partway through on the first attempt, so the adapter resumes with a Range request.

## Archive layout

Member order is not sorted. The first members are:

```
.                                  (dir)
./LICENSE-cc0.txt
./heatmap                          (dir)
./heatmap/09.bin.ttf               ~12-18 MB each, 48 files (~700 MB)
./acas/acas.json.gz
./acas/acas.csv.gz
./traces                           (dir)
./traces/7b                        (dir)
./traces/7b/trace_full_ad077b.json
./traces/7b/trace_full_a31a7b.json
...
./traces/0e/trace_full_~28300e.json
```

Findings against the spec's expected layout `traces/<last two hex chars>/trace_full_<hex>.json`:

| Question | Answer |
|---|---|
| Exact path | `./traces/<last 2 hex>/trace_full_<hex>.json`. Note the leading `./`. |
| Letter case | Lowercase hex in both the directory and the file name. |
| Hex in file name | Yes, so candidate filtering works on the name alone, with no decompression needed. |
| Compression | gzip (magic `1f 8b`) despite the `.json` extension. |
| Non-ICAO addresses | Prefixed with `~` (e.g. `trace_full_~28300e.json`, TIS-B/anonymous). Never in the registry pool, so skipped. |
| Directory order | Not sorted (`7b` came before `0e`), so the scanner must not assume order. |
| Members per day | 80,745 on 2026-10-06 (35,109 US-range ICAO traces; 10,517 `~` traces). |

## Trace document

Top-level keys seen: `icao`, `r` (registration), `t` (ICAO type), `dbFlags`, `desc`, `ownOp`
(the registered owner/operator from readsb's database, which is useful as an operator hint), `year`, `version`,
`timestamp` (a **float**, e.g. `1791244800.0`). The point array matches readsb's README:

```json
{"icao": "ad077b", "r": "N939DZ", "t": "B739", "dbFlags": 0, "ownOp": "DELTA AIR LINES INC",
 "version": "readsb 3.16.15 05df27d", "timestamp": 1791244800.0,
 "trace": [[68989.57, 33.644062, -84.410299, "ground", 5.2, null, 1, null,
            {"type": "adsb_icao", "nic": 8, ...}, "adsb_icao", null, null, null, null], ...]}
```

- Index 3 really is mixed: `"ground"`, an integer (ft), or null.
- Flags seen: `0`, `1` (stale), `2` (new leg), `4` (geometric vertical rate).
- The detail object (index 8) carries `flight` (callsign, space padded), `category` (`A7` = rotorcraft), `squawk`, etc.

## Sample parsed into TracePoint rows

`tracker/tests/fixtures/trace_full_ad077b.json` is the real file above (an airliner at ATL, chosen because it is
not a helicopter). `tracker.tests.test_format.TraceParseTests.test_real_sample_trace` parses it. First rows:

| hex | ts_utc | lat | lon | alt_baro_ft | alt_geom_ft | on_ground | gs_kt | track_deg | vrate_fpm | new_leg | stale |
|---|---|---|---|---|---|---|---|---|---|---|---|
| ad077b | 2026-10-06 19:09:49.57 | 33.644062 | -84.410299 | null | null | true | 5.2 | null | null | false | true |
| ad077b | 2026-10-06 19:10:03.89 | 33.644059 | -84.410405 | null | null | true | 0.5 | null | null | false | false |

## Measured full-day scan (milestone 4 figures)

Run on 2026-10-08 in the build sandbox, not on the droplet, for 2026-10-06 (3 parts, 4.0 GB). The FAA registry
host was unreachable from the sandbox, so the candidate filter was approximated: every US-range ICAO trace
(35,109 of them) was decompressed and kept if it reported emitter category `A7` (rotorcraft) and had a point in
the bounding box. This is **more** work than production, which decompresses only registry rotorcraft.

| Measure | Value |
|---|---|
| Bytes streamed | 4.00 GB |
| Wall time | 322 s (~5.4 min), dominated by download |
| Peak RSS | 99 MB |
| Rotorcraft (A7) traces worldwide | 2,215 |
| Kept (in bbox) | 115 aircraft, 2.9 MB of gzip traces |
| Types kept | B06 20, B407 16, EC35 14, R44 14, EC45 13, EC30 7, B429 6, ... |
| Inference on those 115 | 589 legs, 3 s; with no sites loaded: 104 `landed_observed`, 485 `signal_lost` |

So the spec's estimates move as follows. Disk is about 3 MB/day rather than 10 MB/day, so the 90-day backfill
needs about 270 MB. A day takes about 5-6 minutes, so the backfill needs about 8-9 hours. Memory is far
inside 1 GB. Re-measure on the droplet with the real registry pool and record it in the ingest log. A
helicopter that never transmits category A7 would be missed by this approximation but caught by the registry.
