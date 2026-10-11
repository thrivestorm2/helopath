from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from tracker.faa import merge_duplicate_faa_sites
from tracker.models import Aircraft, LandingCluster, Leg, Mission, MonthlyAggregate, Site

NOW = timezone.now() - timedelta(days=1)


class MergeTests(TestCase):
    def setUp(self):
        User.objects.create_superuser("root", password="x")
        self.seed = Site.objects.create(name="Osf Saint Francis Heliport", site_type=Site.HOSPITAL, faa_id="IL01",
                                        lat=40.698, lon=-89.593, radius_m=300, origin="faa_seed", city="Peoria",
                                        state="IL")
        self.mine = Site.objects.create(name="OSF Saint Francis", site_type=Site.HOSPITAL, faa_id=" il01",
                                        lat=40.6982, lon=-89.5931, radius_m=200, origin="manual", reviewed=True,
                                        notes="Roof pad")
        self.other = Site.objects.create(name="Base", site_type=Site.BASE, lat=40, lon=-89, weather_hangar=self.seed)
        self.a = Aircraft.objects.create(tail_number="N1", icao_hex="a00001", status=Aircraft.CONFIRMED,
                                         base=self.seed, inferred_base=self.seed)
        self.leg = Leg.objects.create(hex="a00001", aircraft=self.a, source="adsblol", start_ts=NOW, end_ts=NOW,
                                      start_lat=40, start_lon=-89, end_lat=40.698, end_lon=-89.593,
                                      end_state=Leg.LANDED, start_site=self.other, end_site=self.seed)
        self.mission = Mission.objects.create(source="adsblol", aircraft=self.a, start_ts=NOW, end_ts=NOW,
                                              origin_site=self.seed, destination_site=self.seed, label="unknown",
                                              confidence=0.5, rule_fired="test")
        MonthlyAggregate.objects.create(source="adsblol", destination_site=self.seed, month=NOW.date().replace(day=1),
                                        label="unknown", count=1, mean_confidence=0.5, includes_unreviewed=False)
        LandingCluster.objects.create(source="adsblol", lat=40.7, lon=-89.59, landing_count=3, distinct_aircraft=1,
                                      first_seen=NOW, last_seen=NOW, nearest_site=self.seed)

    def test_merges_into_the_reviewed_site_and_moves_everything(self):
        [(keep, names)] = merge_duplicate_faa_sites()
        self.assertEqual((keep.pk, names), (self.mine.pk, ["Osf Saint Francis Heliport"]))
        self.assertFalse(Site.objects.filter(pk=self.seed.pk).exists())
        keep.refresh_from_db()
        self.assertEqual((keep.faa_id, keep.radius_m, keep.city, keep.state, keep.reviewed),
                         ("IL01", 300, "Peoria", "IL", True))
        self.assertIn("Roof pad", keep.notes)
        self.assertIn("Merged with duplicate 'Osf Saint Francis Heliport'", keep.notes)
        self.a.refresh_from_db()
        self.leg.refresh_from_db()
        self.mission.refresh_from_db()
        self.other.refresh_from_db()
        self.assertEqual((self.a.base, self.a.inferred_base, self.leg.end_site), (keep, keep, keep))
        self.assertEqual((self.mission.origin_site, self.mission.destination_site), (keep, keep))
        self.assertEqual(self.other.weather_hangar, keep)
        self.assertEqual(MonthlyAggregate.objects.get().destination_site, keep)
        self.assertEqual(LandingCluster.objects.get().nearest_site, keep)
        self.assertEqual(merge_duplicate_faa_sites(), [])  # nothing left to merge

    def test_admin_save_merges(self):
        self.client.force_login(User.objects.get(username="root"))
        from django.urls import reverse
        change = reverse("admin:tracker_site_change", args=[self.other.pk])
        page = self.client.get(change)
        data = {k: v for k, v in page.context["adminform"].form.initial.items() if v is not None}
        data.update({"faa_id": "", "weather_hangar": "", "is_active": "on", "_save": "Save"})
        r = self.client.post(change, data, follow=True)
        self.assertContains(r, "Merged Osf Saint Francis Heliport into OSF Saint Francis: same FAA ID IL01.")
