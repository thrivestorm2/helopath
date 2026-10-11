import io

from django.contrib.auth.models import Group, User
from django.test import TestCase, override_settings
from django.urls import reverse

from tracker.models import FaaFacility, Site
from tracker.seed import seed_sites

CSV = ("SITE_TYPE_CODE,STATE_CODE,ARPT_ID,CITY,ARPT_NAME,FACILITY_USE_CODE,LAT_DECIMAL,LONG_DECIMAL,ELEV\n"
       "A,IL,PIA,PEORIA,GENERAL WAYNE A DOWNING PEORIA INTL,PU,40.6642,-89.6933,660\n"
       "H,WI,WI99,MENOMONEE FALLS,FROEDTERT MENOMONEE FALLS HOSPITAL,PR,43.0902,-88.1785,860\n"
       "H,IL,IL01,PEORIA,OSF SAINT FRANCIS MEDICAL CENTER,PR,40.6981,-89.5934,700\n")


class FaaLookupTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("boss", password="x", is_staff=True)
        self.admin.groups.add(Group.objects.get(name="Admin"))
        self.client.force_login(self.admin)
        self.url = reverse("faa_facility")

    def test_not_loaded_yet(self):
        self.assertEqual(self.client.get(self.url, {"id": "PIA"}).json(), {
            "found": False, "message": "FAA data is not loaded yet: run Seed FAA sites on the Operations page."})

    def test_seeding_keeps_every_facility_and_lookup_fills_the_form(self):
        seed_sites(io.BytesIO(CSV.encode()))
        self.assertEqual(FaaFacility.objects.count(), 3)  # nationwide, though Wisconsin is outside the box
        self.assertFalse(Site.objects.filter(faa_id="WI99").exists())
        d = self.client.get(self.url, {"id": " wi99 "}).json()
        self.assertEqual({k: d[k] for k in ("found", "faa_id", "name", "site_type", "city", "state")}, {
            "found": True, "faa_id": "WI99", "name": "Froedtert Menomonee Falls Hospital",
            "site_type": Site.HOSPITAL, "city": "Menomonee Falls", "state": "WI"})
        self.assertEqual((d["lat"], d["lon"], d["elevation_ft"]), (43.0902, -88.1785, 860))
        airport = self.client.get(self.url, {"id": "KPIA"}).json()  # ICAO form
        self.assertEqual((airport["faa_id"], airport["site_type"], airport["radius_m"]), ("PIA", Site.AIRPORT, 1500))
        self.assertEqual(self.client.get(self.url, {"id": "ZZZ9"}).json(),
                         {"found": False, "message": "No FAA facility with that ID."})
        # Seeding again replaces the table rather than adding to it.
        seed_sites(io.BytesIO(CSV.encode()))
        self.assertEqual(FaaFacility.objects.count(), 3)

    def test_viewers_cannot_and_form_wires_it(self):
        viewer = User.objects.create_user("v", password="x")
        viewer.groups.add(Group.objects.get(name="Viewer"))
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(self.url, {"id": "PIA"}).status_code, 403)
        self.client.force_login(User.objects.create_superuser("root", password="x"))
        self.assertContains(self.client.get(reverse("admin:tracker_site_add")), reverse("faa_facility"))


class ClusterFaaLinkTests(TestCase):
    def setUp(self):
        from django.utils import timezone
        from tracker.models import LandingCluster
        now = timezone.now()
        root = User.objects.create_superuser("root", password="x")
        self.client.force_login(root)
        FaaFacility.objects.create(faa_id="WI99", name="Froedtert Menomonee Falls Hospital",
                                   site_type=Site.HOSPITAL, city="Menomonee Falls", state="WI",
                                   lat=43.0910, lon=-88.1785, elevation_ft=860)  # ~90 m north of the spot
        FaaFacility.objects.create(faa_id="PIA", name="Peoria Intl", site_type=Site.AIRPORT, city="Peoria",
                                   state="IL", lat=40.6642, lon=-89.6933)
        self.pia_site = Site.objects.create(name="Peoria Airport", site_type=Site.AIRPORT, faa_id="PIA",
                                            lat=40.6642, lon=-89.6933, radius_m=100)
        mk = lambda lat, lon: LandingCluster.objects.create(  # noqa: E731
            source="adsblol", lat=lat, lon=lon, landing_count=9, distinct_aircraft=2, first_seen=now, last_seen=now)
        self.near_faa = mk(43.0902, -88.1785)
        self.near_site = mk(40.6654, -89.6933)    # ~130 m from PIA, outside its 100 m radius
        self.far = mk(41.5, -88.0)                # ~ 300 m+ from anything FAA
        FaaFacility.objects.create(faa_id="IL77", name="Far Heliport", site_type=Site.OTHER, city="X", state="IL",
                                   lat=41.5030, lon=-88.0)  # ~330 m away: not linked

    def test_queue_links_spots_to_faa_facilities(self):
        r = self.client.get(reverse("cluster_queue"))
        by_pk = {c.pk: c for c in r.context["clusters"]}
        c = by_pk[self.near_faa.pk]
        self.assertEqual((c.faa.faa_id, c.faa_distance_m), ("WI99", 89))
        for part in ("faa_id=WI99", "name=Froedtert+Menomonee+Falls+Hospital", "site_type=hospital_helipad",
                     "city=Menomonee+Falls", "state=WI", "reviewed=on", "lat=43.090200", "next=%2Fclusters%2F"):
            self.assertIn(part, c.create_url)
        self.assertContains(r, "Create FAA site")
        c = by_pk[self.near_site.pk]
        self.assertEqual(c.faa_site, self.pia_site)
        self.assertTrue(c.create_url.startswith(reverse("admin:tracker_site_change", args=[self.pia_site.pk])))
        self.assertContains(r, "Edit site")
        self.assertIsNone(by_pk[self.far.pk].faa)
        # The pre-filled form saves as a reviewed FAA site.
        form = self.client.get(by_pk[self.near_faa.pk].create_url)
        initial = form.context["adminform"].form.initial
        self.assertEqual((initial["faa_id"], initial["reviewed"]), ("WI99", "on"))
        self.assertContains(form, 'id="id_reviewed" checked')

    @override_settings(SPOT_MAP_MIN_LANDINGS=0)
    def test_map_spot_mentions_faa(self):
        r = self.client.get(reverse("site_map"), {"f": 1, "show": "spot"})
        spots = {m["lat"]: m for m in r.context["markers"] if m["kind"] == "spot"}
        self.assertIn("FAA WI99: Froedtert Menomonee Falls Hospital (89 m)", spots[43.0902]["info"])
