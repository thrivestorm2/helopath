from django.contrib.auth.models import Group, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from tracker import site_import as imp
from tracker.models import FaaFacility, PendingRebuild, Site

HEADER = ",".join(imp.COLUMNS)


def csv_text(*rows):
    return "\n".join([HEADER, *rows]) + "\n"


def row(name, lat="", lon="", street="", city="", state="", site_type="base", faa_id="", notes="", url=""):
    return f'"{name}",Air Evac Lifeteam,,{site_type},"{street}",{city},{state},,{lat},{lon},{faa_id},{url},"{notes}"'


class AnalyseTests(TestCase):
    def setUp(self):
        self.hospital = Site.objects.create(name="Parkland Health Center", site_type=Site.HOSPITAL,
                                            lat=37.7700, lon=-90.4200, radius_m=300)
        self.base = Site.objects.create(name="Air Evac 11 base", site_type=Site.BASE, lat=38.3215, lon=-88.8531,
                                        city="Mount Vernon", state="IL", notes="old note")
        FaaFacility.objects.create(faa_id="IL99", name="Mattoon Heliport", site_type=Site.OTHER, lat=39.4845,
                                   lon=-88.2703, elevation_ft=700)
        FaaFacility.objects.create(faa_id="MVN", name="Mount Vernon Outland", site_type=Site.AIRPORT, lat=38.3233,
                                   lon=-88.8586, elevation_ft=480)

    def analyse(self, *rows, **kw):
        return imp.analyse(imp.read_csv(csv_text(*rows)), **kw)

    def test_new_rows_inside_ticked_outside_not(self):
        inside, outside = self.analyse(row("Air Evac 35 - Marion, IL", 37.7396, -88.9933),
                                       row("Air Evac 54 - Big Spring, TX", 32.2297, -101.5271))
        self.assertEqual((inside.status, inside.ticked, inside.outside), ("new", True, False))
        self.assertEqual((outside.status, outside.ticked, outside.outside), ("new", False, True))
        self.assertEqual((inside.values["site_type"], inside.values["radius_m"]), ("base", 300))
        self.assertIn("Program: Air Evac Lifeteam", inside.note)

    def test_heliport_gives_faa_pad_airport_is_only_noted(self):
        mattoon, = self.analyse(row("Air Evac 145 - Mattoon, IL", 39.48431, -88.27014))
        self.assertEqual((mattoon.faa.faa_id, mattoon.located_by, mattoon.values["lat"], mattoon.values["reviewed"]),
                         ("IL99", "faa", 39.4845, True))
        self.assertEqual(mattoon.values["elevation_ft"], 700)
        marion, = self.analyse(row("Air Evac 99 - by the airport", 38.3300, -88.8700))  # ~1.2 km from MVN
        self.assertIsNone(marion.faa)
        self.assertEqual((marion.status, marion.values["lat"]), ("new", 38.33))  # keeps its own pin
        self.assertIn("at Mount Vernon Outland (MVN)", marion.note)

    def test_update_shows_changes_and_blanks_never_erase(self):
        r, = self.analyse(row("Air Evac Lifeteam 11 – Mount Vernon, IL", 38.3230, -88.8531, notes="base phone"))
        self.assertEqual((r.status, r.existing), ("update", self.base))
        labels = [c[0] for c in r.changes]
        self.assertEqual(labels, ["Name", "Location", "Notes"])  # blank city/state: no change
        imp.apply([r], User.objects.create_superuser("root", password="x"))
        self.base.refresh_from_db()
        self.assertEqual((self.base.name, self.base.city, self.base.state),
                         ("Air Evac Lifeteam 11 – Mount Vernon, IL", "Mount Vernon", "IL"))
        self.assertTrue(self.base.notes.startswith("old note\nImported from CSV"))
        again, = self.analyse(row("Air Evac Lifeteam 11 – Mount Vernon, IL", 38.3230, -88.8531, notes="base phone"))
        self.assertEqual(again.status, "same")

    def test_address_lookup_never_moves_an_existing_site(self):
        r, = self.analyse(row("Air Evac 11 base", street="600 Aviation Drive", city="Mount Vernon", state="IL"),
                          geocoder=lambda r: (38.3230, -88.8560), pause=0)
        self.assertEqual((r.status, r.located_by), ("update", "address"))
        self.assertNotIn("Location", [c[0] for c in r.changes])

    def test_hospital_is_never_made_a_base(self):
        r, = self.analyse(row("Air Evac 8 - Farmington, MO", 37.7701, -90.4201, notes="phone"))
        self.assertEqual((r.status, r.existing, r.ticked), ("covered", self.hospital, False))
        imp.apply([r], User.objects.create_superuser("root", password="x"))
        self.hospital.refresh_from_db()
        self.assertEqual((self.hospital.name, self.hospital.site_type, self.hospital.lat),
                         ("Parkland Health Center", Site.HOSPITAL, 37.77))
        self.assertIn("phone", self.hospital.notes)

    def test_unlocated_and_lookup_limit(self):
        rows = self.analyse(row("No address"), row("Nowhere", street="1 Nope Rd", city="Nope"),
                            geocoder=lambda r: None, pause=0)
        self.assertEqual([(r.status, r.problem) for r in rows],
                         [("unlocated", "No coordinates or address."),
                          ("unlocated", "Address not found on OpenStreetMap.")])
        many = [row(f"Base {i}", street=f"{i} Main St", city="Peoria", state="IL") for i in range(imp.GEOCODE_MAX + 2)]
        calls = []
        rows = self.analyse(*many, geocoder=lambda r: calls.append(r) or (40.69, -89.59), pause=0)
        self.assertEqual(len(calls), imp.GEOCODE_MAX)
        self.assertIn("add its latitude/longitude", rows[-1].problem)

    def test_bad_header(self):
        with self.assertRaises(ValueError):
            imp.read_csv("foo,bar\n1,2\n")


class ImportPageTests(TestCase):
    def setUp(self):
        self.root = User.objects.create_superuser("root", password="x")
        self.keep = Site.objects.create(name="Not in the file", site_type=Site.BASE, lat=40.0, lon=-89.0)

    def upload(self, text):
        return self.client.post(reverse("site_import"), {"file": SimpleUploadedFile("bases.csv", text.encode())})

    def test_preview_then_import_ticked_rows(self):
        self.client.force_login(self.root)
        r = self.upload(csv_text(row("Air Evac 35 - Marion, IL", 37.7396, -88.9933),
                                 row("Air Evac 156 - Madison Co., IL", 38.73492, -89.94675),
                                 row("Air Evac 54 - Big Spring, TX", 32.2297, -101.5271)))
        self.assertContains(r, ">Import ticked rows</button>")
        self.assertContains(r, "2 new")
        self.assertContains(r, "1 outside the study area")
        self.assertEqual(Site.objects.count(), 1)  # nothing saved yet
        r = self.client.post(reverse("site_import"), {"action": "import", "row": ["2", "3"]}, follow=True)
        self.assertContains(r, "Imported: 2 sites added, 0 updated.")
        self.assertEqual(sorted(Site.objects.filter(origin="csv_import").values_list("name", flat=True)),
                         ["Air Evac 156 - Madison Co., IL", "Air Evac 35 - Marion, IL"])
        self.keep.refresh_from_db()
        self.assertTrue(self.keep.is_active)  # sites missing from the file are untouched
        self.assertEqual(PendingRebuild.objects.filter(kind="partial").count(), 2)
        self.assertEqual(self.client.post(reverse("site_import"), {"action": "import", "row": ["2"]},
                                          follow=True).redirect_chain[-1][0], reverse("site_import"))  # expired

    def test_super_admins_only(self):
        admin = User.objects.create_user("boss", password="x", is_staff=True)
        admin.groups.add(Group.objects.get(name="Admin"))
        viewer = User.objects.create_user("v", password="x")
        viewer.groups.add(Group.objects.get(name="Viewer"))
        for user in (admin, viewer):
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse("site_import")).status_code, 403)
            self.assertNotContains(self.client.get(reverse("site_list")), "Import from CSV")
        self.client.force_login(self.root)
        self.assertContains(self.client.get(reverse("site_list")), "Import from CSV")
