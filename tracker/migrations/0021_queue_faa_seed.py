"""Queue one 'Seed FAA sites' task so the nationwide FAA facility table (for FAA ID look-ups) is filled."""

from django.db import migrations


def queue(apps, schema_editor):
    OpsTask = apps.get_model("tracker", "OpsTask")
    Site = apps.get_model("tracker", "Site")
    if Site.objects.filter(origin="faa_seed").exists() and not OpsTask.objects.filter(
            kind="seed_sites", status__in=["queued", "running"]).exists():
        OpsTask.objects.create(kind="seed_sites")


class Migration(migrations.Migration):
    dependencies = [("tracker", "0020_faa_facility")]
    operations = [migrations.RunPython(queue, migrations.RunPython.noop)]
