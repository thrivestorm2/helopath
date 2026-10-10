"""Small geometry helpers. Pure Python, no GIS dependency."""
import math
from dataclasses import dataclass

EARTH_R_M = 6371008.8


def haversine_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_M * math.asin(math.sqrt(a))


@dataclass(frozen=True)
class BBox:
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float

    def contains(self, lat, lon):
        return self.lat_min <= lat <= self.lat_max and self.lon_min <= lon <= self.lon_max

    @classmethod
    def from_settings(cls):
        from django.conf import settings

        return cls(settings.BBOX_LAT_MIN, settings.BBOX_LAT_MAX, settings.BBOX_LON_MIN, settings.BBOX_LON_MAX)


class SiteIndex:
    """Grid index over active sites for nearest-site lookups (~0.05 deg cells, about 5 km)."""

    CELL = 0.05

    def __init__(self, sites):
        self.sites = list(sites)
        self.grid = {}
        for s in self.sites:
            self.grid.setdefault(self._key(s.lat, s.lon), []).append(s)

    def _key(self, lat, lon):
        return (math.floor(lat / self.CELL), math.floor(lon / self.CELL))

    def within(self, lat, lon, max_m):
        """[(distance_m, site)] for sites within max_m, nearest first (ties broken by id)."""
        span = int(max_m / 111_000 / self.CELL / max(math.cos(math.radians(lat)), 0.2)) + 1
        ky, kx = self._key(lat, lon)
        out = []
        for dy in range(-span, span + 1):
            for dx in range(-span, span + 1):
                for s in self.grid.get((ky + dy, kx + dx), ()):
                    d = haversine_m(lat, lon, s.lat, s.lon)
                    if d <= max_m:
                        out.append((d, s))
        out.sort(key=lambda t: (t[0], t[1].pk or 0))
        return out

    def containing(self, lat, lon):
        """Nearest site whose own radius contains the point, or None."""
        for d, s in self.within(lat, lon, 5000):
            if d <= s.radius_m:
                return d, s
        return None


def bearing_deg(lat1, lon1, lat2, lon2):
    """Initial great-circle bearing from point 1 to point 2, degrees clockwise from north."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def angle_diff_deg(a, b):
    """Smallest absolute difference between two headings (0-180)."""
    return abs((a - b + 180) % 360 - 180)
