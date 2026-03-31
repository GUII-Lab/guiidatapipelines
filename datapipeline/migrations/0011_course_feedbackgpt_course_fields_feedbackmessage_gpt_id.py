from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('datapipeline', '0010_image'),
    ]

    operations = [
        migrations.CreateModel(
            name='Course',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('course_id', models.SlugField(unique=True)),
                ('course_name', models.CharField(max_length=200)),
                ('instructor_name', models.CharField(max_length=100)),
                ('password', models.CharField(max_length=100)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.AddField(
            model_name='feedbackmessage',
            name='gpt_id',
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='feedbackgpt',
            name='course',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='surveys', to='datapipeline.course'),
        ),
        migrations.AddField(
            model_name='feedbackgpt',
            name='week_number',
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='feedbackgpt',
            name='survey_label',
            field=models.CharField(blank=True, default='', max_length=200),
        ),
    ]
