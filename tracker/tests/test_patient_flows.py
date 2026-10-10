from datetime import date

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from tracker.models import MonthlyAggregate, Site


class PatientFlowTests(TestCase):
    def setUp(self):
        mk = lambda name: Site.objects.create(name=name, site_type=Site.HOSPITAL, lat=40, lon=-89)
        self.a, self.b, self.c, self.d = mk("Hospital A"), mk("Hospital B"), mk("Hospital C"), mk("Hospital D")

        def agg(origin, dest, label, count, unreviewed=True, month=date(2026, 9, 1)):
            MonthlyAggregate.objects.create(source="adsblol", origin_site=origin, destination_site=dest, label=label,
                                            month=month, count=count, mean_confidence=0.9,
                                            includes_unreviewed=unreviewed)

        agg(self.b, self.a, "interfacility", 3)
        agg(self.b, self.a, "interfacility", 2, month=date(2026, 8, 1))  # months add up
        agg(self.d, self.a, "interfacility", 1)
        agg(None, self.a, "scene", 2)
        agg(self.a, self.c, "interfacility", 4)
        agg(None, self.a, "single_hospital_unresolved", 5)  # direction unknown: not counted
        agg(None, None, "reposition", 9)
        agg(self.b, self.a, "interfacility", 1, unreviewed=False)  # the confirmed-only rows
        viewer = User.objects.create_user("v", password="x")
        viewer.groups.add(Group.objects.get(name="Viewer"))
        self.client.force_login(viewer)

    def test_hospital_page_counts_received_and_sent(self):
        r = self.client.get(reverse("site_detail", args=[self.a.pk]))
        flows = r.context["flows"]
        self.assertEqual((flows["received"], flows["from_scenes"], flows["from_hospitals"], flows["sent"]),
                         (8, 2, 6, 4))
        self.assertEqual(flows["months"], [date(2026, 8, 1), date(2026, 9, 1)])
        # Rows per facility (scenes are one row), per month, ranked by total; each links to its page.
        b_link = reverse("site_detail", args=[self.b.pk])
        self.assertEqual([(name, counts, avg, tot) for name, _, counts, avg, tot in flows["received_rows"]],
                         [("Hospital B", [2, 3], 2.5, 5), ("Scenes", [0, 2], 1, 2), ("Hospital D", [0, 1], 0.5, 1)])
        self.assertEqual(flows["received_rows"][0][1], b_link)
        self.assertIn("/routes/map/?origin=none", flows["received_rows"][1][1])  # scenes row opens the map
        self.assertEqual([(name, counts, avg, tot) for name, _, counts, avg, tot in flows["sent_rows"]],
                         [("Hospital C", [0, 4], 2, 4)])
        self.assertContains(r, ">Avg</th>")
        self.assertContains(r, '<th class="n" title="September 2026">Sep</th>')
        self.assertContains(r, f'<tr data-href="{b_link}"><td class="fac"><a href="{b_link}">Hospital B</a>')
        self.assertContains(r, f'<tr class="name-row" data-href="{b_link}">')  # phones: name on its own line
        self.assertContains(r, "5 visits where the direction is unknown are not counted")
        flows = self.client.get(reverse("site_detail", args=[self.a.pk]) + "?confirmed=1").context["flows"]
        self.assertEqual((flows["received"], flows["sent"]), (1, 0))
        self.assertTrue(flows["received_rows"][0][1].endswith("?confirmed=1"))  # links keep the view
        flows = self.client.get(reverse("site_detail", args=[self.c.pk])).context["flows"]
        self.assertEqual((flows["received"], flows["sent"]), (4, 0))

    def test_sites_list_columns_and_sort(self):
        r = self.client.get(reverse("site_list") + "?sort=-received")
        rows = {s.name: (s.received, s.sent) for s in r.context["page"]}
        self.assertEqual(rows, {"Hospital A": (8, 4), "Hospital B": (0, 5), "Hospital C": (4, 0),
                                "Hospital D": (0, 1)})
        self.assertEqual([s.name for s in r.context["page"]][:2], ["Hospital A", "Hospital C"])
        r = self.client.get(reverse("site_list") + "?sort=-sent")
        self.assertEqual(r.context["page"][0].name, "Hospital B")
        self.assertContains(r, ">Received</a>")
        r = self.client.get(reverse("site_list") + "?confirmed=1")
        rows = {s.name: (s.received, s.sent) for s in r.context["page"]}
        self.assertEqual(rows["Hospital A"], (1, 0))
        self.assertEqual(rows["Hospital B"], (0, 1))

    def test_latest_six_months_shown_average_and_total_over_all(self):
        e = Site.objects.create(name="Hospital E", site_type=Site.HOSPITAL, lat=40, lon=-89)
        for m in range(1, 9):  # Jan..Aug 2026, m patients each month from Hospital B
            MonthlyAggregate.objects.create(source="adsblol", origin_site=self.b, destination_site=e,
                                            label="interfacility", month=date(2026, m, 1), count=m,
                                            mean_confidence=0.9, includes_unreviewed=True)
        r = self.client.get(reverse("site_detail", args=[e.pk]))
        flows = r.context["flows"]
        self.assertEqual(flows["months"], [date(2026, m, 1) for m in range(3, 9)])  # latest 6 of 8
        name, _, counts, avg, tot = flows["received_rows"][0]
        self.assertEqual((name, counts, tot), ("Hospital B", [3, 4, 5, 6, 7, 8], 36))
        self.assertEqual(avg, 36 / 8)  # all 8 months
        self.assertContains(r, "Showing the latest 6 of 8 months")
        self.assertContains(r, "<td class=\"n\">4.5</td>")
