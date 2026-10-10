from datetime import timedelta

from django.contrib.auth.models import Group, User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from tracker.coverage import build_coverage, cells_for_track
from tracker.models import CoverageCell, Leg

T0 = 1_790_000_000


def leg(track, days_ago=1, source="adsblol"):
    ts = timezone.now() - timedelta(days=days_ago)
    return Leg.objects.create(hex="a00001", source=source, start_ts=ts, end_ts=ts, start_lat=track[0][0],
                              start_lon=track[0][1], end_lat=track[-1][0], end_lon=track[-1][1],
                              end_state=Leg.LANDED, simplified_track=track)


@override_settings(COVERAGE_CELL_DEG=0.1, COVERAGE_GAP_S=90)
class CoverageTests(TestCase):
    def test_track_fills_cells_between_points(self):
        cells = cells_for_track([[40.05, -89.05, 1000, T0], [40.05, -88.55, 900, T0 + 60]])
        self.assertEqual(sorted(cells), [(400, j) for j in range(-891, -885)])  # five squares crossed plus the end
        self.assertEqual(cells[(400, -891)], 1000)
        self.assertEqual(cells[(400, -886)], 900)
        self.assertIsNone(cells[(400, -889)])  # crossed between positions: no altitude known

    def test_reception_gap_is_not_filled(self):
        # Steady 30 s spacing, then a 10-minute jump: a gap in reception, so the squares in between stay empty.
        track = [[40.05, -89.05, 1000, T0], [40.05, -89.04, 1000, T0 + 30], [40.05, -89.03, 1000, T0 + 60],
                 [40.05, -88.55, 1000, T0 + 660]]
        self.assertEqual(sorted(cells_for_track(track)), [(400, -891), (400, -886)])
        self.assertEqual(cells_for_track([]), {})

    def test_build_counts_legs_per_cell_in_window(self):
        leg([[40.05, -89.05, 1200, T0], [40.05, -89.05, 800, T0 + 30]])
        leg([[40.05, -89.05, 500, T0], [40.15, -89.05, 600, T0 + 30]])
        leg([[40.05, -89.05, 100, T0]], days_ago=200)  # outside the window
        leg([[40.05, -89.05, 100, T0]], source="opensky")  # another source
        self.assertEqual(build_coverage("adsblol"), 2)
        cells = {(c.lat_i, c.lon_i): (c.legs, c.lowest_ft) for c in CoverageCell.objects.filter(source="adsblol")}
        self.assertEqual(cells, {(400, -891): (2, 500), (401, -891): (1, 600)})
        build_coverage("adsblol")  # replaces, not adds
        self.assertEqual(CoverageCell.objects.filter(source="adsblol").count(), 2)

    def test_map_toggle_and_data(self):
        user = User.objects.create_user("v", password="x")
        user.groups.add(Group.objects.get(name="Viewer"))
        self.client.force_login(user)
        r = self.client.get(reverse("site_map"))
        self.assertNotIn("coverage", r.context["shown"])  # off by default
        self.assertContains(r, 'name="show" value="coverage" >')
        r = self.client.get(reverse("site_map"), {"f": 1, "show": ["coverage", "base"]})
        self.assertEqual(r.context["shown"], ["base", "coverage"])
        self.assertContains(r, 'name="show" value="coverage" checked')
        self.assertEqual(self.client.get(reverse("coverage_data")).json()["cells"], [])
        for n in range(20):
            CoverageCell.objects.create(source="adsblol", lat_i=400, lon_i=-900 + n, legs=n + 1, lowest_ft=800.4)
        data = self.client.get(reverse("coverage_data")).json()
        self.assertEqual((data["size"], data["top"], len(data["cells"])), (0.1, 20, 20))
        self.assertIn([400, -900, 1, 800], data["cells"])
        self.client.logout()
        self.assertEqual(self.client.get(reverse("coverage_data")).status_code, 302)  # login required
