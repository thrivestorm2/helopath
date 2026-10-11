from datetime import timedelta
from unittest import mock

from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from tracker.inference import pipeline
from tracker.models import Aircraft, Leg, Site
from tracker.resolution import auto_expand_radii, auto_radius_ok

NOW = timezone.now() - timedelta(days=2)


class AutoRadiusTests(TestCase):
    def setUp(self):
        User.objects.create_superuser("root", password="x")
        self.a = Aircraft.objects.create(tail_number="N1", icao_hex="a00001", status=Aircraft.CONFIRMED)

    def landings(self, lat, lon, dlats):
        for dlat in dlats:
            Leg.objects.create(hex="a00001", aircraft=self.a, source="adsblol", start_ts=NOW, end_ts=NOW,
                               start_lat=40, start_lon=-89, end_lat=lat + dlat, end_lon=lon, end_state=Leg.LANDED)

    def run_it(self):
        with mock.patch("tracker.rawstore.hexes", return_value={"a00001"}), \
                mock.patch.object(pipeline, "build_legs") as rebuild_legs:
            changes = auto_expand_radii("adsblol")
        return changes, rebuild_legs

    def test_widens_within_limits_and_rederives_legs(self):
        hosp = Site.objects.create(name="Hospital A", site_type=Site.HOSPITAL, lat=40.0, lon=-89.0, radius_m=300)
        self.landings(40.0, -89.0, (0.005, 0.006, 0.007))  # ~550-780 m out: suggested 850 m
        changes, rebuild_legs = self.run_it()
        hosp.refresh_from_db()
        self.assertEqual(hosp.radius_m, 850)
        self.assertEqual([(s.pk, old, new, n) for s, old, new, n in changes], [(hosp.pk, 300, 850, 3)])
        self.assertIn("Radius widened automatically from 300 m to 850 m: 3 landings just outside", hosp.notes)
        self.assertTrue(LogEntry.objects.filter(object_id=str(hosp.pk), change_message__contains="widened").exists())
        rebuild_legs.assert_called_once_with("adsblol", {"a00001"})

    def test_hospital_capped_at_1000(self):
        hosp = Site.objects.create(name="Hospital B", site_type=Site.HOSPITAL, lat=40.0, lon=-89.0, radius_m=300)
        self.landings(40.0, -89.0, (0.009, 0.010, 0.011))  # ~1-1.2 km out: suggested 1,300 m
        changes, rebuild_legs = self.run_it()
        hosp.refresh_from_db()
        self.assertEqual((changes, hosp.radius_m), ([], 300))
        rebuild_legs.assert_not_called()

    def test_airport_up_to_3000(self):
        apt = Site.objects.create(name="Airport", site_type=Site.AIRPORT, lat=40.0, lon=-89.0, radius_m=1500)
        self.landings(40.0, -89.0, (0.017, 0.018, 0.019))  # ~1.9-2.1 km out
        self.run_it()
        apt.refresh_from_db()
        self.assertEqual(apt.radius_m, 2200)

    def test_rules(self):
        hosp, apt = Site(site_type=Site.HOSPITAL), Site(site_type=Site.AIRPORT)
        base, other = Site(site_type=Site.BASE), Site(site_type=Site.OTHER)
        self.assertTrue(auto_radius_ok(hosp, 1000, None))
        self.assertFalse(auto_radius_ok(hosp, 1050, None))
        self.assertFalse(auto_radius_ok(base, 1050, None))
        self.assertFalse(auto_radius_ok(other, 1050, None))
        self.assertTrue(auto_radius_ok(apt, 3000, None))
        self.assertFalse(auto_radius_ok(apt, 3050, None))
        self.assertTrue(auto_radius_ok(hosp, 600, 1250))
        self.assertFalse(auto_radius_ok(hosp, 600, 1200))  # must be less than half, not equal

    def test_runs_in_the_nightly_downstream(self):
        with mock.patch("tracker.resolution.auto_expand_radii", return_value=[]) as expand:
            stats = pipeline.build_downstream("adsblol")
        expand.assert_called_once_with("adsblol")
        self.assertEqual(stats["radii_widened"], 0)
