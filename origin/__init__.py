# Marks `origin` as a regular package (not an implicit namespace package).
# Without this, `origin.__file__` is None, which breaks `manage.py test
# origin.tests` (unittest's discovery does os.path.abspath(module.__file__)).
# App config is still provided by origin/apps.py:OriginConfig.
