from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from tracker.models import Aircraft, Site

PAGES = ["dashboard", "aircraft_list", "site_list", "missions", "flows", "flows_csv", "quality", "ingest_log",
         "review_queue", "cluster_queue", "queue_rebuild"]


def make_user(name, role):
    u = User.objects.create_user(name, password="pw-123456789", is_staff=role == "Admin")
    u.groups.add(Group.objects.get(name=role))
    return u


class AccessTests(TestCase):
    def setUp(self):
        self.viewer = make_user("viewer", "Viewer")
        self.admin = make_user("boss", "Admin")
        self.site = Site.objects.create(name="Test Hospital", lat=40.1, lon=-89.2)

    def test_unauthenticated_redirects_to_login(self):
        for name in PAGES:
            r = self.client.get(reverse(name))
            self.assertEqual(r.status_code, 302, name)
            self.assertTrue(r["Location"].startswith(reverse("login")), name)
        for path in ["/sites/1/", "/aircraft/1/"]:
            r = self.client.get(path)
            self.assertEqual(r.status_code, 302, path)

    def test_login_page_is_public_and_carries_attribution(self):
        r = self.client.get(reverse("login"))
        self.assertContains(r, "Open Database License (ODbL)")

    def test_viewer_reads_but_cannot_edit(self):
        self.client.force_login(self.viewer)
        for name in ["dashboard", "aircraft_list", "site_list", "missions", "flows", "quality", "ingest_log"]:
            r = self.client.get(reverse(name))
            self.assertEqual(r.status_code, 200, name)
            self.assertContains(r, "Open Database License (ODbL)")
        for name in ["review_queue", "cluster_queue", "queue_rebuild"]:
            self.assertEqual(self.client.get(reverse(name)).status_code, 403, name)
        for url in ["/admin/", "/admin/tracker/site/add/", f"/admin/tracker/site/{self.site.pk}/change/",
                    "/admin/tracker/site/import/", "/admin/tracker/aircraft/"]:
            r = self.client.get(url)
            self.assertEqual(r.status_code, 302, url)
            self.assertIn("/admin/login/", r["Location"])
        r = self.client.post(f"/admin/tracker/site/{self.site.pk}/change/", {"name": "hacked"})
        self.assertEqual(r.status_code, 302)
        self.site.refresh_from_db()
        self.assertEqual(self.site.name, "Test Hospital")

    def test_admin_edit_is_logged(self):
        self.client.force_login(self.admin)
        r = self.client.post(f"/admin/tracker/site/{self.site.pk}/change/", {
            "name": "Renamed Hospital", "site_type": "hospital_helipad", "faa_id": "", "city": "", "state": "IL",
            "lat": "40.1", "lon": "-89.2", "elevation_ft": "600", "radius_m": "300", "origin": "manual",
            "reviewed": "on", "is_active": "on", "notes": ""})
        self.assertEqual(r.status_code, 302, r.content[:2000])
        self.site.refresh_from_db()
        self.assertEqual(self.site.name, "Renamed Hospital")
        entry = LogEntry.objects.get(object_id=str(self.site.pk))
        self.assertEqual(entry.user, self.admin)
        r = self.client.get(r["Location"])
        self.assertContains(r, "Site changes take effect after the next rebuild")

    def test_admin_cannot_delete_sites(self):
        self.client.force_login(self.admin)
        r = self.client.get(f"/admin/tracker/site/{self.site.pk}/delete/")
        self.assertEqual(r.status_code, 403)

    def test_undiscovered_and_rejected_aircraft_are_hidden(self):
        a = Aircraft.objects.create(tail_number="N1", icao_hex="a00001", status=Aircraft.REJECTED)
        self.client.force_login(self.viewer)
        self.assertNotContains(self.client.get(reverse("aircraft_list")), "N1<")
        self.assertEqual(self.client.get(reverse("aircraft_detail", args=[a.pk])).status_code, 404)

    def test_no_track_export_route(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get("/admin/tracker/leg/").status_code, 404)
        r = self.client.get(reverse("flows_csv"))
        self.assertEqual(r["Content-Type"], "text/csv")
        self.assertIn(b"ODbL", r.content)


class HealthCheckTests(TestCase):
    def test_platform_probe_by_container_ip_is_answered(self):
        # App Platform probes the container by IP; host validation must not turn that into a 400.
        with self.settings(ALLOWED_HOSTS=["helopath.ondigitalocean.app"]):
            for host in ["10.244.3.17:8080", "10.244.3.17", "[fd00::1]:8080"]:
                r = self.client.get("/login/", HTTP_HOST=host)
                self.assertEqual((r.status_code, r.content), (200, b"ok"), host)
            r = self.client.get("/healthz", HTTP_HOST="helopath.ondigitalocean.app")
            self.assertEqual(r.content, b"ok")

    def test_real_hosts_still_validated_and_login_still_required(self):
        with self.settings(ALLOWED_HOSTS=["helopath.ondigitalocean.app"]):
            self.assertEqual(self.client.get("/login/", HTTP_HOST="evil.example").status_code, 400)
            r = self.client.get("/", HTTP_HOST="helopath.ondigitalocean.app")
            self.assertEqual(r.status_code, 302)
            self.assertNotEqual(r.content, b"ok")
            self.assertContains(self.client.get("/login/", HTTP_HOST="helopath.ondigitalocean.app"), "Log in")
