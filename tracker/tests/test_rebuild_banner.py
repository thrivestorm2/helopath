from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from tracker.inference.pipeline import PIPELINE_VERSION
from tracker.models import OpsTask, PendingRebuild, RebuildRequest, Site, WorkerState
from tracker.rebuild_flag import banner, full_reasons, quick_reasons

BANNER = 'class="rebuild-banner"'


def _clear():
    WorkerState.objects.update_or_create(pk=1, defaults={"rules_version": PIPELINE_VERSION})
    PendingRebuild.objects.all().delete()


class RebuildBannerTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("boss", password="x", is_staff=True)
        self.admin.groups.add(Group.objects.get(name="Admin"))
        self.viewer = User.objects.create_user("v", password="x")
        self.viewer.groups.add(Group.objects.get(name="Viewer"))
        self.site = Site.objects.create(name="Base One", site_type=Site.BASE, lat=40, lon=-89)
        self.airport = Site.objects.create(name="Airport KPIA", site_type=Site.AIRPORT, lat=40.6, lon=-89.6)

    def test_new_counting_rules_schedule_a_full_rebuild_tonight(self):
        self.client.force_login(self.admin)
        r = self.client.get(reverse("dashboard"))
        self.assertContains(r, BANNER)
        self.assertContains(r, "Full rebuild scheduled for tonight: the app was updated with new counting rules")
        self.assertContains(r, ">Run now</button>")
        self.client.force_login(self.viewer)
        self.assertNotContains(self.client.get(reverse("dashboard")), BANNER)

    def test_run_now_queues_it_and_returns_to_the_page(self):
        self.client.force_login(self.admin)
        PendingRebuild.objects.create(kind=PendingRebuild.FULL, reason="FAA sites seeded")
        page = reverse("site_detail", args=[self.site.pk])
        r = self.client.post(reverse("queue_rebuild"), {"source": "adsblol", "next": page})
        self.assertRedirects(r, page, fetch_redirect_response=False)
        self.assertEqual(RebuildRequest.objects.count(), 1)
        self.assertEqual(WorkerState.get().rules_version, PIPELINE_VERSION)
        self.assertEqual((full_reasons(), quick_reasons()), ("", ""))  # everything pending is covered
        RebuildRequest.objects.update(processed_at=timezone.now())  # even after it has run
        self.assertNotContains(self.client.get(page), BANNER)
        r = self.client.post(reverse("queue_rebuild"), {"source": "adsblol", "next": "https://evil.example/"})
        self.assertRedirects(r, reverse("dashboard"), fetch_redirect_response=False)

    def _edit(self, site, **changes):
        change = reverse("admin:tracker_site_change", args=[site.pk])
        page = self.client.get(change)
        data = {k: v for k, v in page.context["adminform"].form.initial.items() if v is not None}
        data.update({"weather_hangar": data.get("weather_hangar") or "", "_save": "Save", **changes})
        if data.get("is_active") is False:
            data.pop("is_active")
        r = self.client.post(change, data)
        self.assertEqual(r.status_code, 302, getattr(r, "context", None) and r.context["adminform"].form.errors)

    def _kinds(self):
        out = list(PendingRebuild.objects.values_list("kind", "lat", "lon", "radius_m"))
        PendingRebuild.objects.all().delete()
        return out

    def test_each_edit_asks_only_for_what_it_changes(self):
        _clear()
        self.client.force_login(User.objects.create_superuser("root", password="x"))
        # Name, city, notes, social pages: no figures change, nothing to do.
        self._edit(self.airport, name="Peoria Intl", city="Peoria", notes="tower", facebook="flypia")
        self.assertEqual(self._kinds(), [])
        self.assertIsNone(banner())
        # Type, reviewed, FAA ID: a recount.
        self._edit(self.airport, site_type=Site.OTHER)
        self.assertEqual(self._kinds(), [("recount", None, None, None)])
        # Radius: legs near it (old and new radius at the same place).
        self._edit(self.airport, radius_m=900)
        self.assertEqual(self._kinds(), [("partial", 40.6, -89.6, 300), ("partial", 40.6, -89.6, 900)])
        # Moved: around both places.
        self._edit(self.airport, lat=40.61)
        self.assertEqual(self._kinds(), [("partial", 40.6, -89.6, 900), ("partial", 40.61, -89.6, 900)])
        # Deactivated: where it was; an inactive site's edits need nothing.
        self._edit(self.airport, is_active=False)
        self.assertEqual(self._kinds(), [("partial", 40.61, -89.6, 900)])
        self._edit(self.airport, radius_m=500)
        self.assertEqual(self._kinds(), [])
        # Added: where it is.
        r = self.client.post(reverse("admin:tracker_site_add"), {
            "name": "New Pad", "site_type": Site.HOSPITAL, "lat": 40.2, "lon": -89.1, "elevation_ft": 0,
            "radius_m": 300, "origin": "manual", "is_active": "on", "_save": "Save"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self._kinds(), [("partial", 40.2, -89.1, 300)])
        # Mark reviewed action: a recount (only when something changed).
        url = reverse("admin:tracker_site_changelist")
        self.client.post(url, {"action": "mark_reviewed", "_selected_action": [self.site.pk]})
        self.assertEqual(self._kinds(), [("recount", None, None, None)])
        self.client.post(url, {"action": "mark_reviewed", "_selected_action": [self.site.pk]})
        self.assertEqual(self._kinds(), [])
        # Deactivate action from the site list.
        self.client.post(reverse("admin:tracker_site_changelist"),
                         {"action": "deactivate", "_selected_action": [self.site.pk]})
        self.assertEqual(self._kinds(), [("partial", 40.0, -89.0, 300)])

    def test_hangar_is_a_recount_and_the_banner_says_it_runs_on_its_own(self):
        _clear()
        self.client.force_login(self.admin)
        r = self.client.post(reverse("set_weather_hangar", args=[self.site.pk]), {"hangar": self.airport.pk},
                             follow=True)
        self.assertContains(r, "Figures update on their own in about 2 minutes.")
        self.assertEqual(quick_reasons(), "weather hangar for Base One")
        r = self.client.get(reverse("dashboard"))
        self.assertContains(r, "Figures update on their own in about 2 minutes for recent changes: "
                               "weather hangar for Base One.")
        self.assertNotContains(r, ">Run now</button>")  # nothing to press
        self.assertNotContains(r, "Full rebuild scheduled")

    def test_progress_shown_while_any_rebuild_waits_or_runs(self):
        _clear()
        RebuildRequest.objects.create(source="adsblol")
        self.assertEqual(banner()[0], "running")
        RebuildRequest.objects.update(processed_at=timezone.now())
        self.assertIsNone(banner())
        OpsTask.objects.create(kind=OpsTask.REBUILD, status=OpsTask.RUNNING)
        self.assertEqual(banner()[0], "running")

    def test_operations_rebuild_task_covers_everything_pending(self):
        PendingRebuild.objects.create(kind=PendingRebuild.FULL, reason="FAA sites seeded")
        self.client.force_login(self.admin)
        self.client.post(reverse("operations"), {"kind": OpsTask.REBUILD})
        self.assertEqual((full_reasons(), WorkerState.get().rules_version), ("", PIPELINE_VERSION))


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
        self.assertContains(r, "Full rebuild in progress")
        self.assertContains(r, 'aria-valuenow="0"')
        self.assertContains(r, "queued; the worker starts it within a minute")
        self.assertNotContains(r, ">Run now</button>")  # nothing to click twice
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

    def test_quick_update_progress_is_labelled_and_scaled(self):
        from tracker.models import RebuildRun
        RebuildRun.objects.create(kind=PendingRebuild.PARTIAL, reason="site X edited")
        WorkerState.get()
        WorkerState.objects.filter(pk=1).update(current="Rebuild: chaining missions", rules_version=PIPELINE_VERSION)
        r = self.client.get(reverse("dashboard"))
        self.assertContains(r, "Quick update in progress: <strong class=\"rebuild-step\">chaining missions</strong>")
        self.assertEqual(self.client.get(reverse("rebuild_status")).json()["percent"], 65)

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
        self.assertEqual([progress_percent(s, PendingRebuild.PARTIAL) for s in (
            "building legs: aircraft 100 of 100", "discovering aircraft", "totalling monthly flows")], [30, 35, 85])
