from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("datapipeline", "0042_course_completion_downloads"),
    ]

    operations = [
        migrations.AddField(
            model_name="surveycompletioncertificate",
            name="display_snapshot",
            field=models.JSONField(default=dict),
        ),
    ]
