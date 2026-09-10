from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("datapipeline", "0043_certificate_display_snapshot"),
    ]

    operations = [
        migrations.AddField(
            model_name="feedbackmessage",
            name="form_schema_id",
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name="feedbackmessage",
            name="form_schema_version",
            field=models.CharField(blank=True, max_length=40, null=True),
        ),
        migrations.AddField(
            model_name="feedbackmessage",
            name="form_section_id",
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name="feedbackmessage",
            name="form_field_id",
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name="feedbackmessage",
            name="form_field_label",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="feedbackmessage",
            name="form_response_phase",
            field=models.CharField(blank=True, max_length=16, null=True),
        ),
    ]
