import uuid

from django.db import migrations, models
import django.db.models.deletion


def enable_parsed_document_downloads_for_existing_courses(apps, _schema_editor):
    Course = apps.get_model('datapipeline', 'Course')
    Course.objects.all().update(parsed_document_download_enabled=True)


def disable_parsed_document_downloads_for_existing_courses(apps, _schema_editor):
    Course = apps.get_model('datapipeline', 'Course')
    Course.objects.all().update(parsed_document_download_enabled=False)


class Migration(migrations.Migration):

    dependencies = [
        ('datapipeline', '0041_add_cmpm80k_pdf_ingest_profile'),
    ]

    operations = [
        migrations.AddField(
            model_name='course',
            name='completion_certificate_enabled',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='course',
            name='parsed_document_download_enabled',
            field=models.BooleanField(default=False),
        ),
        migrations.CreateModel(
            name='SurveyCompletionCertificate',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('session_id', models.CharField(max_length=100)),
                ('code', models.CharField(max_length=19, unique=True)),
                ('issued_at', models.DateTimeField(auto_now_add=True)),
                ('progress_snapshot', models.JSONField(default=dict)),
                ('survey', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='completion_certificates', to='datapipeline.feedbackgpt')),
            ],
            options={
                'indexes': [models.Index(fields=['survey', 'code'], name='datapipelin_survey__c9b50c_idx')],
                'constraints': [models.UniqueConstraint(fields=('survey', 'session_id'), name='unique_completion_certificate_per_survey_session')],
            },
        ),
        migrations.RunPython(
            enable_parsed_document_downloads_for_existing_courses,
            disable_parsed_document_downloads_for_existing_courses,
        ),
    ]
