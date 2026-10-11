"""Queue one refresh, which merges sites that share an FAA ID, when any do."""

from collections import Counter

from django.db import migrations


def queue(apps, schema_editor):
    Site = apps.get_model("tracker", "Site")
    OpsTask = apps.get_model("tracker", "OpsTask")
    ids = Counter(f.strip().upper() for f in Site.objects.exclude(faa_id="").values_list("faa_id", flat=True))
    if any(n > 1 for n in ids.values()) and not OpsTask.objects.filter(kind="refresh", status="queued").exists():
        OpsTask.objects.create(kind="refresh")


class Migration(migrations.Migration):
    dependencies = [("tracker", "0021_queue_faa_seed")]
    operations = [migrations.RunPython(queue, migrations.RunPython.noop)]
