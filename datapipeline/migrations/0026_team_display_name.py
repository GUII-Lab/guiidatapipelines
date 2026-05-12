from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('datapipeline', '0025_align_hci271_schemas_to_pdf'),
    ]

    operations = [
        migrations.AddField(
            model_name='team',
            name='display_name',
            field=models.CharField(blank=True, default='', max_length=100),
        ),
        migrations.AddField(
            model_name='surveyteam',
            name='display_name',
            field=models.CharField(blank=True, default='', max_length=100),
        ),
    ]
