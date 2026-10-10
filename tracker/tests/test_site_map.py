import json
from datetime import timedelta

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from tracker.models import Aircraft, Leg, Operator, Program, Site


class SiteMapTests(TestCase):
    def setUp(self):
        self.am, self.phi = Operator.objects.create(name="Air Methods"), Operator.objects.create(name="PHI")
        self.lf = Program.objects.create(name="OSF Life Flight", operator=self.am)
        self.kpia = Site.objects.create(name="Airport KPIA", site_type=Site.AIRPORT, lat=40.66, lon=-89.69)
        self.base = Site.objects.create(name="Base One", site_type=Site.BASE, lat=40, lon=-89, city="Peoria",
                                        state="IL", weather_hangar=self.kpia)
        self.hosp_base = Site.objects.create(name="OSF St Francis", site_type=Site.HOSPITAL, lat=40.7, lon=-89.6)
        self.hosp_base.weather_hangar = self.hosp_base
        self.hosp_base.save()
        self.carle = Site.objects.create(name="Carle", site_type=Site.HOSPITAL, lat=40.1, lon=-88.2)
        self.empty_base = Site.objects.create(name="Base Empty", site_type=Site.BASE, lat=41, lon=-90)
        self.a = Aircraft.objects.create(tail_number="N911LF", icao_hex="a00001", status=Aircraft.CONFIRMED,
                                         base=self.base, operator=self.am, program=self.lf, call_sign="LIFE 1")
        self.b = Aircraft.objects.create(tail_number="N2PH", icao_hex="a00002", status=Aircraft.CONFIRMED,
                                         base=self.hosp_base, operator=self.phi)
        now = timezone.now() - timedelta(days=2)
        Leg.objects.create(hex="a00001", aircraft=self.a, source="adsblol", start_ts=now, end_ts=now, start_lat=40,
                           start_lon=-89, end_lat=40.1, end_lon=-88.2, end_state="landed_observed", end_site=self.carle)
        viewer = User.objects.create_user("v", password="x")
        viewer.groups.add(Group.objects.get(name="Viewer"))
        self.client.force_login(viewer)

    def markers(self, **params):
        r = self.client.get(reverse("site_map"), params)
        self.assertEqual(r.status_code, 200)
        return {m["name"]: m for m in r.context["markers"]}, r

    def test_all_sites_with_kinds_and_info(self):
        m, r = self.markers()
        self.assertEqual({k: v["kind"] for k, v in m.items()}, {
            "Base One": "base", "Base Empty": "base", "OSF St Francis": "hospital",  # a hospital base: hospital
            "Airport KPIA": "hangar", "Carle": "hospital"})
        self.assertEqual(m["Base One"]["info"], ["Peoria, IL", "Base · 1 aircraft: LIFE 1 (N911LF)", "Air Methods",
                                                 "Weather hangar: Airport KPIA"])
        self.assertEqual(m["OSF St Francis"]["info"][:3], ["0 landings in the last 90 days",
                                                           "Also a base · 1 aircraft: N2PH", "PHI"])
        self.assertIn("Hangar: at the base", m["OSF St Francis"]["info"])
        self.assertEqual(m["Airport KPIA"]["info"], ["Weather hangar for Base One"])
        self.assertEqual(m["Carle"]["info"], ["1 landing in the last 90 days"])
        self.assertEqual(m["Carle"]["url"], reverse("site_detail", args=[self.carle.pk]))
        self.assertEqual(r.context["shown"], ["base", "hospital", "hangar"])
        self.assertContains(r, 'id="map-markers"')

    def test_filter_by_operator_and_program(self):
        m, _ = self.markers(operator=self.am.pk)
        self.assertEqual(set(m), {"Base One", "Airport KPIA", "Carle"})  # its base, hangar, hospitals it landed at
        self.assertEqual(m["Carle"]["info"], ["1 landing in the last 90 days by these aircraft"])
        m, _ = self.markers(program=self.lf.pk)
        self.assertEqual(set(m), {"Base One", "Airport KPIA", "Carle"})
        m, _ = self.markers(operator=self.phi.pk)
        self.assertEqual(set(m), {"OSF St Francis"})
        m, _ = self.markers(operator=self.phi.pk, program=self.lf.pk)
        self.assertEqual(m, {})

    def test_toggles_remembered_in_url(self):
        _, r = self.markers(f=1, show=["hospital"])
        self.assertEqual(r.context["shown"], ["hospital"])
        self.assertContains(r, 'name="show" value="hospital" checked')
        self.assertNotContains(r, 'name="show" value="base" checked')

    def test_menu_groups(self):
        _, r = self.markers()
        html = r.content.decode()
        for label in (">Map</a>", ">Activity</summary>", ">Data</summary>", ">Why legs are unresolved</a>"):
            self.assertIn(label, html)
        self.assertNotIn(">Admin</summary>", html)  # viewers: no admin menu
        admin = User.objects.create_user("boss", password="x", is_staff=True)
        admin.groups.add(Group.objects.get(name="Admin"))
        self.client.force_login(admin)
        r = self.client.get(reverse("dashboard"))
        self.assertContains(r, ">Admin</summary>")
        self.assertContains(r, ">Review queue</a>")
        self.assertContains(r, ">Log out</button>")

    def test_markers_are_json_safe(self):
        Site.objects.create(name='</script><b>x', site_type=Site.HOSPITAL, lat=40, lon=-89)
        _, r = self.markers()
        self.assertNotContains(r, "</script><b>x")
        data = r.content.decode().split('id="map-markers" type="application/json">')[1].split("</script>")[0]
        self.assertIn('</script><b>x', [m["name"] for m in json.loads(data)])
