from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import TestCase
from django.utils import timezone

from tracker.inference import pipeline
from tracker.inference.pipeline import PIPELINE_VERSION
from tracker.models import (Aircraft, LandingCluster, Leg, Mission, MonthlyAggregate, PendingRebuild, RebuildRun,
                            Site, WorkerState)
from tracker.rebuild_flag import flag_full, flag_recount, flag_site
from tracker.rebuilds import process_overnight, process_quick

from . import test_pipeline as tp
from .test_pipeline import AIRPORT, HOSP_B, MED, OTHER, SCENE

CHICAGO = ZoneInfo("America/Chicago")


def local(*args):
    return datetime(*args, tzinfo=CHICAGO)


class QuickUpdateTests(TestCase):
    """Recounts and partial rebuilds run on their own, two minutes after the last edit."""

    def setUp(self):
        WorkerState.objects.update_or_create(pk=1, defaults={"rules_version": PIPELINE_VERSION})

    def test_waits_for_the_last_edit_then_recounts_once(self):
        flag_recount("weather hangar for Base One")
        flag_recount("service period changed for N1")
        now = timezone.now()
        with mock.patch.object(pipeline, "build_downstream") as recount:
            self.assertIsNone(process_quick(now))  # too soon after the edits
            recount.side_effect = lambda source, step: [step("discovering aircraft"), step("chaining missions")]
            self.assertEqual(process_quick(now + timedelta(minutes=3)), "recount")
        recount.assert_called_once()
        self.assertFalse(PendingRebuild.objects.filter(processed_at__isnull=True).exists())
        run = RebuildRun.objects.get()
        self.assertEqual((run.kind, run.ok, run.aircraft_rebuilt), ("recount", True, 0))
        self.assertEqual([name for name, _ in run.steps], ["discovering aircraft", "chaining missions"])
        self.assertEqual(run.reason, "service period changed for N1; weather hangar for Base One")
        self.assertEqual(WorkerState.get().current, "")

    def test_partial_rebuilds_only_aircraft_near_the_site(self):
        flag_site("site New Pad added", (40.2, -89.1, 300))
        flag_recount("weather hangar for Base One")
        later = timezone.now() + timedelta(minutes=3)
        with mock.patch.object(pipeline, "hexes_near", return_value={"a1b2c3"}) as near, \
                mock.patch("tracker.rebuilds.rawstore.hexes", return_value={"a1b2c3", "a4d5e6", "a00001"}), \
                mock.patch.object(pipeline, "partial_rebuild") as partial:
            self.assertEqual(process_quick(later), "partial: 1 aircraft")
        near.assert_called_once_with("adsblol", [(40.2, -89.1, 300)])
        partial.assert_called_once_with("adsblol", {"a1b2c3"}, progress=mock.ANY)
        self.assertEqual(RebuildRun.objects.get().aircraft_rebuilt, 1)

    def test_too_large_a_partial_waits_for_the_overnight_full_rebuild(self):
        flag_site("site Big edited", (40.2, -89.1, 300))
        later = timezone.now() + timedelta(minutes=3)
        with mock.patch.object(pipeline, "hexes_near", return_value={"a", "b"}), \
                mock.patch("tracker.rebuilds.rawstore.hexes", return_value={"a", "b", "c", "d"}), \
                mock.patch.object(pipeline, "partial_rebuild") as partial:
            self.assertIn("overnight", process_quick(later))  # 2 of 4 aircraft > 40%
        partial.assert_not_called()
        self.assertEqual(list(PendingRebuild.objects.values_list("kind", "processed_at")), [("full", None)])

    def test_a_failure_is_left_for_the_overnight_full_rebuild_not_retried(self):
        flag_recount("weather hangar for Base One")
        with mock.patch.object(pipeline, "build_downstream", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                process_quick(timezone.now() + timedelta(minutes=3))
        self.assertEqual(PendingRebuild.objects.get().kind, "full")
        self.assertFalse(RebuildRun.objects.get().ok)
        self.assertIsNone(process_quick(timezone.now() + timedelta(minutes=6)))

    def test_worker_tick_runs_it(self):
        from tracker.management.commands.run_worker import tick
        flag_recount("weather hangar for Base One")
        with mock.patch.object(pipeline, "build_downstream"):
            self.assertEqual(tick(now=timezone.now() + timedelta(minutes=3), log=lambda *a: None),
                             "quick update: recount")


class OvernightTests(TestCase):
    """Full rebuilds run on their own only overnight: when one is due, and weekly."""

    def setUp(self):
        WorkerState.objects.update_or_create(pk=1, defaults={"rules_version": PIPELINE_VERSION})
        patcher = mock.patch.object(pipeline, "rebuild", return_value={})
        self.rebuild = patcher.start()
        self.addCleanup(patcher.stop)

    def test_pending_full_rebuild_runs_at_night_once(self):
        flag_full("FAA sites seeded")
        tuesday = local(2026, 10, 13, 14, 0)
        self.assertIsNone(process_overnight(tuesday))  # not in the afternoon
        self.assertEqual(process_overnight(local(2026, 10, 14, 2, 5)), "overnight full rebuild")
        self.rebuild.assert_called_once()
        self.assertFalse(PendingRebuild.objects.filter(processed_at__isnull=True).exists())
        self.assertIsNotNone(WorkerState.get().last_full_rebuild)
        self.assertEqual(RebuildRun.objects.get().reason, "FAA sites seeded")
        flag_full("again")
        self.assertIsNone(process_overnight(local(2026, 10, 14, 3, 0)))  # one per night
        self.assertEqual(self.rebuild.call_count, 1)

    def test_new_counting_rules_run_the_first_night(self):
        WorkerState.objects.filter(pk=1).update(rules_version=PIPELINE_VERSION - 1)
        self.assertEqual(process_overnight(local(2026, 10, 14, 2, 30)), "overnight full rebuild")
        self.assertEqual(WorkerState.get().rules_version, PIPELINE_VERSION)

    def test_weekly_safety_net_on_sunday_only(self):
        self.assertIsNone(process_overnight(local(2026, 10, 14, 2, 30)))  # Wednesday, nothing due
        self.assertEqual(process_overnight(local(2026, 10, 18, 2, 30)), "overnight full rebuild")  # Sunday
        self.assertEqual(RebuildRun.objects.get().reason, "weekly full rebuild")

    def test_never_by_day_even_when_due(self):
        flag_full("FAA sites seeded")
        for hour in (6, 9, 12, 18, 23):
            self.assertIsNone(process_overnight(local(2026, 10, 18, hour, 0)))
        self.rebuild.assert_not_called()


class PartialEqualsFullTests(TestCase):
    """A partial rebuild after a site change gives exactly what a full rebuild gives."""

    setUp = tp.PipelineTests.setUp
    _day_points = tp.PipelineTests._day_points
    _ingest = tp.PipelineTests._ingest
    _ingest_all = tp.PipelineTests._ingest_all

    @staticmethod
    def _snapshot():
        def rows(qs, *fields):
            return sorted(qs.values_list(*fields), key=repr)
        return (
            rows(Leg.objects, "hex", "start_ts", "end_ts", "start_site_id", "end_site_id", "end_state", "confidence",
                 "end_alt_ft"),
            rows(Mission.objects, "aircraft_id", "start_ts", "end_ts", "label", "origin_site_id",
                 "destination_site_id", "confidence", "rule_fired"),
            rows(MonthlyAggregate.objects, "operator_id", "base_id", "origin_site_id", "destination_site_id", "month",
                 "label", "count", "mean_confidence", "includes_unreviewed"),
            rows(Aircraft.objects, "icao_hex", "status", "inferred_base_id", "qualifying_visits"),
            rows(LandingCluster.objects, "lat", "lon", "landing_count", "nearest_site_id"),
            rows(Site.objects, "pk", "radius_m", "is_active"),
        )

    def _check(self, change):
        self._ingest_all()
        before = self._snapshot()
        areas = change()
        hexes = pipeline.hexes_near("adsblol", areas)
        pipeline.partial_rebuild("adsblol", hexes)
        partial = self._snapshot()
        pipeline.rebuild("adsblol")
        self.assertEqual(partial, self._snapshot())
        return hexes, before, partial

    def test_site_added(self):
        def add():
            Site.objects.create(name="Scene Pad", site_type=Site.HOSPITAL, lat=SCENE[0], lon=SCENE[1],
                                elevation_ft=600)
            return [(SCENE[0], SCENE[1], 300)]
        hexes, before, after = self._check(add)
        self.assertEqual(hexes, {MED})  # the other helicopter never goes near it
        self.assertNotEqual(before[0], after[0])  # and the change did matter

    def test_site_moved(self):
        def move():
            site = Site.objects.get(name="Hospital B")
            old = (site.lat, site.lon, site.radius_m)
            site.lat += 0.004
            site.save()
            return [old, (site.lat, site.lon, site.radius_m)]
        self._check(move)

    def test_radius_changed(self):
        def shrink():
            site = Site.objects.get(name="Airport C")
            old = site.radius_m
            site.radius_m = 200
            site.save()
            return [(site.lat, site.lon, old)]
        hexes, _, _ = self._check(shrink)
        self.assertEqual(hexes, {MED, OTHER})  # both use the airport

    def test_site_deactivated(self):
        def deactivate():
            site = Site.objects.get(name="Hospital B")
            site.is_active = False
            site.save()
            return [(HOSP_B[0], HOSP_B[1], site.radius_m)]
        self._check(deactivate)

    def test_far_away_change_touches_no_aircraft(self):
        self._ingest_all()
        self.assertEqual(pipeline.hexes_near("adsblol", [(42.5, -87.0, 300)]), set())
        self.assertEqual(pipeline.hexes_near("adsblol", [(AIRPORT[0], AIRPORT[1], 300)]), {MED, OTHER})
