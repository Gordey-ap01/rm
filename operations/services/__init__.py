"""Сервисный слой приложения ``operations``.

Чистые функции, без HTTP-логики. Используются из view'ов, management-команд,
и могут быть покрыты unit-тестами без поднятия test client.
"""

from . import (
    appointments,
    billing,
    financial_facts,
    import_preview,
    notifications,
    payroll,
    program_wizard,
    reports,
    rescheduling_plans,
    scheduling,
)

__all__ = [
    "appointments",
    "billing",
    "financial_facts",
    "import_preview",
    "notifications",
    "payroll",
    "program_wizard",
    "reports",
    "rescheduling_plans",
    "scheduling",
]
