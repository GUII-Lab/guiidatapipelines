release: python manage.py migrate
web: gunicorn guiidatapipelines.wsgi --log-file -
worker: python manage.py process_leai_jobs
