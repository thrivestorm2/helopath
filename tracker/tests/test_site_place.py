from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from tracker import places
from tracker.models import Aircraft, LandingCluster, Leg, PlaceSuggestion, Site

AREAS = {"elements": [{"type": "area", "tags": {"admin_level": "8", "name": "Peoria"}},
                      {"type": "area", "tags": {"admin_level": "4", "name": "Illinois", "ISO3166-2": "US-IL"}}]}


def fake_fetch(query):
    assert "is_in(" in query and "amenity=hospital" not in query  # the light city/state query only
    return AREAS


def broken_fetch(query):
    raise OSError("overpass down")


class PlaceForPointTests(TestCase):
    def setUp(self):
        places._site_tries.clear()

    def test_from_cache_then_from_osm(self):
        PlaceSuggestion.objects.create(lat3="40.700", lon3="-89.600", name="X", city="Pekin", state="IL")
        self.assertEqual(places.place_for_point(40.7001, -89.6001, fetch=broken_fetch), ("Pekin", "IL"))
        self.assertEqual(places.place_for_point(41.0, -90.0, fetch=fake_fetch), ("Peoria", "IL"))
        with self.assertRaises(OSError):
            places.place_for_point(41.0, -90.0, fetch=broken_fetch)

    def test_worker_fills_only_blank_fields_once_a_day(self):
        blank = Site.objects.create(name="New pad", site_type=Site.HOSPITAL, lat=41, lon=-90)
        half = Site.objects.create(name="Typed", site_type=Site.BASE, lat=41, lon=-90, city="Bartonville")
        done = Site.objects.create(name="Done", site_type=Site.BASE, lat=41, lon=-90, city="A", state="IA")
        fetch = mock.Mock(side_effect=fake_fetch)
        self.assertEqual(places.fill_site_places(10, fetch=fetch), 2)
        for s in (blank, half, done):
            s.refresh_from_db()
        self.assertEqual((blank.city, blank.state), ("Peoria", "IL"))
        self.assertEqual((half.city, half.state), ("Bartonville", "IL"))  # typed city kept
        self.assertEqual((done.city, done.state), ("A", "IA"))
        # A site with no city found is not looked up again until tomorrow.
        Site.objects.filter(pk=blank.pk).update(city="")
        self.assertEqual(places.fill_site_places(10, fetch=fetch), 0)
        tomorrow = timezone.now() + timedelta(days=1, minutes=1)
        self.assertEqual(places.fill_site_places(10, fetch=fetch, now=tomorrow), 1)
        self.assertEqual(places.fill_site_places(0, fetch=fetch, now=tomorrow + timedelta(days=2)), 0)

    def test_failures_do_not_stop_the_worker(self):
        s = Site.objects.create(name="New pad", site_type=Site.HOSPITAL, lat=41, lon=-90)
        self.assertEqual(places.fill_site_places(10, fetch=broken_fetch), 1)
        s.refresh_from_db()
        self.assertEqual(s.city, "")


class SitePlaceViewTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("boss", password="x", is_staff=True)
        self.admin.groups.add(Group.objects.get(name="Admin"))
        self.url = reverse("site_place")

    def test_admin_lookup(self):
        self.client.force_login(self.admin)
        with mock.patch.object(places, "fetch_overpass", side_effect=fake_fetch):
            self.assertEqual(self.client.get(self.url, {"lat": 41, "lon": -90}).json(),
                             {"city": "Peoria", "state": "IL", "error": ""})
        with mock.patch.object(places, "fetch_overpass", side_effect=broken_fetch):
            self.assertEqual(self.client.get(self.url, {"lat": 41, "lon": -90}).json()["error"], "lookup failed")
        self.assertEqual(self.client.get(self.url, {"lat": "x"}).status_code, 400)

    def test_viewers_cannot(self):
        viewer = User.objects.create_user("v", password="x")
        viewer.groups.add(Group.objects.get(name="Viewer"))
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(self.url, {"lat": 41, "lon": -90}).status_code, 403)

    def test_site_form_has_the_lookup(self):
        self.client.force_login(User.objects.create_superuser("root", password="x"))
        r = self.client.get(reverse("admin:tracker_site_add") + "?lat=41&lon=-90")
        self.assertContains(r, reverse("site_place"))


class ClusterQueueOrderTests(TestCase):
    def test_most_landings_first_with_recent_count(self):
        now = timezone.now()
        admin = User.objects.create_user("boss", password="x", is_staff=True)
        admin.groups.add(Group.objects.get(name="Admin"))
        self.client.force_login(admin)
        for i, n in enumerate([4, 30, 12]):
            LandingCluster.objects.create(source="adsblol", lat=40 + i, lon=-89, landing_count=n, distinct_aircraft=1,
                                          first_seen=now, last_seen=now)
        ac = Aircraft.objects.create(tail_number="N1", icao_hex="b00001", status=Aircraft.CONFIRMED)
        for days in (1, 2, 200):
            ts = now - timedelta(days=days)
            Leg.objects.create(hex="b00001", aircraft=ac, source="adsblol", start_ts=ts, end_ts=ts, start_lat=42,
                               start_lon=-89, end_lat=42, end_lon=-89, end_state=Leg.LANDED)
        r = self.client.get(reverse("cluster_queue"))
        self.assertEqual([(c.landing_count, c.recent_landings) for c in r.context["clusters"]],
                         [(30, 0), (12, 2), (4, 0)])
        self.assertContains(r, "Last 90 days")
