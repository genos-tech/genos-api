from django.apps import AppConfig


class OriginConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "origin"

    def ready(self):
        from django.conf import settings

        # Audit of operator commands in production (MIP-2260); a no-op elsewhere.
        from origin.services import operator_audit

        operator_audit.install()

        # Same import-for-side-effect rule as the signals below: the
        # @register() decorators attach the deploy-time config checks.
        from origin import checks  # noqa: F401
        from origin.services import jwt_backend

        # Dual HS256/RS256 session-token backend (MIP-2243). Inert until a switch is set;
        # a bad JWT_* combination fails the boot here rather than at the first sign-in.
        jwt_backend.install(settings)

        # Importing the module is enough — the @receiver decorators in
        # task_signals.py / cache_invalidation.py register the handlers as a
        # side effect.
        from origin.signals import (
            billing_signals,  # noqa: F401
            cache_invalidation,  # noqa: F401
            change_feed_signals,  # noqa: F401
            dependency_signals,  # noqa: F401
            email_suppression_signals,  # noqa: F401
            inbox_signals,  # noqa: F401
            note_role_signals,  # noqa: F401
            note_satellite_signals,  # noqa: F401
            pm_channel_signals,  # noqa: F401
            task_signals,  # noqa: F401
            team_search_signals,  # noqa: F401
            thread_summary_signals,  # noqa: F401
            todo_push_signals,  # noqa: F401
        )
