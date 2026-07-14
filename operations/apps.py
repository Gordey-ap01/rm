from django.apps import AppConfig


class OperationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "operations"
    verbose_name = "Операционная работа"

    def ready(self) -> None:
        from auditlog.registry import auditlog

        from .models import (
            Appointment,
            AppointmentConfirmation,
            AppointmentParticipant,
            AppointmentRescheduleChain,
            AppointmentReschedulePlan,
            AppointmentRescheduleStep,
            AppointmentRescheduleStepDependency,
            AppointmentRoomOverride,
            AppointmentStaffAssignment,
            BalanceAccount,
            Child,
            Consent,
            Document,
            FinancialIntegrityCheckRun,
            FinancialIntegrityFinding,
            FundingServiceQuota,
            FundingSource,
            FundingStaffAllocation,
            GrantRecipientAllocation,
            LedgerEntry,
            ParentGuardian,
            Payment,
            PayrollAccrual,
            PayrollSheet,
            PayrollSheetLine,
            ProgramBlock,
            Recommendation,
            Room,
            Service,
            StaffAvailability,
            StaffCompensationRule,
            StaffMember,
            TimeOffRequest,
            TreatmentProgram,
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
            AppointmentParticipant,
            AppointmentStaffAssignment,
            AppointmentRoomOverride,
            AppointmentRescheduleChain,
            AppointmentReschedulePlan,
            AppointmentRescheduleStep,
            AppointmentRescheduleStepDependency,
            AppointmentConfirmation,
            LedgerEntry,
            TreatmentProgram,
            ProgramBlock,
            FundingServiceQuota,
            FundingStaffAllocation,
            GrantRecipientAllocation,
            StaffAvailability,
            StaffCompensationRule,
            FinancialIntegrityCheckRun,
            FinancialIntegrityFinding,
            PayrollAccrual,
            PayrollSheet,
            PayrollSheetLine,
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
