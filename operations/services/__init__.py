"""Сервисный слой приложения ``operations``.

Чистые функции, без HTTP-логики. Используются из view'ов, management-команд,
и могут быть покрыты unit-тестами без поднятия test client.
"""

from . import appointments, billing, notifications, program_wizard, reports, scheduling

__all__ = ["appointments", "billing", "notifications", "program_wizard", "reports", "scheduling"]
