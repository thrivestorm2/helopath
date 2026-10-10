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
        self.assertEqual([(s["origin_site__name"], s["n"]) for s in flows["senders"]],
                         [("Hospital B", 5), ("Hospital D", 1)])
        self.assertEqual([(s["destination_site__name"], s["n"]) for s in flows["receivers"]], [("Hospital C", 4)])
        self.assertContains(r, "Patients received and sent")
        self.assertContains(r, f'<a href="{reverse("site_detail", args=[self.b.pk])}">Hospital B</a>')
        self.assertContains(r, "5 visits where the direction is unknown are not counted")
        self.assertContains(r, "/routes/map/?origin=none&amp;dest=%d&amp;label=scene" % self.a.pk)  # scene map
        flows = self.client.get(reverse("site_detail", args=[self.a.pk]) + "?confirmed=1").context["flows"]
        self.assertEqual((flows["received"], flows["sent"]), (1, 0))
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
