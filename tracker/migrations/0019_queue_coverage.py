"""Queue one refresh so the worker builds the coverage map now rather than with the next nightly update."""

from django.db import migrations


def queue(apps, schema_editor):
    OpsTask = apps.get_model("tracker", "OpsTask")
    Leg = apps.get_model("tracker", "Leg")
    if Leg.objects.exists() and not OpsTask.objects.filter(kind="refresh", status="queued").exists():
        OpsTask.objects.create(kind="refresh")


class Migration(migrations.Migration):
    dependencies = [("tracker", "0018_coverage_cell")]
    operations = [migrations.RunPython(queue, migrations.RunPython.noop)]
