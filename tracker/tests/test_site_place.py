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


NAMED = {"elements": [
    {"type": "node", "id": 1, "lat": 40.0005, "lon": -89.0, "tags": {"name": "Fire Station 3", "amenity": "fire_station"}},
    {"type": "way", "id": 2, "center": {"lat": 40.002, "lon": -89.0}, "tags": {"name": "OSF Saint Francis", "amenity": "hospital"}},
    {"type": "node", "id": 3, "lat": 40.001, "lon": -89.0, "tags": {"name": "OSF Helipad", "aeroway": "helipad"}},
    {"type": "node", "id": 4, "lat": 40.0001, "lon": -89.0, "tags": {"name": "Peoria Airport", "aeroway": "aerodrome"}},
    {"type": "node", "id": 5, "lat": 40.0, "lon": -89.0, "tags": {"amenity": "hospital"}},  # unnamed
    *AREAS["elements"]]}


class SiteNameTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("boss", password="x", is_staff=True)
        self.admin.groups.add(Group.objects.get(name="Admin"))
        self.client.force_login(self.admin)

    def test_three_names_hospital_first(self):
        names, place = places.site_name_options(40.0, -89.0, fetch=lambda q: NAMED)
        self.assertEqual([(n["name"], n["kind"], n["site_type"]) for n in names], [
            ("OSF Saint Francis", "hospital", "hospital_helipad"), ("OSF Helipad", "helipad", "hospital_helipad"),
            ("Fire Station 3", "fire station", "base")])
        self.assertEqual(place, ("Peoria", "IL"))
        self.assertEqual(names[0]["city"], "Peoria")

    def test_endpoint_with_names_and_cache_fallback(self):
        url = reverse("site_place")
        with mock.patch.object(places, "fetch_overpass", side_effect=lambda q: NAMED):
            d = self.client.get(url, {"lat": 40, "lon": -89, "names": 1}).json()
        self.assertEqual(len(d["names"]), 3)
        self.assertEqual((d["city"], d["state"]), ("Peoria", "IL"))
        PlaceSuggestion.objects.create(lat3="40.000", lon3="-89.000", name="Cached Hospital", city="Pekin",
                                       state="IL", candidates=[{"name": "Cached Hospital", "kind": "hospital",
                                                                "distance_m": 40}])
        with mock.patch.object(places, "fetch_overpass", side_effect=broken_fetch):
            d = self.client.get(url, {"lat": 40, "lon": -89, "names": 1}).json()
            self.assertEqual([n["name"] for n in d["names"]], ["Cached Hospital"])
            self.assertEqual(d["names"][0]["site_type"], "hospital_helipad")
            self.assertEqual(self.client.get(url, {"lat": 41, "lon": -90, "names": 1}).json()["error"], "lookup failed")

    def test_add_form_offers_names_edit_form_does_not(self):
        self.client.force_login(User.objects.create_superuser("root", password="x"))
        self.assertContains(self.client.get(reverse("admin:tracker_site_add")), "const adding = true;")
        site = Site.objects.create(name="X", site_type=Site.BASE, lat=40, lon=-89)
        self.assertContains(self.client.get(reverse("admin:tracker_site_change", args=[site.pk])), "const adding = false;")


class CreateFromQueueTests(TestCase):
    def test_saving_returns_to_queue_and_removes_the_spot(self):
        now = timezone.now()
        root = User.objects.create_superuser("root", password="x")
        self.client.force_login(root)
        spot = LandingCluster.objects.create(source="adsblol", lat=40.2, lon=-88.3, landing_count=9,
                                             distinct_aircraft=2, first_seen=now, last_seen=now)
        other = LandingCluster.objects.create(source="adsblol", lat=40.3, lon=-88.3, landing_count=5,
                                              distinct_aircraft=1, first_seen=now, last_seen=now)
        r = self.client.get(reverse("cluster_queue"))
        create = r.context["clusters"][0].create_url
        self.assertIn("next=%2Fclusters%2F", create)
        form = self.client.get(create)
        data = {k: v for k, v in form.context["adminform"].form.initial.items() if v is not None}
        data.update({"name": "New Base", "city": "Rantoul", "state": "IL", "elevation_ft": 0, "is_active": "on",
                     "_save": "Save"})
        r = self.client.post(create, data, follow=True)
        self.assertRedirects(r, reverse("cluster_queue"))
        self.assertContains(r, "New Base replaces 1 landing spot in the Cluster queue.")
        self.assertFalse(LandingCluster.objects.filter(pk=spot.pk).exists())
        self.assertEqual([c.pk for c in r.context["clusters"]], [other.pk])
