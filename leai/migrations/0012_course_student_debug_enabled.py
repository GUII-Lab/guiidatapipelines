from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("leai", "0011_structured_response_flow")]

    operations = [
        migrations.AddField(
            model_name="course",
            name="student_debug_enabled",
            field=models.BooleanField(default=False),
        ),
    ]
