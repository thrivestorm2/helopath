from django.db import migrations


def seed(apps, schema_editor):
    # Requested by the owner: auto-confirm aircraft registered to Air Methods.
    AutoConfirmRule = apps.get_model("tracker", "AutoConfirmRule")
    AutoConfirmRule.objects.get_or_create(registrant_contains="AIR METHODS LLC")


def unseed(apps, schema_editor):
    apps.get_model("tracker", "AutoConfirmRule").objects.filter(
        registrant_contains="AIR METHODS LLC", created_by__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [("tracker", "0005_autoconfirmrule")]
    operations = [migrations.RunPython(seed, unseed)]
