"""Existing data into service periods.

- Aircraft with an 'Active until' date: that period is closed into the history and a new one starts the next
  day with the same operator and program but no base or call sign (the same as saving it now).
- Existing missions get the operator, program and base their aircraft has (until the next rebuild redoes them
  by period)."""

from datetime import timedelta

from django.db import migrations


def forwards(apps, schema_editor):
    Aircraft = apps.get_model("tracker", "Aircraft")
    ServicePeriod = apps.get_model("tracker", "ServicePeriod")
    Mission = apps.get_model("tracker", "Mission")
    for a in Aircraft.objects.all():  # the values in effect so far (before any period is closed below)
        Mission.objects.filter(aircraft=a).update(operator_id=a.operator_id, program_id=a.program_id,
                                                  base_id=a.base_id or a.inferred_base_id)
    for a in Aircraft.objects.filter(active_to__isnull=False):
        base_id = a.base_id or a.inferred_base_id
        ServicePeriod.objects.create(
            aircraft=a, start=a.active_from, end=a.active_to, base_id=base_id,
            base_inferred=a.base_id is None and base_id is not None,
            base_lat=None if base_id else a.inferred_base_lat, base_lon=None if base_id else a.inferred_base_lon,
            call_sign=a.call_sign, operator_id=a.operator_id, program_id=a.program_id)
        # Missions after the period ended belong to the new one, which has no base yet.
        Mission.objects.filter(aircraft=a, start_ts__date__gt=a.active_to).update(base_id=None)
        a.active_from, a.active_to = a.active_to + timedelta(days=1), None
        a.base_id = a.inferred_base_id = None
        a.inferred_base_lat = a.inferred_base_lon = None
        a.call_sign, a.call_sign_reviewed = "", False
        a.save()


class Migration(migrations.Migration):
    dependencies = [("tracker", "0023_service_periods")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
