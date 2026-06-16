"""Re-export всех view-функций и хелперов из модулей пакета.

``operations/urls.py`` импортирует ``from . import views`` и обращается к именам ниже.
"""

from ._common import csrf_failure, is_admin_user, safe_next_url
from .appointments import (
    appointment_billing,
    appointment_cancel,
    appointment_create,
    appointment_detail,
    appointment_detail_context,
    appointment_edit,
    appointment_move,
)
from .balances import balance_account_create, balance_account_delete, balance_account_edit, balances
from .confirmations import appointment_confirmation_public, appointment_send_confirmation
from .consents import consent_create, consent_list
from .dashboard import (
    dashboard,
    low_balance_accounts,
    needs_attendance_queryset,
    needs_billing_queryset,
    needs_transfer_queryset,
    work_queue,
)
from .documents import document_create, document_list
from .payments import payment_create
from .programs import (
    program_block_create,
    program_block_schedule_wizard,
    program_block_transfer_funds,
    program_create,
)
from .recipients import (
    recipient_contract_pdf,
    recipient_create,
    recipient_detail,
    recipient_edit,
    recipient_list,
    representative_create,
    representative_edit,
)
from .recommendations import recommendation_acknowledge, recommendation_create, recommendation_list
from .reports import grant_report, staff_mass_reschedule, staff_timesheet
from .schedule import schedule
from .scheduling_helpers import suggested_transfer_slots
from .specialist import (
    mark_appointment,
    specialist_action_staff,
    specialist_home,
    specialist_home_redirect,
    staff_availability_create,
    staff_availability_toggle,
    time_off_request_create,
    time_off_request_decide,
)
from .tomorrow import tomorrow

__all__ = [
    "appointment_billing",
    "appointment_cancel",
    "appointment_confirmation_public",
    "appointment_create",
    "appointment_detail",
    "appointment_detail_context",
    "appointment_edit",
    "appointment_move",
    "appointment_send_confirmation",
    "balance_account_create",
    "balance_account_delete",
    "balance_account_edit",
    "balances",
    "consent_create",
    "consent_list",
    "csrf_failure",
    "dashboard",
    "document_create",
    "document_list",
    "grant_report",
    "is_admin_user",
    "low_balance_accounts",
    "mark_appointment",
    "needs_attendance_queryset",
    "needs_billing_queryset",
    "needs_transfer_queryset",
    "payment_create",
    "program_block_create",
    "program_block_schedule_wizard",
    "program_block_transfer_funds",
    "program_create",
    "recipient_contract_pdf",
    "recipient_create",
    "recipient_detail",
    "recipient_edit",
    "recipient_list",
    "recommendation_acknowledge",
    "recommendation_create",
    "recommendation_list",
    "representative_create",
    "representative_edit",
    "safe_next_url",
    "schedule",
    "specialist_action_staff",
    "specialist_home",
    "specialist_home_redirect",
    "staff_availability_create",
    "staff_availability_toggle",
    "staff_mass_reschedule",
    "staff_timesheet",
    "suggested_transfer_slots",
    "time_off_request_create",
    "time_off_request_decide",
    "tomorrow",
    "work_queue",
]
