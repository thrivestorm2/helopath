from datetime import date

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from tracker.models import Aircraft, MonthlyAggregate, Operator, Site


class OperatorPageTests(TestCase):
    def setUp(self):
        self.op = Operator.objects.create(name="Air Evac Lifeteam", parent_company="GMR")
        self.base = Site.objects.create(name="Base 12", site_type=Site.BASE, lat=40, lon=-89)
        self.hosp_a = Site.objects.create(name="Hospital A", lat=40.1, lon=-89.1)
        self.hosp_b = Site.objects.create(name="Hospital B", lat=40.2, lon=-89.2)
        self.a = Aircraft.objects.create(tail_number="N412AE", icao_hex="a4f2c1", status=Aircraft.CONFIRMED,
                                         operator=self.op, base=self.base)
        Aircraft.objects.create(tail_number="N9REJ", icao_hex="a00009", status=Aircraft.REJECTED, operator=self.op)
        for unreviewed in (True, False):
            MonthlyAggregate.objects.create(source="adsblol", operator=self.op, base=self.base,
                                            origin_site=self.hosp_a, destination_site=self.hosp_b,
                                            month=date(2026, 9, 1), label="interfacility", count=7,
                                            mean_confidence=0.9, includes_unreviewed=unreviewed)
            MonthlyAggregate.objects.create(source="adsblol", operator=None, base=None, month=date(2026, 9, 1),
                                            label="unknown", count=3, mean_confidence=0.3,
                                            includes_unreviewed=unreviewed)
        viewer = User.objects.create_user("v", password="x")
        viewer.groups.add(Group.objects.get(name="Viewer"))
        self.client.force_login(viewer)

    def test_dashboard_links_operator_and_base(self):
        r = self.client.get(reverse("dashboard"))
        self.assertContains(r, f'href="{reverse("operator_detail", args=[self.op.pk])}">Air Evac Lifeteam</a>')
        self.assertContains(r, f'href="{reverse("site_detail", args=[self.base.pk])}">Base 12</a>')
        self.assertContains(r, '<span class="muted">(unassigned)</span>')  # no page to link to
        r = self.client.get(reverse("dashboard") + "?confirmed=1")
        self.assertContains(r, f'href="{reverse("operator_detail", args=[self.op.pk])}?confirmed=1"')

    def test_operator_page(self):
        r = self.client.get(reverse("operator_detail", args=[self.op.pk]))
        self.assertContains(r, "Air Evac Lifeteam")
        self.assertContains(r, "Part of GMR")
        self.assertContains(r, "N412AE")
        self.assertNotContains(r, "N9REJ")  # rejected aircraft never shown
        self.assertContains(r, "2026-09")
        self.assertContains(r, "Interfacility")
        self.assertContains(r, f'href="{reverse("site_detail", args=[self.hosp_a.pk])}">Hospital A</a>')
        self.assertContains(r, f'href="{reverse("site_detail", args=[self.base.pk])}">Base 12</a>')
        self.assertNotContains(r, ">Edit<")  # viewers get no edit link

    def test_aircraft_pages_link_operator_and_base(self):
        url = reverse("operator_detail", args=[self.op.pk])
        self.assertContains(self.client.get(reverse("aircraft_list")), f'href="{url}"')
        detail = self.client.get(reverse("aircraft_detail", args=[self.a.pk]))
        self.assertContains(detail, f'href="{url}"')
        self.assertContains(detail, f'href="{reverse("site_detail", args=[self.base.pk])}"')

    def test_rows_are_tappable(self):
        op_url = reverse("operator_detail", args=[self.op.pk])
        self.assertContains(self.client.get(reverse("dashboard")), f'<tr data-href="{op_url}">')
        self.assertContains(self.client.get(reverse("aircraft_list")),
                            f'<tr data-href="{reverse("aircraft_detail", args=[self.a.pk])}">')
        self.assertContains(self.client.get(op_url), f'<tr data-href="{reverse("site_detail", args=[self.base.pk])}">')
        self.assertContains(self.client.get(reverse("dashboard")), "closest('tr[data-href]')")  # the tap handler

    def test_login_required_and_404(self):
        self.assertEqual(self.client.get(reverse("operator_detail", args=[999])).status_code, 404)
        self.client.logout()
        r = self.client.get(reverse("operator_detail", args=[self.op.pk]))
        self.assertEqual(r.status_code, 302)
