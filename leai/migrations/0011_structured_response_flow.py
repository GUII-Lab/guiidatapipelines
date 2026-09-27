from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("leai", "0010_instructor_login_throttle")]

    operations = [
        migrations.AddField(
            model_name="responsesession",
            name="flow_state",
            field=models.JSONField(default=dict),
        ),
        migrations.RunSQL(
            sql="ALTER TABLE leai_responsesession ALTER COLUMN flow_state SET DEFAULT '{}'::jsonb",
            reverse_sql="ALTER TABLE leai_responsesession ALTER COLUMN flow_state DROP DEFAULT",
        ),
    ]
