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
from typing import Iterator

from ..geo import BBox
from .adsblol import _open
from .. import rawstore
from .base import ScanStats, TracePoint, load_trace, parse_readsb_trace, trace_meta

SAMPLE_URL = "https://samples.adsbexchange.com/traces/{d:%Y}/{d:%m}/{d:%d}/{h2}/trace_full_{hex}.json"


class AdsbxSampleAdapter:
    name = "adsbx"

    def __init__(self):
        self.stats = ScanStats()

    def scan_day(self, day: date, candidates: set[str], bbox: BBox) -> Iterator[str]:
        if day.day != 1:
            raise ValueError("ADS-B Exchange samples exist only for the first day of each month")
        self.stats = st = ScanStats(release_used=f"adsbx-sample-{day:%Y-%m-%d}")
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
            packed = gzip.compress(data)
            rawstore.put(self.name, day, hexid, packed)
            st.candidates_kept += 1
            st.bytes_retained += len(packed)
            st.kept_hexes.append(hexid)
            yield hexid

    def parse(self, data: bytes) -> Iterator[TracePoint]:
        return parse_readsb_trace(load_trace(data), self.name)

    def meta(self, data: bytes) -> dict:
        return trace_meta(data)
