from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from tracker.models import Aircraft, OpsTask, Operator, Site


class ReviewQueueTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("boss", password="x", is_staff=True)
        self.admin.groups.add(Group.objects.get(name="Admin"))
        self.client.force_login(self.admin)
        Site.objects.bulk_create([Site(name=f"Hospital {i}", site_type=Site.HOSPITAL, lat=40 + i / 1000, lon=-89,
                                       city="Springfield", state="IL") for i in range(300)])
        now = timezone.now()
        Aircraft.objects.bulk_create([Aircraft(tail_number=f"N{i}AB", icao_hex=f"a{i:05x}", discovered_at=now,
                                               operator_hint="AIR EVAC EMS INC" if i % 2 else "")
                                      for i in range(60)])
        self.a = Aircraft.objects.order_by("-discovered_at", "pk").first()

    def test_page_is_small_and_paginated(self):
        r = self.client.get(reverse("review_queue"))
        self.assertContains(r, "60 discovered aircraft")
        self.assertEqual(r.content.count(b'class="card review"'), 25)
        # Site and operator suggestions appear once per page, not once per card.
        self.assertEqual(r.content.count(b"<option"), 300 + 1)
        self.assertLess(len(r.content), 150_000)
        self.assertContains(r, "AIR EVAC EMS INC")

    def test_operator_typed_by_name_is_created_once(self):
        url = reverse("review_action", args=[self.a.pk])
        self.client.post(url, {"action": "operator", "operator_name": "  Air  Evac Lifeteam "})
        other = Aircraft.objects.exclude(pk=self.a.pk).first()
        self.client.post(reverse("review_action", args=[other.pk]),
                         {"action": "operator", "operator_name": "Air Evac Lifeteam"})
        self.assertEqual(Operator.objects.count(), 1)
        self.a.refresh_from_db()
        self.assertEqual(self.a.operator.name, "Air Evac Lifeteam")
        self.client.post(url, {"action": "operator", "operator_name": ""})
        self.a.refresh_from_db()
        self.assertIsNone(self.a.operator)

    def test_base_picked_from_suggestion_label(self):
        site = Site.objects.get(name="Hospital 7")
        url = reverse("review_action", args=[self.a.pk])
        self.client.post(url, {"action": "set_base", "base_label": f"Hospital 7 · Springfield, IL #{site.pk}"})
        self.a.refresh_from_db()
        self.assertEqual(self.a.base, site)
        r = self.client.post(url, {"action": "set_base", "base_label": "somewhere made up"}, follow=True)
        self.assertContains(r, "Pick the base from the suggestions list")
        self.a.refresh_from_db()
        self.assertEqual(self.a.base, site)  # unchanged by an unrecognised entry
        r = self.client.get(reverse("review_queue"))
        self.assertContains(r, f'value="Hospital 7 · Springfield, IL #{site.pk}"')

    def test_decisions_share_one_queued_refresh_and_return_to_same_page(self):
        ids = list(Aircraft.objects.order_by("pk").values_list("pk", flat=True)[:3])
        for pk, action in zip(ids, ["confirm", "reject", "confirm"]):
            r = self.client.post(reverse("review_action", args=[pk]), {"action": action, "next": "/review/?page=2"})
            self.assertEqual(r["Location"], "/review/?page=2")
        self.assertEqual(OpsTask.objects.filter(kind=OpsTask.REFRESH, status=OpsTask.QUEUED).count(), 1)
        r = self.client.post(reverse("review_action", args=[ids[0]]), {"action": "confirm", "next": "https://evil.example/"})
        self.assertEqual(r["Location"], reverse("review_queue"))
