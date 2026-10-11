"""Clear Facebook / Instagram usernames that are a reserved path word (e.g. "people", saved from a
facebook.com/people/<name>/<id> link before such links were read correctly). The page cannot be recovered from the
word, so the link must be pasted again; the cleared records are printed in the deploy log."""

from django.db import migrations


def clear_reserved(apps, schema_editor):
    from tracker.social import NOT_A_PAGE, SITES
    for model in ("Aircraft", "Program", "Operator", "Site"):
        Model = apps.get_model("tracker", model)
        for obj in Model.objects.exclude(facebook="", instagram=""):
            cleared = [f for f in SITES if getattr(obj, f).lower() in NOT_A_PAGE]
            if cleared:
                for f in cleared:
                    setattr(obj, f, "")
                obj.save(update_fields=cleared)
                label = getattr(obj, "name", "") or getattr(obj, "tail_number", obj.pk)
                print(f"  cleared {', '.join(cleared)} on {model} {label}: paste the page link again")


class Migration(migrations.Migration):
    dependencies = [("tracker", "0027_site_social")]
    operations = [migrations.RunPython(clear_reserved, migrations.RunPython.noop)]
