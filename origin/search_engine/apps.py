from django.apps import AppConfig


class SearchEngineConfig(AppConfig):
    name = "origin.search_engine"
    label = "search_engine"
    default_auto_field = "django.db.models.BigAutoField"
    verbose_name = "Search Engine"

    def ready(self):
        # Wire the Django-aware cost recorder into the neutral seam in
        # `llm/spend.py`. That module deliberately imports no Django, so
        # the LLM adapters stay importable without a database; this is
        # the one place the two halves are joined. Until it runs the
        # recorder is a no-op, which is exactly what we want during
        # migrations and management commands that never touch an LLM.
        from origin.search_engine import spend_recorder  # noqa: PLC0415

        spend_recorder.install()
