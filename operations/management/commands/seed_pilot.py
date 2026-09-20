from __future__ import annotations

import os
from datetime import date, time, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from operations.models import (
    Appointment,
    BalanceAccount,
    Child,
    FundingSource,
    ProgramBlock,
    Room,
    Service,
    StaffMember,
    TreatmentProgram,
)
from operations.services import program_series

PILOT_DATABASE_NAME = "rm_pilot_training"
PILOT_MARKER_GROUP = "RM_PILOT_SEED_V1"
PILOT_INITIALIZATION_LOCK = 7_310_199_715_100_001
DIRECTOR_GROUP = "Руководители"


def _iso_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise CommandError("--date должен иметь формат YYYY-MM-DD.") from exc
    if parsed.isoformat() != value:
        raise CommandError("--date должен иметь формат YYYY-MM-DD.")
    return parsed


class Command(BaseCommand):
    help = "Создает безопасный вымышленный набор данных отдельного учебного пилота."

    def add_arguments(self, parser):
        parser.add_argument(
            "--date",
            required=True,
            help="Учебный день в формате YYYY-MM-DD.",
        )

    def handle(self, *args, **options):
        training_date = options["date"]
        if not isinstance(training_date, date):
            training_date = _iso_date(training_date)

        self._validate_environment()

        with transaction.atomic():
            self._acquire_initialization_lock()
            if Group.objects.filter(name=PILOT_MARKER_GROUP).exists():
                self.stdout.write("Учебная база уже инициализирована; изменения сохранены.")
                return

            if self._database_contains_unknown_data():
                raise CommandError(
                    "База не содержит маркер учебного пилота, но уже содержит данные; "
                    "наполнение отменено."
                )

            password = os.environ.get("RM_PILOT_PASSWORD")
            if not password:
                raise CommandError(
                    "Для первой инициализации задайте непустой RM_PILOT_PASSWORD."
                )

            self._create_baseline(training_date, password)
            Group.objects.create(name=PILOT_MARKER_GROUP)

        self.stdout.write(
            self.style.SUCCESS(
                f"Учебная база создана для {training_date.isoformat()}. "
                "Пользователи: admin, director, specialist1, specialist2."
            )
        )

    def _validate_environment(self) -> None:
        if os.environ.get("RM_PILOT_MODE") != "1":
            raise CommandError("Команда доступна только при RM_PILOT_MODE=1.")
        if connection.vendor != "postgresql":
            raise CommandError("Учебное наполнение разрешено только для PostgreSQL.")
        configured_name = str(connection.settings_dict.get("NAME", ""))
        if configured_name != PILOT_DATABASE_NAME:
            raise CommandError(
                f"Имя базы должно быть ровно {PILOT_DATABASE_NAME}."
            )
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database()")
            actual_name = cursor.fetchone()[0]
        if actual_name != PILOT_DATABASE_NAME:
            raise CommandError(
                f"Фактически подключенная база должна быть ровно {PILOT_DATABASE_NAME}."
            )

    def _acquire_initialization_lock(self) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                [PILOT_INITIALIZATION_LOCK],
            )

    def _database_contains_unknown_data(self) -> bool:
        return any(
            model._base_manager.exists()
            for model in apps.get_models()
            if model not in {ContentType, Permission}
            and model._meta.managed
            and not model._meta.proxy
        )

    def _create_baseline(self, training_date: date, password: str) -> None:
        user_model = get_user_model()
        administrator = user_model.objects.create_user(
            username="admin",
            password=password,
            is_staff=True,
            is_superuser=False,
        )
        director = user_model.objects.create_user(
            username="director",
            password=password,
            is_staff=False,
            is_superuser=False,
        )
        director_group = Group.objects.create(name=DIRECTOR_GROUP)
        director.groups.add(director_group)

        specialists = []
        for number, (full_name, specialization, color) in enumerate(
            (
                ("Учебный Специалист Первый", "Групповые занятия", "#2563eb"),
                ("Учебный Специалист Второй", "Ассистент группы", "#16a34a"),
            ),
            start=1,
        ):
            user = user_model.objects.create_user(
                username=f"specialist{number}",
                password=password,
            )
            specialists.append(
                StaffMember.objects.create(
                    user=user,
                    full_name=full_name,
                    specializations=specialization,
                    color=color,
                )
            )

        service = Service.objects.create(
            name="Учебная групповая услуга",
            code="PILOT-GROUP",
            category=Service.Category.GROUP,
            default_duration_minutes=45,
            default_price=Decimal("1200.00"),
            color="#64748b",
        )
        room = Room.objects.create(
            name="Учебный групповой кабинет",
            room_type=Room.RoomType.GROUP,
            capacity=4,
            allow_group_sessions=True,
            limit_staff_count=True,
            max_staff_count=2,
            limit_recipient_count=True,
            max_recipient_count=4,
            color="#64748b",
        )
        funding = FundingSource.objects.create(
            name="Учебные личные средства",
            source_type=FundingSource.SourceType.PERSONAL,
            transfer_policy=FundingSource.TransferPolicy.NOT_TRANSFERABLE,
            notes="Вымышленный источник учебного пилота.",
        )

        blocks = []
        for number, first_name in enumerate(("Первый", "Второй"), start=1):
            child = Child.objects.create(
                last_name="Учебный",
                first_name=first_name,
                birth_date=date(2018, number, number),
                diagnosis="Вымышленный учебный пример.",
                notes="Не является реальным получателем.",
                color="#00a443",
            )
            account = BalanceAccount.objects.create(
                child=child,
                funding_source=funding,
                unit=BalanceAccount.Unit.SESSIONS,
                service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
                service=service,
                initial_amount=Decimal("2.00"),
                valid_from=training_date,
                valid_until=training_date + timedelta(days=30),
                notes="Вымышленный счет учебного пилота.",
            )
            program = TreatmentProgram.objects.create(
                child=child,
                title=f"Учебная программа: {first_name.lower()}",
                status=TreatmentProgram.Status.ACTIVE,
                starts_on=training_date,
                ends_on=training_date + timedelta(days=30),
                notes="Вымышленная программа учебного пилота.",
            )
            blocks.append(
                ProgramBlock.objects.create(
                    program=program,
                    number=1,
                    title="Учебный групповой каскад",
                    service=service,
                    staff_member=specialists[0],
                    planned_sessions=1,
                    balance_account=account,
                    notes="Исходный каскад для ручной репетиции.",
                )
            )

        preview = program_series.preview_group_series(
            blocks=blocks,
            staff_members=specialists,
            room=room,
            title="Учебная группа первого выезда",
            start_date=training_date,
            end_date=training_date,
            weekdays={training_date.weekday()},
            start_time=time(10, 0),
            duration_minutes=45,
            default_appointment_status=Appointment.Status.PROPOSED,
        )
        result = program_series.create_group_series(
            preview,
            operation_key=uuid5(
                NAMESPACE_URL,
                f"rm-pilot-training:{training_date.isoformat()}",
            ),
            actor=administrator,
        )
        if result.created_count != 1 or result.skipped_count:
            raise CommandError("Не удалось создать исходное групповое занятие пилота.")
