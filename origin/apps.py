from django.apps import AppConfig


class OriginConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "origin"

    def ready(self):
        # Importing the module is enough — the @receiver decorators in
        # task_signals.py / cache_invalidation.py register the handlers as a
        # side effect.
        from origin.signals import task_signals  # noqa: F401
        from origin.signals import cache_invalidation  # noqa: F401
