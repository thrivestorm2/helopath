import csv
import io

from django.contrib.auth.models import Group, User
from django.urls import reverse

from tracker.models import FaaFacility, Site
from tracker.tests.test_unresolved import UnresolvedData
from tracker.views import UNRESOLVED_CSV_COLUMNS


class UnresolvedCsvTests(UnresolvedData):
    def rows(self, **params):
        r = self.client.get(reverse("unresolved_csv"), params)
        self.assertEqual(r["Content-Type"], "text/csv")
        lines = list(csv.reader(io.StringIO(r.content.decode())))
        self.assertIn("Open Database License", lines[0][0])
        self.assertEqual(lines[1], UNRESOLVED_CSV_COLUMNS)
        return [dict(zip(lines[1], line)) for line in lines[2:]], r

    def test_export_lists_every_unresolved_leg_with_context(self):
        FaaFacility.objects.create(faa_id="IL01", name="Hospital A Heliport", site_type=Site.HOSPITAL, city="X",
                                   state="IL", lat=40.0, lon=-89.0)
        self.client.force_login(self.admin)
        rows, r = self.rows()
        self.assertIn("helopath-unresolved-", r["Content-Disposition"])
        self.assertEqual(len(rows), 8)  # 9 legs, 1 resolved
        self.assertIn("8 listed; 9 legs end inside the study area", r.content.decode().splitlines()[0])
        near = [row for row in rows if row["category"] == "near_site"]
        self.assertEqual(len(near), 3)
        self.assertEqual({(x["nearest_site"], x["nearest_site_radius_m"], x["suggested_radius_m"], x["nearest_faa_id"])
                          for x in near}, {("Hospital A", "300", "850", "IL01")})
        spot = next(row for row in rows if row["category"] == "unmatched_spot")
        self.assertEqual((spot["spot_landings"], spot["aircraft"], spot["aircraft_status"]), ("5", "N1", "confirmed"))
        rows, r = self.rows(cat="lost_high")
        self.assertEqual([x["category"] for x in rows], ["lost_high"])
        self.assertIn("-lost_high.csv", r["Content-Disposition"])

    def test_admins_only_and_page_button(self):
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("unresolved")), ">Export CSV</a>")
        viewer = User.objects.create_user("v", password="x")
        viewer.groups.add(Group.objects.get(name="Viewer"))
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(reverse("unresolved_csv")).status_code, 403)
        self.assertNotContains(self.client.get(reverse("unresolved")), ">Export CSV</a>")
