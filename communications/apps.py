from django.apps import AppConfig


class CommunicationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "communications"

    def ready(self):
        from members import services as member_services

        from .services import redact_member_notifications

        # Erase a member's notification data in the same transaction as the anonymisation.
        if redact_member_notifications not in member_services.ANONYMISATION_HOOKS:
            member_services.ANONYMISATION_HOOKS.append(redact_member_notifications)
