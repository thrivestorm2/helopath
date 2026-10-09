import io
import os
import zipfile
from datetime import datetime, timezone
from unittest import mock

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from tracker import nasr, rawstore
from tracker.management.commands.run_worker import tick
from tracker.models import OpsTask, RawTrace, RebuildRequest, Site, WorkerState
from tracker.ops import recover_interrupted

APT_CSV = ("SITE_TYPE_CODE,STATE_CODE,ARPT_ID,CITY,ARPT_NAME,FACILITY_USE_CODE,LAT_DECIMAL,LONG_DECIMAL,ELEV\n"
           "H,IL,1IL1,SPRINGFIELD,MEMORIAL MEDICAL CENTER,PR,39.80,-89.65,620\n").encode()
NOON = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
EVENING = datetime(2026, 10, 9, 19, 0, tzinfo=timezone.utc)


def quiet(*a):
    pass


class WorkerTests(TestCase):
    def test_idle_tick_records_heartbeat(self):
        self.assertEqual(tick(now=NOON, log=quiet), "idle")
        self.assertEqual(WorkerState.get().heartbeat, NOON)

    def test_runs_queued_seed_task_from_upload(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("APT_BASE.csv", APT_CSV)
        task = OpsTask.objects.create(kind=OpsTask.SEED_SITES, upload=buf.getvalue())
        self.assertEqual(tick(now=NOON, log=quiet), "task:seed_sites:ok")
        task.refresh_from_db()
        self.assertEqual(task.status, OpsTask.OK)
        self.assertIn("sites created 1", task.log)
        self.assertIsNone(task.upload)
        self.assertEqual(Site.objects.get().site_type, Site.HOSPITAL)

    def test_failed_task_is_recorded_and_worker_survives(self):
        OpsTask.objects.create(kind=OpsTask.LOAD_REGISTRY)
        with mock.patch("tracker.ops.download_registry", side_effect=OSError("network down")):
            self.assertEqual(tick(now=NOON, log=quiet), "task:load_registry:failed")
        self.assertIn("network down", OpsTask.objects.get().log)
        self.assertEqual(tick(now=NOON, log=quiet), "idle")

    def test_backfill_task_calls_backfill(self):
        OpsTask.objects.create(kind=OpsTask.BACKFILL)
        with mock.patch("tracker.ops.run_backfill") as rb:
            self.assertEqual(tick(now=NOON, log=quiet), "task:backfill:ok")
        rb.assert_called_once()

    def test_nightly_runs_once_per_day_after_scheduled_time(self):
        with mock.patch("tracker.management.commands.run_worker.run_nightly") as nightly, \
                mock.patch("tracker.management.commands.run_worker.utc_today", return_value=EVENING.date()):
            self.assertEqual(tick(now=NOON, log=quiet), "idle")
            self.assertEqual(tick(now=EVENING, log=quiet), "nightly")
            self.assertEqual(tick(now=EVENING, log=quiet), "idle")
        nightly.assert_called_once()

    def test_queued_rebuild_is_processed(self):
        RebuildRequest.objects.create(source="adsblol")
        with mock.patch("tracker.ingest.pipeline.rebuild") as rebuild:
            self.assertEqual(tick(now=NOON, log=quiet), "rebuild")
        rebuild.assert_called_once_with("adsblol")
        self.assertFalse(RebuildRequest.objects.filter(processed_at__isnull=True).exists())

    def test_restart_resumes_backfill_and_fails_other_running_tasks(self):
        b = OpsTask.objects.create(kind=OpsTask.BACKFILL, status=OpsTask.RUNNING)
        r = OpsTask.objects.create(kind=OpsTask.LOAD_REGISTRY, status=OpsTask.RUNNING)
        recover_interrupted()
        b.refresh_from_db()
        r.refresh_from_db()
        self.assertEqual((b.status, r.status), (OpsTask.QUEUED, OpsTask.FAILED))


class OperationsPageTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("boss", password="x", is_staff=True)
        self.admin.groups.add(Group.objects.get(name="Admin"))
        self.viewer = User.objects.create_user("v", password="x")
        self.viewer.groups.add(Group.objects.get(name="Viewer"))

    def test_access(self):
        url = reverse("operations")
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, {"kind": "rebuild"}).status_code, 403)
        self.client.force_login(self.admin)
        r = self.client.get(url)
        self.assertContains(r, "Load FAA registry")
        self.assertContains(r, "never started")

    def test_queue_task_once(self):
        self.client.force_login(self.admin)
        self.client.post(reverse("operations"), {"kind": "load_registry"})
        self.client.post(reverse("operations"), {"kind": "load_registry"})
        self.assertEqual(OpsTask.objects.filter(kind="load_registry").count(), 1)
        self.client.post(reverse("operations"), {"kind": "nonsense"})
        self.assertEqual(OpsTask.objects.count(), 1)


class EnsureAdminTests(TestCase):
    def test_creates_once_and_never_resets(self):
        with mock.patch.dict(os.environ, {"HELOPATH_ADMIN_PASSWORD": "first-password-123"}):
            call_command("ensure_admin", stdout=io.StringIO())
        with mock.patch.dict(os.environ, {"HELOPATH_ADMIN_PASSWORD": "second-password-456"}):
            call_command("ensure_admin", stdout=io.StringIO())
        admin = User.objects.get(username="admin")
        self.assertTrue(admin.is_superuser)
        self.assertTrue(admin.check_password("first-password-123"))

    def test_no_password_no_admin(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HELOPATH_ADMIN_PASSWORD", None)
            call_command("ensure_admin", stdout=io.StringIO(), stderr=io.StringIO())
        self.assertFalse(User.objects.exists())


class RawStoreAndNasrTests(TestCase):
    def test_rawstore(self):
        from datetime import date
        rawstore.put("adsblol", date(2026, 10, 2), "abc123", b"\x1f\x8bday2")
        rawstore.put("adsblol", date(2026, 10, 1), "abc123", b"\x1f\x8bday1")
        rawstore.put("adsblol", date(2026, 10, 1), "def456", b"\x1f\x8bother")
        rawstore.put("adsblol", date(2026, 10, 1), "def456", b"\x1f\x8breplaced")
        self.assertEqual(RawTrace.objects.count(), 3)
        self.assertEqual(rawstore.traces_for_hex("adsblol", "abc123"), [b"\x1f\x8bday1", b"\x1f\x8bday2"])
        self.assertEqual(rawstore.latest_for_hex("adsblol", "def456"), b"\x1f\x8breplaced")
        self.assertEqual(rawstore.hexes("adsblol"), {"abc123", "def456"})
        self.assertEqual(rawstore.purge_before("adsblol", date(2026, 10, 2), keep_hexes={"abc123"}), 1)
        self.assertEqual(rawstore.hexes("adsblol"), {"abc123"})

    def test_nasr_cycle_and_url(self):
        from datetime import date
        self.assertEqual(nasr.cycle_dates(date(2025, 1, 23))[0], date(2025, 1, 23))
        self.assertEqual(nasr.cycle_dates(date(2025, 2, 19)), [date(2025, 1, 23), date(2024, 12, 26)])
        self.assertEqual(nasr.URL.format(d=date(2025, 1, 23)),
                         "https://nfdc.faa.gov/webContent/28DaySub/extra/23_Jan_2025_APT_CSV.zip")
        self.assertEqual(nasr.extract_apt_base(APT_CSV), APT_CSV)
