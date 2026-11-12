from django.apps import AppConfig


class CirculationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "circulation"

    def ready(self):
        from finance import corrections

        from .services import member_has_active_loan

        # A token cannot be refunded while a book is outstanding (PRD 6.6 / 6.7).
        if member_has_active_loan not in corrections.OUTSTANDING_LOAN_CHECKS:
            corrections.OUTSTANDING_LOAN_CHECKS.append(member_has_active_loan)
