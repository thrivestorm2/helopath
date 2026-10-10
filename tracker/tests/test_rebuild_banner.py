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


class RebuildProgressTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("boss", password="x", is_staff=True)
        self.admin.groups.add(Group.objects.get(name="Admin"))
        self.client.force_login(self.admin)

    def test_progress_banner_while_running_and_status_endpoint(self):
        r = self.client.post(reverse("queue_rebuild"), {"source": "adsblol"}, follow=True)
        self.assertEqual(r.content.count(b'class="rebuild-banner'), 1)  # one banner, no extra "queued" message
        self.assertNotContains(r, "Rebuild queued;")
        r = self.client.get(reverse("dashboard"))
        self.assertContains(r, 'class="rebuild-banner running"')
        self.assertContains(r, 'aria-valuenow="0"')
        self.assertContains(r, "queued; the worker starts it within a minute")
        self.assertNotContains(r, ">Queue rebuild</button>")  # nothing to click twice
        WorkerState.objects.filter(pk=1).update(current="Rebuild: building legs: aircraft 41 of 120")
        r = self.client.get(reverse("site_list"))
        self.assertContains(r, "building legs: aircraft 41 of 120")
        self.assertContains(r, 'style="width:30%"')  # 2 + 84 * 40/120
        self.assertContains(r, '<span class="pct">30%</span>')
        self.assertEqual(self.client.get(reverse("rebuild_status")).json(),
                         {"active": True, "step": "building legs: aircraft 41 of 120", "percent": 30})
        RebuildRequest.objects.update(processed_at=timezone.now())
        WorkerState.objects.filter(pk=1).update(current="")
        self.assertEqual(self.client.get(reverse("rebuild_status")).json(),
                         {"active": False, "step": "", "percent": 100})
        self.assertNotContains(self.client.get(reverse("dashboard")), "rebuild-banner")

    def test_second_click_does_not_queue_another(self):
        self.client.post(reverse("queue_rebuild"), {"source": "adsblol"})
        r = self.client.post(reverse("queue_rebuild"), {"source": "adsblol"}, follow=True)
        self.assertEqual(RebuildRequest.objects.count(), 1)
        self.assertContains(r, 'class="rebuild-banner running"')  # the banner is the only feedback
        self.client.post(reverse("operations"), {"kind": OpsTask.REBUILD})
        self.assertFalse(OpsTask.objects.filter(kind=OpsTask.REBUILD).exists())  # nor from Operations

    def test_viewers_never_see_progress(self):
        RebuildRequest.objects.create(source="adsblol")
        viewer = User.objects.create_user("v", password="x")
        viewer.groups.add(Group.objects.get(name="Viewer"))
        self.client.force_login(viewer)
        self.assertNotContains(self.client.get(reverse("dashboard")), "rebuild-banner")
        self.assertEqual(self.client.get(reverse("rebuild_status")).status_code, 403)


class ProgressPercentTests(TestCase):
    def test_steps_map_to_percent(self):
        from tracker.rebuild_flag import progress_percent
        self.assertEqual([progress_percent(s) for s in (
            "queued; the worker starts it within a minute", "clearing derived tables",
            "building legs: aircraft 1 of 682", "building legs: aircraft 682 of 682",
            "building legs: aircraft 1,200 of 2,400", "discovering aircraft", "finding landing clusters",
            "chaining missions", "totalling monthly flows")], [0, 1, 2, 86, 44, 86, 90, 93, 97])
