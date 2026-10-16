from datetime import timedelta

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from tracker.daily import daily_summary, summary_day
from tracker.models import Aircraft, IngestRun, Mission

from . import login_viewer
from . import test_pipeline as tp
from tracker.tests import test_operator


def run(day, status=IngestRun.OK):
    return IngestRun.objects.create(day=day, source="adsblol", status=status)


class SummaryDayTests(TestCase):
    def test_latest_local_day_with_both_utc_days(self):
        today = timezone.localdate()
        self.assertEqual(summary_day("adsblol"), (None, False))
        run(today - timedelta(days=3))
        run(today - timedelta(days=2))
        self.assertEqual(summary_day("adsblol"), (today - timedelta(days=3), False))
        run(today - timedelta(days=1))  # the evening download: yesterday's second half is now in
        self.assertEqual(summary_day("adsblol")[0], today - timedelta(days=2))
        run(today, status=IngestRun.FAILED)
        self.assertEqual(summary_day("adsblol"), (today - timedelta(days=2), True))

    def test_empty_states_render(self):
        login_viewer(self.client)
        r = self.client.get(reverse("dashboard"))
        self.assertContains(r, "No full day of data yet.")
        run(timezone.localdate(), status=IngestRun.FAILED)
        self.assertContains(self.client.get(reverse("dashboard")), "the latest download failed")


class DailySummaryTests(TestCase):
    """On the synthetic three days of tp.PipelineTests."""

    setUp = tp.PipelineTests.setUp
    _day_points = tp.PipelineTests._day_points
    _ingest = tp.PipelineTests._ingest
    _ingest_all = tp.PipelineTests._ingest_all

    def test_figures_for_the_latest_full_day(self):
        self._ingest_all()  # UTC days today-5, -4, -3: the latest full local day is today-4 (the scene flight)
        a = Aircraft.objects.get(icao_hex=tp.MED)
        s = daily_summary("adsblol", Aircraft.VISIBLE)
        self.assertEqual(s.day, self.days[1])
        self.assertGreaterEqual(s.missions.value, 1)
        self.assertEqual(s.by_label["scene"], 1)
        self.assertEqual(s.busiest_aircraft[0], a)
        self.assertEqual(s.busiest_hospital[0], self.hosp_a)  # the scene patient went to Hospital A
        self.assertEqual(s.flew, 1)  # the news helicopter is never discovered
        self.assertIsNotNone(s.legs.usual)
        # Viewers only ever count confirmed aircraft: the discovered one drops out.
        s = daily_summary("adsblol", (Aircraft.CONFIRMED,))
        self.assertEqual((s.missions.value, s.busiest_aircraft), (0, None))

    def test_card_links_and_admin_only_new_line(self):
        self._ingest_all()
        from datetime import datetime, time

        from tracker.inference.pipeline import local_tz
        Aircraft.objects.update(discovered_at=datetime.combine(self.days[1], time(12), tzinfo=local_tz()))
        admin = User.objects.create_user("boss", password="x", is_staff=True)
        admin.groups.add(Group.objects.get(name="Admin"))
        self.client.force_login(admin)
        r = self.client.get(reverse("dashboard"))
        self.assertContains(r, "Daily summary")
        self.assertContains(r, f'{reverse("missions")}?day={self.days[1]:%Y-%m-%d}')
        self.assertContains(r, "Busiest aircraft")
        self.assertContains(r, '<span class="ds-label">New</span>')  # admins
        r = self.client.get(reverse("missions"), {"day": f"{self.days[1]:%Y-%m-%d}"})
        self.assertContains(r, "Missions on ")
        self.assertEqual(len(r.context["page"]), self.client.get(reverse("dashboard")).context["daily"].missions.value)
        self.assertEqual(self.client.get(reverse("missions"), {"day": "2026-13-45"}).status_code, 200)
        login_viewer(self.client)
        r = self.client.get(reverse("dashboard"))
        self.assertContains(r, "Daily summary")
        self.assertNotContains(r, '<span class="ds-label">New</span>')


class CardRulesTests(TestCase):
    """The approved card rules, on hand-made missions."""

    def setUp(self):
        from datetime import datetime, time

        from tracker.inference.pipeline import local_tz
        from tracker.models import Site
        self.today = timezone.localdate()
        self.day = self.today - timedelta(days=2)
        run(self.day)
        run(self.day + timedelta(days=1))
        self.hosp = Site.objects.create(name="Hospital A", site_type=Site.HOSPITAL, lat=40, lon=-89)
        self.a = Aircraft.objects.create(tail_number="N1A", icao_hex="a00001", status=Aircraft.CONFIRMED, operator=test_operator())
        self.b = Aircraft.objects.create(tail_number="N2B", icao_hex="a00002", status=Aircraft.CONFIRMED, operator=test_operator())

        def mission(aircraft, label, hour, origin=None, dest=None):
            t = datetime.combine(self.day, time(hour), tzinfo=local_tz())
            Mission.objects.create(aircraft=aircraft, source="adsblol", start_ts=t, end_ts=t + timedelta(minutes=30),
                                   label=label, origin_site=origin, destination_site=dest, confidence=0.8,
                                   rule_fired="t")
        for h in range(8, 12):
            mission(self.a, "reposition", h)            # 4 repositions: never make it the busiest
        mission(self.b, "scene", 13, dest=self.hosp)
        mission(self.b, "unknown", 14)
        mission(self.b, "single_hospital_unresolved", 15)
        mission(self.b, "unknown", 16)                  # unknown -> unknown, twice: not a route
        mission(self.a, "interfacility", 17, dest=self.hosp)

    def summary(self):
        return daily_summary("adsblol", Aircraft.VISIBLE)

    def test_busiest_ignores_repositions_and_bar_has_no_repositions(self):
        s = self.summary()
        self.assertEqual(s.busiest_aircraft, (self.b, 4))
        self.assertEqual(s.by_label, {"scene": 1, "interfacility": 1, "unknown": 3})
        self.assertEqual([k for k, *_ in s.bar], ["scene", "interfacility", "unknown"])
        self.assertAlmostEqual(sum(p for *_, p in s.bar), 100)

    def test_top_route_needs_a_known_site(self):
        origin, dest, label, n, link = self.summary().top_route
        self.assertEqual((origin, dest, n, link), (None, self.hosp, 1, self.hosp))

    def test_gone_quiet_from_30_days_for_a_week(self):
        from datetime import datetime, time

        from tracker.inference.pipeline import local_tz
        end = datetime.combine(self.day + timedelta(days=1), time.min, tzinfo=local_tz())
        for tail, days in (("N29Q", 29), ("N31Q", 31), ("N36Q", 36), ("N38Q", 38)):
            Aircraft.objects.create(tail_number=tail, icao_hex=f"b000{days}", status=Aircraft.CONFIRMED,
                                    last_seen=end - timedelta(days=days, hours=1), operator=test_operator())
        self.assertEqual([a.tail_number for a, _ in self.summary().quiet], ["N31Q", "N36Q"])

    def test_pills_and_stale_label(self):
        s = self.summary()
        self.assertEqual(s.days_old, 2)
        from tracker.daily import Change, DailySummary
        self.assertEqual(Change(143, 100).pill, ("+43%", "up"))
        self.assertEqual(Change(92, 100).pill, ("−8%", "down"))
        self.assertEqual(Change(101, 100).pill, ("≈", "flat"))
        self.assertIsNone(Change(5, None).pill)
        self.assertEqual(DailySummary(rate_change=2.4).rate_pill, ("+2 pts", "good"))
        self.assertEqual(DailySummary(rate_change=-3).rate_pill, ("−3 pts", "bad"))
        login_viewer(self.client)
        r = self.client.get(reverse("dashboard"))
        self.assertContains(r, "2 days ago")
        self.assertContains(r, 'class="ds-seg scene"')
        self.assertNotContains(r, "Reposition")
