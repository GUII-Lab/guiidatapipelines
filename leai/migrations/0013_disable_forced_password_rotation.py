from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("leai", "0012_course_student_debug_enabled")]

    operations = [
        migrations.AlterField(
            model_name="instructoraccount",
            name="must_change_password",
            field=models.BooleanField(default=False),
        ),
    ]
