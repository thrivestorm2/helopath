"""Social pages are stored as usernames: rename the fields and convert the links saved so far."""

from django.core.exceptions import ValidationError
from django.db import migrations, models


def to_usernames(apps, schema_editor):
    from tracker.social import normalize
    for model in ("Aircraft", "Program", "Operator"):
        Model = apps.get_model("tracker", model)
        for obj in Model.objects.exclude(facebook="", instagram=""):
            for field in ("facebook", "instagram"):
                try:
                    setattr(obj, field, normalize(field, getattr(obj, field)))
                except ValidationError:
                    setattr(obj, field, "")  # not a page link (cannot happen for values saved through the form)
            obj.save(update_fields=["facebook", "instagram"])


def field(label, kind):
    return models.CharField(f"{label} username", max_length=120, blank=True, help_text=kind)


FB = ("e.g. osflifeflight. Pasting the page's link is fine: only the username is kept. Used by Look it up.")
IG = ("e.g. osflifeflight. Pasting the profile link or @username is fine: only the username is kept. "
      "Used by Look it up.")


class Migration(migrations.Migration):
    dependencies = [("tracker", "0025_social_pages")]
    operations = [
        *[op for model in ("aircraft", "program", "operator") for op in (
            migrations.RenameField(model, "facebook_url", "facebook"),
            migrations.RenameField(model, "instagram_url", "instagram"),
            migrations.AlterField(model, "facebook", field("Facebook", FB)),
            migrations.AlterField(model, "instagram", field("Instagram", IG)),
        )],
        migrations.RunPython(to_usernames, migrations.RunPython.noop),
    ]
