import gzip
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator, Optional, Protocol

from django.conf import settings

from ..geo import BBox


@dataclass(slots=True)
class TracePoint:
    hex: str
    ts_utc: datetime
    lat: float
    lon: float
    alt_baro_ft: Optional[float]
    alt_geom_ft: Optional[float]
    on_ground: bool
    gs_kt: Optional[float]
    track_deg: Optional[float]
    vrate_fpm: Optional[float]
    source: str
    # Additions to the spec's normalized list, needed by segmentation and hints.
    new_leg: bool = False      # readsb flag bit 2
    stale: bool = False        # readsb flag bit 1
    callsign: Optional[str] = None

    @property
    def alt_ft(self):
        """Best available altitude (geometric preferred: closer to MSL than pressure altitude)."""
        return self.alt_geom_ft if self.alt_geom_ft is not None else self.alt_baro_ft


@dataclass
class ScanStats:
    release_used: str = ""
    members_scanned: int = 0
    candidates_kept: int = 0
    bytes_streamed: int = 0
    bytes_retained: int = 0
    kept_hexes: list = field(default_factory=list)


class SourceAdapter(Protocol):
    name: str

    def scan_day(self, day: date, candidates: set[str], bbox: BBox) -> Iterator[Path]: ...

    def parse(self, path: Path) -> Iterator[TracePoint]: ...


def raw_root(source: str) -> Path:
    return Path(settings.DATA_DIR) / "raw" / source


def raw_day_dir(source: str, day: date) -> Path:
    return raw_root(source) / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def parse_readsb_trace(doc: dict, source: str) -> Iterator[TracePoint]:
    """Parse a readsb 'trace json' document (README-json.md, 'trace jsons').

    Index 3 is mixed-type: a number (baro ft), the string "ground", or null.
    """
    hexid = doc["icao"].lower()
    t0 = float(doc["timestamp"])
    for p in doc.get("trace", ()):
        if len(p) < 7 or p[1] is None or p[2] is None:
            continue
        alt = p[3]
        on_ground = alt == "ground"
        flags = p[6] or 0
        detail = p[8] if len(p) > 8 and isinstance(p[8], dict) else None
        callsign = (detail.get("flight") or "").strip() or None if detail else None
        yield TracePoint(
            hex=hexid,
            ts_utc=datetime.fromtimestamp(t0 + float(p[0]), tz=timezone.utc),
            lat=float(p[1]),
            lon=float(p[2]),
            alt_baro_ft=None if on_ground else _num(alt),
            alt_geom_ft=_num(p[10]) if len(p) > 10 else None,
            on_ground=on_ground,
            gs_kt=_num(p[4]),
            track_deg=_num(p[5]),
            vrate_fpm=_num(p[7]) if len(p) > 7 else None,
            source=source,
            new_leg=bool(flags & 2),
            stale=bool(flags & 1),
            callsign=callsign,
        )


def load_trace_file(path: Path) -> dict:
    with gzip.open(path, "rb") as f:
        return json.load(f)
