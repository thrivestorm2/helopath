from django.db import migrations


class Migration(migrations.Migration):
    """Separate from 0009 so PostgreSQL does not alter the table in the transaction that filled aircraft.program."""

    dependencies = [
        ("tracker", "0009_programs"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="aircraft",
            name="program_name",
        ),
    ]
