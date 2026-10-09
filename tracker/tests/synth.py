"""Synthetic readsb-format traces for tests (no real aircraft data)."""
import gzip
import io
import json
import math
import tarfile
from datetime import datetime, timezone


def _interp(a, b, f):
    return a + (b - a) * f


def flight(waypoints, start_s, cruise_ft=1500, gs=120, step_s=10, ground_elev=600, land=True, takeoff=True):
    """Trace points for base -> stop -> ... with a landing at each waypoint after the first.

    Returns (points, end_s). Each hop: ground point, climb, cruise, descent, ground point.
    """
    pts = []
    t = start_s
    for (lat1, lon1), (lat2, lon2) in zip(waypoints, waypoints[1:]):
        dist_nm = math.hypot((lat2 - lat1) * 60, (lon2 - lon1) * 60 * math.cos(math.radians(lat1)))
        dur = max(int(dist_nm / gs * 3600), 6 * step_s)
        n = dur // step_s
        if takeoff:
            pts.append([t, lat1, lon1, "ground", 0, 0, 0, None, None, "adsb_icao", None, None, None, None])
            t += 30
        for i in range(1, n + 1):
            f = i / n
            alt = ground_elev + cruise_ft * min(1.0, 4 * min(f, 1 - f) + 0.05)
            flags = 2 if (i == 1) else 0
            vrate = -500 if f > 0.75 else (500 if f < 0.25 else 0)
            detail = {"flight": "LIFE1  "} if i == 1 else None
            pts.append([t, _interp(lat1, lat2, f), _interp(lon1, lon2, f), round(alt), gs if f < 0.95 else 10,
                        90.0, flags, vrate, detail, "adsb_icao", round(alt) + 100, None, None, None])
            t += step_s
        if land:
            pts.append([t, lat2, lon2, "ground", 0, 0, 0, None, None, "adsb_icao", None, None, None, None])
        t += 20 * 60  # 20 min on the ground at each stop (longer than the leg gap rule)
    return pts, t


def trace_doc(hexid, day, points, reg="N123AB", typ="EC45"):
    midnight = datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()
    return {"icao": hexid, "r": reg, "t": typ, "desc": "EUROCOPTER EC-145", "ownOp": "TEST AIR LLC",
            "dbFlags": 0, "timestamp": midnight, "trace": sorted(points, key=lambda p: p[0])}


def build_tar(docs, extra_members=()):
    """A tar archive laid out like an adsb.lol release; returns bytes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in [("./heatmap/00.bin.ttf", b"x" * 1000), *extra_members]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for doc in docs:
            hexid = doc["icao"]
            data = gzip.compress(json.dumps(doc).encode())
            info = tarfile.TarInfo(f"./traces/{hexid[-2:]}/trace_full_{hexid}.json")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def split(data, parts=2):
    size = len(data) // parts + 1
    return [data[i : i + size] for i in range(0, len(data), size)]
