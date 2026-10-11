import io

from django.contrib.auth.models import Group, User
from django.test import TestCase
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
