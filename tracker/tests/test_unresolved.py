from datetime import timedelta

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from tracker.models import Aircraft, LandingCluster, Leg, Site
from tracker.resolution import category_metrics, diagnose, radius_suggestions

NOW = timezone.now() - timedelta(days=2)


class UnresolvedData(TestCase):
    """One leg (or a few) per cause."""

    def setUp(self):
        self.hosp = Site.objects.create(name="Hospital A", site_type=Site.HOSPITAL, lat=40.0, lon=-89.0,
                                        radius_m=300, elevation_ft=600)
        Site.objects.create(name="Hospital Far", site_type=Site.HOSPITAL, lat=40.5, lon=-89.0, radius_m=300)
        self.a = Aircraft.objects.create(tail_number="N1", icao_hex="a00001", status=Aircraft.CONFIRMED,
                                         inferred_base_lat=41.0, inferred_base_lon=-88.0)  # base is not a site
        self.cluster = LandingCluster.objects.create(source="adsblol", lat=40.8, lon=-88.5, landing_count=5,
                                                     distinct_aircraft=2, first_seen=NOW, last_seen=NOW)

        def leg(lat, lon, state=Leg.LANDED, alt=None, site=None):
            Leg.objects.create(hex="a00001", aircraft=self.a, source="adsblol", start_ts=NOW, end_ts=NOW,
                               start_lat=40, start_lon=-89, end_lat=lat, end_lon=lon, end_state=state,
                               end_alt_ft=alt, end_site=site)

        leg(40.0, -89.0, site=self.hosp)                       # resolved
        leg(41.0005, -88.0)                                    # at its base, which is not a site
        for dlat in (0.005, 0.006, 0.007):                     # ~550-780 m north of Hospital A
            leg(40.0 + dlat, -89.0)
        leg(40.8001, -88.5)                                    # repeat spot without a site
        leg(39.2, -90.2)                                       # one-off: likely a scene
        leg(39.3, -90.3, state=Leg.LOST, alt=800)              # lost low away from sites
        leg(39.4, -90.4, state=Leg.LOST, alt=4500)             # lost en route

        self.admin = User.objects.create_user("boss", password="x", is_staff=True)
        self.admin.groups.add(Group.objects.get(name="Admin"))


class UnresolvedTests(UnresolvedData):
    """Each cause lands in the right bucket, and the fixes offered."""

    def test_buckets(self):
        d = diagnose(Leg.objects.all(), "adsblol")
        self.assertEqual((d.total, d.resolved), (9, 1))
        self.assertEqual(dict(d.counts), {"base_not_site": 1, "near_site": 3, "unmatched_spot": 1,
                                          "one_off_landing": 1, "lost_low": 1, "lost_high": 1})
        self.assertEqual(d.fixable, 5)
        self.assertAlmostEqual(d.best_rate, 6 / 9)

    def test_radius_suggestion_stops_short_of_the_next_site(self):
        d = diagnose(Leg.objects.all(), "adsblol")
        [(site, n, suggested, nearest)] = radius_suggestions(d)
        self.assertEqual((site, n), (self.hosp, 3))
        self.assertEqual(suggested, 850)  # 80% of ~556-778 m, rounded up to 50, plus 50
        self.assertIsNone(nearest)  # no other site within 4 km
        Site.objects.create(name="Clinic Next Door", site_type=Site.HOSPITAL, lat=40.0, lon=-89.012)  # ~1 km
        [(_, _, suggested, nearest)] = radius_suggestions(diagnose(Leg.objects.all(), "adsblol"))
        self.assertLessEqual(suggested, nearest / 2)

    def test_page_and_one_tap_radius(self):
        self.client.force_login(self.admin)
        r = self.client.get(reverse("unresolved"))
        self.assertContains(r, "Ended at the aircraft&#x27;s base, which is not a site")
        self.assertContains(r, ">Add as site</a>")
        self.assertContains(r, "site_type=base&amp;lat=41.000000&amp;lon=-88.000000")
        self.assertContains(r, ">Set 850 m</button>")
        self.assertContains(r, f'{reverse("cluster_queue")}#c{self.cluster.pk}')
        self.assertContains(self.client.get(reverse("dashboard")), "Why not higher?")
        r = self.client.post(reverse("set_site_radius", args=[self.hosp.pk]), {"radius_m": 850}, follow=True)
        self.hosp.refresh_from_db()
        self.assertEqual(self.hosp.radius_m, 850)
        self.assertContains(r, "radius 300 m → 850 m")
        from tracker.models import PendingRebuild
        self.assertEqual(list(PendingRebuild.objects.values_list("kind", "reason", "radius_m")),
                         [("partial", "radius of Hospital A", 850)])  # a quick partial rebuild follows
        self.client.post(reverse("set_site_radius", args=[self.hosp.pk]), {"radius_m": 99999})
        self.hosp.refresh_from_db()
        self.assertEqual(self.hosp.radius_m, 850)

    def test_viewers_see_causes_but_cannot_change(self):
        viewer = User.objects.create_user("v", password="x")
        viewer.groups.add(Group.objects.get(name="Viewer"))
        self.client.force_login(viewer)
        r = self.client.get(reverse("unresolved"))
        self.assertContains(r, "Signal lost at altitude")
        self.assertNotContains(r, ">Set 850 m</button>")
        self.assertNotContains(r, ">Add as site</a>")
        self.assertEqual(self.client.post(reverse("set_site_radius", args=[self.hosp.pk]),
                                          {"radius_m": 500}).status_code, 403)


class LegCreateSiteTests(UnresolvedData):
    """"Create site" on each unresolved leg opens the new-site form there and returns to the list."""

    def test_admin_button_per_leg_with_type_name_and_return(self):
        from urllib.parse import parse_qs, urlparse
        self.client.force_login(self.admin)
        r = self.client.get(reverse("unresolved"), {"cat": "base_not_site", "sort": "date"})
        [row] = list(r.context["page"])
        q = parse_qs(urlparse(row.add_site_url).query)
        self.assertEqual(urlparse(row.add_site_url).path, reverse("admin:tracker_site_add"))
        self.assertEqual((q["site_type"], q["lat"], q["lon"], q["name"]),
                         (["base"], ["41.000500"], ["-88.000000"], ["N1 base"]))
        self.assertEqual(q["next"], [reverse("unresolved") + "?cat=base_not_site&sort=date#legs"])
        self.assertContains(r, '>Create site</a>')
        # Other categories open as a hospital helipad to name from the nearby-name buttons.
        r = self.client.get(reverse("unresolved"), {"cat": "lost_low"})
        [row] = list(r.context["page"])
        q = parse_qs(urlparse(row.add_site_url).query, keep_blank_values=True)
        self.assertEqual((q["site_type"], q["name"]), (["hospital_helipad"], [""]))
        self.assertContains(r, "last seen here: check the pin")
        # "Add as site" for a base comes back to its section too.
        self.assertIn("next=" + reverse("unresolved").replace("/", "%2F"), r.context["bases"][0][2])
        # Saving the form returns to the list.
        add = r.context["page"][0].add_site_url
        nxt = parse_qs(urlparse(add).query)["next"][0]
        self.client.force_login(User.objects.create_superuser("root", password="x"))
        resp = self.client.post(add, {"name": "Scene LZ", "site_type": "hospital_helipad", "lat": "39.3",
                                      "lon": "-90.3", "elevation_ft": 0, "radius_m": 300, "origin": "manual",
                                      "is_active": "on", "_save": "Save"})
        self.assertEqual((resp.status_code, resp["Location"]), (302, nxt))
        self.assertTrue(Site.objects.filter(name="Scene LZ").exists())

    def test_viewers_get_no_button(self):
        viewer = User.objects.create_user("v2", password="x")
        viewer.groups.add(Group.objects.get(name="Viewer"))
        self.client.force_login(viewer)
        self.assertNotContains(self.client.get(reverse("unresolved")), ">Create site</a>")


class UnresolvedCategoryTests(UnresolvedData):
    """Per-category metrics and the leg list (same legs as UnresolvedTests)."""

    def test_metrics(self):
        m = {row["key"]: row for row in category_metrics(diagnose(Leg.objects.all(), "adsblol"))}
        self.assertEqual([m[k]["legs"] for k in ("base_not_site", "near_site", "unmatched_spot", "one_off_landing",
                                                 "lost_low", "lost_high")], [1, 3, 1, 1, 1, 1])
        self.assertAlmostEqual(m["near_site"]["share"], 3 / 9)
        self.assertEqual(m["near_site"]["aircraft"], 1)
        self.assertEqual(m["near_site"]["median_nearest"], "667 m")  # middle of ~556, 667, 778 m
        self.assertEqual(m["lost_high"]["median_agl_ft"], 4500 - 650)  # no site nearby: default ground height
        self.assertEqual(m["one_off_landing"]["median_nearest"], "> 25 km")

    def test_list_filter_and_sort_by_category(self):
        self.client.force_login(self.admin)
        r = self.client.get(reverse("unresolved"), {"cat": "near_site"})
        self.assertEqual(r.context["page"].paginator.count, 3)
        self.assertContains(r, "Unresolved legs: Ended just outside a site&#x27;s radius")
        self.assertEqual({row.category for row in r.context["page"]}, {"near_site"})
        r = self.client.get(reverse("unresolved"), {"sort": "category"})
        cats = [row.category for row in r.context["page"]]
        self.assertEqual(cats, ["base_not_site", "near_site", "near_site", "near_site", "unmatched_spot",
                                "one_off_landing", "lost_low", "lost_high"])  # in the table's order
        r = self.client.get(reverse("unresolved"), {"sort": "-category"})
        self.assertEqual([row.category for row in r.context["page"]][0], "lost_high")
        r = self.client.get(reverse("unresolved"), {"sort": "nearest"})
        self.assertEqual([row.category for row in r.context["page"]][:3], ["near_site"] * 3)
        r = self.client.get(reverse("unresolved"), {"sort": "bogus"})
        self.assertEqual(r.context["sort"], "-date")
        self.assertContains(r, 'sort=category#legs">Category</a>')  # the category column header sorts
