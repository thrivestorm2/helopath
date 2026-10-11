from datetime import timedelta

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from tracker.models import Aircraft, Leg, Site
from tracker.quality import Quality, site_quality

NOW = timezone.now() - timedelta(days=1)


class QualityTests(TestCase):
    def setUp(self):
        self.h = Site.objects.create(name="Hospital A", site_type=Site.HOSPITAL, lat=40.0, lon=-89.0)
        self.b = Site.objects.create(name="Base B", site_type=Site.BASE, lat=41.0, lon=-89.0)
        self.apt = Site.objects.create(name="Airport C", site_type=Site.AIRPORT, lat=42.0, lon=-89.0)
        self.a = Aircraft.objects.create(tail_number="N1", icao_hex="a00001", status=Aircraft.CONFIRMED)
        self.d = Aircraft.objects.create(tail_number="N2", icao_hex="a00002", status=Aircraft.DISCOVERED)

    def leg(self, end=None, state=Leg.LANDED, start=None, on_ground=False, lat=40.0, lon=-89.0, days=1, ac=None):
        ts = timezone.now() - timedelta(days=days)
        Leg.objects.create(hex="a00001", aircraft=ac or self.a, source="adsblol", start_ts=ts, end_ts=ts,
                           start_lat=lat, start_lon=lon, end_lat=lat, end_lon=lon, end_state=state, end_site=end,
                           start_site=start, start_on_ground=on_ground)

    def test_score_maths(self):
        for _ in range(4):
            self.leg(end=self.h)                                  # 4 landings seen: 1 each
        self.leg(end=self.h, state=Leg.INFERRED)                  # 1/2
        self.leg(start=self.h, on_ground=True, end=None, state=Leg.LOST, lat=45, lon=-80)   # departure seen: 1
        self.leg(start=self.h, on_ground=False, lat=45, lon=-80, state=Leg.LOST)            # first seen airborne: 0
        self.leg(state=Leg.LOST, lat=40.02, lon=-89.0)            # lost ~2.2 km away: 0
        self.leg(state=Leg.LOST, lat=40.05, lon=-89.0)            # ~5.6 km away: not counted
        self.leg(end=self.h, days=120)                            # outside the 90-day window
        q = site_quality([self.h], Aircraft.VISIBLE)[self.h.pk]
        self.assertEqual(dict(q.counts), {"observed": 4, "inferred": 1, "dep_ground": 1, "dep_air": 1, "lost": 1})
        self.assertEqual(q.events, 8)
        self.assertEqual(q.score, round(100 * 5.5 / 8))  # 69
        self.assertIn("hsl(83,", q.color)

    def test_not_enough_data_and_colours(self):
        self.leg(end=self.b)
        q = site_quality([self.b], Aircraft.VISIBLE)[self.b.pk]
        self.assertEqual((q.score, q.label, q.color), (None, "not enough data", "#8a94a6"))
        best, worst = Quality(), Quality()
        best.counts["observed"], worst.counts["lost"] = 10, 10
        self.assertEqual((best.score, best.color), (100, "hsl(120, 70%, 38%)"))
        self.assertEqual((worst.score, worst.color), (0, "hsl(0, 70%, 38%)"))

    def test_lost_signal_counts_for_nearest_site_only(self):
        Site.objects.create(name="Clinic", site_type=Site.HOSPITAL, lat=40.015, lon=-89.0)
        self.leg(state=Leg.LOST, lat=40.012, lon=-89.0)  # 1.3 km from Hospital A, 0.3 km from the clinic
        self.assertEqual(site_quality([self.h], Aircraft.VISIBLE)[self.h.pk].counts["lost"], 0)

    def test_badge_on_every_site_page_list_column_and_map(self):
        viewer = User.objects.create_user("v", password="x")
        viewer.groups.add(Group.objects.get(name="Viewer"))
        self.client.force_login(viewer)
        for _ in range(5):
            self.leg(end=self.h)
            self.leg(end=self.apt, state=Leg.LOST, lat=42.0)
        for site in (self.h, self.b, self.apt):
            r = self.client.get(reverse("site_detail", args=[site.pk]))
            self.assertContains(r, 'class="quality-badge"')
        self.assertContains(self.client.get(reverse("site_detail", args=[self.h.pk])), "ADS-B quality 100")
        self.assertContains(self.client.get(reverse("site_detail", args=[self.b.pk])), "ADS-B quality not enough data")
        r = self.client.get(reverse("site_list"), {"sort": "quality"})
        self.assertEqual([x.name for x in r.context["page"]], ["Airport C", "Hospital A", "Base B"])  # worst first
        r = self.client.get(reverse("site_list"), {"sort": "-quality"})
        self.assertEqual([x.name for x in r.context["page"]], ["Hospital A", "Airport C", "Base B"])  # no data last
        self.assertContains(self.client.get(reverse("site_list")), "ADS-B quality</a>")
        r = self.client.get(reverse("site_map"))
        hosp = next(m for m in r.context["markers"] if m["name"] == "Hospital A")
        self.assertIn("ADS-B quality: 100 (5 events, 90 days)", hosp["info"])
        # The confirmed-only view leaves out discovered aircraft.
        self.leg(end=self.h, state=Leg.LOST, ac=self.d)
        self.assertEqual(site_quality([self.h], (Aircraft.CONFIRMED,))[self.h.pk].events, 5)


class SiteHeaderTests(TestCase):
    def test_faa_id_in_parentheses_after_the_name(self):
        viewer = User.objects.create_user("v", password="x")
        viewer.groups.add(Group.objects.get(name="Viewer"))
        self.client.force_login(viewer)
        with_id = Site.objects.create(name="OSF Saint Francis", site_type=Site.HOSPITAL, faa_id="IL01", lat=40, lon=-89)
        without = Site.objects.create(name="Field Pad", site_type=Site.OTHER, lat=40, lon=-89)
        r = self.client.get(reverse("site_detail", args=[with_id.pk]))
        self.assertContains(r, '<h1>OSF Saint Francis <span class="muted faa-id">(IL01)</span></h1>', html=False)
        self.assertNotContains(r, "· FAA IL01")  # not repeated in the details line
        r = self.client.get(reverse("site_detail", args=[without.pk]))
        self.assertContains(r, "<h1>Field Pad</h1>")
