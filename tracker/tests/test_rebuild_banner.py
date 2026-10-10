from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from tracker.inference.pipeline import PIPELINE_VERSION
from tracker.models import OpsTask, RebuildRequest, Site, WorkerState
from tracker.rebuild_flag import banner_reason

BANNER = 'class="rebuild-banner"'


class RebuildBannerTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("boss", password="x", is_staff=True)
        self.admin.groups.add(Group.objects.get(name="Admin"))
        self.viewer = User.objects.create_user("v", password="x")
        self.viewer.groups.add(Group.objects.get(name="Viewer"))
        self.site = Site.objects.create(name="Base One", site_type=Site.BASE, lat=40, lon=-89)
        self.airport = Site.objects.create(name="Airport KPIA", site_type=Site.AIRPORT, lat=40.6, lon=-89.6)

    def _clear(self):
        WorkerState.objects.update_or_create(pk=1, defaults={"rebuild_reason": "", "rules_version": PIPELINE_VERSION})

    def test_new_counting_rules_show_banner_for_admins_only(self):
        self.client.force_login(self.admin)
        r = self.client.get(reverse("dashboard"))
        self.assertContains(r, BANNER)
        self.assertContains(r, "the app was updated with new counting rules")
        self.client.force_login(self.viewer)
        self.assertNotContains(self.client.get(reverse("dashboard")), BANNER)

    def test_queue_from_banner_clears_it_and_returns_to_the_page(self):
        self.client.force_login(self.admin)
        page = reverse("site_detail", args=[self.site.pk])
        r = self.client.post(reverse("queue_rebuild"), {"source": "adsblol", "next": page})
        self.assertRedirects(r, page, fetch_redirect_response=False)
        self.assertEqual(RebuildRequest.objects.count(), 1)
        self.assertEqual(WorkerState.get().rules_version, PIPELINE_VERSION)
        RebuildRequest.objects.update(processed_at=timezone.now())  # even after it has run
        self.assertNotContains(self.client.get(page), BANNER)
        r = self.client.post(reverse("queue_rebuild"), {"source": "adsblol", "next": "https://evil.example/"})
        self.assertRedirects(r, reverse("dashboard"), fetch_redirect_response=False)

    def test_site_and_hangar_edits_flag_a_rebuild(self):
        self._clear()
        self.client.force_login(self.admin)
        self.assertIsNone(banner_reason())
        self.client.post(reverse("set_weather_hangar", args=[self.site.pk]), {"hangar": self.airport.pk})
        self.assertEqual(banner_reason(), "weather hangar for Base One")
        self.client.force_login(User.objects.create_superuser("root", password="x"))
        change = reverse("admin:tracker_site_change", args=[self.airport.pk])
        page = self.client.get(change)
        self.assertEqual(page.status_code, 200)
        data = {k: v for k, v in page.context["adminform"].form.initial.items() if v is not None}
        data.update({"name": "Airport KPIA", "radius_m": 900, "weather_hangar": "", "_save": "Save"})
        self.client.post(change, data)
        self.assertEqual(banner_reason(), "site Airport KPIA edited; weather hangar for Base One")
        r = self.client.get(reverse("dashboard"))
        self.assertContains(r, "Rebuild needed to apply recent changes: site Airport KPIA edited")

    def test_hidden_while_a_rebuild_is_waiting_or_running(self):
        self._clear()
        WorkerState.objects.filter(pk=1).update(rebuild_reason="site X edited")
        RebuildRequest.objects.create(source="adsblol")
        self.assertIsNone(banner_reason())
        RebuildRequest.objects.update(processed_at=timezone.now())
        self.assertEqual(banner_reason(), "site X edited")
        OpsTask.objects.create(kind=OpsTask.REBUILD, status=OpsTask.RUNNING)
        self.assertIsNone(banner_reason())

    def test_operations_rebuild_task_clears_it(self):
        self.client.force_login(self.admin)
        self.client.post(reverse("operations"), {"kind": OpsTask.REBUILD})
        state = WorkerState.get()
        self.assertEqual((state.rebuild_reason, state.rules_version), ("", PIPELINE_VERSION))
