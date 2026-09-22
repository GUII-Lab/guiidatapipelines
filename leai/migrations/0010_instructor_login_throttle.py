from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("leai", "0009_product_usage_event"),
    ]

    operations = [
        migrations.CreateModel(
            name="InstructorLoginThrottle",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("key_digest", models.CharField(max_length=64, unique=True)),
                ("window_start", models.DateTimeField(db_index=True)),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
            ],
        ),
        migrations.AddConstraint(
            model_name="instructorloginthrottle",
            constraint=models.CheckConstraint(
                check=models.Q(attempts__lte=120),
                name="leai_login_throttle_attempts_bounded",
            ),
        ),
    ]
