from datetime import timedelta

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from tracker.daily import daily_summary, summary_day
from tracker.models import Aircraft, IngestRun

from . import login_viewer
from . import test_pipeline as tp


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
        Aircraft.objects.update(discovered_at=timezone.now() - timedelta(days=4, hours=-1))
        admin = User.objects.create_user("boss", password="x", is_staff=True)
        admin.groups.add(Group.objects.get(name="Admin"))
        self.client.force_login(admin)
        r = self.client.get(reverse("dashboard"))
        self.assertContains(r, "daily summary")
        self.assertContains(r, f'{reverse("missions")}?day={self.days[1]:%Y-%m-%d}')
        self.assertContains(r, "Busiest aircraft")
        r = self.client.get(reverse("missions"), {"day": f"{self.days[1]:%Y-%m-%d}"})
        self.assertContains(r, "Missions on ")
        self.assertEqual(len(r.context["page"]), self.client.get(reverse("dashboard")).context["daily"].missions.value)
        self.assertEqual(self.client.get(reverse("missions"), {"day": "2026-13-45"}).status_code, 200)
        login_viewer(self.client)
        r = self.client.get(reverse("dashboard"))
        self.assertContains(r, "daily summary")
        self.assertNotContains(r, "aircraft discovered")
