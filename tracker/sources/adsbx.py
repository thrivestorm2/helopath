"""ADS-B Exchange free sample adapter (milestone 8, optional; evaluation only).

ADS-B Exchange publishes readsb-format trace files for the first day of each month
(https://samples.adsbexchange.com/traces/YYYY/MM/01/<last2>/trace_full_<hex>.json).
Because the layout matches readsb, parsing is shared. Unlike adsb.lol there is no
single archive, so scan_day fetches one file per candidate hex.

Status: stub. The URL layout above has NOT been verified from this environment.
Results are always tagged source='adsbx' and never mixed with adsblol builds.
"""
import gzip
import json
import urllib.error
from datetime import date
from pathlib import Path
from typing import Iterator

from ..geo import BBox
from .adsblol import _open
from .base import ScanStats, TracePoint, load_trace_file, parse_readsb_trace, raw_day_dir

SAMPLE_URL = "https://samples.adsbexchange.com/traces/{d:%Y}/{d:%m}/{d:%d}/{h2}/trace_full_{hex}.json"


class AdsbxSampleAdapter:
    name = "adsbx"

    def __init__(self):
        self.stats = ScanStats()

    def scan_day(self, day: date, candidates: set[str], bbox: BBox) -> Iterator[Path]:
        if day.day != 1:
            raise ValueError("ADS-B Exchange samples exist only for the first day of each month")
        self.stats = st = ScanStats(release_used=f"adsbx-sample-{day:%Y-%m-%d}")
        out_dir = raw_day_dir(self.name, day)
        out_dir.mkdir(parents=True, exist_ok=True)
        for hexid in sorted(candidates):
            st.members_scanned += 1
            try:
                with _open(SAMPLE_URL.format(d=day, h2=hexid[-2:], hex=hexid)) as r:
                    raw = r.read()
            except urllib.error.HTTPError:
                continue
            st.bytes_streamed += len(raw)
            data = gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
            doc = json.loads(data)
            if not any(bbox.contains(p[1], p[2]) for p in doc.get("trace", ()) if p[1] is not None):
                continue
            path = out_dir / f"{hexid}.json.gz"
            path.write_bytes(gzip.compress(data))
            st.candidates_kept += 1
            st.bytes_retained += path.stat().st_size
            st.kept_hexes.append(hexid)
            yield path

    def parse(self, path: Path) -> Iterator[TracePoint]:
        return parse_readsb_trace(load_trace_file(path), self.name)

    def meta(self, path: Path) -> dict:
        doc = load_trace_file(path)
        return {k: doc.get(k) for k in ("icao", "r", "t", "desc", "ownOp")}
