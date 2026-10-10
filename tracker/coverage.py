"""ADS-B coverage inferred from the tracks we received: which grid cells positions came from, and how often.

Heavy coverage here means many legs were received in a cell; an empty cell may simply have had no helicopter
fly through it, so "no colour" means "no evidence of coverage", not "no coverage"."""

import math
from collections import Counter
from datetime import timedelta
from statistics import median

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import CoverageCell, Leg


def cell_of(lat, lon, size=None):
    size = size or settings.COVERAGE_CELL_DEG
    return math.floor(lat / size), math.floor(lon / size)


def cells_for_track(track, size=None):
    """{cell: lowest altitude or None} for one simplified track ([lat, lon, alt_ft or None, unix ts] points).
    Consecutive points are joined (so the cells between them count) unless the time between them shows a
    reception gap."""
    size = size or settings.COVERAGE_CELL_DEG
    cells = {}

    def mark(lat, lon, alt=None):
        c = cell_of(lat, lon, size)
        if c not in cells:
            cells[c] = alt
        elif alt is not None and (cells[c] is None or alt < cells[c]):
            cells[c] = alt

    if not track:
        return cells
    deltas = [b[3] - a[3] for a, b in zip(track, track[1:]) if b[3] > a[3]]
    max_gap = max(settings.COVERAGE_GAP_S, 2.5 * median(deltas)) if deltas else settings.COVERAGE_GAP_S
    for a, b in zip(track, track[1:]):
        mark(a[0], a[1], a[2])
        if 0 <= b[3] - a[3] <= max_gap:
            steps = math.ceil(max(abs(b[0] - a[0]), abs(b[1] - a[1])) / (size / 2))
            for i in range(1, steps):
                t = i / steps
                mark(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
    last = track[-1]
    mark(last[0], last[1], last[2])
    return cells


def build_coverage(source, days=None):
    """Replace the source's CoverageCell rows from its legs of the last `days` days. Returns the cell count."""
    days = days or settings.COVERAGE_DAYS
    legs = Counter()
    lowest = {}
    tracks = (Leg.objects.filter(source=source, start_ts__gte=timezone.now() - timedelta(days=days))
              .values_list("simplified_track", flat=True))
    for track in tracks.iterator(chunk_size=2000):
        for c, alt in cells_for_track(track).items():
            legs[c] += 1
            if alt is not None and (c not in lowest or alt < lowest[c]):
                lowest[c] = alt
    with transaction.atomic():
        CoverageCell.objects.filter(source=source).delete()
        CoverageCell.objects.bulk_create(
            [CoverageCell(source=source, lat_i=c[0], lon_i=c[1], legs=n, lowest_ft=lowest.get(c)) for c, n in legs.items()],
            batch_size=2000)
    return len(legs)
