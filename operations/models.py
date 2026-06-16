from __future__ import annotations

import uuid
from datetime import time
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import QuerySet, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

ACTIVE_APPOINTMENT_STATUSES = ["proposed", "confirmed", "completed", "reserved"]
ACTION_REQUIRED_BILLING_STATUSES = [
    "completed",
    "cancelled",
    "no_show",
    "rescheduled",
]


class SoftDeleteQuerySet(QuerySet):
    def alive(self) -> SoftDeleteQuerySet:
        return self.filter(archived_at__isnull=True)

    def dead(self) -> SoftDeleteQuerySet:
        return self.filter(archived_at__isnull=False)

    def delete(self) -> tuple[int, dict[str, int]]:
        """Мягкое удаление: выставляет ``archived_at`` без физического DELETE."""
        count = 0
        for obj in self:
            obj.archive()
            count += 1
        return count, {self.model._meta.label: count}

    def hard_delete(self) -> tuple[int, dict[str, int]]:
        return super().delete()

    def restore(self) -> int:
        count = 0
        for obj in self:
            obj.restore()
            count += 1
        return count


class SoftDeleteManager(models.Manager.from_queryset(SoftDeleteQuerySet)):
    """Default manager: возвращает только незаархивированные записи."""

    def get_queryset(self) -> SoftDeleteQuerySet:
        return super().get_queryset().filter(archived_at__isnull=True)


class AllObjectsManager(models.Manager.from_queryset(SoftDeleteQuerySet)):
    """Доступ ко всем записям, включая архивные (для админки и отчётов)."""


class SoftDeleteMixin(models.Model):
    archived_at = models.DateTimeField(_("архивировано"), null=True, blank=True, db_index=True)

    objects: Any = SoftDeleteManager()
    all_objects: Any = AllObjectsManager()

    class Meta:
        abstract = True

    def archive(self) -> None:
        if self.archived_at is None:
            self.archived_at = timezone.now()
            self.save(update_fields=["archived_at", "updated_at"])

    def restore(self) -> None:
        if self.archived_at is not None:
            self.archived_at = None
            self.save(update_fields=["archived_at", "updated_at"])

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField("создано", auto_now_add=True)
    updated_at = models.DateTimeField("обновлено", auto_now=True)

    class Meta:
        abstract = True


class ParentGuardian(TimeStampedModel, SoftDeleteMixin):
    class RelationshipType(models.TextChoices):
        MOTHER = "mother", "Мать"
        FATHER = "father", "Отец"
        GUARDIAN = "guardian", "Опекун"
        GRANDMOTHER = "grandmother", "Бабушка"
        GRANDFATHER = "grandfather", "Дедушка"
        OTHER = "other", "Другое"

    last_name = models.CharField("фамилия", max_length=120)
    first_name = models.CharField("имя", max_length=120)
    middle_name = models.CharField("отчество", max_length=120, blank=True)
    phone = models.CharField("телефон", max_length=40)
    phone_alt = models.CharField("дополнительный телефон", max_length=40, blank=True)
    email = models.EmailField("email", blank=True)
    relationship_type = models.CharField(
        "тип связи",
        max_length=30,
        choices=RelationshipType.choices,
        default=RelationshipType.MOTHER,
    )
    notes = models.TextField("примечания", blank=True)

    class Meta:
        verbose_name = "представитель"
        verbose_name_plural = "представители"
        ordering = ["last_name", "first_name"]

    def __str__(self) -> str:
        return self.full_name

    @property
    def full_name(self) -> str:
        return " ".join(part for part in [self.last_name, self.first_name, self.middle_name] if part)


class Child(TimeStampedModel, SoftDeleteMixin):
    class Status(models.TextChoices):
        ACTIVE = "active", "Активен"
        PAUSED = "paused", "Приостановлен"
        COMPLETED = "completed", "Завершил курс"
        WAITING = "waiting", "Ожидает"

    last_name = models.CharField("фамилия", max_length=120)
    first_name = models.CharField("имя", max_length=120)
    middle_name = models.CharField("отчество", max_length=120, blank=True)
    birth_date = models.DateField("дата рождения", null=True, blank=True)
    phone = models.CharField("телефон получателя", max_length=40, blank=True)
    email = models.EmailField("email получателя", blank=True)
    status = models.CharField("статус", max_length=30, choices=Status.choices, default=Status.ACTIVE)
    primary_parent = models.ForeignKey(
        ParentGuardian,
        verbose_name="основной представитель",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="children",
    )
    diagnosis = models.TextField("диагноз/особенности", blank=True)
    notes = models.TextField("примечания", blank=True)
    color = models.CharField("цветовая метка", max_length=20, default="#00a443")

    class Meta:
        verbose_name = "получатель"
        verbose_name_plural = "получатели"
        ordering = ["last_name", "first_name"]

    def __str__(self) -> str:
        return self.full_name

    @property
    def full_name(self) -> str:
        return " ".join(part for part in [self.last_name, self.first_name, self.middle_name] if part)


class StaffMember(TimeStampedModel, SoftDeleteMixin):
    class Status(models.TextChoices):
        ACTIVE = "active", "Активен"
        VACATION = "vacation", "Отпуск"
        SICK = "sick", "Больничный"
        INACTIVE = "inactive", "Неактивен"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        verbose_name="пользователь",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="staff_profile",
    )
    full_name = models.CharField("ФИО", max_length=200)
    specializations = models.CharField("специализации", max_length=255, blank=True)
    phone = models.CharField("телефон", max_length=40, blank=True)
    email = models.EmailField("email", blank=True)
    status = models.CharField("статус", max_length=30, choices=Status.choices, default=Status.ACTIVE)
    color = models.CharField("цвет", max_length=20, default="#2563eb")
    can_use_mobile = models.BooleanField("доступ к мобильному экрану", default=True)

    class Meta:
        verbose_name = "специалист"
        verbose_name_plural = "специалисты"
        ordering = ["full_name"]

    def __str__(self) -> str:
        return self.full_name


class Service(TimeStampedModel, SoftDeleteMixin):
    class Category(models.TextChoices):
        CONSULTATION = "consultation", "Диагностика/консультация"
        SPEECH = "speech", "Логопед"
        DEFECTOLOGY = "defectology", "Дефектолог"
        PHYSICAL = "physical", "АФК"
        MASSAGE = "massage", "Массаж"
        GROUP = "group", "Группа/присмотр"
        OTHER = "other", "Другое"

    name = models.CharField("название", max_length=160)
    code = models.CharField("код", max_length=40, unique=True)
    category = models.CharField("категория", max_length=40, choices=Category.choices, default=Category.OTHER)
    default_duration_minutes = models.PositiveIntegerField("длительность по умолчанию, мин", default=30)
    default_price = models.DecimalField("цена по умолчанию", max_digits=10, decimal_places=2, default=0)
    is_active = models.BooleanField("активна", default=True)
    color = models.CharField("цвет", max_length=20, default="#16a34a")

    class Meta:
        verbose_name = "услуга"
        verbose_name_plural = "услуги"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Room(TimeStampedModel, SoftDeleteMixin):
    class RoomType(models.TextChoices):
        GYM_BIG = "gym_big", "АФК большой"
        GYM_SMALL = "gym_small", "АФК маленький"
        CABINET = "cabinet", "Кабинет"
        SALING = "saling", "Система Салинг"
        GROUP = "group", "Группа"
        OTHER = "other", "Другое"

    name = models.CharField("название", max_length=160)
    room_type = models.CharField("тип", max_length=40, choices=RoomType.choices, default=RoomType.CABINET)
    capacity = models.PositiveIntegerField("вместимость", default=1)
    is_active = models.BooleanField("активно", default=True)
    color = models.CharField("цвет", max_length=20, default="#f97316")

    class Meta:
        verbose_name = "помещение"
        verbose_name_plural = "помещения"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class FundingSource(TimeStampedModel, SoftDeleteMixin):
    class SourceType(models.TextChoices):
        PERSONAL = "personal", "Личные средства"
        GRANT = "grant", "Грант"
        SPONSOR = "sponsor", "Спонсор"
        CHARITY_FUND = "charity_fund", "Благотворительный фонд"
        MATERNITY_CAPITAL = "maternity_capital", "Материнский капитал"
        CERTIFICATE = "certificate", "Сертификат"
        TEST = "test", "Тестовый источник"

    class TransferPolicy(models.TextChoices):
        NOT_TRANSFERABLE = "not_transferable", "Нельзя передавать"
        WITHIN_CHILD = "within_child", "Можно менять услугу внутри получателя"
        BETWEEN_CHILDREN = "between_children", "Можно передавать между получателями"

    name = models.CharField("название", max_length=200)
    source_type = models.CharField("тип", max_length=40, choices=SourceType.choices)
    starts_on = models.DateField("дата начала", null=True, blank=True)
    ends_on = models.DateField("дата окончания", null=True, blank=True)
    transfer_policy = models.CharField(
        "правило передачи",
        max_length=40,
        choices=TransferPolicy.choices,
        default=TransferPolicy.NOT_TRANSFERABLE,
    )
    notes = models.TextField("примечания", blank=True)

    class Meta:
        verbose_name = "источник финансирования"
        verbose_name_plural = "источники финансирования"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class BalanceAccount(TimeStampedModel, SoftDeleteMixin):
    class Unit(models.TextChoices):
        SESSIONS = "sessions", "Занятия"
        MONEY = "money", "Рубли"

    class ServiceScope(models.TextChoices):
        ANY = "any", "Любые услуги"
        SPECIFIC_SERVICE = "specific_service", "Конкретная услуга"

    class Status(models.TextChoices):
        ACTIVE = "active", "Активен"
        PAUSED = "paused", "Заморожен"
        EXHAUSTED = "exhausted", "Исчерпан"
        EXPIRED = "expired", "Истек"

    child = models.ForeignKey(Child, verbose_name="получатель", on_delete=models.CASCADE, related_name="balance_accounts")
    funding_source = models.ForeignKey(
        FundingSource,
        verbose_name="источник финансирования",
        on_delete=models.PROTECT,
        related_name="balance_accounts",
    )
    unit = models.CharField("единица учета", max_length=20, choices=Unit.choices)
    service_scope = models.CharField(
        "область применения",
        max_length=30,
        choices=ServiceScope.choices,
        default=ServiceScope.ANY,
    )
    service = models.ForeignKey(
        Service,
        verbose_name="услуга",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="balance_accounts",
    )
    initial_amount = models.DecimalField("начальный остаток", max_digits=12, decimal_places=2, default=0)
    valid_from = models.DateField("действует с", null=True, blank=True)
    valid_until = models.DateField("действует до", null=True, blank=True)
    status = models.CharField("статус", max_length=30, choices=Status.choices, default=Status.ACTIVE)
    color = models.CharField("цветовая метка", max_length=20, default="#f59e0b")
    notes = models.TextField("примечания", blank=True)

    class Meta:
        verbose_name = "счет баланса"
        verbose_name_plural = "счета баланса"
        ordering = ["child__last_name", "child__first_name", "funding_source__name"]

    def __str__(self) -> str:
        scope = self.service.name if self.service else self.get_service_scope_display()
        return f"{self.child}: {self.funding_source} / {scope} / {self.get_unit_display()}"

    def clean(self) -> None:
        if self.service_scope == self.ServiceScope.SPECIFIC_SERVICE and not self.service_id:
            raise ValidationError({"service": "Для счета по конкретной услуге нужно выбрать услугу."})
        if self.service_scope == self.ServiceScope.ANY and self.service_id:
            raise ValidationError({"service": "Для счета на любые услуги поле услуги должно быть пустым."})

    @property
    def current_balance(self) -> Decimal:
        total = self.ledger_entries.aggregate(total=Sum("amount"))["total"] or Decimal("0")
        return self.initial_amount + total

    def can_pay_for(self, service: Service) -> bool:
        return self.service_scope == self.ServiceScope.ANY or self.service_id == service.id

    @property
    def warning_level(self) -> str:
        balance = self.current_balance
        if balance <= 0:
            return "exhausted"
        if self.unit == self.Unit.SESSIONS:
            if balance <= 1:
                return "critical"
            if balance <= 3:
                return "warning"
            if balance <= 7:
                return "notice"
            return "ok"
        if self.service_id and self.service.default_price:
            price = self.service.default_price
            if balance <= price:
                return "critical"
            if balance <= price * Decimal("3"):
                return "warning"
            if balance <= price * Decimal("7"):
                return "notice"
        return "ok"

    @property
    def is_low_balance(self) -> bool:
        return self.warning_level in {"exhausted", "critical", "warning", "notice"}

    @property
    def warning_label(self) -> str:
        return {
            "exhausted": "исчерпан",
            "critical": "1 занятие",
            "warning": "до 3",
            "notice": "до 7",
            "ok": "ок",
        }.get(self.warning_level, "ок")


class TreatmentProgram(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        ACTIVE = "active", "Активна"
        PAUSED = "paused", "Пауза"
        COMPLETED = "completed", "Завершена"
        CANCELLED = "cancelled", "Отменена"

    child = models.ForeignKey(Child, verbose_name="получатель", on_delete=models.CASCADE, related_name="treatment_programs")
    title = models.CharField("название", max_length=200)
    consultation = models.ForeignKey(
        "Appointment",
        verbose_name="первичная консультация",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_programs",
    )
    status = models.CharField("статус", max_length=30, choices=Status.choices, default=Status.DRAFT)
    starts_on = models.DateField("дата начала", null=True, blank=True)
    ends_on = models.DateField("дата окончания", null=True, blank=True)
    color = models.CharField("цвет", max_length=20, default="#1267f2")
    notes = models.TextField("примечания", blank=True)

    class Meta:
        verbose_name = "программа занятий"
        verbose_name_plural = "программы занятий"
        ordering = ["child__last_name", "starts_on", "title"]

    def __str__(self) -> str:
        return f"{self.child}: {self.title}"


class ProgramBlock(TimeStampedModel):
    class Status(models.TextChoices):
        PLANNED = "planned", "Запланирован"
        SCHEDULED = "scheduled", "Расписан"
        IN_PROGRESS = "in_progress", "Идёт"
        COMPLETED = "completed", "Завершён"
        CANCELLED = "cancelled", "Отменён"

    program = models.ForeignKey(TreatmentProgram, verbose_name="программа", on_delete=models.CASCADE, related_name="blocks")
    number = models.PositiveIntegerField("номер блока", default=1)
    title = models.CharField("название блока", max_length=200)
    service = models.ForeignKey(Service, verbose_name="услуга", on_delete=models.PROTECT)
    staff_member = models.ForeignKey(StaffMember, verbose_name="специалист", on_delete=models.PROTECT, null=True, blank=True)
    planned_sessions = models.PositiveIntegerField("план занятий", default=1)
    balance_account = models.ForeignKey(
        BalanceAccount,
        verbose_name="счёт оплаты",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="program_blocks",
    )
    status = models.CharField("статус", max_length=30, choices=Status.choices, default=Status.PLANNED)
    color = models.CharField("цвет", max_length=20, default="#b71b55")
    notes = models.TextField("примечания", blank=True)

    class Meta:
        verbose_name = "блок программы"
        verbose_name_plural = "блоки программ"
        ordering = ["program", "number"]
        constraints = [
            models.UniqueConstraint(fields=["program", "number"], name="unique_program_block_number"),
        ]

    def __str__(self) -> str:
        return f"{self.program} / {self.number}. {self.title}"

    @property
    def scheduled_count(self) -> int:
        return self.appointments.exclude(status=Appointment.Status.CANCELLED).count()

    @property
    def paid_count(self) -> int:
        return self.appointments.filter(billing_decision=Appointment.BillingDecision.CHARGE).count()


class AppointmentSeries(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        ACTIVE = "active", "Активна"
        COMPLETED = "completed", "Завершена"
        CANCELLED = "cancelled", "Отменена"

    child = models.ForeignKey(Child, verbose_name="получатель", on_delete=models.CASCADE, related_name="appointment_series")
    service = models.ForeignKey(Service, verbose_name="услуга", on_delete=models.PROTECT)
    staff_member = models.ForeignKey(StaffMember, verbose_name="специалист", on_delete=models.PROTECT)
    room = models.ForeignKey(Room, verbose_name="помещение", on_delete=models.PROTECT, null=True, blank=True)
    program_block = models.ForeignKey(
        ProgramBlock,
        verbose_name="блок программы",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="series",
    )
    title = models.CharField("название серии", max_length=200)
    start_date = models.DateField("дата начала")
    end_date = models.DateField("дата окончания")
    days_of_week = models.CharField("дни недели", max_length=80, help_text="Например: ПН,СР,ПТ")
    time = models.TimeField("время")
    duration_minutes = models.PositiveIntegerField("длительность, мин")
    status = models.CharField("статус", max_length=30, choices=Status.choices, default=Status.DRAFT)

    class Meta:
        verbose_name = "серия занятий"
        verbose_name_plural = "серии занятий"
        ordering = ["start_date", "time"]

    def __str__(self) -> str:
        return self.title

    DAY_MAP = {
        "ПН": 0,
        "ВТ": 1,
        "СР": 2,
        "ЧТ": 3,
        "ПТ": 4,
        "СБ": 5,
        "ВС": 6,
    }

    def materialize_series(self) -> int:
        """Create missing Appointment instances for this series.

        Iterates ``start_date`` → ``end_date`` and on every day matching
        ``days_of_week`` creates an ``Appointment`` if one does not already
        exist for the same series, date and time.

        Returns the number of newly created appointments.
        """
        import datetime as dtmod

        from django.utils import timezone

        days_raw = [d.strip().upper() for d in self.days_of_week.split(",")]
        weekdays = {self.DAY_MAP[d] for d in days_raw if d in self.DAY_MAP}
        if not weekdays:
            return 0

        tz = timezone.get_current_timezone()
        created = 0
        delta = self.end_date - self.start_date
        overlaps = set(
            Appointment.objects.filter(
                status__in=ACTIVE_APPOINTMENT_STATUSES,
                child_id=self.child_id,
                staff_member_id=self.staff_member_id,
                starts_at__date__gte=self.start_date,
                starts_at__date__lte=self.end_date,
            ).values_list("starts_at", flat=True)
        )
        for offset in range(delta.days + 1):
            day = self.start_date + dtmod.timedelta(days=offset)
            if day.weekday() not in weekdays:
                continue
            series_time = self.time
            starts_at = dtmod.datetime.combine(day, series_time).replace(tzinfo=tz)
            ends_at = starts_at + dtmod.timedelta(minutes=self.duration_minutes)
            if starts_at in overlaps:
                continue
            sequence_number = None
            if self.program_block_id:
                sequence_number = (
                    Appointment.objects.filter(program_block=self.program_block)
                    .exclude(status=Appointment.Status.CANCELLED)
                    .count()
                    + 1
                )
            Appointment.objects.create(
                child=self.child,
                staff_member=self.staff_member,
                service=self.service,
                room=self.room,
                starts_at=starts_at,
                ends_at=ends_at,
                status=Appointment.Status.CONFIRMED,
                series=self,
                program_block=self.program_block,
                sequence_number=sequence_number,
            )
            created += 1
        return created


class Appointment(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        PROPOSED = "proposed", "Предложено"
        CONFIRMED = "confirmed", "Согласовано"
        COMPLETED = "completed", "Проведено"
        CANCELLED = "cancelled", "Отменено"
        NO_SHOW = "no_show", "Неявка"
        RESCHEDULED = "rescheduled", "Перенесено"
        RESERVED = "reserved", "Бронь"

    class AttendanceStatus(models.TextChoices):
        UNKNOWN = "unknown", "Не отмечено"
        ATTENDED = "attended", "Пришел"
        MISSED = "missed", "Не пришел"
        EXCUSED = "excused", "Уважительная причина"

    class BillingDecision(models.TextChoices):
        UNDECIDED = "undecided", "Не решено"
        CHARGE = "charge", "Списать"
        DO_NOT_CHARGE = "do_not_charge", "Не списывать"

    child = models.ForeignKey(Child, verbose_name="получатель", on_delete=models.CASCADE, related_name="appointments")
    staff_member = models.ForeignKey(StaffMember, verbose_name="специалист", on_delete=models.PROTECT, related_name="appointments")
    service = models.ForeignKey(Service, verbose_name="услуга", on_delete=models.PROTECT, related_name="appointments")
    room = models.ForeignKey(Room, verbose_name="помещение", on_delete=models.PROTECT, null=True, blank=True, related_name="appointments")
    starts_at = models.DateTimeField("начало")
    ends_at = models.DateTimeField("окончание")
    status = models.CharField("статус", max_length=30, choices=Status.choices, default=Status.CONFIRMED)
    attendance_status = models.CharField(
        "посещение",
        max_length=30,
        choices=AttendanceStatus.choices,
        default=AttendanceStatus.UNKNOWN,
    )
    billing_decision = models.CharField(
        "решение по списанию",
        max_length=30,
        choices=BillingDecision.choices,
        default=BillingDecision.UNDECIDED,
    )
    billing_account = models.ForeignKey(
        BalanceAccount,
        verbose_name="счет списания",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="appointments",
    )
    source_appointment = models.ForeignKey(
        "self",
        verbose_name="исходное занятие",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rescheduled_to",
    )
    series = models.ForeignKey(
        AppointmentSeries,
        verbose_name="серия",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="appointments",
    )
    program_block = models.ForeignKey(
        ProgramBlock,
        verbose_name="блок программы",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="appointments",
    )
    sequence_number = models.PositiveIntegerField("номер в блоке", null=True, blank=True)
    staff_availability_override = models.BooleanField("назначено вне графика специалиста", default=False)
    staff_availability_override_reason = models.TextField("причина назначения вне графика", blank=True)
    admin_note = models.TextField("заметка администратора", blank=True)
    specialist_note = models.TextField("заметка специалиста", blank=True)
    specialist_marked_at = models.DateTimeField("специалист отметил", null=True, blank=True)

    class Meta:
        verbose_name = "занятие"
        verbose_name_plural = "занятия"
        ordering = ["starts_at"]

    def __str__(self) -> str:
        local_start = timezone.localtime(self.starts_at)
        return f"{local_start:%d.%m.%Y %H:%M} - {self.child} / {self.service}"

    def clean(self) -> None:
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValidationError({"ends_at": "Окончание должно быть позже начала."})
        if self.status in ACTIVE_APPOINTMENT_STATUSES and self.starts_at and self.ends_at:
            self._validate_no_overlap()
            self._validate_staff_availability()
        if self.billing_account_id:
            if self.billing_account.child_id != self.child_id:
                raise ValidationError({"billing_account": "Счет должен принадлежать этому получателю."})
            if not self.billing_account.can_pay_for(self.service):
                raise ValidationError({"billing_account": "Счет не подходит для этой услуги."})
        if self.program_block_id:
            if self.program_block.program.child_id != self.child_id:
                raise ValidationError({"program_block": "Блок программы должен принадлежать этому получателю."})
            if self.program_block.service_id != self.service_id:
                raise ValidationError({"program_block": "Блок программы должен соответствовать услуге занятия."})
        if self.billing_decision == self.BillingDecision.CHARGE and not self.billing_account_id:
            raise ValidationError({"billing_account": "Для списания нужно выбрать счет баланса."})

    def save(self, *args: object, validate_schedule: bool = True, **kwargs: object) -> None:
        if not self.pk and self.program_block_id and not self.sequence_number:
            self.sequence_number = (
                Appointment.objects.filter(program_block_id=self.program_block_id)
                .exclude(status=Appointment.Status.CANCELLED)
                .count()
                + 1
            )
        if validate_schedule:
            self.full_clean(exclude={"specialist_note"} if not self.pk else None)
        super().save(*args, **kwargs)

    def _validate_no_overlap(self) -> None:
        """Кросс-БД проверка отсутствия пересечений.

        На PostgreSQL дополнительно работает DB-уровневый EXCLUDE constraint
        (см. миграцию ``0004_pg_only_constraints``), но Python-проверка
        покрывает оба бэкенда и даёт внятные сообщения об ошибках в формах.
        """
        qs = Appointment.objects.filter(
            status__in=ACTIVE_APPOINTMENT_STATUSES,
            starts_at__lt=self.ends_at,
            ends_at__gt=self.starts_at,
        )
        if self.pk:
            qs = qs.exclude(pk=self.pk)
        messages: list[str] = []
        if self.child_id and qs.filter(child_id=self.child_id).exists():
            other = qs.filter(child_id=self.child_id).first()
            when = other.starts_at.strftime("%d.%m %H:%M") if other else ""
            messages.append(
                f"у получателя уже есть занятие в это время ({when})" if when
                else "у получателя уже есть занятие в это время"
            )
        if self.staff_member_id and qs.filter(staff_member_id=self.staff_member_id).exists():
            messages.append("специалист уже занят в это время")
        if self.room_id:
            room_capacity = max(getattr(self.room, "capacity", 1) or 1, 1)
            if qs.filter(room_id=self.room_id).count() >= room_capacity:
                messages.append("кабинет уже занят в это время")
        if messages:
            raise ValidationError("Конфликт расписания: " + ", ".join(messages) + ".")

    def _validate_staff_availability(self) -> None:
        if not self.staff_member_id or not self.starts_at or not self.ends_at:
            return

        local_start = timezone.localtime(self.starts_at)
        local_end = timezone.localtime(self.ends_at)
        day = local_start.date()
        if local_end.date() != day:
            raise ValidationError("Недоступность специалиста: занятие должно помещаться в один день.")
        if self.staff_availability_override:
            return

        if TimeOffRequest.objects.filter(
            staff_member_id=self.staff_member_id,
            status=TimeOffRequest.Status.APPROVED,
            starts_on__lte=day,
            ends_on__gte=day,
        ).exists():
            raise ValidationError("Недоступность специалиста: согласован отпуск/отгул.")

        windows = list(
            StaffAvailability.objects.filter(
                staff_member_id=self.staff_member_id,
                weekday=day.weekday(),
                is_active=True,
            ).order_by("starts_at")
        )
        start_time = local_start.time().replace(second=0, microsecond=0)
        end_time = local_end.time().replace(second=0, microsecond=0)
        if not windows:
            if time(9, 0) <= start_time and end_time <= time(18, 0):
                return
            raise ValidationError("Недоступность специалиста: время вне базового окна 09:00-18:00.")

        if not any(window.starts_at <= start_time and end_time <= window.ends_at for window in windows):
            raise ValidationError("Недоступность специалиста: время вне рабочего графика.")

    @property
    def duration_minutes(self) -> int:
        return int((self.ends_at - self.starts_at).total_seconds() // 60)


class LedgerEntry(TimeStampedModel):
    class EntryType(models.TextChoices):
        CREDIT = "credit", "Пополнение"
        DEBIT = "debit", "Списание"
        CORRECTION = "correction", "Корректировка"
        TRANSFER = "transfer", "Перенос"

    account = models.ForeignKey(
        BalanceAccount,
        verbose_name="счет",
        on_delete=models.CASCADE,
        related_name="ledger_entries",
    )
    entry_type = models.CharField("тип операции", max_length=30, choices=EntryType.choices)
    amount = models.DecimalField("сумма операции", max_digits=12, decimal_places=2)
    appointment = models.ForeignKey(
        Appointment,
        verbose_name="занятие",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ledger_entries",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="создал",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    reason = models.TextField("основание", blank=True)

    class Meta:
        verbose_name = "операция по балансу"
        verbose_name_plural = "операции по балансам"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.get_entry_type_display()} {self.amount} ({self.account})"

    def clean(self) -> None:
        if self.entry_type == self.EntryType.CREDIT and self.amount < 0:
            raise ValidationError({"amount": "Пополнение должно быть положительным."})
        if self.entry_type == self.EntryType.DEBIT and self.amount > 0:
            raise ValidationError({"amount": "Списание должно быть отрицательным."})
        if self.appointment_id and self.appointment.child_id != self.account.child_id:
            raise ValidationError({"appointment": "Занятие должно относиться к получателю счета."})


class Note(TimeStampedModel):
    class Priority(models.TextChoices):
        LOW = "low", "Низкий"
        MEDIUM = "medium", "Средний"
        HIGH = "high", "Высокий"
        URGENT = "urgent", "Срочно"

    child = models.ForeignKey(
        Child,
        verbose_name="получатель",
        on_delete=models.CASCADE,
        related_name="journal_notes",
        null=True,
        blank=True,
    )
    parent = models.ForeignKey(
        ParentGuardian,
        verbose_name="представитель",
        on_delete=models.CASCADE,
        related_name="journal_notes",
        null=True,
        blank=True,
    )
    staff_member = models.ForeignKey(StaffMember, verbose_name="специалист", on_delete=models.SET_NULL, null=True, blank=True)
    appointment = models.ForeignKey(Appointment, verbose_name="занятие", on_delete=models.SET_NULL, null=True, blank=True)
    title = models.CharField("заголовок", max_length=200)
    text = models.TextField("текст")
    priority = models.CharField("приоритет", max_length=20, choices=Priority.choices, default=Priority.MEDIUM)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name="автор", on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        verbose_name = "заметка"
        verbose_name_plural = "заметки"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.title


class AppointmentConfirmation(TimeStampedModel):
    class TargetType(models.TextChoices):
        SPECIALIST = "specialist", "Специалист"
        REPRESENTATIVE = "representative", "Представитель"
        RECIPIENT = "recipient", "Получатель"

    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает ответа"
        CONFIRMED = "confirmed", "Подтверждено"
        DECLINED = "declined", "Отклонено"

    class DeliveryStatus(models.TextChoices):
        PENDING = "pending", "Готово к отправке"
        SENT = "sent", "Отправлено"
        FAILED = "failed", "Ошибка отправки"

    appointment = models.ForeignKey(
        Appointment,
        verbose_name="занятие",
        on_delete=models.CASCADE,
        related_name="confirmations",
    )
    target_type = models.CharField("кому отправлено", max_length=30, choices=TargetType.choices)
    representative = models.ForeignKey(
        ParentGuardian,
        verbose_name="представитель",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="appointment_confirmations",
    )
    email = models.EmailField("email")
    token = models.UUIDField("токен подтверждения", default=uuid.uuid4, unique=True, editable=False)
    subject = models.CharField("тема письма", max_length=200)
    message = models.TextField("текст письма")
    status = models.CharField("статус ответа", max_length=30, choices=Status.choices, default=Status.PENDING)
    delivery_status = models.CharField(
        "статус отправки",
        max_length=30,
        choices=DeliveryStatus.choices,
        default=DeliveryStatus.PENDING,
    )
    delivery_error = models.TextField("ошибка отправки", blank=True)
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="отправил",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sent_appointment_confirmations",
    )
    sent_at = models.DateTimeField("отправлено", null=True, blank=True)
    responded_at = models.DateTimeField("ответ получен", null=True, blank=True)
    response_note = models.TextField("комментарий к ответу", blank=True)

    class Meta:
        verbose_name = "подтверждение занятия"
        verbose_name_plural = "подтверждения занятий"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.appointment} -> {self.email}"


class StaffAvailability(TimeStampedModel):
    class Weekday(models.IntegerChoices):
        MONDAY = 0, "Понедельник"
        TUESDAY = 1, "Вторник"
        WEDNESDAY = 2, "Среда"
        THURSDAY = 3, "Четверг"
        FRIDAY = 4, "Пятница"
        SATURDAY = 5, "Суббота"
        SUNDAY = 6, "Воскресенье"

    staff_member = models.ForeignKey(
        StaffMember,
        verbose_name="специалист",
        on_delete=models.CASCADE,
        related_name="availability_windows",
    )
    weekday = models.PositiveSmallIntegerField("день недели", choices=Weekday.choices)
    starts_at = models.TimeField("начало")
    ends_at = models.TimeField("окончание")
    is_active = models.BooleanField("активно", default=True)
    note = models.CharField("комментарий", max_length=255, blank=True)

    class Meta:
        verbose_name = "рабочее окно специалиста"
        verbose_name_plural = "рабочие окна специалистов"
        ordering = ["staff_member__full_name", "weekday", "starts_at"]

    def __str__(self) -> str:
        return f"{self.staff_member}: {self.get_weekday_display()} {self.starts_at:%H:%M}-{self.ends_at:%H:%M}"

    def clean(self) -> None:
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValidationError({"ends_at": "Окончание должно быть позже начала."})


class TimeOffRequest(TimeStampedModel):
    class RequestType(models.TextChoices):
        VACATION = "vacation", "Отпуск"
        DAY_OFF = "day_off", "Отгул"
        SICK = "sick", "Больничный"
        SCHEDULE_CHANGE = "schedule_change", "Изменение графика"
        OTHER = "other", "Другое"

    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает решения"
        APPROVED = "approved", "Согласовано"
        REJECTED = "rejected", "Отклонено"
        CANCELLED = "cancelled", "Отменено"

    staff_member = models.ForeignKey(
        StaffMember,
        verbose_name="специалист",
        on_delete=models.CASCADE,
        related_name="time_off_requests",
    )
    request_type = models.CharField("тип заявки", max_length=30, choices=RequestType.choices)
    starts_on = models.DateField("дата начала")
    ends_on = models.DateField("дата окончания")
    reason = models.TextField("причина/комментарий", blank=True)
    status = models.CharField("статус", max_length=30, choices=Status.choices, default=Status.PENDING)
    admin_note = models.TextField("комментарий администратора", blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="решение принял",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="decided_time_off_requests",
    )
    decided_at = models.DateTimeField("решение принято", null=True, blank=True)

    class Meta:
        verbose_name = "заявка специалиста"
        verbose_name_plural = "заявки специалистов"
        ordering = ["status", "starts_on", "staff_member__full_name"]

    def __str__(self) -> str:
        return f"{self.staff_member}: {self.get_request_type_display()} {self.starts_on:%d.%m}-{self.ends_on:%d.%m}"

    def clean(self) -> None:
        if self.starts_on and self.ends_on and self.ends_on < self.starts_on:
            raise ValidationError({"ends_on": "Дата окончания не может быть раньше даты начала."})


class Recommendation(TimeStampedModel):
    class Category(models.TextChoices):
        TREATMENT_METHOD = "treatment_method", "Методика работы"
        HOME_TASK = "home_task", "Домашнее задание"
        EQUIPMENT = "equipment", "Оснащение / материалы"
        OBSERVATION = "observation", "Наблюдение"
        OTHER = "other", "Другое"

    child = models.ForeignKey(
        Child, verbose_name="получатель", on_delete=models.CASCADE, related_name="recommendations"
    )
    staff_member = models.ForeignKey(
        StaffMember,
        verbose_name="специалист",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recommendations",
    )
    appointment = models.ForeignKey(
        Appointment,
        verbose_name="занятие",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recommendations",
    )
    category = models.CharField(
        "категория", max_length=30, choices=Category.choices, default=Category.OTHER
    )
    title = models.CharField("заголовок", max_length=200)
    body = models.TextField("содержание")
    due_on = models.DateField("выполнить до", null=True, blank=True)
    is_acknowledged = models.BooleanField("принято к сведению", default=False)
    acknowledged_at = models.DateTimeField("когда отмечено", null=True, blank=True)
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="отметил",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="acknowledged_recommendations",
    )

    class Meta:
        verbose_name = "рекомендация"
        verbose_name_plural = "рекомендации"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["child", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.title} — {self.child}"

    def clean(self) -> None:
        if self.is_acknowledged and self.acknowledged_at is None:
            self.acknowledged_at = timezone.now()
        if not self.is_acknowledged:
            self.acknowledged_at = None
            self.acknowledged_by = None

    def acknowledge(self, *, actor: Any | None = None) -> None:
        self.is_acknowledged = True
        self.acknowledged_at = timezone.now()
        self.acknowledged_by = actor
        self.save(update_fields=["is_acknowledged", "acknowledged_at", "acknowledged_by", "updated_at"])


def document_upload_path(instance: Document, filename: str) -> str:
    return f"documents/{instance.child_id}/{filename}"


class Document(TimeStampedModel):
    class Category(models.TextChoices):
        MEDICAL_REPORT = "medical_report", "Медицинское заключение"
        CONSENT = "consent", "Согласие"
        IPR = "ipr", "ИПР / ИПРА"
        CONTRACT = "contract", "Договор"
        OTHER = "other", "Прочее"

    child = models.ForeignKey(
        Child, verbose_name="получатель", on_delete=models.CASCADE, related_name="documents"
    )
    category = models.CharField(
        "категория", max_length=30, choices=Category.choices, default=Category.OTHER
    )
    title = models.CharField("название", max_length=200)
    file = models.FileField("файл", upload_to=document_upload_path)
    issued_on = models.DateField("выдан", null=True, blank=True)
    expires_on = models.DateField("действует до", null=True, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="загрузил",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_documents",
    )
    note = models.TextField("комментарий", blank=True)

    class Meta:
        verbose_name = "документ"
        verbose_name_plural = "документы"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["child", "category"])]

    def __str__(self) -> str:
        return f"{self.title} — {self.child}"

    def clean(self) -> None:
        if self.expires_on and self.issued_on and self.expires_on < self.issued_on:
            raise ValidationError({"expires_on": "Срок действия не может быть раньше даты выдачи."})

    @property
    def is_expired(self) -> bool:
        return self.expires_on is not None and self.expires_on < timezone.localdate()

    @property
    def expires_soon(self) -> bool:
        if self.expires_on is None:
            return False
        return timezone.localdate() <= self.expires_on <= timezone.localdate() + timezone.timedelta(days=30)


class Consent(TimeStampedModel):
    class ConsentType(models.TextChoices):
        PERSONAL_DATA = "personal_data", "Обработка персональных данных"
        PHOTO_VIDEO = "photo_video", "Фото- и видеосъёмка"
        EXTERNAL_SPECIALIST = "external_specialist", "Внешний специалист"
        OTHER = "other", "Иное"

    child = models.ForeignKey(
        Child, verbose_name="получатель", on_delete=models.CASCADE, related_name="consents"
    )
    consent_type = models.CharField("тип согласия", max_length=30, choices=ConsentType.choices)
    signed_on = models.DateField("подписано", null=True, blank=True)
    expires_on = models.DateField("действует до", null=True, blank=True)
    document = models.ForeignKey(
        Document,
        verbose_name="документ",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="consents",
    )
    note = models.TextField("комментарий", blank=True)

    class Meta:
        verbose_name = "согласие"
        verbose_name_plural = "согласия"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["child", "consent_type"])]

    def __str__(self) -> str:
        return f"{self.get_consent_type_display()} — {self.child}"

    def clean(self) -> None:
        if self.expires_on and self.signed_on and self.expires_on < self.signed_on:
            raise ValidationError({"expires_on": "Срок действия не может быть раньше даты подписания."})

    @property
    def is_valid(self) -> bool:
        if self.signed_on is None:
            return False
        if self.expires_on is None:
            return True
        return self.expires_on >= timezone.localdate()


class Payment(TimeStampedModel):
    class Method(models.TextChoices):
        BANK_TRANSFER = "bank_transfer", "Банковский перевод"
        CASH = "cash", "Наличные"
        GRANT_TRANSFER = "grant_transfer", "Перевод гранта"
        SPONSOR_TRANSFER = "sponsor_transfer", "Перевод спонсора"
        OTHER = "other", "Другое"

    balance_account = models.ForeignKey(
        BalanceAccount,
        verbose_name="счёт",
        on_delete=models.PROTECT,
        related_name="payments",
    )
    amount = models.DecimalField("сумма пополнения", max_digits=12, decimal_places=2)
    method = models.CharField("способ", max_length=30, choices=Method.choices)
    paid_at = models.DateField("дата оплаты", default=timezone.localdate)
    reference = models.CharField("номер платёжки / комментарий", max_length=200, blank=True)
    comment = models.TextField("комментарий", blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="создал",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_payments",
    )

    class Meta:
        verbose_name = "платёж"
        verbose_name_plural = "платежи"
        ordering = ["-paid_at", "-created_at"]
        indexes = [models.Index(fields=["balance_account", "-paid_at"])]

    def __str__(self) -> str:
        return f"+{self.amount} {self.balance_account}"

    def clean(self) -> None:
        if self.amount is None or self.amount <= 0:
            raise ValidationError({"amount": "Сумма пополнения должна быть положительной."})


class Discount(TimeStampedModel):
    child = models.ForeignKey(
        Child, verbose_name="получатель", on_delete=models.CASCADE, related_name="discounts"
    )
    service = models.ForeignKey(
        Service, verbose_name="услуга", on_delete=models.CASCADE, null=True, blank=True, related_name="discounts"
    )
    percentage = models.DecimalField("процент скидки", max_digits=5, decimal_places=2)
    valid_from = models.DateField("действует с", null=True, blank=True)
    valid_until = models.DateField("действует до", null=True, blank=True)
    is_active = models.BooleanField("активна", default=True)
    note = models.TextField("комментарий", blank=True)

    class Meta:
        verbose_name = "скидка"
        verbose_name_plural = "скидки"
        ordering = ["child__last_name", "child__first_name"]

    def __str__(self) -> str:
        return f"{self.child}: {self.percentage}%"


class Certificate(TimeStampedModel):
    class CertificateType(models.TextChoices):
        MATERNITY_CAPITAL = "maternity_capital", "Материнский капитал"
        REGIONAL = "regional", "Региональный сертификат"
        SPONSOR = "sponsor", "Спонсорский сертификат"
        OTHER = "other", "Прочее"

    child = models.ForeignKey(
        Child, verbose_name="получатель", on_delete=models.CASCADE, related_name="certificates"
    )
    certificate_type = models.CharField("тип", max_length=30, choices=CertificateType.choices)
    number = models.CharField("номер", max_length=100, blank=True)
    total_amount = models.DecimalField("полная сумма", max_digits=12, decimal_places=2)
    remaining_amount = models.DecimalField("остаток", max_digits=12, decimal_places=2)
    valid_from = models.DateField("действует с", null=True, blank=True)
    valid_until = models.DateField("действует до", null=True, blank=True)
    note = models.TextField("комментарий", blank=True)

    class Meta:
        verbose_name = "сертификат"
        verbose_name_plural = "сертификаты"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.get_certificate_type_display()} №{self.number} — {self.child}"

    @property
    def is_available(self) -> bool:
        return self.remaining_amount > 0
