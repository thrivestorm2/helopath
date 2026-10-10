from datetime import datetime, timedelta, timezone

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from tracker.hangars import suggest_hangars
from tracker.inference import pipeline
from tracker.models import Aircraft, Leg, Mission, Operator, Site

T0 = datetime(2026, 9, 2, 14, tzinfo=timezone.utc)  # 09:00 Chicago
SCENE = (40.25, -89.05)


class LegFactory:
    """Legs flown one after another; a stop is a Site or a (lat, lon) scene point."""

    def __init__(self, aircraft):
        self.aircraft, self.t = aircraft, T0

    def fly(self, *stops, lost_at=(), gap=timedelta(minutes=20)):
        for i, (a, b) in enumerate(zip(stops, stops[1:])):
            la, lb = (a.lat, a.lon) if isinstance(a, Site) else a, (b.lat, b.lon) if isinstance(b, Site) else b
            lost = i in lost_at
            Leg.objects.create(
                hex=self.aircraft.icao_hex, aircraft=self.aircraft, source="adsblol", start_ts=self.t,
                end_ts=self.t + timedelta(minutes=30), start_lat=la[0], start_lon=la[1], end_lat=lb[0], end_lon=lb[1],
                start_site=a if isinstance(a, Site) else None,
                end_site=b if isinstance(b, Site) and not lost else None,
                end_state=Leg.LOST if lost else Leg.LANDED, confidence=1.0, start_on_ground=True)
            self.t += timedelta(minutes=30) + gap
        return self

    def next_day(self):
        self.t = T0.replace(day=self.t.day + 1)
        return self


def missions():
    return list(Mission.objects.order_by("start_ts").values_list(
        "label", "origin_site__name", "destination_site__name", "rule_fired"))


class WeatherHangarMissionTests(TestCase):
    def setUp(self):
        mk = lambda name, t, lat, lon, **kw: Site.objects.create(name=name, site_type=t, lat=lat, lon=lon, **kw)
        self.base = mk("Base One", Site.BASE, 40.0, -89.0)
        self.hangar = mk("Airport KPIA", Site.AIRPORT, 40.66, -89.69, radius_m=1500)
        self.hosp = mk("Hospital A", Site.HOSPITAL, 40.1, -89.2)
        self.a = Aircraft.objects.create(tail_number="N1", icao_hex="a00001", status=Aircraft.CONFIRMED, base=self.base)
        legs = LegFactory(self.a)
        legs.fly(self.base, self.hangar)                         # evening: storm coming, go to the hangar
        legs.next_day().fly(self.hangar, SCENE, self.hosp, self.hangar)  # morning call flown from the hangar
        legs.fly(self.hangar, self.base, gap=timedelta(hours=3))  # weather clears: back to base

    def test_without_hangar_the_weather_trips_are_lost(self):
        pipeline.build_missions("adsblol")
        labels = [m[0] for m in missions()]
        self.assertNotIn("scene", labels)  # the hangar flight is never counted
        self.assertIn("unknown", labels)

    def test_hangar_is_a_second_home(self):
        self.base.weather_hangar = self.hangar
        self.base.save()
        pipeline.build_missions("adsblol")
        self.assertEqual(missions(), [
            ("reposition", "Base One", "Airport KPIA", "base>hangar"),
            ("scene", None, "Hospital A", "hangar>U>H>hangar"),
            ("reposition", "Airport KPIA", "Base One", "hangar>base"),
        ])

    def test_hangar_at_the_base_itself_changes_nothing(self):
        self.base.weather_hangar = self.base
        self.base.save()
        pipeline.build_missions("adsblol")
        labels = [m[0] for m in missions()]
        self.assertNotIn("scene", labels)  # same as no hangar: the airport trip is not a home
        self.assertIn("unknown", labels)

    def test_inferred_base_ignores_the_hangar(self):
        Aircraft.objects.filter(pk=self.a.pk).update(base=None)
        legs = LegFactory(self.a)
        legs.t = T0 + timedelta(days=3)
        for _ in range(2):  # two more days starting in the hangar (3 in all) vs one at the base
            legs.fly(self.hangar, SCENE, self.hangar).next_day()
        pipeline.infer_bases("adsblol")
        self.a.refresh_from_db()
        self.assertEqual(self.a.inferred_base, self.hangar)  # without a hangar set, the airport wins
        self.base.weather_hangar = self.hangar
        self.base.save()
        pipeline.infer_bases("adsblol")
        self.a.refresh_from_db()
        self.assertEqual(self.a.inferred_base, self.base)

    def test_suggested_hangars(self):
        legs = LegFactory(self.a)
        legs.t = T0 + timedelta(days=3)
        legs.fly(self.hangar, self.base)
        found = suggest_hangars(self.base, "adsblol")
        self.assertEqual([(s.name, days, n) for s, days, n in found], [("Airport KPIA", 2, 1)])
        self.assertEqual(suggest_hangars(self.hosp, "adsblol"), [])  # nothing based there


class HospitalBaseTests(TestCase):
    def setUp(self):
        self.hosp_a = Site.objects.create(name="Hospital A", site_type=Site.HOSPITAL, lat=40.0, lon=-89.0)
        self.hosp_b = Site.objects.create(name="Hospital B", site_type=Site.HOSPITAL, lat=40.3, lon=-88.9)
        self.a = Aircraft.objects.create(tail_number="N1", icao_hex="a00001", status=Aircraft.CONFIRMED,
                                         base=self.hosp_a)
        self.legs = LegFactory(self.a)

    def test_return_to_hospital_base_is_the_drop_off(self):
        self.legs.fly(self.hosp_a, SCENE, self.hosp_a)
        self.legs.fly(self.hosp_a, self.hosp_b, self.hosp_a)
        self.legs.fly(self.hosp_a, SCENE, self.hosp_b, self.hosp_a)
        self.legs.fly(self.hosp_a, self.hosp_a)
        pipeline.build_missions("adsblol")
        self.assertEqual(missions(), [
            ("scene", None, "Hospital A", "base>U>H>base"),
            ("interfacility", "Hospital B", "Hospital A", "base>H>H>base"),
            ("scene", None, "Hospital B", "base>U>H>base"),  # delivered elsewhere: unchanged
            ("unknown", None, None, "base>base"),
        ])
        confs = list(Mission.objects.order_by("start_ts").values_list("confidence", flat=True))
        self.assertEqual(confs[:3], [0.6, 0.6, 1.0])  # drop-off at the base is less certain

    def test_non_hospital_base_unchanged(self):
        self.hosp_a.site_type = Site.BASE
        self.hosp_a.save()
        self.legs.fly(self.hosp_a, SCENE, self.hosp_a)
        pipeline.build_missions("adsblol")
        self.assertEqual(missions()[0][0], "unknown")


class SiteHangarPageTests(TestCase):
    def setUp(self):
        self.base = Site.objects.create(name="Base One", site_type=Site.BASE, lat=40.0, lon=-89.0)
        self.airport = Site.objects.create(name="Airport KPIA", site_type=Site.AIRPORT, lat=40.66, lon=-89.69,
                                           faa_id="PIA")
        self.a = Aircraft.objects.create(tail_number="N1", icao_hex="a00001", status=Aircraft.CONFIRMED,
                                         base=self.base)
        legs = LegFactory(self.a)
        for _ in range(2):
            legs.fly(self.airport, self.base).next_day()
        self.admin = User.objects.create_user("boss", password="x", is_staff=True)
        self.admin.groups.add(Group.objects.get(name="Admin"))
        self.viewer = User.objects.create_user("v", password="x")
        self.viewer.groups.add(Group.objects.get(name="Viewer"))

    def test_admin_sets_and_clears_suggested_hangar(self):
        self.client.force_login(self.admin)
        url = reverse("site_detail", args=[self.base.pk])
        set_url = reverse("set_weather_hangar", args=[self.base.pk])
        r = self.client.get(url)
        # One line in the page header, not a box: the top suggestion with confirm / at this base.
        self.assertContains(r, '<span id="hangar">· Weather hangar:')
        self.assertContains(r, f'<a href="{reverse("site_detail", args=[self.airport.pk])}">Airport KPIA</a>')
        self.assertContains(r, ">suggested</span>")
        self.assertContains(r, '<button class="link">confirm</button>')
        self.assertNotContains(r, 'class="card" id="hangar"')
        r = self.client.post(set_url, {"hangar": self.airport.pk}, follow=True)
        self.assertContains(r, "Weather hangar set to Airport KPIA")
        self.assertContains(r, reverse("queue_rebuild"))
        self.base.refresh_from_db()
        self.assertEqual(self.base.weather_hangar, self.airport)
        # Once set, the header only names it (with a link); changes are made on the admin Edit page.
        self.assertContains(r, f'<a href="{reverse("site_detail", args=[self.airport.pk])}">Airport KPIA</a>')
        for gone in (">suggested</span>", ">confirm</button>", ">at this base</button>"):
            self.assertNotContains(r, gone)
        self.client.post(set_url, {"hangar": ""})
        self.base.refresh_from_db()
        self.assertIsNone(self.base.weather_hangar)
        r = self.client.post(set_url, {"hangar": self.base.pk}, follow=True)
        self.base.refresh_from_db()
        self.assertEqual(self.base.weather_hangar, self.base)  # hangared at the base itself
        self.assertContains(r, "Weather hangar set to the base itself")
        self.assertContains(r, "Weather hangar:\n    at this base")
        self.assertNotContains(r, ">at this base</button>")
        self.assertNotContains(r, ">suggested</span>")

    def test_viewers_see_but_cannot_set(self):
        self.client.force_login(self.viewer)
        r = self.client.get(reverse("site_detail", args=[self.base.pk]))
        self.assertContains(r, "Airport KPIA")
        self.assertNotContains(r, ">confirm</button>")
        r = self.client.post(reverse("set_weather_hangar", args=[self.base.pk]), {"hangar": self.airport.pk})
        self.assertEqual(r.status_code, 403)

    def test_aircraft_page_shows_hangar_from_base(self):
        url = reverse("aircraft_detail", args=[self.a.pk])
        confirm = f'href="{reverse("site_detail", args=[self.base.pk])}#hangar">Confirm on base page</a>'
        self.client.force_login(self.viewer)
        r = self.client.get(url)
        self.assertContains(r, ">Airport KPIA</a>")
        self.assertContains(r, "suggested, not confirmed")
        self.assertNotContains(r, confirm)
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(url), confirm)
        self.base.weather_hangar = self.airport
        self.base.save()
        r = self.client.get(url)
        self.assertContains(r, ">Airport KPIA</a>")
        self.assertNotContains(r, "suggested, not confirmed")
        # Hangared at the base itself.
        self.base.weather_hangar = self.base
        self.base.save()
        r = self.client.get(url)
        self.assertContains(r, "At base")
        self.assertNotContains(r, "suggested, not confirmed")
        # No base, no hangar.
        Aircraft.objects.filter(pk=self.a.pk).update(base=None)
        self.assertContains(self.client.get(url), "<tr><th>Weather hangar</th><td>—</td></tr>")

    def test_admin_form_has_hangar_field(self):
        self.client.force_login(User.objects.create_superuser("root", password="x"))
        r = self.client.get(reverse("admin:tracker_site_change", args=[self.base.pk]))
        self.assertContains(r, 'name="weather_hangar"')


class RouteMapTests(TestCase):
    def setUp(self):
        self.op = Operator.objects.create(name="Air Methods")
        self.base = Site.objects.create(name="Base One", site_type=Site.BASE, lat=40.0, lon=-89.0)
        self.hosp = Site.objects.create(name="Hospital A", site_type=Site.HOSPITAL, lat=40.1, lon=-89.2)
        self.a = Aircraft.objects.create(tail_number="N1", icao_hex="a00001", status=Aircraft.CONFIRMED,
                                         base=self.base, operator=self.op)
        legs = LegFactory(self.a)
        legs.fly(self.base, SCENE, self.hosp, self.base)
        legs.fly(self.base, (40.4, -89.3), self.hosp, self.base, lost_at=(0,))  # signal lost at the scene
        pipeline.build_missions("adsblol")
        pipeline.build_aggregates("adsblol")
        viewer = User.objects.create_user("v", password="x")
        viewer.groups.add(Group.objects.get(name="Viewer"))
        self.client.force_login(viewer)

    def test_routes_link_to_the_map(self):
        r = self.client.get(reverse("operator_detail", args=[self.op.pk]))
        self.assertContains(r, f"/routes/map/?origin=none&amp;dest={self.hosp.pk}&amp;label=scene&amp;operator={self.op.pk}")
        r = self.client.get(reverse("flows"))
        self.assertContains(r, f"/routes/map/?origin=none&amp;dest={self.hosp.pk}&amp;label=scene&amp;operator={self.op.pk}"
                               f"&amp;base={self.base.pk}&amp;month=2026-09")

    def test_map_shows_scene_landings(self):
        r = self.client.get(reverse("route_map"), {"origin": "none", "dest": self.hosp.pk, "label": "scene",
                                                   "operator": self.op.pk, "base": self.base.pk, "month": "2026-09"})
        self.assertEqual(r.status_code, 200)
        points = r.context["points"]
        self.assertEqual(sorted((p["lat"], p["lost"]) for p in points), [(40.25, False), (40.4, True)])
        self.assertEqual(points[0]["tail"], "N1")
        self.assertContains(r, "2 landings away from known sites in 2 inferred missions.")
        self.assertContains(r, 'id="route-points"')
        self.assertContains(r, "Hospital A")
        # Other filters narrow it.
        r = self.client.get(reverse("route_map"), {"origin": "none", "dest": self.hosp.pk, "month": "2026-10"})
        self.assertEqual(r.context["points"], [])
        r = self.client.get(reverse("route_map"), {"operator": "none"})
        self.assertEqual(r.context["points"], [])

    def test_login_required(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("route_map")).status_code, 302)
