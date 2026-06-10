from django.apps import AppConfig


class OperationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "operations"
    verbose_name = "Операционная работа"

    def ready(self) -> None:
        from auditlog.registry import auditlog

        from .models import (
            Appointment,
            BalanceAccount,
            Child,
            Consent,
            Document,
            FundingSource,
            ParentGuardian,
            Payment,
            Recommendation,
            Room,
            Service,
            StaffMember,
            TimeOffRequest,
        )

        tracked = [
            Child,
            ParentGuardian,
            StaffMember,
            Service,
            Room,
            FundingSource,
            BalanceAccount,
            Appointment,
            Recommendation,
            Document,
            Consent,
            Payment,
            TimeOffRequest,
        ]
        for model in tracked:
            auditlog.register(
                model,
                exclude_fields=["updated_at", "created_at"],
            )
