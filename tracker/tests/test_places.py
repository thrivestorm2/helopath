from datetime import timedelta
from urllib.parse import parse_qs, urlparse

from django.contrib.auth.models import Group, User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from tracker import places
from tracker.management.commands.run_worker import tick
from tracker.models import LandingCluster, PlaceSuggestion, Site

LAT, LON = 40.69160, -89.59310

# Shape of a real Overpass "out center tags" reply: nodes carry lat/lon, ways/relations a center.
REPLY = {"elements": [
    {"type": "way", "id": 111, "center": {"lat": 40.6925, "lon": -89.5935},
     "tags": {"amenity": "hospital", "name": "OSF Saint Francis Medical Center"}},
    {"type": "node", "id": 222, "lat": 40.6917, "lon": -89.5932, "tags": {"aeroway": "helipad", "name": "OSF Helipad"}},
    {"type": "node", "id": 333, "lat": 40.6918, "lon": -89.5933, "tags": {"aeroway": "helipad"}},  # unnamed: skipped
    {"type": "way", "id": 444, "center": {"lat": 40.6950, "lon": -89.5900},
     "tags": {"healthcare": "hospital", "name": "Children's Hospital of Illinois"}},
]}


def fake(reply=REPLY, calls=None):
    def fetch(query):
        if calls is not None:
            calls.append(query)
        return reply
    return fetch


def cluster(n, lat=LAT, lon=LON):
    now = timezone.now()
    return LandingCluster.objects.create(source="adsblol", lat=lat, lon=lon, landing_count=n, distinct_aircraft=2,
                                         first_seen=now, last_seen=now)


class PlaceLookupTests(TestCase):
    def test_rank_prefers_named_hospitals_nearest_first(self):
        ranked = places.rank(REPLY["elements"], LAT, LON)
        self.assertEqual([c["name"] for c in ranked],
                         ["OSF Saint Francis Medical Center", "Children's Hospital of Illinois", "OSF Helipad"])
        self.assertEqual(ranked[0]["osm_url"], "https://www.openstreetmap.org/way/111")
        self.assertLess(ranked[0]["distance_m"], 150)

    def test_query_covers_hospitals_and_helipads_within_radius(self):
        q = places.overpass_query(LAT, LON, 500)
        for part in ["[amenity=hospital]", "[healthcare=hospital]", "[aeroway=helipad]", "around:500,40.691600"]:
            self.assertIn(part, q)

    def test_only_busy_clusters_are_looked_up_and_answers_are_cached(self):
        busy, quiet, exactly = cluster(25), cluster(8, lat=41.0), cluster(20, lat=41.5)
        calls = []
        self.assertEqual(places.suggest_pending(fetch=fake(calls=calls)), 1)
        self.assertEqual(len(calls), 1)  # only the cluster with MORE than 20 landings
        s = places.cached(busy.lat, busy.lon)
        self.assertEqual((s.name, s.kind), ("OSF Saint Francis Medical Center", "hospital"))
        # Rebuilt clusters at (nearly) the same spot reuse the cached answer.
        busy.delete()
        cluster(30, lat=LAT + 0.0002, lon=LON - 0.0002)
        self.assertEqual(places.suggest_pending(fetch=fake(calls=calls)), 0)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(places.cached(quiet.lat, quiet.lon))
        self.assertIsNone(places.cached(exactly.lat, exactly.lon))

    def test_failures_are_recorded_and_retried_after_an_hour(self):
        cluster(25)

        def broken(query):
            raise OSError("network down")
        places.suggest_pending(fetch=broken)
        s = places.cached(LAT, LON)
        self.assertIn("network down", s.error)
        self.assertEqual(places.suggest_pending(fetch=fake()), 0)  # not hammered straight away
        PlaceSuggestion.objects.update(looked_up_at=timezone.now() - timedelta(hours=2))
        self.assertEqual(places.suggest_pending(fetch=fake()), 1)
        self.assertEqual(places.cached(LAT, LON).error, "")

    @override_settings(PLACE_LOOKUPS_PER_TICK=2)
    def test_lookups_per_tick_are_limited(self):
        for i in range(5):
            cluster(25, lat=40 + i)
        self.assertEqual(places.suggest_pending(fetch=fake()), 2)

    def test_nothing_named_nearby(self):
        cluster(25)
        places.suggest_pending(fetch=fake({"elements": []}))
        s = places.cached(LAT, LON)
        self.assertEqual((s.name, s.error), ("", ""))

    def test_worker_tick_does_lookups_in_spare_time(self):
        from unittest import mock
        cluster(25)
        with mock.patch("tracker.places.fetch_overpass", side_effect=fake()):
            self.assertEqual(tick(log=lambda *_: None), "idle (place lookups: 1)")


class ClusterQueueSuggestionTests(TestCase):
    def setUp(self):
        admin = User.objects.create_user("boss", password="x", is_staff=True)
        admin.groups.add(Group.objects.get(name="Admin"))
        self.client.force_login(admin)

    def test_suggestion_shown_and_prefilled_into_create_site(self):
        cluster(25)
        places.suggest_pending(fetch=fake())
        r = self.client.get(reverse("cluster_queue"))
        self.assertContains(r, "<strong>OSF Saint Francis Medical Center</strong>")
        self.assertContains(r, "2 other nearby")
        self.assertContains(r, "OpenStreetMap contributors")
        url = r.context["clusters"][0].create_url
        q = parse_qs(urlparse(url).query)
        self.assertEqual(q["name"], ["OSF Saint Francis Medical Center"])
        self.assertEqual(q["site_type"], [Site.HOSPITAL])
        self.assertEqual(q["origin"], ["from_cluster"])
        # The admin add form picks the values up as initial data.
        form = self.client.get(url)
        self.assertContains(form, 'value="OSF Saint Francis Medical Center"')

    def test_pending_and_small_clusters(self):
        cluster(25)
        cluster(5, lat=41.0)
        r = self.client.get(reverse("cluster_queue"))
        self.assertContains(r, "Looking up nearby hospitals")
        small = [c for c in r.context["clusters"] if c.landing_count == 5][0]
        q = parse_qs(urlparse(small.create_url).query)
        self.assertEqual((q["site_type"], q["name"]), ([Site.BASE], ["Cluster 41.0000,-89.5931"]))
