release: python manage.py prepare_leai_database && python manage.py migrate && python manage.py verify_leai_environment --expect "$LEAI_ENV"
web: gunicorn guiidatapipelines.wsgi --log-file -
