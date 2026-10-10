import math
from datetime import date

from django.test import TestCase

from tracker.geo import SiteIndex, bearing_deg, haversine_m
from tracker.inference.legs import infer_legs
from tracker.models import Leg, Site
from tracker.sources.base import parse_readsb_trace

from . import synth

BASE = (40.00, -89.00)
FAR_HOSPITAL = (40.90, -88.60)   # ~105 km NNE of base: ~28 min at 120 kt
DAY = date(2026, 10, 1)
T0 = 14 * 3600


def line(a, b, t0, gs_kt=120, alt=1800, step=10, land=False, takeoff=False, gs_end=None):
    """Points flying a straight line a -> b at gs_kt, with the true track. Returns (points, end_time)."""
    dist = haversine_m(*a, *b)
    dur = dist / (gs_kt / 1.943844)
    n = max(int(dur // step), 2)
    trk = bearing_deg(*a, *b)
    pts = []
    t = t0
    if takeoff:
        pts.append([t, a[0], a[1], "ground", 0, trk, 0, None, None, "adsb_icao", None, None, None, None])
        t += 20
    for i in range(1, n + 1):
        f = i / n
        gs = gs_end if (gs_end is not None and f > 0.8) else gs_kt
        pts.append([t, a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, alt, gs, round(trk, 1), 0, 0, None,
                    "adsb_icao", alt + 100, None, None, None])
        t += step
    if land:
        pts.append([t, b[0], b[1], "ground", 0, trk, 0, None, None, "adsb_icao", None, None, None, None])
    return pts, t


def point_at(a, b, f):
    return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)


def drop(pts, t_from, t_to):
    """Remove points in a time window (coverage hole)."""
    return [p for p in pts if not (t_from <= p[0] < t_to)]


class StitchTests(TestCase):
    def setUp(self):
        self.base = Site.objects.create(name="Base", site_type=Site.BASE, lat=BASE[0], lon=BASE[1], elevation_ft=600)
        self.hosp = Site.objects.create(name="Far Hospital", site_type=Site.HOSPITAL, lat=FAR_HOSPITAL[0],
                                        lon=FAR_HOSPITAL[1], elevation_ft=600)

    def legs(self, pts):
        doc = synth.trace_doc("a1b2c3", DAY, pts)
        return infer_legs(list(parse_readsb_trace(doc, "adsblol")), SiteIndex(Site.objects.all()))

    def test_coverage_hole_mid_cruise_is_rejoined(self):
        pts, _ = line(BASE, FAR_HOSPITAL, T0, takeoff=True, land=True)
        pts = drop(pts, T0 + 600, T0 + 1200)  # 10 minutes with no data, still on course
        legs = self.legs(pts)
        self.assertEqual(len(legs), 1)
        leg = legs[0]
        self.assertEqual((leg.start_site, leg.end_site, leg.end_state), (self.base, self.hosp, Leg.LANDED))
        self.assertEqual(leg.gaps_bridged, 1)

    def test_two_holes_rejoin_into_one_leg(self):
        # At 90 kt the trip is ~38 min; two 11-minute holes (each long enough to split a leg).
        pts, _ = line(BASE, FAR_HOSPITAL, T0, gs_kt=90, takeoff=True, land=True)
        pts = drop(drop(pts, T0 + 300, T0 + 960), T0 + 1200, T0 + 1860)
        legs = self.legs(pts)
        self.assertEqual([(len(legs), legs[0].gaps_bridged)], [(1, 2)])

    def test_without_stitching_the_hole_would_split_the_flight(self):
        from tracker.inference.legs import segment

        pts, _ = line(BASE, FAR_HOSPITAL, T0, takeoff=True, land=True)
        pts = drop(pts, T0 + 600, T0 + 1200)
        doc = synth.trace_doc("a1b2c3", DAY, pts)
        self.assertEqual(len(segment(parse_readsb_trace(doc, "adsblol"))), 2)

    def test_unseen_stop_and_departure_the_other_way_stays_separate(self):
        scene = point_at(BASE, FAR_HOSPITAL, 0.4)
        out, t = line(BASE, scene, T0, takeoff=True)           # signal lost on arrival at the scene
        back, _ = line(scene, BASE, t + 25 * 60, land=True)    # reappears 25 min later heading home
        legs = self.legs(out + back)
        self.assertEqual(len(legs), 2)
        self.assertEqual(legs[0].gaps_bridged + legs[1].gaps_bridged, 0)

    def test_same_heading_but_too_slow_across_gap_stays_separate(self):
        # Reappears only 2 km further on after 15 minutes: it stopped (or hovered) somewhere unseen.
        mid = point_at(BASE, FAR_HOSPITAL, 0.3)
        out, t = line(BASE, mid, T0, takeoff=True)
        resume = (mid[0] + 0.018 * math.cos(math.radians(20)), mid[1] + 0.018 * math.sin(math.radians(20)))
        on, _ = line(resume, FAR_HOSPITAL, t + 15 * 60, land=True)
        self.assertEqual(len(self.legs(out + on)), 2)

    def test_half_speed_across_gap_stays_separate(self):
        # Covers the gap at ~55% of cruise (like real case a0c85b): it likely slowed, orbited or
        # stopped briefly, so the two pieces stay separate.
        mid = point_at(BASE, FAR_HOSPITAL, 0.2)
        out, t = line(BASE, mid, T0, takeoff=True)
        gap_s = 20 * 60
        cruise_mps = 120 / 1.943844
        frac = 0.55 * cruise_mps * gap_s / haversine_m(*BASE, *FAR_HOSPITAL)
        on, _ = line(point_at(BASE, FAR_HOSPITAL, 0.2 + frac), FAR_HOSPITAL, t + gap_s, land=True)
        self.assertEqual(len(self.legs(out + on)), 2)

    def test_jump_too_far_for_its_speed_stays_separate(self):
        # After an 11-minute gap it reappears ~73 km ahead (~215 kt implied vs 120 flown).
        out, t = line(BASE, point_at(BASE, FAR_HOSPITAL, 0.2), T0, takeoff=True)
        on, _ = line(point_at(BASE, FAR_HOSPITAL, 0.9), FAR_HOSPITAL, t + 660, land=True)
        self.assertEqual(len(self.legs(out + on)), 2)

    def test_slow_at_the_join_stays_separate(self):
        # Slowing to 15 kt before a 12-minute hole: likely a landing below coverage, even though
        # where it reappears would fit cruising speed.
        mid = point_at(BASE, FAR_HOSPITAL, 0.3)
        out, t = line(BASE, mid, T0, takeoff=True, gs_end=15)
        on, _ = line(point_at(BASE, FAR_HOSPITAL, 0.72), FAR_HOSPITAL, t + 720, land=True)
        self.assertEqual(len(self.legs(out + on)), 2)

    def test_gap_longer_than_limit_stays_separate(self):
        pts, _ = line(BASE, FAR_HOSPITAL, T0, gs_kt=60, takeoff=True, land=True)   # slow: ~57 min trip
        pts = drop(pts, T0 + 600, T0 + 600 + 35 * 60)                              # 35 min hole > 30
        self.assertEqual(len(self.legs(pts)), 2)


class StitchPagesTests(TestCase):
    def test_quality_page_and_leg_table_show_rejoined_gaps(self):
        from django.contrib.auth.models import User
        from django.utils import timezone

        from tracker.models import Aircraft

        a = Aircraft.objects.create(tail_number="N1ST", icao_hex="a1b2c3", status=Aircraft.CONFIRMED)
        now = timezone.now()
        Leg.objects.create(hex="a1b2c3", aircraft=a, source="adsblol", start_ts=now, end_ts=now, start_lat=40,
                           start_lon=-89, end_lat=40.5, end_lon=-88.8, end_state=Leg.LOST, gaps_bridged=2)
        self.client.force_login(User.objects.create_superuser("s", password="x"))
        self.assertContains(self.client.get("/quality/"), "Coverage gaps rejoined")
        self.assertContains(self.client.get(f"/aircraft/{a.pk}/"), "2 gaps rejoined")
