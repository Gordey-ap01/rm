from __future__ import annotations

import unicodedata
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import models, transaction
from django.db.models import F, Max, Q, QuerySet, Sum
from django.utils import timezone
from django.utils.text import get_valid_filename
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


class ImmutableHistoryQuerySet(QuerySet):
    def bulk_create(
        self,
        objs: list[models.Model],
        batch_size: int | None = None,
        ignore_conflicts: bool = False,
        update_conflicts: bool = False,
        update_fields: list[str] | None = None,
        unique_fields: list[str] | None = None,
    ) -> list[models.Model]:
        raise ValidationError(
            "Неизменяемую историю нельзя создавать в обход проверки модели."
        )

    def update(self, **kwargs: object) -> int:
        raise ValidationError("Неизменяемую историю нельзя обновить.")

    def delete(self) -> tuple[int, dict[str, int]]:
        raise ValidationError("Неизменяемую историю нельзя удалить.")

    def bulk_update(
        self,
        objs: list[models.Model],
        fields: list[str],
        batch_size: int | None = None,
    ) -> int:
        raise ValidationError("Неизменяемую историю нельзя обновить.")


class ImmutableHistoryManager(models.Manager.from_queryset(ImmutableHistoryQuerySet)):
    pass


def normalize_immutable_reason(value: object) -> str:
    normalized = unicodedata.normalize(
        "NFKC",
        str(value or "").replace("\r\n", "\n").replace("\r", "\n"),
    ).strip()
    if any(
        unicodedata.category(character) == "Cf"
        or (
            unicodedata.category(character) == "Cc"
            and character != "\n"
        )
        for character in normalized
    ):
        raise ValidationError(
            "Основание содержит невидимые управляющие символы."
        )
    if len(normalized) < 5:
        raise ValidationError(
            "Укажите содержательное основание (минимум 5 символов)."
        )
    return normalized


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
    passport_series = models.CharField("серия паспорта", max_length=20, blank=True)
    passport_number = models.CharField("номер паспорта", max_length=30, blank=True)
    passport_issued_by = models.TextField("кем выдан паспорт", blank=True)
    passport_issued_on = models.DateField("дата выдачи паспорта", null=True, blank=True)
    registration_address = models.TextField("адрес регистрации", blank=True)
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
        return " ".join(
            part for part in [self.last_name, self.first_name, self.middle_name] if part
        )


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
    registration_address = models.TextField("адрес регистрации", blank=True)
    residential_address = models.TextField("адрес проживания", blank=True)
    status = models.CharField(
        "статус", max_length=30, choices=Status.choices, default=Status.ACTIVE
    )
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
        return " ".join(
            part for part in [self.last_name, self.first_name, self.middle_name] if part
        )

    def save(self, *args: object, **kwargs: object) -> None:
        super().save(*args, **kwargs)
        self._sync_primary_representative_link()

    def _sync_primary_representative_link(self) -> None:
        if not self.pk or not self.primary_parent_id:
            return
        RecipientRepresentative.objects.filter(child=self).exclude(
            representative_id=self.primary_parent_id
        ).filter(Q(is_primary=True) | Q(signs_contract=True)).update(
            is_primary=False,
            signs_contract=False,
        )
        RecipientRepresentative.objects.update_or_create(
            child=self,
            representative_id=self.primary_parent_id,
            defaults={
                "relationship_type": self.primary_parent.relationship_type,
                "is_primary": True,
                "signs_contract": True,
                "receives_schedule": True,
            },
        )


class RecipientRepresentative(TimeStampedModel):
    child = models.ForeignKey(
        Child,
        verbose_name="получатель",
        on_delete=models.CASCADE,
        related_name="representative_links",
    )
    representative = models.ForeignKey(
        ParentGuardian,
        verbose_name="представитель",
        on_delete=models.CASCADE,
        related_name="recipient_links",
    )
    relationship_type = models.CharField(
        "тип связи",
        max_length=30,
        choices=ParentGuardian.RelationshipType.choices,
        default=ParentGuardian.RelationshipType.MOTHER,
    )
    is_primary = models.BooleanField("основной представитель", default=False)
    signs_contract = models.BooleanField("подписывает договор", default=False)
    receives_schedule = models.BooleanField("получает расписание", default=True)
    is_payer = models.BooleanField("плательщик", default=False)
    notes = models.TextField("примечания", blank=True)

    class Meta:
        verbose_name = "представитель получателя"
        verbose_name_plural = "представители получателей"
        ordering = [
            "child__last_name",
            "child__first_name",
            "-is_primary",
            "representative__last_name",
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["child", "representative"], name="unique_recipient_representative"
            ),
            models.UniqueConstraint(
                fields=["child"],
                condition=Q(is_primary=True),
                name="unique_primary_representative_per_child",
            ),
            models.UniqueConstraint(
                fields=["child"],
                condition=Q(signs_contract=True),
                name="unique_contract_signer_per_child",
            ),
        ]
        indexes = [
            models.Index(fields=["child", "receives_schedule"]),
            models.Index(fields=["representative", "receives_schedule"]),
        ]

    def __str__(self) -> str:
        return f"{self.child} — {self.representative}"


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
    status = models.CharField(
        "статус", max_length=30, choices=Status.choices, default=Status.ACTIVE
    )
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
    category = models.CharField(
        "категория", max_length=40, choices=Category.choices, default=Category.OTHER
    )
    default_duration_minutes = models.PositiveIntegerField(
        "длительность по умолчанию, мин", default=30
    )
    default_price = models.DecimalField(
        "цена по умолчанию", max_digits=10, decimal_places=2, default=0
    )
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
    room_type = models.CharField(
        "тип", max_length=40, choices=RoomType.choices, default=RoomType.CABINET
    )
    capacity = models.PositiveIntegerField("вместимость", default=1)
    limit_staff_count = models.BooleanField("ограничивать число специалистов", default=True)
    max_staff_count = models.PositiveIntegerField("максимум специалистов одновременно", default=1)
    limit_recipient_count = models.BooleanField("ограничивать число получателей", default=True)
    max_recipient_count = models.PositiveIntegerField(
        "максимум получателей одновременно", default=1
    )
    allow_group_sessions = models.BooleanField("разрешены групповые занятия", default=False)
    is_active = models.BooleanField("активно", default=True)
    color = models.CharField("цвет", max_length=20, default="#f97316")

    class Meta:
        verbose_name = "помещение"
        verbose_name_plural = "помещения"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    @property
    def effective_max_staff_count(self) -> int:
        return self.max_staff_count or 1

    @property
    def effective_max_recipient_count(self) -> int:
        return self.max_recipient_count or 1


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


class StaffCompensationRule(TimeStampedModel):
    class SessionScope(models.TextChoices):
        ALL = "all", "Все занятия"
        INDIVIDUAL = "individual", "Индивидуальные"
        GROUP = "group", "Групповые"

    class RateType(models.TextChoices):
        PER_SESSION = "per_session", "За занятие"
        HOURLY = "hourly", "За час"

    class GroupPayPolicy(models.TextChoices):
        PER_SESSION = "per_session", "Один раз за группу"
        PER_CHARGED_PARTICIPANT = "per_charged_participant", "По списанным участникам"
        FIXED_GROUP_AMOUNT = "fixed_group_amount", "Фиксированная сумма за группу"

    staff_member = models.ForeignKey(
        StaffMember,
        verbose_name="специалист",
        on_delete=models.CASCADE,
        related_name="compensation_rules",
    )
    service = models.ForeignKey(
        Service,
        verbose_name="услуга",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="compensation_rules",
        help_text="Оставьте пустым, если ставка действует на все услуги специалиста.",
    )
    funding_source = models.ForeignKey(
        FundingSource,
        verbose_name="источник финансирования",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="compensation_rules",
        help_text="Оставьте пустым для общей ставки; ставка по источнику финансирования приоритетнее.",
    )
    session_scope = models.CharField(
        "формат занятий",
        max_length=30,
        choices=SessionScope.choices,
        default=SessionScope.ALL,
        help_text="Ограничьте ставку индивидуальными или групповыми занятиями, если суммы отличаются.",
    )
    rate_type = models.CharField(
        "тип ставки", max_length=30, choices=RateType.choices, default=RateType.PER_SESSION
    )
    amount = models.DecimalField("сумма", max_digits=12, decimal_places=2)
    group_pay_policy = models.CharField(
        "начисление в группе",
        max_length=40,
        choices=GroupPayPolicy.choices,
        default=GroupPayPolicy.PER_SESSION,
        help_text="Для групповых занятий: один раз за группу, по списанным участникам или фиксированной суммой.",
    )
    group_fixed_amount = models.DecimalField(
        "фиксированная сумма за группу",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Заполняется только для варианта «Фиксированная сумма за группу».",
    )
    min_duration_minutes = models.PositiveIntegerField(
        "мин. длительность, мин",
        null=True,
        blank=True,
        help_text="Оставьте пустым, если нижней границы по длительности нет.",
    )
    max_duration_minutes = models.PositiveIntegerField(
        "макс. длительность, мин",
        null=True,
        blank=True,
        help_text="Оставьте пустым, если верхней границы по длительности нет.",
    )
    starts_on = models.DateField("действует с", null=True, blank=True)
    ends_on = models.DateField("действует по", null=True, blank=True)
    is_active = models.BooleanField("активна", default=True)
    note = models.TextField("примечание", blank=True)

    class Meta:
        verbose_name = "правило начисления специалиста"
        verbose_name_plural = "правила начисления специалистов"
        ordering = [
            "staff_member__full_name",
            "service__name",
            "funding_source__name",
            "-starts_on",
            "-created_at",
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gte=0), name="staff_comp_amount_non_negative"
            ),
            models.CheckConstraint(
                condition=Q(group_fixed_amount__isnull=True) | Q(group_fixed_amount__gte=0),
                name="staff_comp_group_fixed_non_negative",
            ),
            models.CheckConstraint(
                condition=~Q(group_pay_policy="fixed_group_amount")
                | Q(group_fixed_amount__isnull=False),
                name="staff_comp_group_fixed_required",
            ),
            models.CheckConstraint(
                condition=Q(starts_on__isnull=True)
                | Q(ends_on__isnull=True)
                | Q(ends_on__gte=models.F("starts_on")),
                name="staff_comp_dates_order",
            ),
            models.CheckConstraint(
                condition=Q(min_duration_minutes__isnull=True) | Q(min_duration_minutes__gte=1),
                name="staff_comp_min_duration_positive",
            ),
            models.CheckConstraint(
                condition=Q(max_duration_minutes__isnull=True) | Q(max_duration_minutes__gte=1),
                name="staff_comp_max_duration_positive",
            ),
            models.CheckConstraint(
                condition=Q(min_duration_minutes__isnull=True)
                | Q(max_duration_minutes__isnull=True)
                | Q(max_duration_minutes__gte=models.F("min_duration_minutes")),
                name="staff_comp_duration_range_order",
            ),
        ]
        indexes = [
            models.Index(fields=["staff_member", "is_active", "starts_on", "ends_on"]),
            models.Index(fields=["staff_member", "service", "funding_source", "is_active"]),
            models.Index(
                fields=["staff_member", "session_scope", "service", "funding_source", "is_active"]
            ),
        ]

    def clean(self) -> None:
        if (
            self.group_pay_policy == self.GroupPayPolicy.FIXED_GROUP_AMOUNT
            and self.group_fixed_amount is None
        ):
            raise ValidationError(
                {"group_fixed_amount": "Укажите фиксированную сумму для группового занятия."}
            )
        if self.group_fixed_amount is not None and self.group_fixed_amount < 0:
            raise ValidationError(
                {"group_fixed_amount": "Фиксированная сумма не может быть отрицательной."}
            )
        if self.starts_on and self.ends_on and self.ends_on < self.starts_on:
            raise ValidationError({"ends_on": "Дата окончания не может быть раньше даты начала."})
        if (
            self.min_duration_minutes
            and self.max_duration_minutes
            and self.max_duration_minutes < self.min_duration_minutes
        ):
            raise ValidationError(
                {
                    "max_duration_minutes": "Максимальная длительность не может быть меньше минимальной."
                }
            )

    def __str__(self) -> str:
        parts = [self.staff_member.full_name]
        if self.service_id:
            parts.append(self.service.name)
        if self.funding_source_id:
            parts.append(self.funding_source.name)
        duration = ""
        if self.min_duration_minutes or self.max_duration_minutes:
            start = self.min_duration_minutes or "..."
            end = self.max_duration_minutes or "..."
            duration = f", {start}-{end} мин"
        modifiers = []
        if self.session_scope != self.SessionScope.ALL:
            modifiers.append(self.get_session_scope_display())
        if self.group_pay_policy != self.GroupPayPolicy.PER_SESSION:
            modifiers.append(f"группа: {self.get_group_pay_policy_display()}")
        modifier = f"; {', '.join(modifiers)}" if modifiers else ""
        return (
            " / ".join(parts)
            + f": {self.amount} ({self.get_rate_type_display()}{duration}{modifier})"
        )


class FundingServiceQuota(TimeStampedModel):
    class LifecycleStatus(models.TextChoices):
        ACTIVE = "active", "Активна"
        CLOSED = "closed", "Закрыта"

    funding_source = models.ForeignKey(
        FundingSource,
        verbose_name="источник финансирования",
        on_delete=models.PROTECT,
        related_name="service_quotas",
    )
    service = models.ForeignKey(
        Service,
        verbose_name="услуга",
        on_delete=models.PROTECT,
        related_name="funding_quotas",
    )
    planned_sessions = models.PositiveIntegerField("план занятий", default=0)
    starts_on = models.DateField("действует с", null=True, blank=True)
    ends_on = models.DateField("действует по", null=True, blank=True)
    note = models.TextField("примечание", blank=True)
    lifecycle_status = models.CharField(
        "статус плановой позиции",
        max_length=20,
        choices=LifecycleStatus.choices,
        default=LifecycleStatus.ACTIVE,
    )
    current_revision = models.OneToOneField(
        "FundingServiceQuotaRevision",
        verbose_name="текущая редакция",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="current_for_service_quota",
    )

    class Meta:
        verbose_name = "квота финансирования по услуге"
        verbose_name_plural = "квоты финансирования по услугам"
        ordering = ["funding_source__name", "service__name", "starts_on"]
        constraints = [
            models.CheckConstraint(
                condition=Q(starts_on__isnull=True)
                | Q(ends_on__isnull=True)
                | Q(ends_on__gte=models.F("starts_on")),
                name="funding_service_quota_dates_order",
            ),
            models.CheckConstraint(
                condition=Q(lifecycle_status="active") | Q(ends_on__isnull=False),
                name="funding_service_quota_closed_has_end",
            ),
        ]
        indexes = [
            models.Index(fields=["funding_source", "service", "starts_on", "ends_on"]),
        ]

    versioned_fields = (
        "funding_source_id",
        "service_id",
        "planned_sessions",
        "starts_on",
        "ends_on",
        "note",
        "lifecycle_status",
        "current_revision_id",
    )

    def _ensure_service_write(self) -> None:
        if not self.pk:
            return
        current = type(self).objects.get(pk=self.pk)
        if any(getattr(self, field) != getattr(current, field) for field in self.versioned_fields):
            raise ValidationError(
                "Квоту нельзя изменять напрямую. Создайте редакцию через грантовый сервис."
            )

    def save(self, *args: object, **kwargs: object) -> None:
        self.full_clean()
        self._ensure_service_write()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.funding_source} / {self.service}: {self.planned_sessions}"


class FundingStaffAllocation(TimeStampedModel):
    class LifecycleStatus(models.TextChoices):
        ACTIVE = "active", "Активно"
        CLOSED = "closed", "Закрыто"

    service_quota = models.ForeignKey(
        FundingServiceQuota,
        verbose_name="квота услуги",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="staff_allocations",
        help_text="Можно оставить пустым, если распределение задаётся сразу как специалист + услуга + количество.",
    )
    funding_source = models.ForeignKey(
        FundingSource,
        verbose_name="источник финансирования",
        on_delete=models.PROTECT,
        related_name="staff_allocations",
    )
    service = models.ForeignKey(
        Service,
        verbose_name="услуга",
        on_delete=models.PROTECT,
        related_name="staff_funding_allocations",
    )
    staff_member = models.ForeignKey(
        StaffMember,
        verbose_name="специалист",
        on_delete=models.PROTECT,
        related_name="funding_allocations",
    )
    allocated_sessions = models.PositiveIntegerField("выделено занятий", default=0)
    session_pay_amount = models.DecimalField(
        "стоимость занятия специалисту",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    starts_on = models.DateField("действует с", null=True, blank=True)
    ends_on = models.DateField("действует по", null=True, blank=True)
    note = models.TextField("примечание", blank=True)
    lifecycle_status = models.CharField(
        "статус плановой позиции",
        max_length=20,
        choices=LifecycleStatus.choices,
        default=LifecycleStatus.ACTIVE,
    )
    current_revision = models.OneToOneField(
        "FundingStaffAllocationRevision",
        verbose_name="текущая редакция",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="current_for_staff_allocation",
    )

    class Meta:
        verbose_name = "распределение квоты по специалисту"
        verbose_name_plural = "распределения квот по специалистам"
        ordering = ["funding_source__name", "service__name", "staff_member__full_name", "starts_on"]
        constraints = [
            models.CheckConstraint(
                condition=Q(session_pay_amount__isnull=True) | Q(session_pay_amount__gte=0),
                name="funding_staff_alloc_pay_non_negative",
            ),
            models.CheckConstraint(
                condition=Q(starts_on__isnull=True)
                | Q(ends_on__isnull=True)
                | Q(ends_on__gte=models.F("starts_on")),
                name="funding_staff_alloc_dates_order",
            ),
            models.CheckConstraint(
                condition=Q(lifecycle_status="active") | Q(ends_on__isnull=False),
                name="funding_staff_alloc_closed_has_end",
            ),
        ]
        indexes = [
            models.Index(fields=["funding_source", "service", "staff_member"]),
            models.Index(fields=["service_quota", "staff_member"]),
        ]

    versioned_fields = (
        "service_quota_id",
        "funding_source_id",
        "service_id",
        "staff_member_id",
        "allocated_sessions",
        "session_pay_amount",
        "starts_on",
        "ends_on",
        "note",
        "lifecycle_status",
        "current_revision_id",
    )

    def clean(self) -> None:
        if self.service_quota_id:
            if self.service_quota.funding_source_id != self.funding_source_id:
                raise ValidationError(
                    {"funding_source": "Источник должен совпадать с квотой услуги."}
                )
            if self.service_quota.service_id != self.service_id:
                raise ValidationError({"service": "Услуга должна совпадать с квотой услуги."})

    def _ensure_service_write(self) -> None:
        if not self.pk:
            return
        current = type(self).objects.get(pk=self.pk)
        if any(getattr(self, field) != getattr(current, field) for field in self.versioned_fields):
            raise ValidationError(
                "Распределение нельзя изменять напрямую. Создайте редакцию через грантовый сервис."
            )

    def save(self, *args: object, **kwargs: object) -> None:
        if self.service_quota_id:
            self.funding_source_id = self.service_quota.funding_source_id
            self.service_id = self.service_quota.service_id
        self.full_clean()
        self._ensure_service_write()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.funding_source} / {self.service} / {self.staff_member}: {self.allocated_sessions}"


class FundingServiceQuotaRevision(TimeStampedModel):
    class EventType(models.TextChoices):
        CREATED = "created", "Создана"
        REVISED = "revised", "Изменена"
        CLOSED = "closed", "Закрыта"
        LEGACY_IMPORT = "legacy_import", "Перенесена из прежней модели"

    class ActorRole(models.TextChoices):
        DIRECTOR = "director", "Руководитель"
        LEGACY = "legacy", "Автор неизвестен"

    service_quota = models.ForeignKey(
        FundingServiceQuota,
        verbose_name="квота услуги",
        on_delete=models.PROTECT,
        related_name="revisions",
    )
    revision_number = models.PositiveIntegerField("номер редакции")
    event_type = models.CharField("действие", max_length=20, choices=EventType.choices)
    planned_sessions = models.PositiveIntegerField("план занятий", default=0)
    starts_on = models.DateField("действует с", null=True, blank=True)
    ends_on = models.DateField("действует по", null=True, blank=True)
    note = models.TextField("примечание", blank=True)
    lifecycle_status = models.CharField(
        "статус плановой позиции",
        max_length=20,
        choices=FundingServiceQuota.LifecycleStatus.choices,
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="решение зафиксировал",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="funding_service_quota_revisions",
    )
    actor_role_snapshot = models.CharField(
        "роль на момент решения",
        max_length=20,
        choices=ActorRole.choices,
    )
    reason = models.TextField("основание")
    supersedes = models.ForeignKey(
        "self",
        verbose_name="предыдущая редакция",
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="superseded_by",
    )
    decided_at = models.DateTimeField("время решения", null=True, blank=True)

    class Meta:
        verbose_name = "редакция квоты финансирования"
        verbose_name_plural = "редакции квот финансирования"
        ordering = ["service_quota_id", "-revision_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["service_quota", "revision_number"],
                name="unique_funding_quota_revision_number",
            ),
            models.UniqueConstraint(
                fields=["supersedes"],
                condition=Q(supersedes__isnull=False),
                name="unique_funding_quota_revision_successor",
            ),
            models.CheckConstraint(
                condition=Q(revision_number__gte=1),
                name="funding_quota_revision_number_positive",
            ),
            models.CheckConstraint(
                condition=Q(starts_on__isnull=True)
                | Q(ends_on__isnull=True)
                | Q(ends_on__gte=models.F("starts_on")),
                name="funding_quota_revision_dates_order",
            ),
            models.CheckConstraint(
                condition=Q(lifecycle_status="active") | Q(ends_on__isnull=False),
                name="funding_quota_revision_closed_has_end",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        event_type="legacy_import",
                        actor__isnull=True,
                        actor_role_snapshot="legacy",
                        decided_at__isnull=True,
                    )
                    | (
                        ~Q(event_type="legacy_import")
                        & Q(
                            actor__isnull=False,
                            actor_role_snapshot="director",
                            decided_at__isnull=False,
                        )
                        & ~Q(reason="")
                    )
                ),
                name="funding_quota_revision_actor_matches_event",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        event_type__in=["created", "legacy_import"],
                        supersedes__isnull=True,
                    )
                    | Q(
                        event_type__in=["revised", "closed"],
                        supersedes__isnull=False,
                    )
                ),
                name="funding_quota_revision_chain_matches_event",
            ),
            models.CheckConstraint(
                condition=~Q(event_type="closed") | Q(lifecycle_status="closed"),
                name="funding_quota_closed_event_has_closed_status",
            ),
        ]
        indexes = [
            models.Index(fields=["service_quota", "-revision_number"]),
            models.Index(fields=["actor", "-created_at"]),
        ]

    def clean(self) -> None:
        if self.reason is not None:
            self.reason = self.reason.strip()
        if self.event_type != self.EventType.LEGACY_IMPORT and len(self.reason) < 5:
            raise ValidationError({"reason": "Укажите содержательное основание (минимум 5 символов)."})
        if self.supersedes_id and self.supersedes.service_quota_id != self.service_quota_id:
            raise ValidationError({"supersedes": "Предыдущая редакция должна относиться к той же квоте."})

    def save(self, *args: object, **kwargs: object) -> None:
        if self.pk:
            raise ValidationError(
                "Редакцию квоты нельзя изменять. Создайте следующую редакцию."
            )
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args: object, **kwargs: object) -> None:
        raise ValidationError("Редакцию квоты нельзя удалить.")

    def __str__(self) -> str:
        return f"{self.service_quota} / редакция {self.revision_number}"


class FundingStaffAllocationRevision(TimeStampedModel):
    class EventType(models.TextChoices):
        CREATED = "created", "Создано"
        REVISED = "revised", "Изменено"
        CLOSED = "closed", "Закрыто"
        LEGACY_IMPORT = "legacy_import", "Перенесено из прежней модели"

    class ActorRole(models.TextChoices):
        DIRECTOR = "director", "Руководитель"
        LEGACY = "legacy", "Автор неизвестен"

    staff_allocation = models.ForeignKey(
        FundingStaffAllocation,
        verbose_name="распределение специалисту",
        on_delete=models.PROTECT,
        related_name="revisions",
    )
    revision_number = models.PositiveIntegerField("номер редакции")
    event_type = models.CharField("действие", max_length=20, choices=EventType.choices)
    allocated_sessions = models.PositiveIntegerField("выделено занятий", default=0)
    session_pay_amount = models.DecimalField(
        "стоимость занятия специалисту",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    starts_on = models.DateField("действует с", null=True, blank=True)
    ends_on = models.DateField("действует по", null=True, blank=True)
    note = models.TextField("примечание", blank=True)
    lifecycle_status = models.CharField(
        "статус плановой позиции",
        max_length=20,
        choices=FundingStaffAllocation.LifecycleStatus.choices,
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="решение зафиксировал",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="funding_staff_allocation_revisions",
    )
    actor_role_snapshot = models.CharField(
        "роль на момент решения",
        max_length=20,
        choices=ActorRole.choices,
    )
    reason = models.TextField("основание")
    supersedes = models.ForeignKey(
        "self",
        verbose_name="предыдущая редакция",
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="superseded_by",
    )
    decided_at = models.DateTimeField("время решения", null=True, blank=True)

    class Meta:
        verbose_name = "редакция распределения грантовой квоты"
        verbose_name_plural = "редакции распределений грантовых квот"
        ordering = ["staff_allocation_id", "-revision_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["staff_allocation", "revision_number"],
                name="unique_funding_staff_revision_number",
            ),
            models.UniqueConstraint(
                fields=["supersedes"],
                condition=Q(supersedes__isnull=False),
                name="unique_funding_staff_revision_successor",
            ),
            models.CheckConstraint(
                condition=Q(revision_number__gte=1),
                name="funding_staff_revision_number_positive",
            ),
            models.CheckConstraint(
                condition=Q(session_pay_amount__isnull=True) | Q(session_pay_amount__gte=0),
                name="funding_staff_revision_pay_non_negative",
            ),
            models.CheckConstraint(
                condition=Q(starts_on__isnull=True)
                | Q(ends_on__isnull=True)
                | Q(ends_on__gte=models.F("starts_on")),
                name="funding_staff_revision_dates_order",
            ),
            models.CheckConstraint(
                condition=Q(lifecycle_status="active") | Q(ends_on__isnull=False),
                name="funding_staff_revision_closed_has_end",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        event_type="legacy_import",
                        actor__isnull=True,
                        actor_role_snapshot="legacy",
                        decided_at__isnull=True,
                    )
                    | (
                        ~Q(event_type="legacy_import")
                        & Q(
                            actor__isnull=False,
                            actor_role_snapshot="director",
                            decided_at__isnull=False,
                        )
                        & ~Q(reason="")
                    )
                ),
                name="funding_staff_revision_actor_matches_event",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        event_type__in=["created", "legacy_import"],
                        supersedes__isnull=True,
                    )
                    | Q(
                        event_type__in=["revised", "closed"],
                        supersedes__isnull=False,
                    )
                ),
                name="funding_staff_revision_chain_matches_event",
            ),
            models.CheckConstraint(
                condition=~Q(event_type="closed") | Q(lifecycle_status="closed"),
                name="funding_staff_closed_event_has_closed_status",
            ),
        ]
        indexes = [
            models.Index(fields=["staff_allocation", "-revision_number"]),
            models.Index(fields=["actor", "-created_at"]),
        ]

    def clean(self) -> None:
        if self.reason is not None:
            self.reason = self.reason.strip()
        if self.event_type != self.EventType.LEGACY_IMPORT and len(self.reason) < 5:
            raise ValidationError({"reason": "Укажите содержательное основание (минимум 5 символов)."})
        if self.supersedes_id and self.supersedes.staff_allocation_id != self.staff_allocation_id:
            raise ValidationError(
                {"supersedes": "Предыдущая редакция должна относиться к тому же распределению."}
            )

    def save(self, *args: object, **kwargs: object) -> None:
        if self.pk:
            raise ValidationError(
                "Редакцию распределения нельзя изменять. Создайте следующую редакцию."
            )
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args: object, **kwargs: object) -> None:
        raise ValidationError("Редакцию распределения нельзя удалить.")

    def __str__(self) -> str:
        return f"{self.staff_allocation} / редакция {self.revision_number}"


class FundingPayrollBudget(TimeStampedModel):
    class EnforcementMode(models.TextChoices):
        WARNING = "warning", "Предупреждать о превышении"
        HARD = "hard", "Запрещать превышение"

    class LifecycleStatus(models.TextChoices):
        ACTIVE = "active", "Активен"
        CLOSED = "closed", "Закрыт"

    funding_source = models.ForeignKey(
        FundingSource,
        verbose_name="источник финансирования",
        on_delete=models.PROTECT,
        related_name="payroll_budgets",
    )
    starts_on = models.DateField("действует с")
    ends_on = models.DateField("действует по")
    planned_amount = models.DecimalField(
        "бюджет оплаты труда",
        max_digits=14,
        decimal_places=2,
    )
    enforcement_mode = models.CharField(
        "контроль лимита",
        max_length=20,
        choices=EnforcementMode.choices,
        default=EnforcementMode.HARD,
    )
    lifecycle_status = models.CharField(
        "статус бюджета",
        max_length=20,
        choices=LifecycleStatus.choices,
        default=LifecycleStatus.ACTIVE,
    )
    note = models.TextField("примечание", blank=True)
    current_revision = models.OneToOneField(
        "FundingPayrollBudgetRevision",
        verbose_name="текущая редакция",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="current_for_payroll_budget",
    )

    class Meta:
        verbose_name = "бюджет оплаты труда по источнику"
        verbose_name_plural = "бюджеты оплаты труда по источникам"
        ordering = ["funding_source__name", "starts_on", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=Q(ends_on__gte=models.F("starts_on")),
                name="payroll_budget_dates_order",
            ),
            models.CheckConstraint(
                condition=Q(planned_amount__gt=0),
                name="payroll_budget_amount_positive",
            ),
        ]
        indexes = [
            models.Index(fields=["funding_source", "starts_on", "ends_on"]),
            models.Index(fields=["lifecycle_status", "starts_on", "ends_on"]),
        ]

    versioned_fields = (
        "funding_source_id",
        "starts_on",
        "ends_on",
        "planned_amount",
        "enforcement_mode",
        "lifecycle_status",
        "note",
        "current_revision_id",
    )

    def _ensure_service_write(self) -> None:
        if not self.pk:
            return
        current = type(self).objects.get(pk=self.pk)
        if any(getattr(self, field) != getattr(current, field) for field in self.versioned_fields):
            raise ValidationError(
                "Бюджет оплаты труда нельзя изменять напрямую. "
                "Создайте редакцию через грантовый сервис."
            )

    def save(self, *args: object, **kwargs: object) -> None:
        self.full_clean()
        self._ensure_service_write()
        super().save(*args, **kwargs)

    def delete(self, *args: object, **kwargs: object) -> None:
        raise ValidationError("Бюджет оплаты труда нельзя удалить. Закройте его редакцией.")

    def __str__(self) -> str:
        return (
            f"{self.funding_source}: {self.starts_on:%d.%m.%Y}-"
            f"{self.ends_on:%d.%m.%Y} / {self.planned_amount}"
        )


class GrantFixedCompensation(TimeStampedModel):
    class CompensationScope(models.TextChoices):
        SERVICE_DELIVERY = "service_delivery", "Фиксировано за оказание услуги"
        PROJECT_ROLE = "project_role", "Дополнительная проектная роль"

    class LifecycleStatus(models.TextChoices):
        ACTIVE = "active", "Активна"
        CLOSED = "closed", "Закрыта"

    payroll_budget = models.ForeignKey(
        FundingPayrollBudget,
        verbose_name="бюджет оплаты труда",
        on_delete=models.PROTECT,
        related_name="fixed_compensations",
    )
    staff_member = models.ForeignKey(
        StaffMember,
        verbose_name="сотрудник",
        on_delete=models.PROTECT,
        related_name="grant_fixed_compensations",
    )
    compensation_scope = models.CharField(
        "вид фиксированной оплаты",
        max_length=30,
        choices=CompensationScope.choices,
    )
    service = models.ForeignKey(
        Service,
        verbose_name="услуга",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="grant_fixed_compensations",
    )
    assignment_label = models.CharField("проектная роль", max_length=200, blank=True)
    assignment_key = models.CharField(
        "ключ проектной роли",
        max_length=200,
        null=True,
        blank=True,
        editable=False,
    )
    period_from = models.DateField("период с")
    period_to = models.DateField("период по")
    accrual_on = models.DateField("дата начисления")
    amount = models.DecimalField("фиксированная сумма", max_digits=14, decimal_places=2)
    lifecycle_status = models.CharField(
        "статус позиции",
        max_length=20,
        choices=LifecycleStatus.choices,
        default=LifecycleStatus.ACTIVE,
    )
    note = models.TextField("примечание", blank=True)
    current_revision = models.OneToOneField(
        "GrantFixedCompensationRevision",
        verbose_name="текущая редакция",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="current_for_fixed_compensation",
    )

    class Meta:
        verbose_name = "фиксированная оплата грантового проекта"
        verbose_name_plural = "фиксированные оплаты грантовых проектов"
        ordering = [
            "payroll_budget__funding_source__name",
            "staff_member__full_name",
            "period_from",
            "pk",
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(period_to__gte=models.F("period_from")),
                name="grant_fixed_comp_dates_order",
            ),
            models.CheckConstraint(
                condition=Q(accrual_on__gte=models.F("period_from"))
                & Q(accrual_on__lte=models.F("period_to")),
                name="grant_fixed_comp_accrual_in_period",
            ),
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="grant_fixed_comp_amount_positive",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        compensation_scope="service_delivery",
                        service__isnull=False,
                        assignment_label="",
                        assignment_key__isnull=True,
                    )
                    | (
                        Q(
                            compensation_scope="project_role",
                            service__isnull=True,
                            assignment_key__isnull=False,
                        )
                        & ~Q(assignment_label="")
                        & ~Q(assignment_key="")
                    )
                ),
                name="grant_fixed_comp_scope_fields",
            ),
        ]
        indexes = [
            models.Index(
                fields=[
                    "payroll_budget",
                    "staff_member",
                    "compensation_scope",
                    "period_from",
                    "period_to",
                ]
            ),
            models.Index(fields=["service", "staff_member", "period_from", "period_to"]),
            models.Index(fields=["assignment_key", "staff_member", "period_from", "period_to"]),
        ]

    versioned_fields = (
        "payroll_budget_id",
        "staff_member_id",
        "compensation_scope",
        "service_id",
        "assignment_label",
        "assignment_key",
        "period_from",
        "period_to",
        "accrual_on",
        "amount",
        "lifecycle_status",
        "note",
        "current_revision_id",
    )

    @staticmethod
    def normalize_assignment_key(value: str) -> str:
        return " ".join((value or "").split()).lower()

    def _normalize_scope_fields(self) -> None:
        self.assignment_label = (self.assignment_label or "").strip()
        if self.compensation_scope == self.CompensationScope.SERVICE_DELIVERY:
            self.assignment_label = ""
            self.assignment_key = None
        else:
            self.assignment_key = self.normalize_assignment_key(self.assignment_label) or None

    def clean(self) -> None:
        errors: dict[str, str] = {}
        self._normalize_scope_fields()
        if self.compensation_scope == self.CompensationScope.SERVICE_DELIVERY:
            if not self.service_id:
                errors["service"] = "Для фиксированной оплаты услуги выберите услугу."
        elif self.compensation_scope == self.CompensationScope.PROJECT_ROLE:
            if self.service_id:
                errors["service"] = "Для проектной роли услуга должна быть пустой."
            if not self.assignment_label:
                errors["assignment_label"] = "Укажите название проектной роли."
        if self.period_to and self.period_from and self.period_to < self.period_from:
            errors["period_to"] = "Дата окончания не может быть раньше даты начала."
        if (
            self.accrual_on
            and self.period_from
            and self.period_to
            and not (self.period_from <= self.accrual_on <= self.period_to)
        ):
            errors["accrual_on"] = "Дата начисления должна входить в период позиции."
        if (
            self.payroll_budget_id
            and self.period_from
            and self.period_to
            and self.accrual_on
        ):
            if (
                not self.pk
                and self.payroll_budget.lifecycle_status
                != FundingPayrollBudget.LifecycleStatus.ACTIVE
            ):
                errors["payroll_budget"] = (
                    "Новую фиксированную позицию можно добавить только в активный бюджет."
                )
            if (
                self.period_from < self.payroll_budget.starts_on
                or self.period_to > self.payroll_budget.ends_on
            ):
                errors["period_to"] = "Период позиции должен входить в выбранный бюджет."
            if not (
                self.payroll_budget.starts_on
                <= self.accrual_on
                <= self.payroll_budget.ends_on
            ):
                errors["accrual_on"] = "Дата начисления должна входить в период бюджета."
        if errors:
            raise ValidationError(errors)

    def _ensure_service_write(self) -> None:
        if not self.pk:
            return
        current = type(self).objects.get(pk=self.pk)
        if any(getattr(self, field) != getattr(current, field) for field in self.versioned_fields):
            raise ValidationError(
                "Фиксированную оплату нельзя изменять напрямую. "
                "Создайте редакцию через грантовый сервис."
            )

    def save(self, *args: object, **kwargs: object) -> None:
        self._normalize_scope_fields()
        self.full_clean()
        self._ensure_service_write()
        super().save(*args, **kwargs)

    def delete(self, *args: object, **kwargs: object) -> None:
        raise ValidationError("Фиксированную оплату нельзя удалить. Закройте ее редакцией.")

    def __str__(self) -> str:
        subject = self.service if self.service_id else self.assignment_label
        return (
            f"{self.payroll_budget.funding_source} / {self.staff_member} / {subject}: "
            f"{self.amount}"
        )


class FundingPayrollBudgetRevision(TimeStampedModel):
    class EventType(models.TextChoices):
        CREATED = "created", "Создан"
        REVISED = "revised", "Изменен"
        CLOSED = "closed", "Закрыт"
        LEGACY_IMPORT = "legacy_import", "Перенесен из прежней модели"

    class ActorRole(models.TextChoices):
        DIRECTOR = "director", "Руководитель"
        LEGACY = "legacy", "Автор неизвестен"

    payroll_budget = models.ForeignKey(
        FundingPayrollBudget,
        verbose_name="бюджет оплаты труда",
        on_delete=models.PROTECT,
        related_name="revisions",
    )
    revision_number = models.PositiveIntegerField("номер редакции")
    event_type = models.CharField("действие", max_length=20, choices=EventType.choices)
    starts_on = models.DateField("действует с")
    ends_on = models.DateField("действует по")
    planned_amount = models.DecimalField(
        "бюджет оплаты труда",
        max_digits=14,
        decimal_places=2,
    )
    enforcement_mode = models.CharField(
        "контроль лимита",
        max_length=20,
        choices=FundingPayrollBudget.EnforcementMode.choices,
    )
    lifecycle_status = models.CharField(
        "статус бюджета",
        max_length=20,
        choices=FundingPayrollBudget.LifecycleStatus.choices,
    )
    note = models.TextField("примечание", blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="решение зафиксировал",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="funding_payroll_budget_revisions",
    )
    actor_role_snapshot = models.CharField(
        "роль на момент решения",
        max_length=20,
        choices=ActorRole.choices,
    )
    reason = models.TextField("основание")
    supersedes = models.ForeignKey(
        "self",
        verbose_name="предыдущая редакция",
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="superseded_by",
    )
    decided_at = models.DateTimeField("время решения", null=True, blank=True)

    class Meta:
        verbose_name = "редакция бюджета оплаты труда"
        verbose_name_plural = "редакции бюджетов оплаты труда"
        ordering = ["payroll_budget_id", "-revision_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["payroll_budget", "revision_number"],
                name="unique_payroll_budget_revision_number",
            ),
            models.UniqueConstraint(
                fields=["supersedes"],
                condition=Q(supersedes__isnull=False),
                name="unique_payroll_budget_revision_successor",
            ),
            models.CheckConstraint(
                condition=Q(revision_number__gte=1),
                name="payroll_budget_revision_number_positive",
            ),
            models.CheckConstraint(
                condition=Q(ends_on__gte=models.F("starts_on")),
                name="payroll_budget_revision_dates_order",
            ),
            models.CheckConstraint(
                condition=Q(planned_amount__gt=0),
                name="payroll_budget_revision_amount_positive",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        event_type="legacy_import",
                        actor__isnull=True,
                        actor_role_snapshot="legacy",
                        decided_at__isnull=True,
                    )
                    | (
                        ~Q(event_type="legacy_import")
                        & Q(
                            actor__isnull=False,
                            actor_role_snapshot="director",
                            decided_at__isnull=False,
                        )
                        & ~Q(reason="")
                    )
                ),
                name="payroll_budget_revision_actor_event",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        event_type__in=["created", "legacy_import"],
                        supersedes__isnull=True,
                    )
                    | Q(
                        event_type__in=["revised", "closed"],
                        supersedes__isnull=False,
                    )
                ),
                name="payroll_budget_revision_chain_event",
            ),
            models.CheckConstraint(
                condition=~Q(event_type="closed") | Q(lifecycle_status="closed"),
                name="payroll_budget_closed_event_status",
            ),
        ]
        indexes = [
            models.Index(fields=["payroll_budget", "-revision_number"]),
            models.Index(fields=["actor", "-created_at"]),
        ]

    def clean(self) -> None:
        self.reason = (self.reason or "").strip()
        if self.event_type != self.EventType.LEGACY_IMPORT and len(self.reason) < 5:
            raise ValidationError({"reason": "Укажите содержательное основание (минимум 5 символов)."})
        if self.supersedes_id and self.supersedes.payroll_budget_id != self.payroll_budget_id:
            raise ValidationError(
                {"supersedes": "Предыдущая редакция должна относиться к тому же бюджету."}
            )

    def save(self, *args: object, **kwargs: object) -> None:
        if self.pk:
            raise ValidationError(
                "Редакцию бюджета нельзя изменять. Создайте следующую редакцию."
            )
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args: object, **kwargs: object) -> None:
        raise ValidationError("Редакцию бюджета нельзя удалить.")

    def __str__(self) -> str:
        return f"{self.payroll_budget} / редакция {self.revision_number}"


class GrantFixedCompensationRevision(TimeStampedModel):
    class EventType(models.TextChoices):
        CREATED = "created", "Создана"
        REVISED = "revised", "Изменена"
        CLOSED = "closed", "Закрыта"
        LEGACY_IMPORT = "legacy_import", "Перенесена из прежней модели"

    class ActorRole(models.TextChoices):
        DIRECTOR = "director", "Руководитель"
        LEGACY = "legacy", "Автор неизвестен"

    fixed_compensation = models.ForeignKey(
        GrantFixedCompensation,
        verbose_name="фиксированная оплата",
        on_delete=models.PROTECT,
        related_name="revisions",
    )
    revision_number = models.PositiveIntegerField("номер редакции")
    event_type = models.CharField("действие", max_length=20, choices=EventType.choices)
    budget_revision_at_decision = models.ForeignKey(
        FundingPayrollBudgetRevision,
        verbose_name="редакция бюджета на момент решения",
        on_delete=models.PROTECT,
        related_name="fixed_compensation_revisions",
    )
    compensation_scope = models.CharField(
        "вид фиксированной оплаты",
        max_length=30,
        choices=GrantFixedCompensation.CompensationScope.choices,
    )
    service = models.ForeignKey(
        Service,
        verbose_name="услуга",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="grant_fixed_compensation_revisions",
    )
    assignment_label = models.CharField("проектная роль", max_length=200, blank=True)
    assignment_key = models.CharField(
        "ключ проектной роли",
        max_length=200,
        null=True,
        blank=True,
    )
    period_from = models.DateField("период с")
    period_to = models.DateField("период по")
    accrual_on = models.DateField("дата начисления")
    amount = models.DecimalField("фиксированная сумма", max_digits=14, decimal_places=2)
    lifecycle_status = models.CharField(
        "статус позиции",
        max_length=20,
        choices=GrantFixedCompensation.LifecycleStatus.choices,
    )
    note = models.TextField("примечание", blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="решение зафиксировал",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="grant_fixed_compensation_revisions",
    )
    actor_role_snapshot = models.CharField(
        "роль на момент решения",
        max_length=20,
        choices=ActorRole.choices,
    )
    reason = models.TextField("основание")
    supersedes = models.ForeignKey(
        "self",
        verbose_name="предыдущая редакция",
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="superseded_by",
    )
    decided_at = models.DateTimeField("время решения", null=True, blank=True)

    class Meta:
        verbose_name = "редакция фиксированной оплаты грантового проекта"
        verbose_name_plural = "редакции фиксированных оплат грантовых проектов"
        ordering = ["fixed_compensation_id", "-revision_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["fixed_compensation", "revision_number"],
                name="unique_grant_fixed_revision_number",
            ),
            models.UniqueConstraint(
                fields=["supersedes"],
                condition=Q(supersedes__isnull=False),
                name="unique_grant_fixed_revision_successor",
            ),
            models.CheckConstraint(
                condition=Q(revision_number__gte=1),
                name="grant_fixed_revision_number_positive",
            ),
            models.CheckConstraint(
                condition=Q(period_to__gte=models.F("period_from")),
                name="grant_fixed_revision_dates_order",
            ),
            models.CheckConstraint(
                condition=Q(accrual_on__gte=models.F("period_from"))
                & Q(accrual_on__lte=models.F("period_to")),
                name="grant_fixed_revision_accrual_period",
            ),
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="grant_fixed_revision_amount_positive",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        compensation_scope="service_delivery",
                        service__isnull=False,
                        assignment_label="",
                        assignment_key__isnull=True,
                    )
                    | (
                        Q(
                            compensation_scope="project_role",
                            service__isnull=True,
                            assignment_key__isnull=False,
                        )
                        & ~Q(assignment_label="")
                        & ~Q(assignment_key="")
                    )
                ),
                name="grant_fixed_revision_scope_fields",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        event_type="legacy_import",
                        actor__isnull=True,
                        actor_role_snapshot="legacy",
                        decided_at__isnull=True,
                    )
                    | (
                        ~Q(event_type="legacy_import")
                        & Q(
                            actor__isnull=False,
                            actor_role_snapshot="director",
                            decided_at__isnull=False,
                        )
                        & ~Q(reason="")
                    )
                ),
                name="grant_fixed_revision_actor_event",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        event_type__in=["created", "legacy_import"],
                        supersedes__isnull=True,
                    )
                    | Q(
                        event_type__in=["revised", "closed"],
                        supersedes__isnull=False,
                    )
                ),
                name="grant_fixed_revision_chain_event",
            ),
            models.CheckConstraint(
                condition=~Q(event_type="closed") | Q(lifecycle_status="closed"),
                name="grant_fixed_closed_event_status",
            ),
        ]
        indexes = [
            models.Index(fields=["fixed_compensation", "-revision_number"]),
            models.Index(fields=["actor", "-created_at"]),
        ]

    def clean(self) -> None:
        self.reason = (self.reason or "").strip()
        if self.event_type != self.EventType.LEGACY_IMPORT and len(self.reason) < 5:
            raise ValidationError({"reason": "Укажите содержательное основание (минимум 5 символов)."})
        if (
            self.supersedes_id
            and self.supersedes.fixed_compensation_id != self.fixed_compensation_id
        ):
            raise ValidationError(
                {"supersedes": "Предыдущая редакция должна относиться к той же позиции."}
            )
        if (
            self.budget_revision_at_decision_id
            and self.fixed_compensation_id
            and self.budget_revision_at_decision.payroll_budget_id
            != self.fixed_compensation.payroll_budget_id
        ):
            raise ValidationError(
                {"budget_revision_at_decision": "Редакция должна относиться к бюджету позиции."}
            )

    def save(self, *args: object, **kwargs: object) -> None:
        if self.pk:
            raise ValidationError(
                "Редакцию фиксированной оплаты нельзя изменять. "
                "Создайте следующую редакцию."
            )
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args: object, **kwargs: object) -> None:
        raise ValidationError("Редакцию фиксированной оплаты нельзя удалить.")

    def __str__(self) -> str:
        return f"{self.fixed_compensation} / редакция {self.revision_number}"


class DonorReport(TimeStampedModel):
    class ReportKind(models.TextChoices):
        INTERNAL_GRANT_RECONCILIATION_V1 = (
            "internal_grant_reconciliation_v1",
            "Внутренняя сверка грантового проекта",
        )

    funding_source = models.ForeignKey(
        FundingSource,
        verbose_name="источник финансирования",
        on_delete=models.PROTECT,
        related_name="donor_reports",
    )
    counterparty = models.ForeignKey(
        "Counterparty",
        verbose_name="донор / контрагент",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="donor_reports",
    )
    date_from = models.DateField("период с")
    date_to = models.DateField("период по")
    report_kind = models.CharField(
        "вид отчета",
        max_length=50,
        choices=ReportKind.choices,
        default=ReportKind.INTERNAL_GRANT_RECONCILIATION_V1,
    )
    current_snapshot = models.OneToOneField(
        "DonorReportSnapshot",
        verbose_name="текущий снимок",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="current_for_donor_report",
    )

    class Meta:
        verbose_name = "закрытый отчет донору"
        verbose_name_plural = "закрытые отчеты донорам"
        ordering = ["-date_to", "-date_from", "funding_source__name", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=Q(date_to__gte=models.F("date_from")),
                name="donor_report_dates_order",
            ),
            models.UniqueConstraint(
                fields=[
                    "funding_source",
                    "counterparty",
                    "date_from",
                    "date_to",
                    "report_kind",
                ],
                nulls_distinct=False,
                name="unique_donor_report_identity",
            ),
        ]
        indexes = [
            models.Index(
                fields=["funding_source", "date_from", "date_to", "report_kind"],
                name="donor_report_source_period",
            ),
            models.Index(fields=["counterparty", "date_to"], name="donor_report_donor_period"),
        ]

    immutable_fields = (
        "funding_source_id",
        "counterparty_id",
        "date_from",
        "date_to",
        "report_kind",
        "current_snapshot_id",
    )

    def clean(self) -> None:
        if self.date_from and self.date_to and self.date_to < self.date_from:
            raise ValidationError({"date_to": "Дата окончания не может быть раньше даты начала."})

    def _ensure_service_write(self) -> None:
        if not self.pk:
            return
        current = type(self).objects.get(pk=self.pk)
        if any(getattr(self, field) != getattr(current, field) for field in self.immutable_fields):
            raise ValidationError(
                "Закрытый отчет нельзя изменять напрямую. "
                "Создайте исправляющий снимок через сервис отчетов."
            )

    def save(self, *args: object, **kwargs: object) -> None:
        self.full_clean()
        self._ensure_service_write()
        super().save(*args, **kwargs)

    def delete(self, *args: object, **kwargs: object) -> None:
        raise ValidationError("Закрытый отчет нельзя удалить.")

    def __str__(self) -> str:
        return (
            f"{self.funding_source}: {self.date_from:%d.%m.%Y}-"
            f"{self.date_to:%d.%m.%Y}"
        )


class DonorReportSnapshot(TimeStampedModel):
    class EventType(models.TextChoices):
        CLOSED = "closed", "Закрыт"
        CORRECTED = "corrected", "Исправлен"

    class ActorRole(models.TextChoices):
        DIRECTOR = "director", "Руководитель"

    report = models.ForeignKey(
        DonorReport,
        verbose_name="отчет",
        on_delete=models.PROTECT,
        related_name="snapshots",
    )
    snapshot_number = models.PositiveIntegerField("номер снимка")
    event_type = models.CharField("событие", max_length=20, choices=EventType.choices)
    snapshot_schema_version = models.CharField("версия схемы", max_length=64)
    canonicalizer_version = models.CharField("версия канонизации", max_length=32)
    payload = models.JSONField("закрытый payload")
    evidence_manifest = models.JSONField("закрытый манифест доказательств")
    payload_sha256 = models.CharField("SHA-256 payload", max_length=64)
    evidence_manifest_sha256 = models.CharField("SHA-256 манифеста", max_length=64)
    data_as_of = models.DateTimeField("данные по состоянию на")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="закрыл отчет",
        on_delete=models.PROTECT,
        related_name="closed_donor_report_snapshots",
    )
    actor_role_snapshot = models.CharField(
        "роль на момент закрытия",
        max_length=20,
        choices=ActorRole.choices,
    )
    reason = models.TextField("основание")
    closed_at = models.DateTimeField("время закрытия")
    supersedes = models.ForeignKey(
        "self",
        verbose_name="предыдущий снимок",
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="superseded_by",
    )

    class Meta:
        verbose_name = "неизменяемый снимок отчета донору"
        verbose_name_plural = "неизменяемые снимки отчетов донорам"
        ordering = ["report_id", "-snapshot_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["report", "snapshot_number"],
                name="unique_donor_report_snapshot_number",
            ),
            models.UniqueConstraint(
                fields=["supersedes"],
                condition=Q(supersedes__isnull=False),
                name="unique_donor_report_snapshot_successor",
            ),
            models.CheckConstraint(
                condition=Q(snapshot_number__gte=1),
                name="donor_report_snapshot_number_positive",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        event_type="closed",
                        snapshot_number=1,
                        supersedes__isnull=True,
                    )
                    | Q(
                        event_type="corrected",
                        snapshot_number__gt=1,
                        supersedes__isnull=False,
                    )
                ),
                name="donor_report_snapshot_chain_event",
            ),
            models.CheckConstraint(
                condition=Q(actor_role_snapshot="director"),
                name="donor_report_snapshot_director_actor",
            ),
            models.CheckConstraint(
                condition=~Q(reason=""),
                name="donor_report_snapshot_reason_required",
            ),
            models.CheckConstraint(
                condition=Q(closed_at__gte=F("data_as_of")),
                name="donor_report_snapshot_time_order",
            ),
        ]
        indexes = [
            models.Index(fields=["report", "-closed_at"], name="donor_snapshot_report_closed"),
            models.Index(fields=["payload_sha256"], name="donor_snapshot_payload_hash"),
        ]

    def clean(self) -> None:
        from operations.services.donor_reports import (
            CANONICALIZER_VERSION,
            SNAPSHOT_SCHEMA_VERSION,
            _validate_evidence_manifest,
            _validate_payload_values,
            canonical_sha256,
        )

        errors: dict[str, str] = {}
        if not isinstance(self.payload, dict):
            errors["payload"] = "Payload снимка должен быть JSON-объектом."
        if not isinstance(self.evidence_manifest, dict):
            errors["evidence_manifest"] = "Манифест доказательств должен быть JSON-объектом."
        if isinstance(self.payload, dict):
            try:
                _validate_payload_values(self.payload)
            except ValidationError as exc:
                errors["payload"] = "; ".join(exc.messages)
        if isinstance(self.evidence_manifest, dict):
            try:
                _validate_evidence_manifest(self.evidence_manifest)
            except ValidationError as exc:
                errors["evidence_manifest"] = "; ".join(exc.messages)
        for field_name in ("payload_sha256", "evidence_manifest_sha256"):
            value = getattr(self, field_name, "")
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                errors[field_name] = "Ожидается SHA-256 в нижнем шестнадцатеричном формате."
        if self.snapshot_schema_version != SNAPSHOT_SCHEMA_VERSION:
            errors["snapshot_schema_version"] = "Неподдерживаемая версия схемы снимка."
        if self.canonicalizer_version != CANONICALIZER_VERSION:
            errors["canonicalizer_version"] = "Неподдерживаемая версия канонизации."
        if isinstance(self.payload, dict):
            try:
                if self.payload_sha256 != canonical_sha256(self.payload):
                    errors["payload_sha256"] = "Хеш не соответствует payload."
            except (TypeError, ValueError):
                errors["payload"] = "Payload нельзя канонизировать."
        if isinstance(self.evidence_manifest, dict):
            try:
                if self.evidence_manifest_sha256 != canonical_sha256(
                    self.evidence_manifest
                ):
                    errors["evidence_manifest_sha256"] = (
                        "Хеш не соответствует манифесту доказательств."
                    )
            except (TypeError, ValueError):
                errors["evidence_manifest"] = (
                    "Манифест доказательств нельзя канонизировать."
                )
            if (
                self.evidence_manifest.get("payload_sha256")
                != self.payload_sha256
            ):
                errors["evidence_manifest"] = (
                    "Манифест должен ссылаться на хеш своего payload."
                )
        if len((self.reason or "").strip()) < 5:
            errors["reason"] = "Укажите содержательное основание (минимум 5 символов)."
        if self.actor_role_snapshot != self.ActorRole.DIRECTOR:
            errors["actor_role_snapshot"] = "Закрыть отчет может только руководитель."
        if self.supersedes_id:
            if self.supersedes.report_id != self.report_id:
                errors["supersedes"] = "Предыдущий снимок должен относиться к тому же отчету."
            if self.snapshot_number != self.supersedes.snapshot_number + 1:
                errors["snapshot_number"] = "Номер снимка должен продолжать цепочку без пропусков."
        if errors:
            raise ValidationError(errors)

    def save(self, *args: object, **kwargs: object) -> None:
        if self.pk:
            raise ValidationError(
                "Закрытый снимок неизменяем. Создайте исправляющую версию."
            )
        self.reason = (self.reason or "").strip()
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args: object, **kwargs: object) -> None:
        raise ValidationError("Закрытый снимок нельзя удалить.")

    def __str__(self) -> str:
        return f"{self.report} / снимок {self.snapshot_number}"


class DonorReportSubmission(TimeStampedModel):
    class EventType(models.TextChoices):
        SUBMITTED = "submitted", "Сдан"
        REPLACED = "replaced", "Заменен"

    class ActorRole(models.TextChoices):
        DIRECTOR = "director", "Руководитель"

    report_snapshot = models.ForeignKey(
        DonorReportSnapshot,
        verbose_name="снимок отчета",
        on_delete=models.PROTECT,
        related_name="submissions",
    )
    submission_number = models.PositiveIntegerField("номер сдачи")
    event_type = models.CharField("событие", max_length=20, choices=EventType.choices)
    supersedes = models.ForeignKey(
        "self",
        verbose_name="предыдущая сдача",
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="superseded_by",
    )
    storage_key = models.CharField("приватный storage key", max_length=320, unique=True)
    original_filename = models.CharField("исходное имя файла", max_length=255)
    content_type = models.CharField("проверенный MIME", max_length=120)
    file_size = models.PositiveBigIntegerField("размер файла")
    file_sha256 = models.CharField("SHA-256 файла", max_length=64)
    submitted_on = models.DateField("фактическая дата сдачи")
    recorded_at = models.DateTimeField("техническое время записи")
    external_reference = models.CharField(
        "внешний номер / ссылка",
        max_length=500,
        blank=True,
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="зафиксировал сдачу",
        on_delete=models.PROTECT,
        related_name="donor_report_submissions",
    )
    actor_role_snapshot = models.CharField(
        "роль на момент сдачи",
        max_length=20,
        choices=ActorRole.choices,
    )
    reason = models.TextField("основание")
    note = models.TextField("примечание", blank=True)

    objects = ImmutableHistoryManager()

    class Meta:
        verbose_name = "неизменяемый факт сдачи отчета"
        verbose_name_plural = "неизменяемые факты сдачи отчетов"
        ordering = ["report_snapshot_id", "-submission_number"]
        permissions = [
            (
                "download_donorreportsubmission",
                "Может скачивать приватные файлы сдачи отчетов",
            )
        ]
        default_permissions = ("view",)
        base_manager_name = "objects"
        constraints = [
            models.UniqueConstraint(
                fields=["report_snapshot", "submission_number"],
                name="unique_donor_submission_number",
            ),
            models.UniqueConstraint(
                fields=["supersedes"],
                condition=Q(supersedes__isnull=False),
                name="unique_donor_submission_successor",
            ),
            models.CheckConstraint(
                condition=Q(submission_number__gte=1),
                name="donor_submission_number_positive",
            ),
            models.CheckConstraint(
                condition=Q(submission_number__lte=999999),
                name="donor_submission_number_bounded",
            ),
            models.CheckConstraint(
                condition=Q(file_size__gte=1),
                name="donor_submission_size_positive",
            ),
            models.CheckConstraint(
                condition=Q(file_size__lte=25 * 1024 * 1024),
                name="donor_submission_size_bounded",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        event_type="submitted",
                        submission_number=1,
                        supersedes__isnull=True,
                    )
                    | Q(
                        event_type="replaced",
                        submission_number__gt=1,
                        supersedes__isnull=False,
                    )
                ),
                name="donor_submission_chain_event",
            ),
            models.CheckConstraint(
                condition=Q(actor_role_snapshot="director"),
                name="donor_submission_director_actor",
            ),
            models.CheckConstraint(
                condition=Q(
                    content_type__in=[
                        "application/pdf",
                        (
                            "application/vnd.openxmlformats-officedocument."
                            "wordprocessingml.document"
                        ),
                        (
                            "application/vnd.openxmlformats-officedocument."
                            "spreadsheetml.sheet"
                        ),
                        "application/vnd.oasis.opendocument.text",
                        "application/vnd.oasis.opendocument.spreadsheet",
                    ]
                ),
                name="donor_submission_mime_allowed",
            ),
            models.CheckConstraint(
                condition=~Q(reason=""),
                name="donor_submission_reason_required",
            ),
        ]
        indexes = [
            models.Index(
                fields=["report_snapshot", "-recorded_at"],
                name="donor_submission_snapshot_time",
            ),
            models.Index(fields=["file_sha256"], name="donor_submission_file_hash"),
        ]

    def clean(self) -> None:
        from operations.services import private_artifacts

        errors: dict[str, str] = {}
        if len(self.file_sha256 or "") != 64 or any(
            character not in "0123456789abcdef" for character in (self.file_sha256 or "")
        ):
            errors["file_sha256"] = "Ожидается SHA-256 в нижнем шестнадцатеричном формате."
        if self.file_size and self.file_size > 25 * 1024 * 1024:
            errors["file_size"] = "Файл превышает ограничение 25 MiB."
        extension = private_artifacts.MIME_TO_EXTENSION.get(self.content_type)
        if extension is None:
            errors["content_type"] = "Формат файла не поддерживается."
        elif self.report_snapshot_id and self.submission_number and self.file_sha256:
            try:
                expected_key = private_artifacts.build_storage_key(
                    snapshot_id=self.report_snapshot_id,
                    submission_number=self.submission_number,
                    file_sha256=self.file_sha256,
                    extension=extension,
                )
            except ValueError:
                errors["storage_key"] = "Storage key нельзя построить из метаданных."
            else:
                if self.storage_key != expected_key:
                    errors["storage_key"] = "Storage key не соответствует неизменяемым метаданным."
        original_filename = self.original_filename or ""
        if (
            not original_filename.strip()
            or "/" in original_filename
            or "\\" in original_filename
            or any(
                unicodedata.category(character) in {"Cc", "Cf"}
                for character in original_filename
            )
        ):
            errors["original_filename"] = "Имя файла содержит недопустимые символы."
        elif extension and not original_filename.lower().endswith(f".{extension}"):
            errors["original_filename"] = "Расширение имени файла не соответствует формату."
        try:
            normalize_immutable_reason(self.reason)
        except ValidationError as exc:
            errors["reason"] = exc.messages[0]
        if self.actor_role_snapshot != self.ActorRole.DIRECTOR:
            errors["actor_role_snapshot"] = "Сдачу может зафиксировать только руководитель."
        if self.actor_id:
            from operations.services.authority import AuthorityRole, authority_role

            actor = self.actor
            if not actor.is_active or authority_role(actor) != AuthorityRole.DIRECTOR:
                errors["actor"] = "Фактический пользователь не является руководителем."
        if self.submitted_on and self.submitted_on > timezone.localdate():
            errors["submitted_on"] = "Дата сдачи не может быть в будущем."
        if self.recorded_at and self.report_snapshot_id:
            snapshot_date = timezone.localtime(self.report_snapshot.closed_at).date()
            recorded_date = timezone.localtime(self.recorded_at).date()
            if self.recorded_at < self.report_snapshot.closed_at:
                errors["recorded_at"] = "Сдача не может быть записана раньше закрытия снимка."
            if not self.pk and not (
                timezone.now() - timedelta(minutes=5)
                <= self.recorded_at
                <= timezone.now() + timedelta(seconds=5)
            ):
                errors["recorded_at"] = "Техническое время сдачи должно задаваться сервером."
            if self.submitted_on and not snapshot_date <= self.submitted_on <= recorded_date:
                errors["submitted_on"] = (
                    "Дата сдачи должна быть не раньше закрытия снимка "
                    "и не позже технической даты записи."
                )
        if "\n" in (self.external_reference or "") or "\r" in (self.external_reference or ""):
            errors["external_reference"] = "Внешний номер или ссылка должны быть одной строкой."
        if self.supersedes_id:
            if self.supersedes.report_snapshot_id != self.report_snapshot_id:
                errors["supersedes"] = "Предыдущая сдача должна относиться к тому же снимку."
            if self.submission_number != self.supersedes.submission_number + 1:
                errors["submission_number"] = "Номер сдачи должен продолжать цепочку без пропусков."
            if self.file_sha256 == self.supersedes.file_sha256:
                errors["file_sha256"] = "Заменяющий файл должен отличаться от предыдущего."
            if self.recorded_at and self.recorded_at < self.supersedes.recorded_at:
                errors["recorded_at"] = "Замена не может предшествовать предыдущей сдаче."
        if errors:
            raise ValidationError(errors)

    def save(self, *args: object, **kwargs: object) -> None:
        if self.pk:
            raise ValidationError("Факт сдачи неизменяем. Создайте заменяющую сдачу.")
        self.reason = normalize_immutable_reason(self.reason)
        self.note = (self.note or "").strip()
        self.external_reference = (self.external_reference or "").strip()
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args: object, **kwargs: object) -> None:
        raise ValidationError("Факт сдачи нельзя удалить.")

    def __str__(self) -> str:
        return f"{self.report_snapshot} / сдача {self.submission_number}"


class DonorReportSubmissionAccess(TimeStampedModel):
    class ActorRole(models.TextChoices):
        DIRECTOR = "director", "Руководитель"
        ADMINISTRATOR = "administrator", "Администратор"

    class PermissionBasis(models.TextChoices):
        DIRECTOR_ROLE = "director_role", "Роль руководителя"
        EXPLICIT_PERMISSION = "explicit_permission", "Отдельное разрешение"

    submission = models.ForeignKey(
        DonorReportSubmission,
        verbose_name="сдача отчета",
        on_delete=models.PROTECT,
        related_name="access_events",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="получил файл",
        on_delete=models.PROTECT,
        related_name="donor_report_submission_accesses",
    )
    actor_role_snapshot = models.CharField(
        "роль на момент доступа",
        max_length=20,
        choices=ActorRole.choices,
    )
    permission_basis = models.CharField(
        "основание доступа",
        max_length=30,
        choices=PermissionBasis.choices,
    )
    verified_sha256 = models.CharField("проверенный SHA-256", max_length=64)
    accessed_at = models.DateTimeField("время выдачи")

    objects = ImmutableHistoryManager()

    class Meta:
        verbose_name = "факт выдачи сдачи отчета"
        verbose_name_plural = "факты выдачи сдач отчетов"
        ordering = ["-accessed_at", "-pk"]
        default_permissions = ("view",)
        base_manager_name = "objects"
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(
                        actor_role_snapshot="director",
                        permission_basis="director_role",
                    )
                    | Q(
                        actor_role_snapshot="administrator",
                        permission_basis="explicit_permission",
                    )
                ),
                name="donor_submission_access_authority",
            )
        ]
        indexes = [
            models.Index(
                fields=["submission", "-accessed_at"],
                name="donor_access_submission_time",
            ),
            models.Index(fields=["actor", "-accessed_at"], name="donor_access_actor_time"),
            models.Index(fields=["-accessed_at"], name="donor_access_time"),
        ]

    def clean(self) -> None:
        errors: dict[str, str] = {}
        expected_basis = {
            self.ActorRole.DIRECTOR: self.PermissionBasis.DIRECTOR_ROLE,
            self.ActorRole.ADMINISTRATOR: self.PermissionBasis.EXPLICIT_PERMISSION,
        }.get(self.actor_role_snapshot)
        if expected_basis != self.permission_basis:
            errors["permission_basis"] = "Основание доступа не соответствует роли."
        if self.actor_id:
            from operations.services.authority import AuthorityRole, authority_role

            actor = self.actor
            actual_role = authority_role(actor)
            valid_actor = actor.is_active and (
                (
                    self.actor_role_snapshot == self.ActorRole.DIRECTOR
                    and actual_role == AuthorityRole.DIRECTOR
                )
                or (
                    self.actor_role_snapshot == self.ActorRole.ADMINISTRATOR
                    and actual_role == AuthorityRole.ADMINISTRATOR
                    and not hasattr(actor, "staff_profile")
                    and actor.has_perm("operations.download_donorreportsubmission")
                )
            )
            if not valid_actor:
                errors["actor"] = "Фактический пользователь не имеет указанного доступа."
        if self.submission_id and self.verified_sha256 != self.submission.file_sha256:
            errors["verified_sha256"] = "Проверенный hash не соответствует сдаче."
        if (
            self.submission_id
            and self.accessed_at
            and self.accessed_at < self.submission.recorded_at
        ):
            errors["accessed_at"] = "Выдача файла не может предшествовать его сдаче."
        if self.accessed_at and not self.pk and not (
            timezone.now() - timedelta(minutes=5)
            <= self.accessed_at
            <= timezone.now() + timedelta(seconds=5)
        ):
            errors["accessed_at"] = "Техническое время выдачи должно задаваться сервером."
        if errors:
            raise ValidationError(errors)

    def save(self, *args: object, **kwargs: object) -> None:
        if self.pk:
            raise ValidationError("Факт выдачи неизменяем.")
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args: object, **kwargs: object) -> None:
        raise ValidationError("Факт выдачи нельзя удалить.")

    def __str__(self) -> str:
        return f"{self.submission_id}: {self.actor_id} / {self.accessed_at:%d.%m.%Y %H:%M}"


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

    child = models.ForeignKey(
        Child, verbose_name="получатель", on_delete=models.CASCADE, related_name="balance_accounts"
    )
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
    initial_amount = models.DecimalField(
        "начальный остаток", max_digits=12, decimal_places=2, default=0
    )
    valid_from = models.DateField("действует с", null=True, blank=True)
    valid_until = models.DateField("действует до", null=True, blank=True)
    status = models.CharField(
        "статус", max_length=30, choices=Status.choices, default=Status.ACTIVE
    )
    color = models.CharField("цветовая метка", max_length=20, default="#f59e0b")
    notes = models.TextField("примечания", blank=True)

    class Meta:
        verbose_name = "счет баланса"
        verbose_name_plural = "счета баланса"
        ordering = ["child__last_name", "child__first_name", "funding_source__name"]

    def __str__(self) -> str:
        scope = self.service.name if self.service else self.get_service_scope_display()
        return f"{self.child}: {self.funding_source} / {scope} / {self.get_unit_display()}"

    @property
    def certificate_link_label(self) -> str:
        try:
            certificate = self.certificate
        except ObjectDoesNotExist:
            return ""
        number = certificate.number or "б/н"
        return f"{certificate.get_certificate_type_display()} №{number}"

    @property
    def is_certificate_linked(self) -> bool:
        return bool(self.certificate_link_label)

    def clean(self) -> None:
        if self.service_scope == self.ServiceScope.SPECIFIC_SERVICE and not self.service_id:
            raise ValidationError(
                {"service": "Для счета по конкретной услуге нужно выбрать услугу."}
            )
        if self.service_scope == self.ServiceScope.ANY and self.service_id:
            raise ValidationError(
                {"service": "Для счета на любые услуги поле услуги должно быть пустым."}
            )

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


class GrantRecipientAllocation(TimeStampedModel):
    funding_source = models.ForeignKey(
        FundingSource,
        verbose_name="источник финансирования",
        on_delete=models.CASCADE,
        related_name="recipient_allocations",
    )
    child = models.ForeignKey(
        Child,
        verbose_name="получатель",
        on_delete=models.CASCADE,
        related_name="grant_allocations",
    )
    service = models.ForeignKey(
        Service,
        verbose_name="услуга",
        on_delete=models.CASCADE,
        related_name="grant_recipient_allocations",
    )
    allocated_sessions = models.PositiveIntegerField("выделено занятий", default=0)
    balance_account = models.ForeignKey(
        BalanceAccount,
        verbose_name="счет баланса",
        on_delete=models.PROTECT,
        related_name="grant_recipient_allocations",
    )
    valid_from = models.DateField("действует с", null=True, blank=True)
    valid_until = models.DateField("действует до", null=True, blank=True)
    note = models.TextField("примечание", blank=True)

    class Meta:
        verbose_name = "грантовое выделение получателю"
        verbose_name_plural = "грантовые выделения получателям"
        ordering = ["funding_source__name", "service__name", "child__last_name", "valid_from"]
        constraints = [
            models.CheckConstraint(
                condition=Q(valid_from__isnull=True)
                | Q(valid_until__isnull=True)
                | Q(valid_until__gte=models.F("valid_from")),
                name="grant_recipient_alloc_dates_order",
            ),
        ]
        indexes = [
            models.Index(fields=["funding_source", "service", "valid_from", "valid_until"]),
            models.Index(fields=["child", "service"]),
            models.Index(fields=["balance_account"]),
        ]

    def clean(self) -> None:
        if self.valid_from and self.valid_until and self.valid_until < self.valid_from:
            raise ValidationError(
                {"valid_until": "Дата окончания не может быть раньше даты начала."}
            )
        if self.balance_account_id:
            if self.balance_account.child_id != self.child_id:
                raise ValidationError({"balance_account": "Счет должен принадлежать получателю."})
            if self.balance_account.funding_source_id != self.funding_source_id:
                raise ValidationError(
                    {
                        "balance_account": "Счет должен относиться к тому же источнику финансирования."
                    }
                )
            if self.balance_account.unit != BalanceAccount.Unit.SESSIONS:
                raise ValidationError(
                    {"balance_account": "Грантовое выделение занятий требует счет в занятиях."}
                )
            if not self.balance_account.can_pay_for(self.service):
                raise ValidationError({"balance_account": "Счет не подходит для услуги."})

    def save(self, *args: object, **kwargs: object) -> None:
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return (
            f"{self.funding_source} / {self.child} / {self.service}: " f"{self.allocated_sessions}"
        )


class TreatmentProgram(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        ACTIVE = "active", "Активна"
        PAUSED = "paused", "Пауза"
        COMPLETED = "completed", "Завершена"
        CANCELLED = "cancelled", "Отменена"

    child = models.ForeignKey(
        Child,
        verbose_name="получатель",
        on_delete=models.CASCADE,
        related_name="treatment_programs",
    )
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

    program = models.ForeignKey(
        TreatmentProgram, verbose_name="программа", on_delete=models.CASCADE, related_name="blocks"
    )
    number = models.PositiveIntegerField("номер блока", default=1)
    title = models.CharField("название блока", max_length=200)
    service = models.ForeignKey(Service, verbose_name="услуга", on_delete=models.PROTECT)
    staff_member = models.ForeignKey(
        StaffMember, verbose_name="специалист", on_delete=models.PROTECT, null=True, blank=True
    )
    planned_sessions = models.PositiveIntegerField("план занятий", default=1)
    balance_account = models.ForeignKey(
        BalanceAccount,
        verbose_name="счёт оплаты",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="program_blocks",
    )
    status = models.CharField(
        "статус", max_length=30, choices=Status.choices, default=Status.PLANNED
    )
    color = models.CharField("цвет", max_length=20, default="#b71b55")
    notes = models.TextField("примечания", blank=True)

    class Meta:
        verbose_name = "блок программы"
        verbose_name_plural = "блоки программ"
        ordering = ["program", "number"]
        constraints = [
            models.UniqueConstraint(
                fields=["program", "number"], name="unique_program_block_number"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.program} / {self.number}. {self.title}"

    @property
    def scheduled_count(self) -> int:
        return self.appointment_participants.exclude(
            appointment_status__in=[Appointment.Status.CANCELLED, Appointment.Status.RESCHEDULED]
        ).count()

    @property
    def paid_count(self) -> int:
        return self.appointment_participants.filter(
            billing_decision=Appointment.BillingDecision.CHARGE,
            billing_account__isnull=False,
        ).count()


class AppointmentSeries(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        ACTIVE = "active", "Активна"
        COMPLETED = "completed", "Завершена"
        CANCELLED = "cancelled", "Отменена"

    class MaterializationMode(models.TextChoices):
        CREATE_APPOINTMENTS = "create_appointments", "Создавать занятия"
        JOIN_EXISTING = "join_existing", "Присоединять к существующим"

    child = models.ForeignKey(
        Child,
        verbose_name="получатель",
        on_delete=models.CASCADE,
        related_name="appointment_series",
    )
    service = models.ForeignKey(Service, verbose_name="услуга", on_delete=models.PROTECT)
    staff_member = models.ForeignKey(
        StaffMember, verbose_name="специалист", on_delete=models.PROTECT
    )
    room = models.ForeignKey(
        Room, verbose_name="помещение", on_delete=models.PROTECT, null=True, blank=True
    )
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
    session_type = models.CharField(
        "тип занятия",
        max_length=20,
        choices=(
            ("individual", "Индивидуальное"),
            ("group", "Групповое"),
        ),
        default="individual",
    )
    operation_key = models.UUIDField(
        "ключ операции",
        default=uuid.uuid4,
        unique=True,
        editable=False,
    )
    materialization_mode = models.CharField(
        "режим серии",
        max_length=30,
        choices=MaterializationMode.choices,
        default=MaterializationMode.CREATE_APPOINTMENTS,
    )
    operation_fingerprint = models.CharField(
        "отпечаток операции",
        max_length=64,
        blank=True,
        editable=False,
    )
    default_appointment_status = models.CharField(
        "статус создаваемых занятий",
        max_length=30,
        choices=(
            ("proposed", "Предложено"),
            ("confirmed", "Согласовано"),
            ("reserved", "Бронь"),
        ),
        default="proposed",
    )
    allow_unpaid_reserve = models.BooleanField(
        "разрешена бронь сверх оплаты",
        default=False,
    )
    allow_outside_availability = models.BooleanField(
        "разрешено назначение вне графика",
        default=False,
    )
    override_reason = models.TextField("основание исключений", blank=True)
    status = models.CharField("статус", max_length=30, choices=Status.choices, default=Status.DRAFT)

    class Meta:
        verbose_name = "серия занятий"
        verbose_name_plural = "серии занятий"
        ordering = ["start_date", "time"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(materialization_mode="create_appointments")
                    | Q(session_type="group")
                ),
                name="join_existing_requires_group_series",
            ),
        ]

    def __str__(self) -> str:
        return self.title

    def delete(self, *args: object, **kwargs: object) -> None:
        if self.pk and (
            self.occurrences.exists()
            or self.default_participants.exists()
            or self.default_staff_assignments.exists()
        ):
            raise ValidationError("Серию с историей нельзя удалить; измените ее статус.")
        super().delete(*args, **kwargs)

    def clean(self) -> None:
        errors: dict[str, str] = {}
        if self.start_date and self.end_date and self.end_date < self.start_date:
            errors["end_date"] = "Дата окончания не может быть раньше даты начала."
        if (
            self.materialization_mode == self.MaterializationMode.JOIN_EXISTING
            and self.session_type != "group"
        ):
            errors["session_type"] = (
                "Присоединять получателя можно только к групповым занятиям."
            )
        if (
            self.materialization_mode == self.MaterializationMode.JOIN_EXISTING
            and self.allow_unpaid_reserve
        ):
            errors["allow_unpaid_reserve"] = (
                "Присоединение к существующему занятию требует доступной оплаты."
            )
        if self.allow_unpaid_reserve and self.default_appointment_status != "reserved":
            errors["default_appointment_status"] = (
                "Занятия сверх оплаты должны создаваться со статусом «Бронь»."
            )
        if (
            self.allow_unpaid_reserve or self.allow_outside_availability
        ) and not self.override_reason.strip():
            errors["override_reason"] = "Укажите основание для исключения."
        if errors:
            raise ValidationError(errors)

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
        if self.status != self.Status.ACTIVE:
            raise ValidationError("Создавать занятия можно только для активной серии.")
        if self.materialization_mode != self.MaterializationMode.CREATE_APPOINTMENTS:
            raise ValidationError("Эта серия не создает новые занятия.")
        if self.session_type == "group":
            raise ValidationError(
                "Групповая серия создается сервисом групповых программ."
            )

        import datetime as dtmod

        from django.utils import timezone

        from operations import schedule_writes

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
            try:
                with schedule_writes.lock_schedule_write(
                    room_ids=[self.room_id],
                ) as locked:
                    room = locked.room_for(self.room_id)
                    schedule_writes.ensure_room_capacity(
                        starts_at=starts_at,
                        ends_at=ends_at,
                        children=[self.child],
                        staff_members=[self.staff_member],
                        room=room,
                        status=Appointment.Status.CONFIRMED,
                    )
                    Appointment.objects.create(
                        child=self.child,
                        staff_member=self.staff_member,
                        service=self.service,
                        room=room,
                        starts_at=starts_at,
                        ends_at=ends_at,
                        status=Appointment.Status.CONFIRMED,
                        series=self,
                        program_block=self.program_block,
                    )
            except ValidationError:
                continue
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

    class SessionType(models.TextChoices):
        INDIVIDUAL = "individual", "Индивидуальное"
        GROUP = "group", "Групповое"

    child = models.ForeignKey(
        Child, verbose_name="получатель", on_delete=models.CASCADE, related_name="appointments"
    )
    staff_member = models.ForeignKey(
        StaffMember,
        verbose_name="специалист",
        on_delete=models.PROTECT,
        related_name="appointments",
    )
    service = models.ForeignKey(
        Service, verbose_name="услуга", on_delete=models.PROTECT, related_name="appointments"
    )
    room = models.ForeignKey(
        Room,
        verbose_name="помещение",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="appointments",
    )
    starts_at = models.DateTimeField("начало")
    ends_at = models.DateTimeField("окончание")
    session_type = models.CharField(
        "тип занятия",
        max_length=20,
        choices=SessionType.choices,
        default=SessionType.INDIVIDUAL,
    )
    title = models.CharField("название группового занятия", max_length=200, blank=True)
    status = models.CharField(
        "статус", max_length=30, choices=Status.choices, default=Status.CONFIRMED
    )
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
    staff_availability_override = models.BooleanField(
        "назначено вне графика специалиста", default=False
    )
    staff_availability_override_reason = models.TextField(
        "причина назначения вне графика", blank=True
    )
    admin_note = models.TextField("заметка администратора", blank=True)
    specialist_note = models.TextField("заметка специалиста", blank=True)
    specialist_marked_at = models.DateTimeField("специалист отметил", null=True, blank=True)

    class Meta:
        verbose_name = "занятие"
        verbose_name_plural = "занятия"
        ordering = ["starts_at"]

    def participant_label(self) -> str:
        if self.pk:
            names = [
                participant.child.full_name
                for participant in self.participants.select_related("child").order_by(
                    "starts_at_snapshot", "child__last_name", "child__first_name"
                )
            ]
            if names:
                return ", ".join(names)
        if self.child_id:
            return self.child.full_name
        return "Без получателя"

    def __str__(self) -> str:
        local_start = timezone.localtime(self.starts_at)
        subject = self.title.strip() if self.title else self.participant_label()
        return f"{local_start:%d.%m.%Y %H:%M} - {subject} / {self.service}"

    def clean(self) -> None:
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValidationError({"ends_at": "Окончание должно быть позже начала."})
        if self.status in ACTIVE_APPOINTMENT_STATUSES and self.starts_at and self.ends_at:
            self._validate_no_overlap()
            self._validate_staff_availability()
        if self.billing_account_id:
            if self.billing_account.child_id != self.child_id:
                raise ValidationError(
                    {"billing_account": "Счет должен принадлежать этому получателю."}
                )
            if not self.billing_account.can_pay_for(self.service):
                raise ValidationError({"billing_account": "Счет не подходит для этой услуги."})
        if self.program_block_id:
            if self.program_block.program.child_id != self.child_id:
                raise ValidationError(
                    {"program_block": "Блок программы должен принадлежать этому получателю."}
                )
            if self.program_block.service_id != self.service_id:
                raise ValidationError(
                    {"program_block": "Блок программы должен соответствовать услуге занятия."}
                )
        if self.billing_decision == self.BillingDecision.CHARGE and not self.billing_account_id:
            raise ValidationError({"billing_account": "Для списания нужно выбрать счет баланса."})

    def save(
        self,
        *args: object,
        validate_schedule: bool = True,
        sync_legacy: bool = True,
        **kwargs: object,
    ) -> None:
        with transaction.atomic():
            if not self.pk and self.program_block_id and not self.sequence_number:
                ProgramBlock.objects.select_for_update().only("pk").get(
                    pk=self.program_block_id
                )
                participant_max = AppointmentParticipant.objects.filter(
                    program_block_id=self.program_block_id
                ).aggregate(value=Max("sequence_number"))["value"]
                legacy_max = Appointment.objects.filter(
                    program_block_id=self.program_block_id
                ).aggregate(value=Max("sequence_number"))["value"]
                self.sequence_number = max(participant_max or 0, legacy_max or 0) + 1
            if validate_schedule:
                self.full_clean(exclude={"specialist_note"} if not self.pk else None)
            super().save(*args, **kwargs)
            if sync_legacy:
                self._sync_legacy_participant_and_staff_assignment()

    def _validate_no_overlap(self) -> None:
        """Кросс-БД проверка отсутствия пересечений.

        На PostgreSQL дополнительно работает DB-уровневый EXCLUDE constraint
        (см. миграции ``0004_pg_only_constraints`` и ``0011_appointment_session_type_appointment_title_and_more``),
        но Python-проверка
        покрывает оба бэкенда и даёт внятные сообщения об ошибках в формах.
        """
        messages: list[str] = []
        from operations.schedule_validation import appointment_validation_conflicts

        room = None if getattr(self, "_skip_room_limit_validation", False) else self.room
        conflicts = appointment_validation_conflicts(
            self,
            self.starts_at,
            self.ends_at,
            room=room,
        )

        if conflicts.get("child") and conflicts["child"].exists():
            messages.append("у получателя уже есть занятие в это время")
        if conflicts.get("staff") and conflicts["staff"].exists():
            messages.append("специалист уже занят в это время")
        if self.room_id and conflicts.get("room_over_limit"):
            reasons = conflicts.get("room_limit_reasons") or {}
            if reasons.get("staff"):
                messages.append("кабинет уже занят по лимиту специалистов")
            if reasons.get("recipients"):
                messages.append("кабинет уже занят по лимиту получателей")
            if reasons.get("group"):
                messages.append("кабинет не разрешает групповые занятия")
        if messages:
            raise ValidationError("Конфликт расписания: " + ", ".join(messages) + ".")

    def _validate_staff_availability(self) -> None:
        if not self.starts_at or not self.ends_at:
            return
        if self.staff_availability_override:
            return

        from operations.schedule_validation import staff_unavailability_reason

        messages = []
        if self.pk:
            assignments = list(self.staff_assignments.select_related("staff_member").order_by("pk"))
            if assignments:
                for assignment in assignments:
                    if assignment.override_availability:
                        continue
                    reason = staff_unavailability_reason(
                        assignment.staff_member,
                        self.starts_at,
                        self.ends_at,
                    )
                    if reason:
                        messages.append(f"{assignment.staff_member}: {reason}")
            elif self.staff_member_id:
                reason = staff_unavailability_reason(
                    self.staff_member,
                    self.starts_at,
                    self.ends_at,
                )
                if reason:
                    messages.append(reason)
        elif self.staff_member_id:
            reason = staff_unavailability_reason(
                self.staff_member,
                self.starts_at,
                self.ends_at,
            )
            if reason:
                messages.append(reason)

        if messages:
            raise ValidationError("Недоступность специалиста: " + "; ".join(messages) + ".")

    @property
    def duration_minutes(self) -> int:
        return int((self.ends_at - self.starts_at).total_seconds() // 60)

    @property
    def primary_participant(self) -> AppointmentParticipant | None:
        return self.participants.order_by("pk").first()

    @property
    def primary_staff_assignment(self) -> AppointmentStaffAssignment | None:
        return self.staff_assignments.order_by("pk").first()

    def _sync_legacy_participant_and_staff_assignment(self) -> None:
        if not self.pk:
            return
        now = timezone.now()
        participants_qs = self.participants.all()
        has_participants = participants_qs.exists()
        should_sync_legacy_child = self.child_id and (
            not has_participants or participants_qs.filter(child_id=self.child_id).exists()
        )
        if should_sync_legacy_child:
            participant_defaults = {
                "starts_at_snapshot": self.starts_at,
                "ends_at_snapshot": self.ends_at,
                "appointment_status": self.status,
            }
            if not has_participants:
                participant_defaults.update(
                    {
                        "attendance_status": self.attendance_status,
                        "billing_decision": self.billing_decision,
                        "billing_account_id": self.billing_account_id,
                        "program_block_id": self.program_block_id,
                        "sequence_number": self.sequence_number,
                        "admin_note": self.admin_note,
                        "specialist_note": self.specialist_note,
                        "marked_by_staff_at": self.specialist_marked_at,
                    }
                )
            AppointmentParticipant.objects.update_or_create(
                appointment=self,
                child_id=self.child_id,
                defaults=participant_defaults,
            )
        if has_participants:
            participant_updates = {
                "starts_at_snapshot": self.starts_at,
                "ends_at_snapshot": self.ends_at,
                "appointment_status": self.status,
                "updated_at": now,
            }
            if should_sync_legacy_child:
                participants_qs = participants_qs.exclude(child_id=self.child_id)
            participants_qs.update(**participant_updates)

        assignments_qs = self.staff_assignments.all()
        has_assignments = assignments_qs.exists()
        should_sync_legacy_staff = self.staff_member_id and (
            not has_assignments
            or assignments_qs.filter(staff_member_id=self.staff_member_id).exists()
        )
        if should_sync_legacy_staff:
            assignment_defaults = {
                "starts_at_snapshot": self.starts_at,
                "ends_at_snapshot": self.ends_at,
                "appointment_status": self.status,
            }
            if not has_assignments:
                assignment_defaults.update(
                    {
                        "role": AppointmentStaffAssignment.Role.PRIMARY,
                        "override_availability": self.staff_availability_override,
                        "override_reason": self.staff_availability_override_reason,
                    }
                )
            AppointmentStaffAssignment.objects.update_or_create(
                appointment=self,
                staff_member_id=self.staff_member_id,
                defaults=assignment_defaults,
            )
        if has_assignments:
            assignment_updates = {
                "starts_at_snapshot": self.starts_at,
                "ends_at_snapshot": self.ends_at,
                "appointment_status": self.status,
                "updated_at": now,
            }
            if should_sync_legacy_staff:
                assignments_qs = assignments_qs.exclude(staff_member_id=self.staff_member_id)
            assignments_qs.update(**assignment_updates)


class AppointmentParticipant(TimeStampedModel):
    appointment = models.ForeignKey(
        Appointment,
        verbose_name="занятие",
        on_delete=models.CASCADE,
        related_name="participants",
    )
    child = models.ForeignKey(
        Child,
        verbose_name="получатель",
        on_delete=models.CASCADE,
        related_name="appointment_participations",
    )
    attendance_status = models.CharField(
        "посещение",
        max_length=30,
        choices=Appointment.AttendanceStatus.choices,
        default=Appointment.AttendanceStatus.UNKNOWN,
    )
    billing_decision = models.CharField(
        "решение по списанию",
        max_length=30,
        choices=Appointment.BillingDecision.choices,
        default=Appointment.BillingDecision.UNDECIDED,
    )
    billing_account = models.ForeignKey(
        BalanceAccount,
        verbose_name="счет списания",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="appointment_participants",
    )
    price_snapshot = models.DecimalField(
        "цена на момент занятия",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    program_block = models.ForeignKey(
        ProgramBlock,
        verbose_name="блок программы",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="appointment_participants",
    )
    sequence_number = models.PositiveIntegerField("номер в блоке", null=True, blank=True)
    source_participant = models.ForeignKey(
        "self",
        verbose_name="исходный участник",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rescheduled_to",
    )
    admin_note = models.TextField("заметка администратора", blank=True)
    specialist_note = models.TextField("заметка специалиста", blank=True)
    marked_by_staff_at = models.DateTimeField("специалист отметил", null=True, blank=True)
    starts_at_snapshot = models.DateTimeField("начало")
    ends_at_snapshot = models.DateTimeField("окончание")
    appointment_status = models.CharField(
        "статус занятия",
        max_length=30,
        choices=Appointment.Status.choices,
        default=Appointment.Status.CONFIRMED,
    )

    class Meta:
        verbose_name = "участник занятия"
        verbose_name_plural = "участники занятий"
        ordering = ["starts_at_snapshot", "child__last_name", "child__first_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["appointment", "child"], name="unique_appointment_participant_child"
            ),
            models.UniqueConstraint(
                fields=["program_block", "sequence_number"],
                condition=(
                    Q(program_block__isnull=False, sequence_number__isnull=False)
                    & ~Q(
                        appointment_status__in=[
                            Appointment.Status.CANCELLED,
                            Appointment.Status.RESCHEDULED,
                        ]
                    )
                ),
                name="unique_program_block_participant_sequence",
            ),
        ]
        indexes = [
            models.Index(fields=["child", "starts_at_snapshot", "ends_at_snapshot"]),
            models.Index(fields=["appointment", "appointment_status"]),
            models.Index(fields=["program_block", "sequence_number"]),
        ]

    def __str__(self) -> str:
        return f"{self.appointment} / {self.child}"

    def clean(self) -> None:
        if (
            self.ends_at_snapshot
            and self.starts_at_snapshot
            and self.ends_at_snapshot <= self.starts_at_snapshot
        ):
            raise ValidationError({"ends_at_snapshot": "Окончание должно быть позже начала."})
        if self.billing_account_id:
            if self.billing_account.child_id != self.child_id:
                raise ValidationError(
                    {"billing_account": "Счет должен принадлежать этому получателю."}
                )
            if self.appointment_id and not self.billing_account.can_pay_for(
                self.appointment.service
            ):
                raise ValidationError({"billing_account": "Счет не подходит для услуги занятия."})
        if (
            self.billing_decision == Appointment.BillingDecision.CHARGE
            and not self.billing_account_id
        ):
            raise ValidationError({"billing_account": "Для списания нужно выбрать счет баланса."})
        if self.program_block_id and self.program_block.program.child_id != self.child_id:
            raise ValidationError(
                {"program_block": "Блок программы должен принадлежать этому получателю."}
            )
        if (
            self.program_block_id
            and self.appointment_id
            and self.program_block.service_id != self.appointment.service_id
        ):
            raise ValidationError(
                {"program_block": "Блок программы должен соответствовать услуге занятия."}
            )

    def save(self, *args: object, **kwargs: object) -> None:
        with transaction.atomic():
            if self.program_block_id and not self.sequence_number:
                ProgramBlock.objects.select_for_update().only("pk").get(
                    pk=self.program_block_id
                )
                qs = AppointmentParticipant.objects.filter(
                    program_block_id=self.program_block_id
                )
                if self.pk:
                    qs = qs.exclude(pk=self.pk)
                participant_max = qs.aggregate(value=Max("sequence_number"))["value"]
                legacy_max = Appointment.objects.filter(
                    program_block_id=self.program_block_id
                ).aggregate(value=Max("sequence_number"))["value"]
                self.sequence_number = max(participant_max or 0, legacy_max or 0) + 1
                update_fields = kwargs.get("update_fields")
                if update_fields is not None:
                    kwargs["update_fields"] = set(update_fields) | {"sequence_number"}
            if not self.program_block_id:
                self.sequence_number = None
                update_fields = kwargs.get("update_fields")
                if update_fields is not None:
                    kwargs["update_fields"] = set(update_fields) | {"sequence_number"}
            self.full_clean()
            super().save(*args, **kwargs)


class AppointmentStaffAssignment(TimeStampedModel):
    class Role(models.TextChoices):
        PRIMARY = "primary", "Основной"
        ASSISTANT = "assistant", "Ассистент"
        SUBSTITUTE = "substitute", "Замена"
        OBSERVER = "observer", "Наблюдатель"

    appointment = models.ForeignKey(
        Appointment,
        verbose_name="занятие",
        on_delete=models.CASCADE,
        related_name="staff_assignments",
    )
    staff_member = models.ForeignKey(
        StaffMember,
        verbose_name="специалист",
        on_delete=models.PROTECT,
        related_name="appointment_assignments",
    )
    role = models.CharField("роль", max_length=30, choices=Role.choices, default=Role.PRIMARY)
    starts_at_snapshot = models.DateTimeField("начало")
    ends_at_snapshot = models.DateTimeField("окончание")
    appointment_status = models.CharField(
        "статус занятия",
        max_length=30,
        choices=Appointment.Status.choices,
        default=Appointment.Status.CONFIRMED,
    )
    override_availability = models.BooleanField("назначено вне графика", default=False)
    override_reason = models.TextField("причина назначения вне графика", blank=True)

    class Meta:
        verbose_name = "назначение специалиста"
        verbose_name_plural = "назначения специалистов"
        ordering = ["starts_at_snapshot", "staff_member__full_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["appointment", "staff_member"], name="unique_appointment_staff_assignment"
            ),
        ]
        indexes = [
            models.Index(fields=["staff_member", "starts_at_snapshot", "ends_at_snapshot"]),
            models.Index(fields=["appointment", "appointment_status"]),
        ]

    def __str__(self) -> str:
        return f"{self.appointment} / {self.staff_member}"

    def clean(self) -> None:
        if (
            self.ends_at_snapshot
            and self.starts_at_snapshot
            and self.ends_at_snapshot <= self.starts_at_snapshot
        ):
            raise ValidationError({"ends_at_snapshot": "Окончание должно быть позже начала."})


class AppointmentSeriesParticipant(TimeStampedModel):
    series = models.ForeignKey(
        AppointmentSeries,
        verbose_name="серия",
        on_delete=models.CASCADE,
        related_name="default_participants",
    )
    child = models.ForeignKey(
        Child,
        verbose_name="получатель",
        on_delete=models.PROTECT,
        related_name="appointment_series_memberships",
    )
    program_block = models.ForeignKey(
        ProgramBlock,
        verbose_name="каскад",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="series_memberships",
    )
    billing_account = models.ForeignKey(
        BalanceAccount,
        verbose_name="счет оплаты",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="series_memberships",
    )
    position = models.PositiveIntegerField("порядок", default=1)

    class Meta:
        verbose_name = "участник серии"
        verbose_name_plural = "участники серий"
        ordering = ["series", "position", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["series", "child"],
                name="unique_appointment_series_child",
            ),
        ]
        indexes = [models.Index(fields=["series", "position"])]

    def __str__(self) -> str:
        return f"{self.series} / {self.child}"

    def clean(self) -> None:
        errors: dict[str, str] = {}
        if self.program_block_id:
            if self.child_id and self.program_block.program.child_id != self.child_id:
                errors["program_block"] = "Каскад должен принадлежать получателю серии."
            if self.series_id and self.program_block.service_id != self.series.service_id:
                errors["program_block"] = "Каскад должен соответствовать услуге серии."
        if self.billing_account_id:
            if self.child_id and self.billing_account.child_id != self.child_id:
                errors["billing_account"] = "Счет должен принадлежать получателю серии."
            if self.series_id and not self.billing_account.can_pay_for(self.series.service):
                errors["billing_account"] = "Счет не подходит для услуги серии."
        if errors:
            raise ValidationError(errors)

    def save(self, *args: object, **kwargs: object) -> None:
        self.full_clean()
        super().save(*args, **kwargs)


class AppointmentSeriesStaffAssignment(TimeStampedModel):
    class Role(models.TextChoices):
        PRIMARY = "primary", "Основной"
        ASSISTANT = "assistant", "Ассистент"
        SUBSTITUTE = "substitute", "Замена"
        OBSERVER = "observer", "Наблюдатель"

    series = models.ForeignKey(
        AppointmentSeries,
        verbose_name="серия",
        on_delete=models.CASCADE,
        related_name="default_staff_assignments",
    )
    staff_member = models.ForeignKey(
        StaffMember,
        verbose_name="специалист",
        on_delete=models.PROTECT,
        related_name="appointment_series_assignments",
    )
    role = models.CharField("роль", max_length=30, choices=Role.choices, default=Role.PRIMARY)
    override_availability = models.BooleanField("назначать вне графика", default=False)
    override_reason = models.TextField("основание выхода вне графика", blank=True)

    class Meta:
        verbose_name = "назначение специалиста серии"
        verbose_name_plural = "назначения специалистов серий"
        ordering = ["series", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["series", "staff_member"],
                name="unique_appointment_series_staff",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.series} / {self.staff_member}"

    def clean(self) -> None:
        if self.override_availability and not self.override_reason.strip():
            raise ValidationError(
                {"override_reason": "Укажите основание назначения вне графика."}
            )

    def save(self, *args: object, **kwargs: object) -> None:
        self.full_clean()
        super().save(*args, **kwargs)


class AppointmentSeriesOccurrence(TimeStampedModel):
    class Outcome(models.TextChoices):
        CREATED = "created", "Создано"
        JOINED = "joined", "Присоединено"
        SKIPPED = "skipped", "Пропущено"
        UNCHANGED = "unchanged", "Без изменений"

    series = models.ForeignKey(
        AppointmentSeries,
        verbose_name="серия",
        on_delete=models.CASCADE,
        related_name="occurrences",
    )
    scheduled_starts_at = models.DateTimeField("плановое начало")
    appointment = models.ForeignKey(
        Appointment,
        verbose_name="занятие",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="series_occurrences",
    )
    appointment_participant = models.ForeignKey(
        AppointmentParticipant,
        verbose_name="участник занятия",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="series_join_occurrences",
    )
    outcome = models.CharField("результат", max_length=20, choices=Outcome.choices)
    reason_code = models.CharField("код причины", max_length=50, blank=True)
    reason = models.TextField("причина", blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="создал",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="appointment_series_occurrences",
    )

    class Meta:
        verbose_name = "результат даты серии"
        verbose_name_plural = "результаты дат серии"
        ordering = ["series", "scheduled_starts_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["series", "scheduled_starts_at"],
                name="unique_appointment_series_occurrence",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        outcome="joined",
                        appointment__isnull=False,
                        appointment_participant__isnull=False,
                    )
                    | (
                        ~Q(outcome="joined")
                        & Q(appointment_participant__isnull=True)
                    )
                ),
                name="joined_occurrence_requires_participant",
            ),
        ]
        indexes = [
            models.Index(fields=["appointment", "series"]),
        ]

    def __str__(self) -> str:
        local_start = timezone.localtime(self.scheduled_starts_at)
        return f"{self.series} / {local_start:%d.%m.%Y %H:%M} / {self.get_outcome_display()}"

    def save(self, *args: object, **kwargs: object) -> None:
        if self.pk:
            raise ValidationError("Результат даты серии неизменяем.")
        self.full_clean()
        super().save(*args, **kwargs)

    def clean(self) -> None:
        errors: dict[str, str] = {}
        if self.outcome == self.Outcome.JOINED:
            if not self.appointment_id:
                errors["appointment"] = "Для присоединения нужно групповое занятие."
            if not self.appointment_participant_id:
                errors["appointment_participant"] = (
                    "Для присоединения нужен фактический участник."
                )
            elif (
                self.appointment_id
                and self.appointment_participant.appointment_id != self.appointment_id
            ):
                errors["appointment_participant"] = (
                    "Участник должен относиться к этому же занятию."
                )
        elif self.appointment_participant_id:
            errors["appointment_participant"] = (
                "Участник занятия указывается только для исхода «Присоединено»."
            )
        if errors:
            raise ValidationError(errors)

    def delete(self, *args: object, **kwargs: object) -> None:
        raise ValidationError("Результат даты серии нельзя удалить.")


def room_usage_counts(appointment_qs: QuerySet[Appointment]) -> tuple[int, int]:
    """Count room usage across snapshot rows plus legacy fallback appointments."""
    appointment_ids = list(appointment_qs.values_list("id", flat=True))
    if not appointment_ids:
        return 0, 0

    staff_rows = list(
        AppointmentStaffAssignment.objects.filter(
            appointment_id__in=appointment_ids,
            appointment_status__in=ACTIVE_APPOINTMENT_STATUSES,
        )
        .values_list("appointment_id", "staff_member_id")
        .distinct()
    )
    participant_rows = list(
        AppointmentParticipant.objects.filter(
            appointment_id__in=appointment_ids,
            appointment_status__in=ACTIVE_APPOINTMENT_STATUSES,
        )
        .values_list("appointment_id", "child_id")
        .distinct()
    )

    appointments_with_staff_snapshots = {appointment_id for appointment_id, _ in staff_rows}
    appointments_with_participant_snapshots = {
        appointment_id for appointment_id, _ in participant_rows
    }
    legacy_staff_count = appointment_qs.exclude(id__in=appointments_with_staff_snapshots).count()
    legacy_recipient_count = appointment_qs.exclude(
        id__in=appointments_with_participant_snapshots
    ).count()
    return len(staff_rows) + legacy_staff_count, len(participant_rows) + legacy_recipient_count


class AppointmentRoomOverride(TimeStampedModel):
    class OverrideType(models.TextChoices):
        STAFF_LIMIT = "staff_limit", "Лимит специалистов"
        RECIPIENT_LIMIT = "recipient_limit", "Лимит получателей"
        EXCLUSIVE_ROOM = "exclusive_room", "Эксклюзивный кабинет"
        ROOM_INACTIVE = "room_inactive", "Неактивный кабинет"
        OTHER = "other", "Другое"

    appointment = models.ForeignKey(
        Appointment,
        verbose_name="занятие",
        on_delete=models.CASCADE,
        related_name="room_overrides",
    )
    override_type = models.CharField("тип разрешения", max_length=30, choices=OverrideType.choices)
    reason = models.TextField("причина")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="создал",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_room_overrides",
    )

    class Meta:
        verbose_name = "разрешение кабинета"
        verbose_name_plural = "разрешения кабинетов"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.appointment}: {self.get_override_type_display()}"

    def clean(self) -> None:
        if not self.reason.strip():
            raise ValidationError({"reason": "Для одноразового разрешения нужна причина."})


class AppointmentReschedulePlan(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        READY = "ready", "Готов к проверке"
        NEEDS_RECHECK = "needs_recheck", "Нужна перепроверка"
        APPLIED = "applied", "Применен"
        CANCELLED = "cancelled", "Отменен"

    class PlanType(models.TextChoices):
        SINGLE_MOVE = "single_move", "Перенос занятия"
        CASCADE_SHIFT = "cascade_shift", "Каскадный сдвиг"
        STAFF_ABSENCE = "staff_absence", "Отсутствие специалиста"
        MANUAL = "manual", "Ручной план"

    status = models.CharField("статус", max_length=30, choices=Status.choices, default=Status.DRAFT)
    plan_type = models.CharField(
        "тип плана", max_length=30, choices=PlanType.choices, default=PlanType.SINGLE_MOVE
    )
    root_appointment = models.ForeignKey(
        Appointment,
        verbose_name="исходное занятие",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reschedule_plans",
    )
    staff_member = models.ForeignKey(
        StaffMember,
        verbose_name="специалист",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reschedule_plans",
    )
    date_from = models.DateField("дата начала", null=True, blank=True)
    date_to = models.DateField("дата окончания", null=True, blank=True)
    reason = models.TextField("причина", blank=True)
    validation_summary = models.JSONField("сводка проверки", default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="создал",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_reschedule_plans",
    )
    applied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="применил",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="applied_reschedule_plans",
    )
    applied_at = models.DateTimeField("применено", null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="отменил",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cancelled_reschedule_plans",
    )
    cancelled_at = models.DateTimeField("отменено", null=True, blank=True)

    class Meta:
        verbose_name = "план переноса расписания"
        verbose_name_plural = "планы переноса расписания"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(date_from__isnull=True)
                    | Q(date_to__isnull=True)
                    | Q(date_to__gte=models.F("date_from"))
                ),
                name="reschedule_plan_dates_order",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(plan_type__in=["single_move", "cascade_shift"])
                    | Q(root_appointment__isnull=False)
                ),
                name="reschedule_plan_root_required",
            ),
            models.CheckConstraint(
                condition=(~Q(plan_type="staff_absence") | Q(staff_member__isnull=False)),
                name="reschedule_plan_staff_required",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["root_appointment", "status"]),
            models.Index(fields=["staff_member", "date_from", "date_to"]),
        ]

    def __str__(self) -> str:
        if self.root_appointment_id:
            return f"{self.get_plan_type_display()}: {self.root_appointment}"
        if self.staff_member_id:
            return f"{self.get_plan_type_display()}: {self.staff_member}"
        return self.get_plan_type_display()

    def clean(self) -> None:
        if self.date_from and self.date_to and self.date_to < self.date_from:
            raise ValidationError({"date_to": "Дата окончания не может быть раньше даты начала."})
        if (
            self.plan_type
            in {
                self.PlanType.SINGLE_MOVE,
                self.PlanType.CASCADE_SHIFT,
            }
            and not self.root_appointment_id
        ):
            raise ValidationError(
                {"root_appointment": "Для плана переноса нужно исходное занятие."}
            )
        if self.plan_type == self.PlanType.STAFF_ABSENCE and not self.staff_member_id:
            raise ValidationError({"staff_member": "Для плана отсутствия нужен специалист."})


class AppointmentRescheduleChain(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        READY = "ready", "Готова"
        STALE = "stale", "Устарела"
        APPLYING = "applying", "Применяется"
        APPLIED = "applied", "Применена"
        FAILED = "failed", "Ошибка"
        CANCELLED = "cancelled", "Отменена"

    class ApplyPolicy(models.TextChoices):
        ATOMIC_ALL_OR_NOTHING = "atomic_all_or_nothing", "Атомарно все или ничего"

    plan = models.ForeignKey(
        AppointmentReschedulePlan,
        verbose_name="план",
        on_delete=models.CASCADE,
        related_name="chains",
    )
    title = models.CharField("название", max_length=200, blank=True)
    status = models.CharField("статус", max_length=30, choices=Status.choices, default=Status.DRAFT)
    apply_policy = models.CharField(
        "правило применения",
        max_length=40,
        choices=ApplyPolicy.choices,
        default=ApplyPolicy.ATOMIC_ALL_OR_NOTHING,
    )
    validation_summary = models.JSONField("сводка проверки", default=dict, blank=True)
    admin_note = models.TextField("заметка администратора", blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="создал",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_reschedule_chains",
    )
    applied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="применил",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="applied_reschedule_chains",
    )
    applied_at = models.DateTimeField("применено", null=True, blank=True)

    class Meta:
        verbose_name = "цепочка переноса расписания"
        verbose_name_plural = "цепочки переноса расписания"
        ordering = ["plan", "created_at"]
        indexes = [
            models.Index(fields=["plan", "status"]),
            models.Index(fields=["status", "updated_at"]),
        ]

    def __str__(self) -> str:
        return self.title or f"{self.plan} / цепочка {self.pk or ''}".strip()


class AppointmentRescheduleStep(TimeStampedModel):
    class ActionType(models.TextChoices):
        MOVE = "move", "Перенести"
        CANCEL = "cancel", "Отменить"
        KEEP = "keep", "Оставить"
        REVIEW_CONFLICT = "review_conflict", "Разобрать конфликт"

    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает решения"
        VALID = "valid", "Проверен"
        STALE = "stale", "Устарел"
        APPLIED = "applied", "Применен"
        SKIPPED = "skipped", "Пропущен"
        FAILED = "failed", "Ошибка"

    class ConfirmationStatus(models.TextChoices):
        NOT_REQUESTED = "not_requested", "Не запрошено"
        WAITING = "waiting", "Ожидает ответов"
        APPROVED = "approved", "Согласовано"
        DECLINED = "declined", "Есть отказ"

    plan = models.ForeignKey(
        AppointmentReschedulePlan,
        verbose_name="план",
        on_delete=models.CASCADE,
        related_name="steps",
    )
    chain = models.ForeignKey(
        AppointmentRescheduleChain,
        verbose_name="цепочка",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="steps",
    )
    position = models.PositiveIntegerField("порядок")
    chain_position = models.PositiveIntegerField("порядок в цепочке", null=True, blank=True)
    chain_required = models.BooleanField("обязателен в цепочке", default=False)
    action_type = models.CharField(
        "действие", max_length=30, choices=ActionType.choices, default=ActionType.MOVE
    )
    status = models.CharField(
        "статус", max_length=30, choices=Status.choices, default=Status.PENDING
    )
    source_appointment = models.ForeignKey(
        Appointment,
        verbose_name="исходное занятие",
        on_delete=models.PROTECT,
        related_name="reschedule_steps",
    )
    blocking_appointment = models.ForeignKey(
        Appointment,
        verbose_name="конфликтующее занятие",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="blocking_reschedule_steps",
    )
    created_appointment = models.ForeignKey(
        Appointment,
        verbose_name="созданное занятие",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_by_reschedule_steps",
    )
    proposed_starts_at = models.DateTimeField("предложенное начало", null=True, blank=True)
    proposed_ends_at = models.DateTimeField("предложенное окончание", null=True, blank=True)
    proposed_room = models.ForeignKey(
        Room,
        verbose_name="предложенный кабинет",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reschedule_steps",
    )
    proposed_primary_staff = models.ForeignKey(
        StaffMember,
        verbose_name="предложенный основной специалист",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reschedule_steps",
    )
    staff_snapshot = models.JSONField("снимок специалистов", default=list, blank=True)
    participant_snapshot = models.JSONField("снимок участников", default=list, blank=True)
    conflict_snapshot = models.JSONField("снимок конфликтов", default=dict, blank=True)
    validation_messages = models.JSONField("сообщения проверки", default=list, blank=True)
    confirmation_status = models.CharField(
        "статус согласования",
        max_length=30,
        choices=ConfirmationStatus.choices,
        default=ConfirmationStatus.NOT_REQUESTED,
    )
    confirmation_summary = models.JSONField("сводка согласования", default=dict, blank=True)
    requires_staff_override = models.BooleanField(
        "требует разрешение выхода вне графика", default=False
    )
    requires_room_override = models.BooleanField("требует разрешение кабинета", default=False)
    admin_note = models.TextField("заметка администратора", blank=True)

    class Meta:
        verbose_name = "шаг плана переноса"
        verbose_name_plural = "шаги плана переноса"
        ordering = ["plan", "position"]
        constraints = [
            models.UniqueConstraint(
                fields=["plan", "position"], name="unique_reschedule_step_position"
            ),
            models.UniqueConstraint(
                fields=["chain", "chain_position"],
                condition=Q(chain__isnull=False),
                name="unique_reschedule_step_chain_position",
            ),
            models.CheckConstraint(
                condition=(
                    Q(proposed_starts_at__isnull=True)
                    | Q(proposed_ends_at__isnull=True)
                    | Q(proposed_ends_at__gt=models.F("proposed_starts_at"))
                ),
                name="reschedule_step_time_order",
            ),
        ]
        indexes = [
            models.Index(fields=["source_appointment", "status"]),
            models.Index(fields=["confirmation_status", "status"]),
            models.Index(fields=["proposed_starts_at", "proposed_ends_at"]),
            models.Index(fields=["chain", "chain_position"]),
            models.Index(fields=["chain", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.plan} / {self.position}. {self.get_action_type_display()}"

    def clean(self) -> None:
        if (
            self.proposed_starts_at
            and self.proposed_ends_at
            and self.proposed_ends_at <= self.proposed_starts_at
        ):
            raise ValidationError(
                {"proposed_ends_at": "Предложенное окончание должно быть позже начала."}
            )
        if self.action_type == self.ActionType.MOVE:
            missing_fields = []
            if not self.proposed_starts_at:
                missing_fields.append("proposed_starts_at")
            if not self.proposed_ends_at:
                missing_fields.append("proposed_ends_at")
            if not self.proposed_primary_staff_id:
                missing_fields.append("proposed_primary_staff")
            if missing_fields:
                raise ValidationError(
                    {
                        field: "Для шага переноса нужно заполнить это поле."
                        for field in missing_fields
                    }
                )


class AppointmentRescheduleStepDependency(TimeStampedModel):
    class RelationType(models.TextChoices):
        FREES_TARGET_SLOT = "frees_target_slot", "Освобождает целевое окно"
        MUST_APPLY_BEFORE = "must_apply_before", "Должен примениться раньше"

    plan = models.ForeignKey(
        AppointmentReschedulePlan,
        verbose_name="план",
        on_delete=models.CASCADE,
        related_name="step_dependencies",
    )
    chain = models.ForeignKey(
        AppointmentRescheduleChain,
        verbose_name="цепочка",
        on_delete=models.CASCADE,
        related_name="dependencies",
    )
    predecessor_step = models.ForeignKey(
        AppointmentRescheduleStep,
        verbose_name="предшествующий шаг",
        on_delete=models.CASCADE,
        related_name="unlocks_successors",
    )
    successor_step = models.ForeignKey(
        AppointmentRescheduleStep,
        verbose_name="следующий шаг",
        on_delete=models.CASCADE,
        related_name="dependency_edges",
    )
    relation_type = models.CharField(
        "тип зависимости",
        max_length=40,
        choices=RelationType.choices,
        default=RelationType.FREES_TARGET_SLOT,
    )
    reason = models.TextField("причина", blank=True)
    snapshot = models.JSONField("снимок", default=dict, blank=True)

    class Meta:
        verbose_name = "зависимость шага переноса"
        verbose_name_plural = "зависимости шагов переноса"
        ordering = ["chain", "predecessor_step__position", "successor_step__position"]
        constraints = [
            models.UniqueConstraint(
                fields=["chain", "predecessor_step", "successor_step", "relation_type"],
                name="unique_reschedule_step_dependency",
            ),
            models.CheckConstraint(
                condition=~Q(predecessor_step=models.F("successor_step")),
                name="reschedule_dependency_not_self",
            ),
        ]
        indexes = [
            models.Index(fields=["chain", "successor_step"]),
            models.Index(fields=["chain", "predecessor_step"]),
            models.Index(fields=["relation_type", "chain"]),
        ]

    def __str__(self) -> str:
        return f"{self.predecessor_step_id} -> {self.successor_step_id}"

    def clean(self) -> None:
        errors = {}
        if self.chain_id and self.plan_id and self.chain.plan_id != self.plan_id:
            errors["chain"] = "Цепочка должна принадлежать тому же плану."
        if (
            self.plan_id
            and self.predecessor_step_id
            and self.predecessor_step.plan_id != self.plan_id
        ):
            errors["predecessor_step"] = "Предшествующий шаг должен принадлежать тому же плану."
        if self.plan_id and self.successor_step_id and self.successor_step.plan_id != self.plan_id:
            errors["successor_step"] = "Следующий шаг должен принадлежать тому же плану."
        if (
            self.chain_id
            and self.predecessor_step_id
            and self.predecessor_step.chain_id
            and self.predecessor_step.chain_id != self.chain_id
        ):
            errors["predecessor_step"] = "Предшествующий шаг должен относиться к этой цепочке."
        if (
            self.chain_id
            and self.successor_step_id
            and self.successor_step.chain_id
            and self.successor_step.chain_id != self.chain_id
        ):
            errors["successor_step"] = "Следующий шаг должен относиться к этой цепочке."
        if self.predecessor_step_id and self.predecessor_step_id == self.successor_step_id:
            errors["successor_step"] = "Шаг не может зависеть от самого себя."
        if errors:
            raise ValidationError(errors)


class LedgerEntry(TimeStampedModel):
    class EntryType(models.TextChoices):
        CREDIT = "credit", "Пополнение"
        DEBIT = "debit", "Списание"
        CORRECTION = "correction", "Корректировка"
        TRANSFER = "transfer", "Перенос"

    class TransferSide(models.TextChoices):
        DEBIT = "debit", "Списание"
        CREDIT = "credit", "Пополнение"

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
    appointment_participant = models.ForeignKey(
        AppointmentParticipant,
        verbose_name="участник занятия",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ledger_entries",
    )
    price_snapshot = models.DecimalField(
        "цена на момент списания",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    balance_transfer = models.ForeignKey(
        "BalanceTransfer",
        verbose_name="операция переноса",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="ledger_entries",
    )
    transfer_side = models.CharField(
        "сторона переноса",
        max_length=10,
        choices=TransferSide.choices,
        null=True,
        blank=True,
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
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(balance_transfer__isnull=True, transfer_side__isnull=True)
                    | Q(
                        balance_transfer__isnull=False,
                        transfer_side="debit",
                        entry_type="transfer",
                        amount__lt=0,
                    )
                    | Q(
                        balance_transfer__isnull=False,
                        transfer_side="credit",
                        entry_type="transfer",
                        amount__gt=0,
                    )
                ),
                name="ledger_transfer_link_matches_entry",
            ),
            models.UniqueConstraint(
                fields=["balance_transfer", "transfer_side"],
                condition=Q(balance_transfer__isnull=False),
                name="unique_balance_transfer_ledger_side",
            ),
        ]
        indexes = [models.Index(fields=["balance_transfer", "transfer_side"])]

    def __str__(self) -> str:
        return f"{self.get_entry_type_display()} {self.amount} ({self.account})"

    def clean(self) -> None:
        errors = {}
        if self.entry_type == self.EntryType.CREDIT and self.amount < 0:
            errors["amount"] = "Пополнение должно быть положительным."
        if self.entry_type == self.EntryType.DEBIT and self.amount > 0:
            errors["amount"] = "Списание должно быть отрицательным."
        if self.balance_transfer_id:
            if self.entry_type != self.EntryType.TRANSFER:
                errors["balance_transfer"] = "Операция переноса допустима только для ledger-переноса."
            if self.transfer_side not in {self.TransferSide.DEBIT, self.TransferSide.CREDIT}:
                errors["transfer_side"] = "Для операции переноса нужно указать сторону."
            elif self.transfer_side == self.TransferSide.DEBIT and self.amount >= 0:
                errors["amount"] = "Debit переноса должен быть отрицательным."
            elif self.transfer_side == self.TransferSide.CREDIT and self.amount <= 0:
                errors["amount"] = "Credit переноса должен быть положительным."
        elif self.transfer_side:
            errors["transfer_side"] = "Сторона переноса требует связанную операцию переноса."
        if (
            self.appointment_id
            and not self.appointment_participant_id
            and self.appointment.child_id != self.account.child_id
        ):
            errors["appointment"] = "Занятие должно относиться к получателю счета."
        if (
            self.appointment_participant_id
            and self.appointment_participant.child_id != self.account.child_id
        ):
            errors["appointment_participant"] = "Участник занятия должен относиться к получателю счета."
        if (
            self.appointment_id
            and self.appointment_participant_id
            and self.appointment_participant.appointment_id != self.appointment_id
        ):
            errors["appointment_participant"] = "Участник должен относиться к выбранному занятию."
        if errors:
            raise ValidationError(errors)


class BalanceTransfer(TimeStampedModel):
    """Immutable business fact that connects both sides of a balance transfer."""

    class OperationKind(models.TextChoices):
        DIRECT = "direct", "Перенос в одинаковых единицах"
        MONEY_TO_SESSIONS = "money_to_sessions", "Конвертация рублей в занятия"

    from_account = models.ForeignKey(
        BalanceAccount,
        verbose_name="исходный счет",
        on_delete=models.PROTECT,
        related_name="outgoing_balance_transfers",
    )
    to_account = models.ForeignKey(
        BalanceAccount,
        verbose_name="целевой счет",
        on_delete=models.PROTECT,
        related_name="incoming_balance_transfers",
    )
    program_block = models.ForeignKey(
        ProgramBlock,
        verbose_name="каскад программы",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="balance_transfers",
    )
    operation_kind = models.CharField(
        "вид операции", max_length=30, choices=OperationKind.choices
    )
    amount_from = models.DecimalField("списано", max_digits=12, decimal_places=2)
    amount_to = models.DecimalField("зачислено", max_digits=12, decimal_places=2)
    from_unit_snapshot = models.CharField(
        "единица исходного счета на момент операции",
        max_length=20,
        choices=BalanceAccount.Unit.choices,
    )
    to_unit_snapshot = models.CharField(
        "единица целевого счета на момент операции",
        max_length=20,
        choices=BalanceAccount.Unit.choices,
    )
    conversion_rate = models.DecimalField(
        "курс конвертации",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    reason = models.TextField("основание")
    idempotency_key = models.UUIDField("ключ идемпотентности", default=uuid.uuid4, unique=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="операцию создал",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="created_balance_transfers",
    )

    class Meta:
        verbose_name = "операция переноса баланса"
        verbose_name_plural = "операции переноса балансов"
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.CheckConstraint(
                condition=~Q(from_account=F("to_account")),
                name="balance_transfer_accounts_differ",
            ),
            models.CheckConstraint(
                condition=Q(amount_from__gt=0, amount_to__gt=0),
                name="balance_transfer_amounts_positive",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        operation_kind="direct",
                        conversion_rate__isnull=True,
                        from_unit_snapshot=F("to_unit_snapshot"),
                        amount_from=F("amount_to"),
                    )
                    | Q(
                        operation_kind="money_to_sessions",
                        conversion_rate__gt=0,
                        from_unit_snapshot=BalanceAccount.Unit.MONEY,
                        to_unit_snapshot=BalanceAccount.Unit.SESSIONS,
                    )
                ),
                name="balance_transfer_kind_snapshot_rules",
            ),
        ]
        indexes = [
            models.Index(fields=["from_account", "-created_at"]),
            models.Index(fields=["to_account", "-created_at"]),
            models.Index(fields=["program_block", "-created_at"]),
        ]

    immutable_fields = (
        "from_account_id",
        "to_account_id",
        "program_block_id",
        "operation_kind",
        "amount_from",
        "amount_to",
        "from_unit_snapshot",
        "to_unit_snapshot",
        "conversion_rate",
        "reason",
        "idempotency_key",
        "created_by_id",
    )

    def clean(self) -> None:
        errors = {}
        if self.from_account_id and self.from_account_id == self.to_account_id:
            errors["to_account"] = "Нельзя переносить в тот же счет."
        if self.amount_from is None or self.amount_from <= 0:
            errors["amount_from"] = "Списываемая сумма должна быть положительной."
        if self.amount_to is None or self.amount_to <= 0:
            errors["amount_to"] = "Зачисляемая сумма должна быть положительной."
        if not self.reason or not self.reason.strip():
            errors["reason"] = "Укажите основание финансовой операции."
        if self.operation_kind == self.OperationKind.DIRECT:
            if self.from_unit_snapshot != self.to_unit_snapshot:
                errors["to_unit_snapshot"] = "Прямой перенос требует одинаковые единицы учета."
            if self.amount_from is not None and self.amount_to is not None and self.amount_from != self.amount_to:
                errors["amount_to"] = "Прямой перенос должен сохранить одинаковую величину."
            if self.conversion_rate is not None:
                errors["conversion_rate"] = "Для прямого переноса курс не указывается."
        elif self.operation_kind == self.OperationKind.MONEY_TO_SESSIONS:
            if self.from_unit_snapshot != BalanceAccount.Unit.MONEY:
                errors["from_unit_snapshot"] = "Конвертация начинается со счета в рублях."
            if self.to_unit_snapshot != BalanceAccount.Unit.SESSIONS:
                errors["to_unit_snapshot"] = "Конвертация зачисляет занятия."
            if self.conversion_rate is None or self.conversion_rate <= 0:
                errors["conversion_rate"] = "Для конвертации нужен положительный курс."
            elif (
                self.amount_from is not None
                and self.amount_to is not None
                and self.amount_from != self.amount_to * self.conversion_rate
            ):
                errors["amount_from"] = "Списанная сумма должна соответствовать курсу и количеству занятий."
        if self.program_block_id and self.to_account_id:
            if self.program_block.program.child_id != self.to_account.child_id:
                errors["program_block"] = "Каскад и целевой счет должны относиться к одному получателю."
            elif (
                self.program_block.balance_account_id
                and self.program_block.balance_account_id != self.to_account_id
            ):
                errors["to_account"] = "Целевой счет должен совпадать со счетом выбранного каскада."
            elif not self.to_account.can_pay_for(self.program_block.service):
                errors["to_account"] = "Целевой счет не подходит для услуги каскада."
        if errors:
            raise ValidationError(errors)

    def _ensure_immutable_fields(self) -> None:
        if not self.pk:
            return
        current = type(self).objects.get(pk=self.pk)
        if any(getattr(self, field) != getattr(current, field) for field in self.immutable_fields):
            raise ValidationError(
                "Зафиксированный перенос нельзя изменять. Для исправления нужна новая операция."
            )

    def save(self, *args: object, **kwargs: object) -> None:
        self.full_clean()
        self._ensure_immutable_fields()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.get_operation_kind_display()} #{self.pk}"


class FinancialIntegrityCheckRun(TimeStampedModel):
    class RunType(models.TextChoices):
        MANUAL = "manual", "Ручной запуск"
        SCHEDULED = "scheduled", "По расписанию"
        MANAGEMENT_COMMAND = "management_command", "Команда управления"
        SYSTEM = "system", "Системный запуск"

    class Status(models.TextChoices):
        RUNNING = "running", "Выполняется"
        COMPLETED = "completed", "Завершено"
        FAILED = "failed", "Ошибка"

    run_type = models.CharField(
        "тип запуска",
        max_length=30,
        choices=RunType.choices,
        default=RunType.MANUAL,
    )
    status = models.CharField(
        "статус",
        max_length=30,
        choices=Status.choices,
        default=Status.RUNNING,
    )
    started_at = models.DateTimeField("начало проверки", default=timezone.now)
    finished_at = models.DateTimeField("окончание проверки", null=True, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="запустил",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="financial_integrity_check_runs",
    )
    candidate_count = models.PositiveIntegerField("проверено занятий", default=0)
    issue_count = models.PositiveIntegerField("всего расхождений", default=0)
    error_count = models.PositiveIntegerField("ошибок", default=0)
    warning_count = models.PositiveIntegerField("предупреждений", default=0)
    info_count = models.PositiveIntegerField("информационных", default=0)
    error_message = models.TextField("сообщение ошибки", blank=True)

    class Meta:
        verbose_name = "запуск финансовой проверки"
        verbose_name_plural = "запуски финансовых проверок"
        ordering = ["-started_at", "-pk"]
        indexes = [
            models.Index(fields=["status", "started_at"]),
            models.Index(fields=["run_type", "started_at"]),
            models.Index(fields=["-started_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_run_type_display()} / {self.get_status_display()} / {self.started_at:%d.%m.%Y %H:%M}"


class FinancialIntegrityFinding(TimeStampedModel):
    class Status(models.TextChoices):
        OPEN = "open", "Открыто"
        ACKNOWLEDGED = "acknowledged", "Принято в работу"
        RESOLVED = "resolved", "Решено"
        IGNORED = "ignored", "Игнорируется"

    class Severity(models.TextChoices):
        ERROR = "error", "Ошибка"
        WARNING = "warning", "Проверить"
        INFO = "info", "Информация"

    issue_key = models.CharField("ключ расхождения", max_length=64, unique=True)
    code = models.CharField("код расхождения", max_length=100)
    severity = models.CharField("важность", max_length=20, choices=Severity.choices)
    status = models.CharField(
        "статус",
        max_length=30,
        choices=Status.choices,
        default=Status.OPEN,
    )
    appointment = models.ForeignKey(
        Appointment,
        verbose_name="занятие",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="financial_integrity_findings",
    )
    appointment_participant = models.ForeignKey(
        AppointmentParticipant,
        verbose_name="участник занятия",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="financial_integrity_findings",
    )
    ledger_entry = models.ForeignKey(
        LedgerEntry,
        verbose_name="ledger-проводка",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="financial_integrity_findings",
    )
    account = models.ForeignKey(
        BalanceAccount,
        verbose_name="счет",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="financial_integrity_findings",
    )
    funding_source = models.ForeignKey(
        FundingSource,
        verbose_name="источник финансирования",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="financial_integrity_findings",
    )
    first_seen_at = models.DateTimeField("впервые найдено")
    last_seen_at = models.DateTimeField("последний раз найдено")
    resolved_at = models.DateTimeField("решено", null=True, blank=True)
    first_seen_run = models.ForeignKey(
        FinancialIntegrityCheckRun,
        verbose_name="первый запуск",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="first_seen_findings",
    )
    last_seen_run = models.ForeignKey(
        FinancialIntegrityCheckRun,
        verbose_name="последний запуск",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="last_seen_findings",
    )
    resolved_run = models.ForeignKey(
        FinancialIntegrityCheckRun,
        verbose_name="запуск решения",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_findings",
    )
    message = models.TextField("сообщение")
    appointment_starts_at = models.DateTimeField("дата занятия", null=True, blank=True)
    appointment_service_name = models.CharField("услуга", max_length=200, blank=True)
    participant_name = models.CharField("участник", max_length=240, blank=True)
    account_label = models.CharField("счет", max_length=255, blank=True)
    funding_source_name = models.CharField("источник", max_length=200, blank=True)
    ledger_entry_type = models.CharField("тип ledger", max_length=30, blank=True)
    ledger_amount = models.DecimalField(
        "сумма ledger",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    payload = models.JSONField("данные проверки", default=dict, blank=True)
    triage_note = models.TextField("заметка разбора", blank=True)
    triaged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="разобрал",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="financial_integrity_triaged_findings",
    )
    triaged_at = models.DateTimeField("разобрано", null=True, blank=True)

    class Meta:
        verbose_name = "финансовое расхождение"
        verbose_name_plural = "финансовые расхождения"
        ordering = ["status", "-last_seen_at", "-pk"]
        indexes = [
            models.Index(fields=["status", "severity", "-last_seen_at"]),
            models.Index(fields=["code", "status"]),
            models.Index(fields=["appointment", "status"]),
            models.Index(fields=["account", "status"]),
            models.Index(fields=["funding_source", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.code} / {self.get_status_display()}"


class FinancialIntegrityFindingEvent(TimeStampedModel):
    class EventType(models.TextChoices):
        CREATED = "created", "Создано"
        ACKNOWLEDGED = "acknowledged", "Принято в работу"
        RETURNED_TO_OPEN = "returned_to_open", "Возвращено в очередь"
        IGNORED = "ignored", "Скрыто из очереди"
        REOPENED = "reopened", "Открыто повторно"
        RESOLVED = "resolved", "Решено"
        SCOPED_RECHECK = "scoped_recheck", "Точечная перепроверка"
        NOTE_ADDED = "note_added", "Добавлена заметка"

    finding = models.ForeignKey(
        FinancialIntegrityFinding,
        verbose_name="финансовое расхождение",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="events",
    )
    event_key = models.CharField("ключ события", max_length=64, unique=True)
    event_type = models.CharField("тип события", max_length=40, choices=EventType.choices)
    event_at = models.DateTimeField("время события", default=timezone.now, db_index=True)
    run = models.ForeignKey(
        FinancialIntegrityCheckRun,
        verbose_name="запуск проверки",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="finding_events",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="пользователь",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="financial_integrity_events",
    )
    status_from = models.CharField("статус был", max_length=30, blank=True)
    status_to = models.CharField("статус стал", max_length=30, blank=True)
    severity = models.CharField("важность", max_length=20, blank=True)
    code = models.CharField("код расхождения", max_length=100, blank=True)
    issue_key = models.CharField("ключ расхождения", max_length=64, blank=True)
    message = models.TextField("сообщение", blank=True)
    note = models.TextField("заметка", blank=True)
    source_snapshot = models.JSONField("snapshot источников", default=dict, blank=True)

    class Meta:
        verbose_name = "событие финансового расхождения"
        verbose_name_plural = "события финансовых расхождений"
        ordering = ["-event_at", "-pk"]
        indexes = [
            models.Index(fields=["finding", "-event_at"]),
            models.Index(fields=["event_type", "-event_at"]),
            models.Index(fields=["run", "event_type"]),
            models.Index(fields=["code", "-event_at"]),
            models.Index(fields=["status_to", "-event_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_event_type_display()} / {self.code or self.issue_key}"


class PayrollAccrual(TimeStampedModel):
    class AccrualKind(models.TextChoices):
        APPOINTMENT = "appointment", "За занятие"
        GRANT_FIXED = "grant_fixed", "Фиксированная грантовая оплата"

    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        APPROVED = "approved", "Утверждено"
        PAID = "paid", "Выплачено"
        CANCELLED = "cancelled", "Отменено"

    dedupe_key = models.CharField("ключ идемпотентности", max_length=160, unique=True)
    accrual_kind = models.CharField(
        "вид начисления",
        max_length=30,
        choices=AccrualKind.choices,
        default=AccrualKind.APPOINTMENT,
    )
    staff_assignment = models.ForeignKey(
        AppointmentStaffAssignment,
        verbose_name="назначение специалиста",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payroll_accruals",
    )
    appointment = models.ForeignKey(
        Appointment,
        verbose_name="занятие",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payroll_accruals",
    )
    appointment_participant = models.ForeignKey(
        AppointmentParticipant,
        verbose_name="участник занятия",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payroll_accruals",
    )
    ledger_entry = models.ForeignKey(
        LedgerEntry,
        verbose_name="проводка списания",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payroll_accruals",
    )
    staff_member = models.ForeignKey(
        StaffMember,
        verbose_name="специалист",
        on_delete=models.PROTECT,
        related_name="payroll_accruals",
    )
    service = models.ForeignKey(
        Service,
        verbose_name="услуга",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="payroll_accruals",
    )
    funding_source = models.ForeignKey(
        FundingSource,
        verbose_name="источник финансирования",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payroll_accruals",
    )
    pay_rule = models.ForeignKey(
        StaffCompensationRule,
        verbose_name="правило начисления",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payroll_accruals",
    )
    grant_allocation_revision = models.ForeignKey(
        FundingStaffAllocationRevision,
        verbose_name="редакция грантовой ставки",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="payroll_accruals",
    )
    grant_fixed_compensation_revision = models.ForeignKey(
        GrantFixedCompensationRevision,
        verbose_name="редакция фиксированной грантовой оплаты",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="payroll_accruals",
    )
    payroll_budget_revision = models.ForeignKey(
        FundingPayrollBudgetRevision,
        verbose_name="проверенная редакция бюджета оплаты труда",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="payroll_accruals",
    )
    work_date = models.DateField("дата начисления")
    period_from_snapshot = models.DateField("период начисления с", null=True, blank=True)
    period_to_snapshot = models.DateField("период начисления по", null=True, blank=True)
    starts_at_snapshot = models.DateTimeField("начало", null=True, blank=True)
    ends_at_snapshot = models.DateTimeField("окончание", null=True, blank=True)
    duration_minutes = models.PositiveIntegerField("длительность, мин", null=True, blank=True)
    rate_type_snapshot = models.CharField(
        "тип ставки",
        max_length=30,
        choices=StaffCompensationRule.RateType.choices,
        null=True,
        blank=True,
    )
    rate_amount_snapshot = models.DecimalField(
        "ставка",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    session_scope_snapshot = models.CharField(
        "формат правила",
        max_length=30,
        choices=StaffCompensationRule.SessionScope.choices,
        null=True,
        blank=True,
    )
    group_pay_policy_snapshot = models.CharField(
        "начисление в группе",
        max_length=40,
        choices=StaffCompensationRule.GroupPayPolicy.choices,
        null=True,
        blank=True,
    )
    charged_participants_count_snapshot = models.PositiveIntegerField(
        "списано участников",
        null=True,
        blank=True,
    )
    pay_units_snapshot = models.PositiveIntegerField(
        "единиц начисления",
        null=True,
        blank=True,
    )
    amount = models.DecimalField("сумма начисления", max_digits=12, decimal_places=2)
    status = models.CharField("статус", max_length=30, choices=Status.choices, default=Status.DRAFT)
    note = models.TextField("примечание", blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="создал",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_payroll_accruals",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="утвердил",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_payroll_accruals",
    )
    approved_at = models.DateTimeField("утверждено", null=True, blank=True)

    class Meta:
        verbose_name = "начисление специалисту"
        verbose_name_plural = "начисления специалистам"
        ordering = ["-work_date", "staff_member__full_name", "service__name"]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gte=0), name="payroll_accrual_amount_non_negative"
            ),
            models.CheckConstraint(
                condition=Q(rate_amount_snapshot__gte=0), name="payroll_accrual_rate_non_negative"
            ),
            models.CheckConstraint(
                condition=Q(charged_participants_count_snapshot__gte=1),
                name="payroll_accrual_charged_count_positive",
            ),
            models.CheckConstraint(
                condition=Q(pay_units_snapshot__gte=1), name="payroll_accrual_pay_units_positive"
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        accrual_kind="appointment",
                        grant_fixed_compensation_revision__isnull=True,
                        service__isnull=False,
                        starts_at_snapshot__isnull=False,
                        ends_at_snapshot__isnull=False,
                        duration_minutes__isnull=False,
                        rate_type_snapshot__isnull=False,
                        rate_amount_snapshot__isnull=False,
                        session_scope_snapshot__isnull=False,
                        group_pay_policy_snapshot__isnull=False,
                        charged_participants_count_snapshot__isnull=False,
                        pay_units_snapshot__isnull=False,
                    )
                    | Q(
                        accrual_kind="grant_fixed",
                        grant_fixed_compensation_revision__isnull=False,
                        staff_assignment__isnull=True,
                        appointment__isnull=True,
                        appointment_participant__isnull=True,
                        ledger_entry__isnull=True,
                        service__isnull=True,
                        funding_source__isnull=False,
                        pay_rule__isnull=True,
                        grant_allocation_revision__isnull=True,
                        period_from_snapshot__isnull=False,
                        period_to_snapshot__isnull=False,
                        starts_at_snapshot__isnull=True,
                        ends_at_snapshot__isnull=True,
                        duration_minutes__isnull=True,
                        rate_type_snapshot__isnull=True,
                        rate_amount_snapshot__isnull=True,
                        session_scope_snapshot__isnull=True,
                        group_pay_policy_snapshot__isnull=True,
                        charged_participants_count_snapshot__isnull=True,
                        pay_units_snapshot__isnull=True,
                    )
                ),
                name="payroll_accrual_kind_fields",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(accrual_kind="grant_fixed")
                    | Q(period_to_snapshot__gte=models.F("period_from_snapshot"))
                ),
                name="payroll_accrual_fixed_period_order",
            ),
            models.CheckConstraint(
                condition=~Q(accrual_kind="grant_fixed") | Q(amount__gt=0),
                name="payroll_accrual_fixed_amount_positive",
            ),
        ]
        indexes = [
            models.Index(fields=["staff_member", "work_date", "status"]),
            models.Index(fields=["appointment", "staff_assignment"]),
            models.Index(fields=["funding_source", "service", "work_date"]),
            models.Index(
                fields=["accrual_kind", "work_date", "status"],
                name="payroll_acc_kind_date_status",
            ),
            models.Index(
                fields=["payroll_budget_revision", "status"],
                name="payroll_acc_budget_status",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.work_date:%d.%m.%Y} / {self.staff_member} / {self.amount}"


class PayrollSheet(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        APPROVED = "approved", "Утвержден"
        SENT = "sent", "Передан в выплату"
        PAID = "paid", "Выплачен"
        CANCELLED = "cancelled", "Отменен"

    staff_member = models.ForeignKey(
        StaffMember,
        verbose_name="специалист",
        on_delete=models.PROTECT,
        related_name="payroll_sheets",
    )
    date_from = models.DateField("дата начала")
    date_to = models.DateField("дата окончания")
    status = models.CharField("статус", max_length=30, choices=Status.choices, default=Status.DRAFT)
    total_amount = models.DecimalField("итого", max_digits=12, decimal_places=2, default=0)
    note = models.TextField("примечание", blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="создал",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_payroll_sheets",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="утвердил",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_payroll_sheets",
    )
    approved_at = models.DateTimeField("утверждено", null=True, blank=True)

    class Meta:
        verbose_name = "расчетный лист"
        verbose_name_plural = "расчетные листы"
        ordering = ["-date_to", "staff_member__full_name"]
        constraints = [
            models.CheckConstraint(
                condition=Q(date_to__gte=models.F("date_from")), name="payroll_sheet_dates_order"
            ),
            models.CheckConstraint(
                condition=Q(total_amount__gte=0), name="payroll_sheet_total_non_negative"
            ),
        ]
        indexes = [
            models.Index(fields=["staff_member", "date_from", "date_to", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.staff_member}: {self.date_from:%d.%m.%Y}-{self.date_to:%d.%m.%Y}"


class PayrollSheetLine(TimeStampedModel):
    payroll_sheet = models.ForeignKey(
        PayrollSheet,
        verbose_name="расчетный лист",
        on_delete=models.CASCADE,
        related_name="lines",
    )
    payroll_accrual = models.ForeignKey(
        PayrollAccrual,
        verbose_name="начисление",
        on_delete=models.PROTECT,
        related_name="sheet_lines",
    )
    accrual_kind_snapshot = models.CharField(
        "вид начисления",
        max_length=30,
        choices=PayrollAccrual.AccrualKind.choices,
    )
    appointment = models.ForeignKey(
        Appointment,
        verbose_name="занятие",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payroll_sheet_lines",
    )
    service = models.ForeignKey(
        Service,
        verbose_name="услуга",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="payroll_sheet_lines",
    )
    funding_source = models.ForeignKey(
        FundingSource,
        verbose_name="источник финансирования",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="payroll_sheet_lines",
    )
    payroll_budget_revision = models.ForeignKey(
        FundingPayrollBudgetRevision,
        verbose_name="проверенная редакция бюджета оплаты труда",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="payroll_sheet_lines",
    )
    work_date = models.DateField("дата начисления")
    period_from_snapshot = models.DateField("период начисления с", null=True, blank=True)
    period_to_snapshot = models.DateField("период начисления по", null=True, blank=True)
    line_label = models.CharField("название строки", max_length=200)
    duration_minutes = models.PositiveIntegerField("длительность, мин", null=True, blank=True)
    amount = models.DecimalField("сумма", max_digits=12, decimal_places=2)
    note = models.TextField("примечание", blank=True)

    class Meta:
        verbose_name = "строка расчетного листа"
        verbose_name_plural = "строки расчетных листов"
        ordering = ["work_date", "line_label", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["payroll_sheet", "payroll_accrual"], name="unique_payroll_sheet_accrual"
            ),
            models.CheckConstraint(
                condition=Q(amount__gte=0), name="payroll_sheet_line_amount_non_negative"
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        accrual_kind_snapshot="appointment",
                        service__isnull=False,
                        duration_minutes__isnull=False,
                    )
                    | Q(
                        accrual_kind_snapshot="grant_fixed",
                        service__isnull=True,
                        funding_source__isnull=False,
                        period_from_snapshot__isnull=False,
                        period_to_snapshot__isnull=False,
                        duration_minutes__isnull=True,
                    )
                ),
                name="payroll_sheet_line_kind_fields",
            ),
            models.CheckConstraint(
                condition=~Q(line_label=""),
                name="payroll_sheet_line_label_required",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(accrual_kind_snapshot="grant_fixed")
                    | Q(period_to_snapshot__gte=models.F("period_from_snapshot"))
                ),
                name="payroll_sheet_line_fixed_period_order",
            ),
        ]
        indexes = [
            models.Index(fields=["payroll_sheet", "work_date"]),
            models.Index(fields=["payroll_accrual"]),
            models.Index(
                fields=["payroll_budget_revision", "payroll_sheet"],
                name="payroll_line_budget_sheet",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.payroll_sheet} / {self.work_date:%d.%m.%Y} / {self.amount}"


class PayrollPayout(TimeStampedModel):
    """One immutable full payment recorded for a payroll sheet."""

    class Method(models.TextChoices):
        BANK_TRANSFER = "bank_transfer", "Банковский перевод"
        CASH = "cash", "Наличные"
        OTHER = "other", "Другое"

    payroll_sheet = models.OneToOneField(
        PayrollSheet,
        verbose_name="расчетный лист",
        on_delete=models.PROTECT,
        related_name="payout",
    )
    amount = models.DecimalField("сумма выплаты", max_digits=12, decimal_places=2)
    method = models.CharField("способ выплаты", max_length=30, choices=Method.choices)
    paid_at = models.DateField("дата выплаты", default=timezone.localdate)
    reference = models.CharField("номер платежа / документа", max_length=200, blank=True)
    note = models.TextField("комментарий", blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="выплату зафиксировал",
        on_delete=models.PROTECT,
        related_name="recorded_payroll_payouts",
    )

    class Meta:
        verbose_name = "выплата специалисту"
        verbose_name_plural = "выплаты специалистам"
        ordering = ["-paid_at", "-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0), name="payroll_payout_amount_positive"
            ),
        ]
        indexes = [models.Index(fields=["-paid_at"])]

    immutable_fields = (
        "payroll_sheet_id",
        "amount",
        "method",
        "paid_at",
        "reference",
        "note",
        "recorded_by_id",
    )

    def clean(self) -> None:
        if self.amount is None or self.amount <= 0:
            raise ValidationError({"amount": "Сумма выплаты должна быть положительной."})

    def _ensure_immutable_fields(self) -> None:
        if not self.pk:
            return
        current = type(self).objects.get(pk=self.pk)
        if any(getattr(self, field) != getattr(current, field) for field in self.immutable_fields):
            raise ValidationError(
                "Зафиксированную выплату нельзя изменять. Для корректировки нужен отдельный "
                "финансовый документ."
            )

    def save(self, *args: object, **kwargs: object) -> None:
        self.full_clean()
        self._ensure_immutable_fields()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.payroll_sheet} / {self.amount}"


class PayrollSheetLifecycleEvent(TimeStampedModel):
    """Append-only journal for payroll transitions with financial consequences."""

    class EventType(models.TextChoices):
        APPROVED = PayrollSheet.Status.APPROVED, "Утвержден"
        SENT = PayrollSheet.Status.SENT, "Передан в выплату"
        PAID = PayrollSheet.Status.PAID, "Выплата зафиксирована"

    class ActorRole(models.TextChoices):
        DIRECTOR = "director", "Руководитель"

    payroll_sheet = models.ForeignKey(
        PayrollSheet,
        verbose_name="расчетный лист",
        on_delete=models.PROTECT,
        related_name="lifecycle_events",
    )
    event_type = models.CharField("тип события", max_length=30, choices=EventType.choices)
    status_from = models.CharField("статус был", max_length=30, choices=PayrollSheet.Status.choices)
    status_to = models.CharField("статус стал", max_length=30, choices=PayrollSheet.Status.choices)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="действие выполнил",
        on_delete=models.PROTECT,
        related_name="payroll_sheet_lifecycle_events",
    )
    actor_role_snapshot = models.CharField(
        "роль на момент действия",
        max_length=30,
        choices=ActorRole.choices,
    )
    note = models.TextField("основание", blank=True)
    payroll_budget_revision = models.ForeignKey(
        FundingPayrollBudgetRevision,
        verbose_name="проверенная редакция бюджета оплаты труда",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="payroll_sheet_lifecycle_events",
    )
    budget_overage_amount = models.DecimalField(
        "превышение бюджета",
        max_digits=14,
        decimal_places=2,
        default=0,
    )
    occurred_at = models.DateTimeField("время действия", default=timezone.now, db_index=True)

    class Meta:
        verbose_name = "событие расчетного листа"
        verbose_name_plural = "события расчетных листов"
        ordering = ["-occurred_at", "-pk"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(
                        event_type="approved",
                        status_from="draft",
                        status_to="approved",
                        actor_role_snapshot="director",
                    )
                    | Q(
                        event_type="sent",
                        status_from="approved",
                        status_to="sent",
                        actor_role_snapshot="director",
                    )
                    | Q(
                        event_type="paid",
                        status_from="sent",
                        status_to="paid",
                        actor_role_snapshot="director",
                    )
                ),
                name="payroll_lifecycle_event_valid_transition",
            ),
            models.CheckConstraint(
                condition=~Q(event_type="sent") | ~Q(note=""),
                name="payroll_lifecycle_sent_note_required",
            ),
            models.CheckConstraint(
                condition=Q(budget_overage_amount__gte=0),
                name="payroll_lifecycle_overage_non_negative",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        event_type="approved",
                    )
                    | Q(
                        payroll_budget_revision__isnull=True,
                        budget_overage_amount=0,
                    )
                ),
                name="payroll_lifecycle_budget_fields_event",
            ),
            models.CheckConstraint(
                condition=(
                    Q(budget_overage_amount=0)
                    | (
                        Q(
                            event_type="approved",
                            payroll_budget_revision__isnull=False,
                        )
                        & ~Q(note="")
                    )
                ),
                name="payroll_lifecycle_overage_reason",
            ),
        ]
        indexes = [
            models.Index(fields=["payroll_sheet", "-occurred_at"]),
            models.Index(fields=["event_type", "-occurred_at"]),
        ]

    immutable_fields = (
        "payroll_sheet_id",
        "event_type",
        "status_from",
        "status_to",
        "actor_id",
        "actor_role_snapshot",
        "note",
        "payroll_budget_revision_id",
        "budget_overage_amount",
        "occurred_at",
    )

    def clean(self) -> None:
        expected_transitions = {
            self.EventType.APPROVED: (PayrollSheet.Status.DRAFT, PayrollSheet.Status.APPROVED),
            self.EventType.SENT: (PayrollSheet.Status.APPROVED, PayrollSheet.Status.SENT),
            self.EventType.PAID: (PayrollSheet.Status.SENT, PayrollSheet.Status.PAID),
        }
        if expected_transitions.get(self.event_type) != (self.status_from, self.status_to):
            raise ValidationError("Недопустимый переход состояния расчетного листа.")
        if self.actor_role_snapshot != self.ActorRole.DIRECTOR:
            raise ValidationError({"actor_role_snapshot": "Действие может выполнить руководитель."})
        if self.event_type == self.EventType.SENT and len(self.note.strip()) < 5:
            raise ValidationError({"note": "Укажите основание передачи в выплату."})
        if self.event_type != self.EventType.APPROVED and (
            self.payroll_budget_revision_id or self.budget_overage_amount
        ):
            raise ValidationError(
                "Бюджетные снимки допустимы только для события утверждения."
            )
        if self.budget_overage_amount and (
            not self.payroll_budget_revision_id or len(self.note.strip()) < 5
        ):
            raise ValidationError(
                {"note": "Для превышения бюджета укажите содержательное основание."}
            )

    def _ensure_immutable_fields(self) -> None:
        if not self.pk:
            return
        current = type(self).objects.get(pk=self.pk)
        if any(getattr(self, field) != getattr(current, field) for field in self.immutable_fields):
            raise ValidationError("Событие расчетного листа нельзя изменять после создания.")

    def save(self, *args: object, **kwargs: object) -> None:
        self.full_clean()
        self._ensure_immutable_fields()
        super().save(*args, **kwargs)

    def delete(self, *args: object, **kwargs: object) -> None:
        raise ValidationError("Событие расчетного листа нельзя удалить.")

    def __str__(self) -> str:
        return f"{self.payroll_sheet_id}: {self.get_event_type_display()}"


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
    staff_member = models.ForeignKey(
        StaffMember, verbose_name="специалист", on_delete=models.SET_NULL, null=True, blank=True
    )
    appointment = models.ForeignKey(
        Appointment, verbose_name="занятие", on_delete=models.SET_NULL, null=True, blank=True
    )
    title = models.CharField("заголовок", max_length=200)
    text = models.TextField("текст")
    priority = models.CharField(
        "приоритет", max_length=20, choices=Priority.choices, default=Priority.MEDIUM
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="автор",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

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
    reschedule_step = models.ForeignKey(
        AppointmentRescheduleStep,
        verbose_name="шаг плана переноса",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
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
    participant = models.ForeignKey(
        AppointmentParticipant,
        verbose_name="участник занятия",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="confirmations",
    )
    staff_assignment = models.ForeignKey(
        AppointmentStaffAssignment,
        verbose_name="назначение специалиста",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="confirmations",
    )
    email = models.EmailField("email")
    token = models.UUIDField("токен подтверждения", default=uuid.uuid4, unique=True, editable=False)
    subject = models.CharField("тема письма", max_length=200)
    message = models.TextField("текст письма")
    status = models.CharField(
        "статус ответа", max_length=30, choices=Status.choices, default=Status.PENDING
    )
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
        indexes = [
            models.Index(fields=["reschedule_step", "status", "delivery_status"]),
        ]

    def __str__(self) -> str:
        return f"{self.appointment} -> {self.email}"


class AppointmentConfirmationDecision(TimeStampedModel):
    class Decision(models.TextChoices):
        CONFIRMED = AppointmentConfirmation.Status.CONFIRMED, "Подтвердить"
        DECLINED = AppointmentConfirmation.Status.DECLINED, "Отклонить"

    class Source(models.TextChoices):
        SPECIALIST_RESPONSE = "specialist_response", "Ответ специалиста"
        REPRESENTATIVE_RESPONSE = "representative_response", "Ответ представителя"
        RECIPIENT_RESPONSE = "recipient_response", "Ответ получателя"
        ADMINISTRATOR_MANUAL = "administrator_manual", "Ручное решение администратора"
        DIRECTOR_MANUAL = "director_manual", "Ручное решение руководителя"

    class ActorRole(models.TextChoices):
        DIRECTOR = "director", "Руководитель"
        ADMINISTRATOR = "administrator", "Администратор"
        SPECIALIST = "specialist", "Специалист"
        REPRESENTATIVE = "representative", "Представитель"
        RECIPIENT = "recipient", "Получатель"

    confirmation = models.ForeignKey(
        AppointmentConfirmation,
        verbose_name="согласование",
        on_delete=models.CASCADE,
        related_name="decision_history",
    )
    decision = models.CharField("решение", max_length=30, choices=Decision.choices)
    source = models.CharField("источник решения", max_length=40, choices=Source.choices)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="решение зафиксировал",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="appointment_confirmation_decisions",
    )
    actor_role_snapshot = models.CharField(
        "роль на момент решения",
        max_length=30,
        choices=ActorRole.choices,
    )
    note = models.TextField("основание или комментарий", blank=True)
    supersedes = models.ForeignKey(
        "self",
        verbose_name="переопределяет решение",
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="superseded_by",
    )
    is_current = models.BooleanField("текущее решение", default=True)

    class Meta:
        verbose_name = "решение по согласованию"
        verbose_name_plural = "решения по согласованиям"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["confirmation"],
                condition=Q(is_current=True),
                name="unique_current_confirmation_decision",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(
                        source__in=[
                            "administrator_manual",
                            "director_manual",
                        ]
                    )
                    | Q(actor__isnull=False)
                ),
                name="confirmation_manual_decision_actor_required",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(
                        source__in=[
                            "administrator_manual",
                            "director_manual",
                        ]
                    )
                    | ~Q(note="")
                ),
                name="confirmation_manual_decision_note_required",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        source="specialist_response",
                        actor_role_snapshot="specialist",
                    )
                    | Q(
                        source="representative_response",
                        actor_role_snapshot="representative",
                    )
                    | Q(
                        source="recipient_response",
                        actor_role_snapshot="recipient",
                    )
                    | Q(
                        source="administrator_manual",
                        actor_role_snapshot="administrator",
                    )
                    | Q(
                        source="director_manual",
                        actor_role_snapshot="director",
                    )
                ),
                name="confirmation_decision_source_role_match",
            ),
        ]
        indexes = [
            models.Index(fields=["confirmation", "is_current", "created_at"]),
            models.Index(fields=["source", "created_at"]),
        ]

    def clean(self) -> None:
        errors = {}
        manual_sources = {
            self.Source.ADMINISTRATOR_MANUAL,
            self.Source.DIRECTOR_MANUAL,
        }
        if self.source in manual_sources and not self.actor_id:
            errors["actor"] = "Для ручного решения нужен пользователь."
        if self.source in manual_sources and len(self.note.strip()) < 5:
            errors["note"] = "Укажите основание ручного решения не короче 5 символов."
        expected_roles = {
            self.Source.SPECIALIST_RESPONSE: self.ActorRole.SPECIALIST,
            self.Source.REPRESENTATIVE_RESPONSE: self.ActorRole.REPRESENTATIVE,
            self.Source.RECIPIENT_RESPONSE: self.ActorRole.RECIPIENT,
            self.Source.ADMINISTRATOR_MANUAL: self.ActorRole.ADMINISTRATOR,
            self.Source.DIRECTOR_MANUAL: self.ActorRole.DIRECTOR,
        }
        if expected_roles.get(self.source) != self.actor_role_snapshot:
            errors["actor_role_snapshot"] = (
                "Роль на момент решения не соответствует источнику решения."
            )
        if (
            self.supersedes_id
            and self.confirmation_id
            and self.supersedes.confirmation_id != self.confirmation_id
        ):
            errors["supersedes"] = "Можно переопределить решение только этого согласования."
        if errors:
            raise ValidationError(errors)

    def save(self, *args: object, **kwargs: object) -> None:
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.confirmation_id}: {self.get_decision_display()} ({self.get_source_display()})"


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
    status = models.CharField(
        "статус", max_length=30, choices=Status.choices, default=Status.PENDING
    )
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

    @property
    def director_priority_required(self) -> bool:
        if not self.starts_on or not self.ends_on:
            return False
        return (
            self.request_type
            in {
                self.RequestType.VACATION,
                self.RequestType.SCHEDULE_CHANGE,
            }
            or self.ends_on > self.starts_on
        )


class TimeOffRequestDecision(TimeStampedModel):
    class Decision(models.TextChoices):
        APPROVED = TimeOffRequest.Status.APPROVED, "Согласовать"
        REJECTED = TimeOffRequest.Status.REJECTED, "Отклонить"

    class Source(models.TextChoices):
        ADMINISTRATOR_MANUAL = "administrator_manual", "Решение администратора"
        DIRECTOR_MANUAL = "director_manual", "Решение руководителя"

    class ActorRole(models.TextChoices):
        ADMINISTRATOR = "administrator", "Администратор"
        DIRECTOR = "director", "Руководитель"

    time_off_request = models.ForeignKey(
        TimeOffRequest,
        verbose_name="заявка специалиста",
        on_delete=models.CASCADE,
        related_name="decision_history",
    )
    decision = models.CharField("решение", max_length=30, choices=Decision.choices)
    source = models.CharField("источник решения", max_length=40, choices=Source.choices)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="решение зафиксировал",
        on_delete=models.PROTECT,
        related_name="time_off_request_decisions",
    )
    actor_role_snapshot = models.CharField(
        "роль на момент решения",
        max_length=30,
        choices=ActorRole.choices,
    )
    note = models.TextField("основание решения")
    director_priority = models.BooleanField(
        "область приоритета руководителя",
        default=False,
    )
    supersedes = models.ForeignKey(
        "self",
        verbose_name="переопределяет решение",
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="superseded_by",
    )
    is_current = models.BooleanField("текущее решение", default=True)

    class Meta:
        verbose_name = "решение по заявке специалиста"
        verbose_name_plural = "решения по заявкам специалистов"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["time_off_request"],
                condition=Q(is_current=True),
                name="unique_current_time_off_decision",
            ),
            models.CheckConstraint(
                condition=~Q(note=""),
                name="time_off_decision_note_required",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        source="administrator_manual",
                        actor_role_snapshot="administrator",
                    )
                    | Q(
                        source="director_manual",
                        actor_role_snapshot="director",
                    )
                ),
                name="time_off_decision_source_role_match",
            ),
        ]
        indexes = [
            models.Index(fields=["time_off_request", "is_current", "created_at"]),
            models.Index(fields=["source", "created_at"]),
            models.Index(fields=["director_priority", "is_current", "created_at"]),
        ]

    @property
    def awaits_director_review(self) -> bool:
        return (
            self.director_priority
            and self.source == self.Source.ADMINISTRATOR_MANUAL
            and self.is_current
        )

    def clean(self) -> None:
        errors = {}
        if len(self.note.strip()) < 5:
            errors["note"] = "Укажите основание решения не короче 5 символов."
        expected_roles = {
            self.Source.ADMINISTRATOR_MANUAL: self.ActorRole.ADMINISTRATOR,
            self.Source.DIRECTOR_MANUAL: self.ActorRole.DIRECTOR,
        }
        if expected_roles.get(self.source) != self.actor_role_snapshot:
            errors["actor_role_snapshot"] = (
                "Роль на момент решения не соответствует источнику решения."
            )
        if (
            self._state.adding
            and self.time_off_request_id
            and self.director_priority
            != self.time_off_request.director_priority_required
        ):
            errors["director_priority"] = (
                "Приоритет руководителя не соответствует типу и периоду заявки."
            )
        if self.supersedes_id and self.supersedes_id == self.pk:
            errors["supersedes"] = "Решение не может переопределять само себя."
        elif (
            self.supersedes_id
            and self.time_off_request_id
            and self.supersedes.time_off_request_id != self.time_off_request_id
        ):
            errors["supersedes"] = "Можно переопределить решение только этой заявки."
        if errors:
            raise ValidationError(errors)

    def save(self, *args: object, **kwargs: object) -> None:
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return (
            f"{self.time_off_request_id}: "
            f"{self.get_decision_display()} ({self.get_source_display()})"
        )


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
        self.save(
            update_fields=["is_acknowledged", "acknowledged_at", "acknowledged_by", "updated_at"]
        )


def document_upload_path(instance: Document, filename: str) -> str:
    if instance.child_id:
        return f"documents/{instance.child_id}/{filename}"
    if instance.counterparty_id:
        return f"documents/counterparties/{instance.counterparty_id}/{filename}"
    return f"documents/{instance.target_type}/{filename}"


def contract_template_upload_path(instance: ContractTemplate, filename: str) -> str:
    return f"contract_templates/{instance.template_type}/{filename}"


def contract_signed_file_upload_path(instance: ContractSignedFile, filename: str) -> str:
    kind = instance.contract_kind or "contract"
    contract_id = (
        instance.service_contract_id
        or instance.donation_contract_id
        or instance.organization_contract_id
        or "unassigned"
    )
    safe_name = get_valid_filename(filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1])
    stamp = timezone.now().strftime("%Y%m%d%H%M%S")
    return f"contract_signed_files/{kind}/{contract_id}/{stamp}_{uuid.uuid4().hex[:8]}_{safe_name}"


def contract_act_signed_file_upload_path(
    instance: ContractActSignedFile,
    filename: str,
) -> str:
    act_id = instance.act_id or "unassigned"
    safe_name = get_valid_filename(filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1])
    stamp = timezone.now().strftime("%Y%m%d%H%M%S")
    return f"contract_act_signed_files/{act_id}/{stamp}_{uuid.uuid4().hex[:8]}_{safe_name}"


def consent_signed_file_upload_path(
    instance: ConsentSignedFile,
    filename: str,
) -> str:
    consent_id = instance.consent_id or "unassigned"
    safe_name = get_valid_filename(filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1])
    stamp = timezone.now().strftime("%Y%m%d%H%M%S")
    return f"consent_signed_files/{consent_id}/{stamp}_{uuid.uuid4().hex[:8]}_{safe_name}"


class Document(TimeStampedModel):
    class Category(models.TextChoices):
        MEDICAL_REPORT = "medical_report", "Медицинское заключение"
        CONSENT = "consent", "Согласие"
        IPR = "ipr", "ИПР / ИПРА"
        CONTRACT = "contract", "Договор"
        ACT = "act", "Акт"
        OTHER = "other", "Прочее"

    class TargetType(models.TextChoices):
        RECIPIENT = "recipient", "Получатель"
        CENTER = "center", "Центр"
        COUNTERPARTY = "counterparty", "Контрагент"
        CONTRACT = "contract", "Договор"
        OTHER = "other", "Прочее"

    child = models.ForeignKey(
        Child,
        verbose_name="получатель",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="documents",
    )
    target_type = models.CharField(
        "цель документа",
        max_length=30,
        choices=TargetType.choices,
        default=TargetType.RECIPIENT,
    )
    counterparty = models.ForeignKey(
        "Counterparty",
        verbose_name="контрагент",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="documents",
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
        indexes = [
            models.Index(fields=["target_type", "category"]),
            models.Index(fields=["child", "category"]),
            models.Index(fields=["counterparty", "category"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~Q(target_type="recipient") | Q(child__isnull=False),
                name="document_recipient_target_requires_child",
            ),
            models.CheckConstraint(
                condition=~Q(target_type="counterparty") | Q(counterparty__isnull=False),
                name="document_counterparty_target_requires_counterparty",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.title} — {self.target_label}"

    def clean(self) -> None:
        errors: dict[str, str] = {}
        if self.expires_on and self.issued_on and self.expires_on < self.issued_on:
            errors["expires_on"] = "Срок действия не может быть раньше даты выдачи."
        if self.target_type == self.TargetType.RECIPIENT and not self.child_id:
            errors["child"] = "Для документа получателя выберите получателя."
        if self.target_type == self.TargetType.COUNTERPARTY and not self.counterparty_id:
            errors["counterparty"] = "Для документа контрагента выберите контрагента."
        if errors:
            raise ValidationError(errors)

    @property
    def target_label(self) -> str:
        if self.child_id:
            return str(self.child)
        if self.counterparty_id:
            return str(self.counterparty)
        if self.target_type == self.TargetType.CENTER:
            return "Центр"
        if self.target_type == self.TargetType.CONTRACT:
            return "Договор"
        return self.get_target_type_display()

    @property
    def is_expired(self) -> bool:
        return self.expires_on is not None and self.expires_on < timezone.localdate()

    @property
    def expires_soon(self) -> bool:
        if self.expires_on is None:
            return False
        return (
            timezone.localdate()
            <= self.expires_on
            <= timezone.localdate() + timezone.timedelta(days=30)
        )


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
    signatory_representative = models.ForeignKey(
        RecipientRepresentative,
        verbose_name="подписант",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="signed_consents",
    )
    template = models.ForeignKey(
        "ContractTemplate",
        verbose_name="шаблон",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="consents",
    )
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
        indexes = [
            models.Index(fields=["child", "consent_type"]),
            models.Index(fields=["template", "consent_type"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_consent_type_display()} — {self.child}"

    def clean(self) -> None:
        errors: dict[str, str] = {}
        if self.expires_on and self.signed_on and self.expires_on < self.signed_on:
            errors["expires_on"] = "Срок действия не может быть раньше даты подписания."
        if (
            self.signatory_representative_id
            and self.child_id
            and self.signatory_representative.child_id != self.child_id
        ):
            errors["signatory_representative"] = (
                "Подписант должен быть представителем выбранного получателя."
            )
        if (
            self.template_id
            and self.template.template_type not in ContractTemplate.consent_template_types()
        ):
            errors["template"] = "Выберите шаблон согласия."
        if self.document_id:
            if self.document.category != Document.Category.CONSENT:
                errors["document"] = "Связанный документ должен иметь категорию согласия."
            elif self.document.target_type != Document.TargetType.RECIPIENT:
                errors["document"] = "Согласие должно ссылаться на документ получателя."
            elif self.child_id and self.document.child_id != self.child_id:
                errors["document"] = "Документ согласия должен относиться к выбранному получателю."
        if errors:
            raise ValidationError(errors)

    @property
    def is_valid(self) -> bool:
        if self.signed_on is None:
            return False
        if self.expires_on is None:
            return True
        return self.expires_on >= timezone.localdate()


class ConsentSignedFile(TimeStampedModel):
    class Status(models.TextChoices):
        ACTIVE = "active", "Действует"
        VOID = "void", "Аннулирован"

    consent = models.ForeignKey(
        Consent,
        verbose_name="согласие",
        on_delete=models.PROTECT,
        related_name="signed_files",
    )
    source_document = models.ForeignKey(
        Document,
        verbose_name="исходный файл согласия",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="consent_signed_files",
    )
    file = models.FileField(
        "архивный файл",
        upload_to=consent_signed_file_upload_path,
    )
    original_filename = models.CharField("исходное имя файла", max_length=255)
    content_type = models.CharField("тип содержимого", max_length=120, blank=True)
    file_size = models.PositiveBigIntegerField("размер файла", default=0)
    file_sha256 = models.CharField("SHA-256", max_length=64)
    signed_on = models.DateField("дата подписания", default=timezone.localdate)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="загрузил",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_consent_signed_files",
    )
    status = models.CharField(
        "статус",
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    void_reason = models.TextField("причина аннулирования", blank=True)
    consent_snapshot = models.JSONField("данные согласия", default=dict, blank=True)
    center_snapshot = models.JSONField("данные центра", default=dict, blank=True)
    recipient_snapshot = models.JSONField("данные получателя", default=dict, blank=True)
    representative_snapshot = models.JSONField("данные представителя", default=dict, blank=True)
    template_snapshot = models.JSONField("данные шаблона", default=dict, blank=True)
    note = models.TextField("комментарий", blank=True)

    immutable_fields = (
        "consent_id",
        "source_document_id",
        "file",
        "original_filename",
        "content_type",
        "file_size",
        "file_sha256",
        "signed_on",
        "uploaded_by_id",
        "consent_snapshot",
        "center_snapshot",
        "recipient_snapshot",
        "representative_snapshot",
        "template_snapshot",
    )

    class Meta:
        verbose_name = "архивный подписанный файл согласия"
        verbose_name_plural = "архивные подписанные файлы согласий"
        ordering = ["-signed_on", "-created_at"]
        indexes = [
            models.Index(fields=["consent", "status", "-signed_on"]),
            models.Index(fields=["status", "-signed_on"]),
            models.Index(fields=["file_sha256"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(file_size__gt=0),
                name="consent_signed_file_size_positive",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.consent} — {self.signed_on:%d.%m.%Y}"

    def _immutable_value(self, field: str):
        value = getattr(self, field)
        if field == "file":
            return value.name
        return value

    def _ensure_immutable_fields(self) -> None:
        if not self.pk:
            return
        current = type(self).objects.get(pk=self.pk)
        for field in self.immutable_fields:
            if self._immutable_value(field) != current._immutable_value(field):
                raise ValidationError(
                    "Архивный подписанный файл согласия нельзя изменять после создания. "
                    "Можно только аннулировать запись отдельным статусом."
                )

    def clean(self) -> None:
        errors: dict[str, str] = {}
        if self.source_document_id:
            if self.source_document.category != Document.Category.CONSENT:
                errors["source_document"] = "Исходный файл должен быть документом согласия."
            elif self.source_document.target_type != Document.TargetType.RECIPIENT:
                errors["source_document"] = (
                    "Подписанное согласие должно исходить из документа получателя."
                )
            elif self.consent_id and self.source_document.child_id != self.consent.child_id:
                errors["source_document"] = (
                    "Исходный файл согласия должен относиться к тому же получателю."
                )
        if self.file_sha256 and len(self.file_sha256) != 64:
            errors["file_sha256"] = "SHA-256 должен состоять из 64 символов."
        if self.status == self.Status.VOID and not self.void_reason:
            errors["void_reason"] = "Для аннулирования укажите причину."
        if errors:
            raise ValidationError(errors)

    def save(self, *args: object, **kwargs: object) -> None:
        self.full_clean()
        self._ensure_immutable_fields()
        super().save(*args, **kwargs)


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


class Counterparty(TimeStampedModel, SoftDeleteMixin):
    class CounterpartyType(models.TextChoices):
        INDIVIDUAL = "individual", "Физическое лицо"
        ORGANIZATION = "organization", "Организация"
        FOUNDATION = "foundation", "Фонд"
        SPONSOR = "sponsor", "Спонсор"
        VENDOR = "vendor", "Поставщик"
        OTHER = "other", "Прочее"

    name = models.CharField("наименование", max_length=200)
    counterparty_type = models.CharField(
        "тип",
        max_length=30,
        choices=CounterpartyType.choices,
        default=CounterpartyType.ORGANIZATION,
    )
    inn = models.CharField("ИНН", max_length=20, blank=True)
    kpp = models.CharField("КПП", max_length=20, blank=True)
    ogrn = models.CharField("ОГРН/ОГРНИП", max_length=20, blank=True)
    legal_address = models.TextField("юридический адрес", blank=True)
    postal_address = models.TextField("почтовый адрес", blank=True)
    bank_details = models.TextField("банковские реквизиты", blank=True)
    contact_person = models.CharField("контактное лицо", max_length=200, blank=True)
    phone = models.CharField("телефон", max_length=40, blank=True)
    email = models.EmailField("email", blank=True)
    notes = models.TextField("примечания", blank=True)

    class Meta:
        verbose_name = "контрагент"
        verbose_name_plural = "контрагенты"
        ordering = ["archived_at", "name"]
        indexes = [
            models.Index(fields=["counterparty_type", "archived_at", "name"]),
        ]

    def __str__(self) -> str:
        return self.name


class CenterLegalProfile(TimeStampedModel):
    full_name = models.CharField("полное наименование", max_length=255)
    short_name = models.CharField("краткое наименование", max_length=160, blank=True)
    director_full_name = models.CharField("ФИО руководителя", max_length=200, blank=True)
    director_short_name = models.CharField("ФИО руководителя кратко", max_length=120, blank=True)
    director_position = models.CharField("должность руководителя", max_length=120, blank=True)
    authority_basis = models.CharField("основание полномочий", max_length=200, blank=True)
    license_number = models.CharField("номер лицензии", max_length=120, blank=True)
    license_date = models.DateField("дата лицензии", null=True, blank=True)
    license_authority = models.CharField("кем выдана лицензия", max_length=255, blank=True)
    ogrn = models.CharField("ОГРН", max_length=20, blank=True)
    inn = models.CharField("ИНН", max_length=20, blank=True)
    kpp = models.CharField("КПП", max_length=20, blank=True)
    legal_address = models.TextField("юридический адрес", blank=True)
    location_address = models.TextField("адрес места оказания услуг", blank=True)
    phone = models.CharField("телефон", max_length=40, blank=True)
    email = models.EmailField("email", blank=True)
    site = models.CharField("сайт", max_length=160, blank=True)
    bank_name = models.CharField("банк", max_length=200, blank=True)
    bank_bik = models.CharField("БИК", max_length=20, blank=True)
    bank_account = models.CharField("расчетный счет", max_length=40, blank=True)
    bank_corr_account = models.CharField("корреспондентский счет", max_length=40, blank=True)
    is_active = models.BooleanField("активный профиль", default=True)
    notes = models.TextField("примечания", blank=True)

    class Meta:
        verbose_name = "юридический профиль центра"
        verbose_name_plural = "юридические профили центра"
        ordering = ["-is_active", "-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["is_active"],
                condition=Q(is_active=True),
                name="unique_active_center_legal_profile",
            ),
        ]

    def __str__(self) -> str:
        return self.short_name or self.full_name

    def clean(self) -> None:
        if not self.is_active:
            return
        active_profiles = CenterLegalProfile.objects.filter(is_active=True)
        if self.pk:
            active_profiles = active_profiles.exclude(pk=self.pk)
        if active_profiles.exists():
            raise ValidationError({"is_active": "Активный юридический профиль центра уже есть."})

    @classmethod
    def get_active(cls) -> CenterLegalProfile | None:
        return cls.objects.filter(is_active=True).order_by("-updated_at", "-pk").first()


class CenterExpenseCategory(TimeStampedModel):
    class ExpenseType(models.TextChoices):
        HOUSEHOLD = "household", "Хозяйственные расходы"
        UTILITIES = "utilities", "Коммунальные платежи"
        EQUIPMENT = "equipment", "Оборудование"
        INVENTORY = "inventory", "Инвентарь и материалы"
        RENT = "rent", "Аренда"
        SERVICES = "services", "Внешние услуги"
        ADMIN_PAYROLL = "admin_payroll", "Административная зарплата"
        OTHER = "other", "Прочее"

    name = models.CharField("название", max_length=160, unique=True)
    expense_type = models.CharField(
        "тип",
        max_length=30,
        choices=ExpenseType.choices,
        default=ExpenseType.OTHER,
    )
    is_active = models.BooleanField("активна", default=True)
    sort_order = models.PositiveIntegerField("порядок", default=0)
    notes = models.TextField("примечания", blank=True)

    class Meta:
        verbose_name = "категория расхода центра"
        verbose_name_plural = "категории расходов центра"
        ordering = ["sort_order", "name"]
        indexes = [
            models.Index(fields=["expense_type", "is_active"]),
        ]

    def __str__(self) -> str:
        return self.name


class CenterExpense(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        APPROVED = "approved", "Утвержден"
        PAID = "paid", "Оплачен"
        CANCELLED = "cancelled", "Отменен"

    expense_date = models.DateField("дата расхода", default=timezone.localdate)
    category = models.ForeignKey(
        CenterExpenseCategory,
        verbose_name="категория",
        on_delete=models.PROTECT,
        related_name="center_expenses",
    )
    title = models.CharField("название", max_length=200)
    description = models.TextField("описание", blank=True)
    counterparty = models.ForeignKey(
        Counterparty,
        verbose_name="контрагент",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="center_expenses",
    )
    total_amount = models.DecimalField("сумма", max_digits=12, decimal_places=2)
    status = models.CharField(
        "статус",
        max_length=30,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="утвердил",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_center_expenses",
    )
    approved_at = models.DateTimeField("утверждено", null=True, blank=True)
    paid_at = models.DateField("оплачено", null=True, blank=True)
    document = models.ForeignKey(
        Document,
        verbose_name="документ",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="center_expenses",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="создал",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_center_expenses",
    )
    notes = models.TextField("примечания", blank=True)
    cancel_reason = models.TextField("причина отмены", blank=True)

    class Meta:
        verbose_name = "расход центра"
        verbose_name_plural = "расходы центра"
        ordering = ["-expense_date", "-created_at"]
        indexes = [
            models.Index(fields=["expense_date", "status"]),
            models.Index(fields=["category", "expense_date"]),
            models.Index(fields=["counterparty", "expense_date"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(total_amount__gt=0),
                name="center_expense_total_amount_positive",
            ),
            models.CheckConstraint(
                condition=~Q(status="paid") | Q(paid_at__isnull=False),
                name="center_expense_paid_requires_paid_at",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.expense_date:%d.%m.%Y} - {self.title}"

    def clean(self) -> None:
        errors: dict[str, str] = {}
        if self.total_amount is None or self.total_amount <= 0:
            errors["total_amount"] = "Сумма расхода должна быть положительной."
        if self.status == self.Status.PAID and self.paid_at is None:
            errors["paid_at"] = "Для оплаченного расхода нужно указать дату оплаты."
        if errors:
            raise ValidationError(errors)

    @property
    def funding_split_total(self) -> Decimal:
        if not self.pk:
            return Decimal("0")
        return self.funding_splits.aggregate(total=Sum("amount"))["total"] or Decimal("0")

    @property
    def unallocated_amount(self) -> Decimal:
        if self.total_amount is None:
            return Decimal("0")
        return self.total_amount - self.funding_split_total


class ExpenseFundingSplit(TimeStampedModel):
    expense = models.ForeignKey(
        CenterExpense,
        verbose_name="расход",
        on_delete=models.CASCADE,
        related_name="funding_splits",
    )
    funding_source = models.ForeignKey(
        FundingSource,
        verbose_name="источник финансирования",
        on_delete=models.PROTECT,
        related_name="expense_splits",
    )
    amount = models.DecimalField("сумма", max_digits=12, decimal_places=2)
    notes = models.TextField("примечания", blank=True)

    class Meta:
        verbose_name = "распределение расхода по источнику"
        verbose_name_plural = "распределение расходов по источникам"
        ordering = ["expense_id", "funding_source__name"]
        indexes = [
            models.Index(fields=["funding_source", "expense"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="expense_funding_split_amount_positive",
            ),
            models.UniqueConstraint(
                fields=["expense", "funding_source"],
                name="unique_expense_funding_source",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.expense}: {self.funding_source} - {self.amount}"

    def clean(self) -> None:
        if self.amount is None or self.amount <= 0:
            raise ValidationError({"amount": "Сумма распределения должна быть положительной."})


class EquipmentAsset(TimeStampedModel):
    class AssetType(models.TextChoices):
        THERAPY_EQUIPMENT = "therapy_equipment", "Реабилитационное оборудование"
        THERAPY_TOOL = "therapy_tool", "Инструменты для занятий"
        FURNITURE = "furniture", "Мебель"
        IT = "it", "ИТ и оргтехника"
        HOUSEHOLD = "household", "Хозяйственный инвентарь"
        OTHER = "other", "Прочее"

    class Status(models.TextChoices):
        ACTIVE = "active", "В работе"
        IN_REPAIR = "in_repair", "В ремонте"
        WRITTEN_OFF = "written_off", "Списан"
        LOST = "lost", "Утерян"
        ARCHIVED = "archived", "Архив"

    name = models.CharField("название", max_length=200)
    asset_type = models.CharField(
        "тип",
        max_length=40,
        choices=AssetType.choices,
        default=AssetType.OTHER,
    )
    inventory_number = models.CharField("инвентарный номер", max_length=80, blank=True)
    purchase_date = models.DateField("дата покупки", null=True, blank=True)
    purchase_expense = models.ForeignKey(
        CenterExpense,
        verbose_name="расход покупки",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="equipment_assets",
    )
    total_amount = models.DecimalField(
        "стоимость", max_digits=12, decimal_places=2, null=True, blank=True
    )
    status = models.CharField(
        "статус",
        max_length=30,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    location = models.CharField("местонахождение", max_length=200, blank=True)
    responsible_staff = models.ForeignKey(
        StaffMember,
        verbose_name="ответственный специалист",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="equipment_assets",
    )
    notes = models.TextField("примечания", blank=True)

    class Meta:
        verbose_name = "оборудование"
        verbose_name_plural = "оборудование"
        ordering = ["status", "asset_type", "name"]
        indexes = [
            models.Index(fields=["status", "asset_type"]),
            models.Index(fields=["purchase_expense"]),
            models.Index(fields=["responsible_staff", "status"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(total_amount__isnull=True) | Q(total_amount__gt=0),
                name="equipment_asset_total_amount_positive_or_null",
            ),
            models.UniqueConstraint(
                fields=["inventory_number"],
                condition=~Q(inventory_number=""),
                name="unique_non_empty_equipment_inventory_number",
            ),
        ]

    def __str__(self) -> str:
        if self.inventory_number:
            return f"{self.name} ({self.inventory_number})"
        return self.name

    def clean(self) -> None:
        errors: dict[str, str] = {}
        if self.total_amount is not None and self.total_amount <= 0:
            errors["total_amount"] = "Стоимость должна быть положительной."
        if (
            self.purchase_expense_id
            and self.purchase_expense.category.expense_type
            != CenterExpenseCategory.ExpenseType.EQUIPMENT
        ):
            errors["purchase_expense"] = (
                "Расход покупки должен относиться к категории оборудования."
            )
        if errors:
            raise ValidationError(errors)


class ContractTemplate(TimeStampedModel):
    class TemplateType(models.TextChoices):
        RECIPIENT_SERVICE = "recipient_service", "Договор с получателем услуг"
        RECIPIENT_FREE_SERVICE = (
            "recipient_free_service",
            "Безвозмездные услуги получателю",
        )
        RECIPIENT_CARE = "recipient_care", "Присмотр и уход"
        RECIPIENT_CERTIFICATE = "recipient_certificate", "Материнский капитал / сертификат"
        DONATION_ONE_TIME = "donation_one_time", "Разовое пожертвование"
        DONATION_MONTHLY = "donation_monthly", "Регулярная помощь"
        DONATION_PROJECT = "donation_project", "Проектное пожертвование"
        SPONSOR = "sponsor", "Спонсорский договор"
        ORGANIZATION_SERVICE = "organization_service", "B2B-договор услуг организации"
        VENDOR = "vendor", "Договор с поставщиком"
        CONSENT_PHOTO_VIDEO = "consent_photo_video", "Согласие на фото/видео"
        ACT = "act", "Акт"
        OTHER = "other", "Прочее"

    template_type = models.CharField(
        "тип шаблона",
        max_length=40,
        choices=TemplateType.choices,
        default=TemplateType.RECIPIENT_SERVICE,
    )
    title = models.CharField("название", max_length=200)
    version = models.CharField("версия", max_length=40, blank=True)
    file = models.FileField(
        "файл шаблона",
        upload_to=contract_template_upload_path,
        blank=True,
    )
    is_active = models.BooleanField("активен", default=True)
    notes = models.TextField("примечания", blank=True)

    class Meta:
        verbose_name = "шаблон договора"
        verbose_name_plural = "шаблоны договоров"
        ordering = ["template_type", "-is_active", "title", "version"]
        indexes = [
            models.Index(fields=["template_type", "is_active"]),
        ]

    def __str__(self) -> str:
        if self.version:
            return f"{self.title} v{self.version}"
        return self.title

    @classmethod
    def service_contract_template_types(cls) -> set[str]:
        return {
            cls.TemplateType.RECIPIENT_SERVICE,
            cls.TemplateType.RECIPIENT_FREE_SERVICE,
            cls.TemplateType.RECIPIENT_CARE,
            cls.TemplateType.RECIPIENT_CERTIFICATE,
            cls.TemplateType.OTHER,
        }

    @classmethod
    def donation_contract_template_types(cls) -> set[str]:
        return {
            cls.TemplateType.DONATION_ONE_TIME,
            cls.TemplateType.DONATION_MONTHLY,
            cls.TemplateType.DONATION_PROJECT,
            cls.TemplateType.SPONSOR,
            cls.TemplateType.OTHER,
        }

    @classmethod
    def organization_service_contract_template_types(cls) -> set[str]:
        return {
            cls.TemplateType.ORGANIZATION_SERVICE,
            cls.TemplateType.OTHER,
        }

    @classmethod
    def consent_template_types(cls) -> set[str]:
        return {
            cls.TemplateType.CONSENT_PHOTO_VIDEO,
            cls.TemplateType.OTHER,
        }

    @classmethod
    def act_template_types(cls) -> set[str]:
        return {
            cls.TemplateType.ACT,
            cls.TemplateType.OTHER,
        }


class DonationContract(TimeStampedModel):
    class ContractType(models.TextChoices):
        ONE_TIME = "one_time", "Разовое пожертвование"
        MONTHLY = "monthly", "Регулярная помощь"
        PROJECT = "project", "Проектный договор"
        OTHER = "other", "Прочее"

    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        ACTIVE = "active", "Активен"
        CLOSED = "closed", "Закрыт"
        CANCELLED = "cancelled", "Отменен"

    counterparty = models.ForeignKey(
        Counterparty,
        verbose_name="контрагент",
        on_delete=models.PROTECT,
        related_name="donation_contracts",
    )
    funding_source = models.ForeignKey(
        FundingSource,
        verbose_name="источник финансирования",
        on_delete=models.PROTECT,
        related_name="donation_contracts",
    )
    contract_type = models.CharField(
        "тип договора",
        max_length=30,
        choices=ContractType.choices,
        default=ContractType.ONE_TIME,
    )
    number = models.CharField("номер", max_length=80, blank=True)
    signed_on = models.DateField("подписан", null=True, blank=True)
    valid_from = models.DateField("действует с", null=True, blank=True)
    valid_until = models.DateField("действует до", null=True, blank=True)
    amount_limit = models.DecimalField(
        "лимит суммы",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    status = models.CharField(
        "статус",
        max_length=30,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    template = models.ForeignKey(
        ContractTemplate,
        verbose_name="шаблон",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="donation_contracts",
    )
    document = models.ForeignKey(
        Document,
        verbose_name="файл договора",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="donation_contracts",
    )
    notes = models.TextField("примечания", blank=True)

    class Meta:
        verbose_name = "договор пожертвования"
        verbose_name_plural = "договоры пожертвования"
        ordering = ["-signed_on", "-created_at"]
        indexes = [
            models.Index(fields=["funding_source", "status", "valid_from", "valid_until"]),
            models.Index(fields=["counterparty", "status"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount_limit__isnull=True) | Q(amount_limit__gt=0),
                name="donation_contract_amount_limit_positive_or_null",
            ),
            models.CheckConstraint(
                condition=Q(valid_from__isnull=True)
                | Q(valid_until__isnull=True)
                | Q(valid_until__gte=models.F("valid_from")),
                name="donation_contract_dates_order",
            ),
            models.UniqueConstraint(
                fields=["contract_type", "number", "signed_on"],
                condition=~Q(number="") & Q(signed_on__isnull=False),
                name="unique_donation_contract_number_signed_on_per_type",
            ),
        ]

    def __str__(self) -> str:
        number = f" №{self.number}" if self.number else ""
        return f"{self.get_contract_type_display()}{number} — {self.counterparty}"

    def clean(self) -> None:
        errors: dict[str, str] = {}
        if self.amount_limit is not None and self.amount_limit <= 0:
            errors["amount_limit"] = "Лимит суммы должен быть положительным."
        if self.valid_from and self.valid_until and self.valid_until < self.valid_from:
            errors["valid_until"] = "Дата окончания не может быть раньше даты начала."
        if (
            self.template_id
            and self.template.template_type
            not in ContractTemplate.donation_contract_template_types()
        ):
            errors["template"] = "Выберите шаблон для пожертвования или спонсорского договора."
        if self.document_id and self.document.category != Document.Category.CONTRACT:
            errors["document"] = "Связанный документ должен иметь категорию договора."
        if self.document_id and self.document.target_type == Document.TargetType.RECIPIENT:
            errors["document"] = "Договор пожертвования нельзя связывать с документом получателя."
        if (
            self.document_id
            and self.document.target_type == Document.TargetType.COUNTERPARTY
            and self.counterparty_id
            and self.document.counterparty_id != self.counterparty_id
        ):
            errors["document"] = "Документ договора должен относиться к выбранному контрагенту."
        if errors:
            raise ValidationError(errors)


class ServiceContract(TimeStampedModel):
    class ContractType(models.TextChoices):
        STANDARD = "standard", "Стандартный договор услуг"
        GRANT = "grant", "Договор по гранту"
        CERTIFICATE = "certificate", "Договор по сертификату"
        OTHER = "other", "Прочее"

    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        ACTIVE = "active", "Активен"
        CLOSED = "closed", "Закрыт"
        CANCELLED = "cancelled", "Отменен"

    child = models.ForeignKey(
        Child,
        verbose_name="получатель",
        on_delete=models.PROTECT,
        related_name="service_contracts",
    )
    representative_link = models.ForeignKey(
        RecipientRepresentative,
        verbose_name="подписант",
        on_delete=models.PROTECT,
        related_name="service_contracts",
    )
    funding_source = models.ForeignKey(
        FundingSource,
        verbose_name="источник финансирования",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="service_contracts",
    )
    certificate = models.ForeignKey(
        "Certificate",
        verbose_name="сертификат",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="service_contracts",
    )
    contract_type = models.CharField(
        "тип договора",
        max_length=30,
        choices=ContractType.choices,
        default=ContractType.STANDARD,
    )
    number = models.CharField("номер", max_length=80, blank=True)
    signed_on = models.DateField("подписан", null=True, blank=True)
    valid_from = models.DateField("действует с", null=True, blank=True)
    valid_until = models.DateField("действует до", null=True, blank=True)
    status = models.CharField(
        "статус",
        max_length=30,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    template = models.ForeignKey(
        ContractTemplate,
        verbose_name="шаблон",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="service_contracts",
    )
    document = models.ForeignKey(
        Document,
        verbose_name="файл договора",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="service_contracts",
    )
    notes = models.TextField("примечания", blank=True)

    class Meta:
        verbose_name = "договор с получателем"
        verbose_name_plural = "договоры с получателями"
        ordering = ["-signed_on", "-created_at"]
        indexes = [
            models.Index(fields=["child", "status", "valid_from", "valid_until"]),
            models.Index(fields=["representative_link", "status"]),
            models.Index(fields=["funding_source", "status", "valid_from", "valid_until"]),
            models.Index(fields=["certificate", "status"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(valid_from__isnull=True)
                | Q(valid_until__isnull=True)
                | Q(valid_until__gte=models.F("valid_from")),
                name="service_contract_dates_order",
            ),
            models.UniqueConstraint(
                fields=["contract_type", "number", "signed_on"],
                condition=~Q(number="") & Q(signed_on__isnull=False),
                name="unique_service_contract_number_signed_on_per_type",
            ),
        ]

    def __str__(self) -> str:
        number = f" №{self.number}" if self.number else ""
        return f"{self.get_contract_type_display()}{number} — {self.child}"

    @property
    def service_lines_total_amount(self) -> Decimal:
        return sum((line.amount for line in self.service_lines.all()), Decimal("0"))

    @property
    def service_lines_summary(self) -> str:
        lines = list(self.service_lines.all())
        if not lines:
            return ""
        first = lines[0]
        suffix = f" + еще {len(lines) - 1}" if len(lines) > 1 else ""
        return f"{first.service_name or first.service.name}: {first.quantity:g} {first.get_unit_display()}{suffix}"

    def clean(self) -> None:
        errors: dict[str, str] = {}
        if self.valid_from and self.valid_until and self.valid_until < self.valid_from:
            errors["valid_until"] = "Дата окончания не может быть раньше даты начала."
        if self.representative_link_id:
            if self.child_id and self.representative_link.child_id != self.child_id:
                errors["representative_link"] = (
                    "Подписант должен относиться к выбранному получателю."
                )
            if not self.representative_link.signs_contract:
                errors["representative_link"] = (
                    "У представителя должен быть флажок подписанта договора."
                )
        if self.certificate_id and self.child_id and self.certificate.child_id != self.child_id:
            errors["certificate"] = "Сертификат должен относиться к выбранному получателю."
        if (
            self.template_id
            and self.template.template_type
            not in ContractTemplate.service_contract_template_types()
        ):
            errors["template"] = "Выберите шаблон договора с получателем услуг."
        if self.document_id:
            if self.document.category != Document.Category.CONTRACT:
                errors["document"] = "Связанный документ должен иметь категорию договора."
            if self.document.target_type != Document.TargetType.RECIPIENT:
                errors["document"] = (
                    "Документ договора с получателем должен быть документом получателя."
                )
            if self.child_id and self.document.child_id != self.child_id:
                errors["document"] = "Документ договора должен относиться к выбранному получателю."
        if errors:
            raise ValidationError(errors)


class ServiceContractLine(TimeStampedModel):
    class Unit(models.TextChoices):
        SESSION = "session", "занятие"
        HOUR = "hour", "час"
        COURSE = "course", "курс"
        MONTH = "month", "месяц"
        OTHER = "other", "другое"

    service_contract = models.ForeignKey(
        ServiceContract,
        verbose_name="договор",
        on_delete=models.CASCADE,
        related_name="service_lines",
    )
    service = models.ForeignKey(
        Service,
        verbose_name="услуга",
        on_delete=models.PROTECT,
        related_name="contract_lines",
    )
    service_name = models.CharField(
        "наименование услуги в договоре",
        max_length=240,
        blank=True,
        help_text="Если оставить пустым, будет использовано название услуги.",
    )
    quantity = models.DecimalField("количество", max_digits=10, decimal_places=2, default=1)
    unit = models.CharField(
        "единица",
        max_length=20,
        choices=Unit.choices,
        default=Unit.SESSION,
    )
    unit_price = models.DecimalField("цена за единицу", max_digits=12, decimal_places=2, default=0)
    starts_on = models.DateField("период с", null=True, blank=True)
    ends_on = models.DateField("период по", null=True, blank=True)
    sort_order = models.PositiveIntegerField("порядок", default=0)
    notes = models.TextField("примечания", blank=True)

    class Meta:
        verbose_name = "строка спецификации договора"
        verbose_name_plural = "строки спецификации договора"
        ordering = ["service_contract", "sort_order", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity__gt=0),
                name="service_contract_line_quantity_positive",
            ),
            models.CheckConstraint(
                condition=Q(unit_price__gte=0),
                name="service_contract_line_unit_price_non_negative",
            ),
            models.CheckConstraint(
                condition=Q(starts_on__isnull=True)
                | Q(ends_on__isnull=True)
                | Q(ends_on__gte=models.F("starts_on")),
                name="service_contract_line_dates_order",
            ),
        ]
        indexes = [
            models.Index(fields=["service_contract", "sort_order"]),
            models.Index(fields=["service", "unit"]),
        ]

    def __str__(self) -> str:
        return f"{self.service_contract}: {self.service_name or self.service.name}"

    @property
    def amount(self) -> Decimal:
        return (self.quantity * self.unit_price).quantize(Decimal("0.01"))

    def clean(self) -> None:
        errors: dict[str, str] = {}
        if self.quantity <= 0:
            errors["quantity"] = "Количество должно быть больше нуля."
        if self.unit_price < 0:
            errors["unit_price"] = "Цена не может быть отрицательной."
        if self.starts_on and self.ends_on and self.ends_on < self.starts_on:
            errors["ends_on"] = "Дата окончания не может быть раньше даты начала."
        if errors:
            raise ValidationError(errors)

    def save(self, *args: object, **kwargs: object) -> None:
        if not self.service_name and self.service_id:
            self.service_name = self.service.name
        self.full_clean()
        super().save(*args, **kwargs)


class OrganizationServiceContract(TimeStampedModel):
    class ContractType(models.TextChoices):
        STANDARD = "standard", "Договор оказания услуг организации"
        PROJECT = "project", "Проектный договор услуг"
        OTHER = "other", "Прочее"

    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        ACTIVE = "active", "Активен"
        CLOSED = "closed", "Закрыт"
        CANCELLED = "cancelled", "Отменен"

    counterparty = models.ForeignKey(
        Counterparty,
        verbose_name="организация",
        on_delete=models.PROTECT,
        related_name="organization_service_contracts",
    )
    funding_source = models.ForeignKey(
        FundingSource,
        verbose_name="источник финансирования",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="organization_service_contracts",
    )
    contract_type = models.CharField(
        "тип договора",
        max_length=30,
        choices=ContractType.choices,
        default=ContractType.STANDARD,
    )
    number = models.CharField("номер", max_length=80, blank=True)
    signed_on = models.DateField("подписан", null=True, blank=True)
    valid_from = models.DateField("действует с", null=True, blank=True)
    valid_until = models.DateField("действует до", null=True, blank=True)
    status = models.CharField(
        "статус",
        max_length=30,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    template = models.ForeignKey(
        ContractTemplate,
        verbose_name="шаблон",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="organization_service_contracts",
    )
    document = models.ForeignKey(
        Document,
        verbose_name="файл договора",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="organization_service_contracts",
    )
    notes = models.TextField("примечания", blank=True)

    class Meta:
        verbose_name = "B2B-договор оказания услуг"
        verbose_name_plural = "B2B-договоры оказания услуг"
        ordering = ["-signed_on", "-created_at"]
        indexes = [
            models.Index(fields=["counterparty", "status", "valid_from", "valid_until"]),
            models.Index(fields=["funding_source", "status", "valid_from", "valid_until"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(valid_from__isnull=True)
                | Q(valid_until__isnull=True)
                | Q(valid_until__gte=models.F("valid_from")),
                name="organization_service_contract_dates_order",
            ),
            models.UniqueConstraint(
                fields=["contract_type", "number", "signed_on"],
                condition=~Q(number="") & Q(signed_on__isnull=False),
                name="unique_organization_service_contract_number_signed_on_per_type",
            ),
        ]

    def __str__(self) -> str:
        number = f" №{self.number}" if self.number else ""
        return f"{self.get_contract_type_display()}{number} — {self.counterparty}"

    @property
    def service_lines_total_amount(self) -> Decimal:
        return sum((line.amount for line in self.service_lines.all()), Decimal("0"))

    @property
    def service_lines_summary(self) -> str:
        lines = list(self.service_lines.all())
        if not lines:
            return ""
        first = lines[0]
        suffix = f" + еще {len(lines) - 1}" if len(lines) > 1 else ""
        return f"{first.service_name or first.service.name}: {first.quantity:g} {first.get_unit_display()}{suffix}"

    def clean(self) -> None:
        errors: dict[str, str] = {}
        if self.valid_from and self.valid_until and self.valid_until < self.valid_from:
            errors["valid_until"] = "Дата окончания не может быть раньше даты начала."
        if (
            self.template_id
            and self.template.template_type
            not in ContractTemplate.organization_service_contract_template_types()
        ):
            errors["template"] = "Выберите B2B-шаблон договора услуг организации."
        if self.document_id:
            if self.document.category != Document.Category.CONTRACT:
                errors["document"] = "Связанный документ должен иметь категорию договора."
            if self.document.target_type == Document.TargetType.RECIPIENT:
                errors["document"] = "B2B-договор нельзя связывать с документом получателя."
            if (
                self.document.target_type == Document.TargetType.COUNTERPARTY
                and self.counterparty_id
                and self.document.counterparty_id != self.counterparty_id
            ):
                errors["document"] = "Документ договора должен относиться к выбранной организации."
        if errors:
            raise ValidationError(errors)


class OrganizationServiceContractLine(TimeStampedModel):
    class Unit(models.TextChoices):
        SESSION = "session", "занятие"
        HOUR = "hour", "час"
        COURSE = "course", "курс"
        MONTH = "month", "месяц"
        OTHER = "other", "другое"

    organization_contract = models.ForeignKey(
        OrganizationServiceContract,
        verbose_name="B2B-договор",
        on_delete=models.CASCADE,
        related_name="service_lines",
    )
    service = models.ForeignKey(
        Service,
        verbose_name="услуга",
        on_delete=models.PROTECT,
        related_name="organization_contract_lines",
    )
    service_name = models.CharField(
        "наименование услуги в договоре",
        max_length=240,
        blank=True,
        help_text="Если оставить пустым, будет использовано название услуги.",
    )
    quantity = models.DecimalField("количество", max_digits=10, decimal_places=2, default=1)
    unit = models.CharField(
        "единица",
        max_length=20,
        choices=Unit.choices,
        default=Unit.SESSION,
    )
    unit_price = models.DecimalField("цена за единицу", max_digits=12, decimal_places=2, default=0)
    starts_on = models.DateField("период с", null=True, blank=True)
    ends_on = models.DateField("период по", null=True, blank=True)
    sort_order = models.PositiveIntegerField("порядок", default=0)
    notes = models.TextField("примечания", blank=True)

    class Meta:
        verbose_name = "строка спецификации B2B-договора"
        verbose_name_plural = "строки спецификации B2B-договора"
        ordering = ["organization_contract", "sort_order", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity__gt=0),
                name="organization_contract_line_quantity_positive",
            ),
            models.CheckConstraint(
                condition=Q(unit_price__gte=0),
                name="organization_contract_line_unit_price_non_negative",
            ),
            models.CheckConstraint(
                condition=Q(starts_on__isnull=True)
                | Q(ends_on__isnull=True)
                | Q(ends_on__gte=models.F("starts_on")),
                name="organization_contract_line_dates_order",
            ),
        ]
        indexes = [
            models.Index(fields=["organization_contract", "sort_order"]),
            models.Index(fields=["service", "unit"]),
        ]

    def __str__(self) -> str:
        return f"{self.organization_contract}: {self.service_name or self.service.name}"

    @property
    def amount(self) -> Decimal:
        return (self.quantity * self.unit_price).quantize(Decimal("0.01"))

    def clean(self) -> None:
        errors: dict[str, str] = {}
        if self.quantity <= 0:
            errors["quantity"] = "Количество должно быть больше нуля."
        if self.unit_price < 0:
            errors["unit_price"] = "Цена не может быть отрицательной."
        if self.starts_on and self.ends_on and self.ends_on < self.starts_on:
            errors["ends_on"] = "Дата окончания не может быть раньше даты начала."
        if errors:
            raise ValidationError(errors)

    def save(self, *args: object, **kwargs: object) -> None:
        if not self.service_name and self.service_id:
            self.service_name = self.service.name
        self.full_clean()
        super().save(*args, **kwargs)


class ContractAct(TimeStampedModel):
    class ActKind(models.TextChoices):
        SERVICE = "service", "Акт к договору с получателем"
        ORGANIZATION_SERVICE = "organization_service", "Акт к B2B-договору услуг"

    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        ISSUED = "issued", "Сформирован"
        SIGNED = "signed", "Подписан"
        CANCELLED = "cancelled", "Отменен"

    act_kind = models.CharField(
        "тип акта",
        max_length=30,
        choices=ActKind.choices,
    )
    service_contract = models.ForeignKey(
        ServiceContract,
        verbose_name="договор с получателем",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="acts",
    )
    organization_contract = models.ForeignKey(
        OrganizationServiceContract,
        verbose_name="B2B-договор услуг организации",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="acts",
    )
    number = models.CharField("номер акта", max_length=80, blank=True)
    act_on = models.DateField("дата акта", null=True, blank=True)
    period_from = models.DateField("период с", null=True, blank=True)
    period_until = models.DateField("период по", null=True, blank=True)
    amount = models.DecimalField(
        "сумма акта",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    status = models.CharField(
        "статус",
        max_length=30,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    template = models.ForeignKey(
        ContractTemplate,
        verbose_name="шаблон",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contract_acts",
    )
    document = models.ForeignKey(
        Document,
        verbose_name="файл акта",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contract_acts",
    )
    act_snapshot = models.JSONField("данные акта", default=dict, blank=True)
    contract_snapshot = models.JSONField("данные договора", default=dict, blank=True)
    center_snapshot = models.JSONField("данные центра", default=dict, blank=True)
    recipient_snapshot = models.JSONField("данные получателя", default=dict, blank=True)
    representative_snapshot = models.JSONField("данные представителя", default=dict, blank=True)
    counterparty_snapshot = models.JSONField("данные контрагента", default=dict, blank=True)
    funding_source_snapshot = models.JSONField(
        "данные источника финансирования",
        default=dict,
        blank=True,
    )
    template_snapshot = models.JSONField("данные шаблона", default=dict, blank=True)
    notes = models.TextField("примечания", blank=True)

    class Meta:
        verbose_name = "акт оказанных услуг"
        verbose_name_plural = "акты оказанных услуг"
        ordering = ["-act_on", "-created_at"]
        indexes = [
            models.Index(fields=["act_kind", "status", "-act_on"]),
            models.Index(fields=["service_contract", "status", "-act_on"]),
            models.Index(fields=["organization_contract", "status", "-act_on"]),
            models.Index(fields=["template", "status"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(Q(amount__isnull=True) | Q(amount__gt=0)),
                name="contract_act_amount_positive_or_null",
            ),
            models.CheckConstraint(
                condition=(
                    Q(period_from__isnull=True)
                    | Q(period_until__isnull=True)
                    | Q(period_until__gte=models.F("period_from"))
                ),
                name="contract_act_period_dates_order",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        act_kind="service",
                        service_contract__isnull=False,
                        organization_contract__isnull=True,
                    )
                    | Q(
                        act_kind="organization_service",
                        service_contract__isnull=True,
                        organization_contract__isnull=False,
                    )
                ),
                name="contract_act_matches_contract_kind",
            ),
            models.UniqueConstraint(
                fields=["act_kind", "number", "act_on"],
                condition=~Q(number="") & Q(act_on__isnull=False),
                name="unique_contract_act_number_act_on_per_kind",
            ),
        ]

    def __str__(self) -> str:
        number = f" №{self.number}" if self.number else ""
        return f"{self.get_act_kind_display()}{number} — {self.contract_label}"

    @property
    def contract(self) -> ServiceContract | OrganizationServiceContract | None:
        return self.service_contract or self.organization_contract

    @property
    def contract_label(self) -> str:
        contract = self.contract
        return str(contract) if contract is not None else "договор не выбран"

    @property
    def target_label(self) -> str:
        if self.service_contract_id:
            return self.service_contract.child.full_name
        if self.organization_contract_id:
            return self.organization_contract.counterparty.name
        return "цель не выбрана"

    def clean(self) -> None:
        errors: dict[str, str] = {}
        if self.period_from and self.period_until and self.period_until < self.period_from:
            errors["period_until"] = "Дата окончания периода не может быть раньше даты начала."
        if self.amount is not None and self.amount <= 0:
            errors["amount"] = "Сумма акта должна быть положительной."
        if self.act_kind == self.ActKind.SERVICE:
            if not self.service_contract_id:
                errors["service_contract"] = "Выберите договор с получателем."
            if self.organization_contract_id:
                errors["organization_contract"] = (
                    "Для акта к договору с получателем B2B-договор должен быть пустым."
                )
        elif self.act_kind == self.ActKind.ORGANIZATION_SERVICE:
            if not self.organization_contract_id:
                errors["organization_contract"] = "Выберите B2B-договор услуг организации."
            if self.service_contract_id:
                errors["service_contract"] = (
                    "Для B2B-акта договор с получателем должен быть пустым."
                )
        else:
            errors["act_kind"] = "Выберите тип акта."
        if (
            self.template_id
            and self.template.template_type not in ContractTemplate.act_template_types()
        ):
            errors["template"] = "Выберите шаблон акта."
        if self.document_id:
            if self.document.category != Document.Category.ACT:
                errors["document"] = "Связанный документ должен иметь категорию акта."
            elif self.act_kind == self.ActKind.SERVICE and self.service_contract_id:
                if self.document.target_type != Document.TargetType.RECIPIENT:
                    errors["document"] = (
                        "Акт к договору с получателем должен быть документом получателя."
                    )
                elif self.document.child_id != self.service_contract.child_id:
                    errors["document"] = "Документ акта должен относиться к получателю договора."
            elif (
                self.act_kind == self.ActKind.ORGANIZATION_SERVICE and self.organization_contract_id
            ):
                if self.document.target_type == Document.TargetType.RECIPIENT:
                    errors["document"] = "B2B-акт нельзя связывать с документом получателя."
                elif (
                    self.document.target_type == Document.TargetType.COUNTERPARTY
                    and self.document.counterparty_id != self.organization_contract.counterparty_id
                ):
                    errors["document"] = "Документ акта должен относиться к организации договора."
        if errors:
            raise ValidationError(errors)


class ContractActSignedFile(TimeStampedModel):
    class Status(models.TextChoices):
        ACTIVE = "active", "Действует"
        VOID = "void", "Аннулирован"

    act = models.ForeignKey(
        ContractAct,
        verbose_name="акт",
        on_delete=models.PROTECT,
        related_name="signed_files",
    )
    source_document = models.ForeignKey(
        Document,
        verbose_name="исходный файл акта",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="contract_act_signed_files",
    )
    file = models.FileField(
        "архивный файл",
        upload_to=contract_act_signed_file_upload_path,
    )
    original_filename = models.CharField("исходное имя файла", max_length=255)
    content_type = models.CharField("тип содержимого", max_length=120, blank=True)
    file_size = models.PositiveBigIntegerField("размер файла", default=0)
    file_sha256 = models.CharField("SHA-256", max_length=64)
    signed_on = models.DateField("дата подписания", default=timezone.localdate)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="загрузил",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_contract_act_signed_files",
    )
    status = models.CharField(
        "статус",
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    void_reason = models.TextField("причина аннулирования", blank=True)
    act_snapshot = models.JSONField("данные акта", default=dict, blank=True)
    contract_snapshot = models.JSONField("данные договора", default=dict, blank=True)
    center_snapshot = models.JSONField("данные центра", default=dict, blank=True)
    recipient_snapshot = models.JSONField("данные получателя", default=dict, blank=True)
    representative_snapshot = models.JSONField("данные представителя", default=dict, blank=True)
    counterparty_snapshot = models.JSONField("данные контрагента", default=dict, blank=True)
    funding_source_snapshot = models.JSONField(
        "данные источника финансирования",
        default=dict,
        blank=True,
    )
    template_snapshot = models.JSONField("данные шаблона", default=dict, blank=True)
    note = models.TextField("комментарий", blank=True)

    immutable_fields = (
        "act_id",
        "source_document_id",
        "file",
        "original_filename",
        "content_type",
        "file_size",
        "file_sha256",
        "signed_on",
        "uploaded_by_id",
        "act_snapshot",
        "contract_snapshot",
        "center_snapshot",
        "recipient_snapshot",
        "representative_snapshot",
        "counterparty_snapshot",
        "funding_source_snapshot",
        "template_snapshot",
    )

    class Meta:
        verbose_name = "архивный подписанный файл акта"
        verbose_name_plural = "архивные подписанные файлы актов"
        ordering = ["-signed_on", "-created_at"]
        indexes = [
            models.Index(fields=["act", "status", "-signed_on"]),
            models.Index(fields=["status", "-signed_on"]),
            models.Index(fields=["file_sha256"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(file_size__gt=0),
                name="contract_act_signed_file_size_positive",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.act} — {self.signed_on:%d.%m.%Y}"

    def _immutable_value(self, field: str):
        value = getattr(self, field)
        if field == "file":
            return value.name
        return value

    def _ensure_immutable_fields(self) -> None:
        if not self.pk:
            return
        current = type(self).objects.get(pk=self.pk)
        for field in self.immutable_fields:
            if self._immutable_value(field) != current._immutable_value(field):
                raise ValidationError(
                    "Архивный подписанный файл акта нельзя изменять после создания. "
                    "Можно только аннулировать запись отдельным статусом."
                )

    def clean(self) -> None:
        errors: dict[str, str] = {}
        if self.source_document_id:
            if self.source_document.category != Document.Category.ACT:
                errors["source_document"] = "Исходный файл должен быть документом акта."
            if (
                self.act_id
                and self.act.act_kind == ContractAct.ActKind.SERVICE
                and self.act.service_contract_id
            ):
                service_contract = self.act.service_contract
                if self.source_document.target_type != Document.TargetType.RECIPIENT:
                    errors["source_document"] = (
                        "Подписанный акт к договору с получателем должен исходить "
                        "из документа получателя."
                    )
                elif self.source_document.child_id != service_contract.child_id:
                    errors["source_document"] = (
                        "Исходный файл акта должен относиться к получателю договора."
                    )
            if (
                self.act_id
                and self.act.act_kind == ContractAct.ActKind.ORGANIZATION_SERVICE
                and self.act.organization_contract_id
            ):
                organization_contract = self.act.organization_contract
                if self.source_document.target_type == Document.TargetType.RECIPIENT:
                    errors["source_document"] = (
                        "Подписанный B2B-акт нельзя создавать из документа получателя."
                    )
                elif (
                    self.source_document.target_type == Document.TargetType.COUNTERPARTY
                    and self.source_document.counterparty_id
                    != organization_contract.counterparty_id
                ):
                    errors["source_document"] = (
                        "Исходный файл акта должен относиться к организации договора."
                    )
        if self.file_sha256 and len(self.file_sha256) != 64:
            errors["file_sha256"] = "SHA-256 должен состоять из 64 символов."
        if self.status == self.Status.VOID and not self.void_reason:
            errors["void_reason"] = "Для аннулирования укажите причину."
        if errors:
            raise ValidationError(errors)

    def save(self, *args: object, **kwargs: object) -> None:
        self.full_clean()
        self._ensure_immutable_fields()
        super().save(*args, **kwargs)


class ContractLegalSnapshot(TimeStampedModel):
    class ContractKind(models.TextChoices):
        SERVICE = "service", "Договор с получателем"
        DONATION = "donation", "Договор пожертвования"
        ORGANIZATION_SERVICE = "organization_service", "B2B-договор услуг организации"

    contract_kind = models.CharField(
        "тип договора",
        max_length=30,
        choices=ContractKind.choices,
    )
    service_contract = models.ForeignKey(
        ServiceContract,
        verbose_name="договор с получателем",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="legal_snapshots",
    )
    donation_contract = models.ForeignKey(
        DonationContract,
        verbose_name="договор пожертвования",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="legal_snapshots",
    )
    organization_contract = models.ForeignKey(
        OrganizationServiceContract,
        verbose_name="B2B-договор услуг организации",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="legal_snapshots",
    )
    document = models.OneToOneField(
        Document,
        verbose_name="файл договора",
        on_delete=models.PROTECT,
        related_name="contract_legal_snapshot",
    )
    generated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="сформировал",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="generated_contract_legal_snapshots",
    )
    contract_snapshot = models.JSONField("данные договора", default=dict, blank=True)
    center_snapshot = models.JSONField("данные центра", default=dict, blank=True)
    recipient_snapshot = models.JSONField("данные получателя", default=dict, blank=True)
    representative_snapshot = models.JSONField("данные представителя", default=dict, blank=True)
    counterparty_snapshot = models.JSONField("данные контрагента", default=dict, blank=True)
    funding_source_snapshot = models.JSONField(
        "данные источника финансирования",
        default=dict,
        blank=True,
    )
    template_snapshot = models.JSONField("данные шаблона", default=dict, blank=True)
    note = models.TextField("комментарий", blank=True)

    class Meta:
        verbose_name = "юридический снимок договора"
        verbose_name_plural = "юридические снимки договоров"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["contract_kind", "-created_at"]),
            models.Index(fields=["service_contract", "-created_at"]),
            models.Index(fields=["donation_contract", "-created_at"]),
            models.Index(fields=["organization_contract", "-created_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(
                        contract_kind="service",
                        service_contract__isnull=False,
                        donation_contract__isnull=True,
                        organization_contract__isnull=True,
                    )
                    | Q(
                        contract_kind="donation",
                        service_contract__isnull=True,
                        donation_contract__isnull=False,
                        organization_contract__isnull=True,
                    )
                    | Q(
                        contract_kind="organization_service",
                        service_contract__isnull=True,
                        donation_contract__isnull=True,
                        organization_contract__isnull=False,
                    )
                ),
                name="contract_snapshot_matches_contract_kind",
            ),
        ]

    def __str__(self) -> str:
        contract = self.service_contract or self.donation_contract or self.organization_contract
        return f"{self.get_contract_kind_display()} — {contract}"

    def clean(self) -> None:
        errors: dict[str, str] = {}
        if self.contract_kind == self.ContractKind.SERVICE:
            if not self.service_contract_id:
                errors["service_contract"] = "Выберите договор с получателем."
            if self.donation_contract_id:
                errors["donation_contract"] = (
                    "Для договора с получателем договор пожертвования должен быть пустым."
                )
            if self.organization_contract_id:
                errors["organization_contract"] = (
                    "Для договора с получателем B2B-договор должен быть пустым."
                )
        elif self.contract_kind == self.ContractKind.DONATION:
            if not self.donation_contract_id:
                errors["donation_contract"] = "Выберите договор пожертвования."
            if self.service_contract_id:
                errors["service_contract"] = (
                    "Для договора пожертвования договор с получателем должен быть пустым."
                )
            if self.organization_contract_id:
                errors["organization_contract"] = (
                    "Для договора пожертвования B2B-договор должен быть пустым."
                )
        elif self.contract_kind == self.ContractKind.ORGANIZATION_SERVICE:
            if not self.organization_contract_id:
                errors["organization_contract"] = "Выберите B2B-договор услуг организации."
            if self.service_contract_id:
                errors["service_contract"] = (
                    "Для B2B-договора договор с получателем должен быть пустым."
                )
            if self.donation_contract_id:
                errors["donation_contract"] = (
                    "Для B2B-договора договор пожертвования должен быть пустым."
                )
        if self.document_id:
            if self.document.category != Document.Category.CONTRACT:
                errors["document"] = (
                    "Snapshot можно связать только с документом категории договора."
                )
            if self.contract_kind == self.ContractKind.SERVICE and self.service_contract_id:
                if self.document.target_type != Document.TargetType.RECIPIENT:
                    errors["document"] = (
                        "Snapshot договора с получателем должен ссылаться на документ получателя."
                    )
                elif self.document.child_id != self.service_contract.child_id:
                    errors["document"] = (
                        "Документ snapshot должен относиться к получателю договора."
                    )
            if self.contract_kind == self.ContractKind.DONATION and self.donation_contract_id:
                if self.document.target_type == Document.TargetType.RECIPIENT:
                    errors["document"] = (
                        "Snapshot договора пожертвования нельзя ссылать на документ получателя."
                    )
                elif (
                    self.document.target_type == Document.TargetType.COUNTERPARTY
                    and self.document.counterparty_id != self.donation_contract.counterparty_id
                ):
                    errors["document"] = (
                        "Документ snapshot должен относиться к контрагенту договора."
                    )
            if (
                self.contract_kind == self.ContractKind.ORGANIZATION_SERVICE
                and self.organization_contract_id
            ):
                if self.document.target_type == Document.TargetType.RECIPIENT:
                    errors["document"] = (
                        "Snapshot B2B-договора нельзя ссылать на документ получателя."
                    )
                elif (
                    self.document.target_type == Document.TargetType.COUNTERPARTY
                    and self.document.counterparty_id != self.organization_contract.counterparty_id
                ):
                    errors["document"] = (
                        "Документ snapshot должен относиться к организации договора."
                    )
        if errors:
            raise ValidationError(errors)


class ContractSignedFile(TimeStampedModel):
    class ContractKind(models.TextChoices):
        SERVICE = "service", "Договор с получателем"
        DONATION = "donation", "Договор пожертвования"
        ORGANIZATION_SERVICE = "organization_service", "B2B-договор услуг организации"

    class Status(models.TextChoices):
        ACTIVE = "active", "Действует"
        VOID = "void", "Аннулирован"

    contract_kind = models.CharField(
        "тип договора",
        max_length=30,
        choices=ContractKind.choices,
    )
    service_contract = models.ForeignKey(
        ServiceContract,
        verbose_name="договор с получателем",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="signed_files",
    )
    donation_contract = models.ForeignKey(
        DonationContract,
        verbose_name="договор пожертвования",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="signed_files",
    )
    organization_contract = models.ForeignKey(
        OrganizationServiceContract,
        verbose_name="B2B-договор услуг организации",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="signed_files",
    )
    source_document = models.ForeignKey(
        Document,
        verbose_name="исходный файл договора",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="contract_signed_files",
    )
    file = models.FileField(
        "архивный файл",
        upload_to=contract_signed_file_upload_path,
    )
    original_filename = models.CharField("исходное имя файла", max_length=255)
    content_type = models.CharField("тип содержимого", max_length=120, blank=True)
    file_size = models.PositiveBigIntegerField("размер файла", default=0)
    file_sha256 = models.CharField("SHA-256", max_length=64)
    signed_on = models.DateField("дата подписания", default=timezone.localdate)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="загрузил",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_contract_signed_files",
    )
    status = models.CharField(
        "статус",
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    void_reason = models.TextField("причина аннулирования", blank=True)
    contract_snapshot = models.JSONField("данные договора", default=dict, blank=True)
    center_snapshot = models.JSONField("данные центра", default=dict, blank=True)
    recipient_snapshot = models.JSONField("данные получателя", default=dict, blank=True)
    representative_snapshot = models.JSONField("данные представителя", default=dict, blank=True)
    counterparty_snapshot = models.JSONField("данные контрагента", default=dict, blank=True)
    funding_source_snapshot = models.JSONField(
        "данные источника финансирования",
        default=dict,
        blank=True,
    )
    template_snapshot = models.JSONField("данные шаблона", default=dict, blank=True)
    note = models.TextField("комментарий", blank=True)

    immutable_fields = (
        "contract_kind",
        "service_contract_id",
        "donation_contract_id",
        "organization_contract_id",
        "source_document_id",
        "file",
        "original_filename",
        "content_type",
        "file_size",
        "file_sha256",
        "signed_on",
        "uploaded_by_id",
        "contract_snapshot",
        "center_snapshot",
        "recipient_snapshot",
        "representative_snapshot",
        "counterparty_snapshot",
        "funding_source_snapshot",
        "template_snapshot",
    )

    class Meta:
        verbose_name = "архивный подписанный файл договора"
        verbose_name_plural = "архивные подписанные файлы договоров"
        ordering = ["-signed_on", "-created_at"]
        indexes = [
            models.Index(fields=["contract_kind", "status", "-signed_on"]),
            models.Index(fields=["service_contract", "status", "-signed_on"]),
            models.Index(fields=["donation_contract", "status", "-signed_on"]),
            models.Index(fields=["organization_contract", "status", "-signed_on"]),
            models.Index(fields=["file_sha256"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(
                        contract_kind="service",
                        service_contract__isnull=False,
                        donation_contract__isnull=True,
                        organization_contract__isnull=True,
                    )
                    | Q(
                        contract_kind="donation",
                        service_contract__isnull=True,
                        donation_contract__isnull=False,
                        organization_contract__isnull=True,
                    )
                    | Q(
                        contract_kind="organization_service",
                        service_contract__isnull=True,
                        donation_contract__isnull=True,
                        organization_contract__isnull=False,
                    )
                ),
                name="contract_signed_file_matches_contract_kind",
            ),
            models.CheckConstraint(
                condition=Q(file_size__gt=0),
                name="contract_signed_file_size_positive",
            ),
        ]

    def __str__(self) -> str:
        contract = self.service_contract or self.donation_contract or self.organization_contract
        return f"{self.get_contract_kind_display()} — {contract} — {self.signed_on:%d.%m.%Y}"

    @property
    def contract(self) -> ServiceContract | DonationContract | OrganizationServiceContract | None:
        return self.service_contract or self.donation_contract or self.organization_contract

    def _immutable_value(self, field: str):
        value = getattr(self, field)
        if field == "file":
            return value.name
        return value

    def _ensure_immutable_fields(self) -> None:
        if not self.pk:
            return
        current = type(self).objects.get(pk=self.pk)
        for field in self.immutable_fields:
            if self._immutable_value(field) != current._immutable_value(field):
                raise ValidationError(
                    "Архивный подписанный файл нельзя изменять после создания. "
                    "Можно только аннулировать запись отдельным статусом."
                )

    def clean(self) -> None:
        errors: dict[str, str] = {}
        if self.contract_kind == self.ContractKind.SERVICE:
            if not self.service_contract_id:
                errors["service_contract"] = "Выберите договор с получателем."
            if self.donation_contract_id:
                errors["donation_contract"] = (
                    "Для договора с получателем договор пожертвования должен быть пустым."
                )
            if self.organization_contract_id:
                errors["organization_contract"] = (
                    "Для договора с получателем B2B-договор должен быть пустым."
                )
        elif self.contract_kind == self.ContractKind.DONATION:
            if not self.donation_contract_id:
                errors["donation_contract"] = "Выберите договор пожертвования."
            if self.service_contract_id:
                errors["service_contract"] = (
                    "Для договора пожертвования договор с получателем должен быть пустым."
                )
            if self.organization_contract_id:
                errors["organization_contract"] = (
                    "Для договора пожертвования B2B-договор должен быть пустым."
                )
        elif self.contract_kind == self.ContractKind.ORGANIZATION_SERVICE:
            if not self.organization_contract_id:
                errors["organization_contract"] = "Выберите B2B-договор услуг организации."
            if self.service_contract_id:
                errors["service_contract"] = (
                    "Для B2B-договора договор с получателем должен быть пустым."
                )
            if self.donation_contract_id:
                errors["donation_contract"] = (
                    "Для B2B-договора договор пожертвования должен быть пустым."
                )
        if self.source_document_id:
            if self.source_document.category != Document.Category.CONTRACT:
                errors["source_document"] = "Исходный файл должен быть документом договора."
            if self.contract_kind == self.ContractKind.SERVICE and self.service_contract_id:
                if self.source_document.target_type != Document.TargetType.RECIPIENT:
                    errors["source_document"] = (
                        "Подписанный файл договора с получателем должен исходить из документа получателя."
                    )
                elif self.source_document.child_id != self.service_contract.child_id:
                    errors["source_document"] = (
                        "Исходный файл должен относиться к получателю договора."
                    )
            if self.contract_kind == self.ContractKind.DONATION and self.donation_contract_id:
                if self.source_document.target_type == Document.TargetType.RECIPIENT:
                    errors["source_document"] = (
                        "Подписанный файл пожертвования нельзя создавать из документа получателя."
                    )
                elif (
                    self.source_document.target_type == Document.TargetType.COUNTERPARTY
                    and self.source_document.counterparty_id
                    != self.donation_contract.counterparty_id
                ):
                    errors["source_document"] = (
                        "Исходный файл должен относиться к контрагенту договора."
                    )
            if (
                self.contract_kind == self.ContractKind.ORGANIZATION_SERVICE
                and self.organization_contract_id
            ):
                if self.source_document.target_type == Document.TargetType.RECIPIENT:
                    errors["source_document"] = (
                        "Подписанный файл B2B-договора нельзя создавать из документа получателя."
                    )
                elif (
                    self.source_document.target_type == Document.TargetType.COUNTERPARTY
                    and self.source_document.counterparty_id
                    != self.organization_contract.counterparty_id
                ):
                    errors["source_document"] = (
                        "Исходный файл должен относиться к организации договора."
                    )
        if self.file_sha256 and len(self.file_sha256) != 64:
            errors["file_sha256"] = "SHA-256 должен состоять из 64 символов."
        if self.status == self.Status.VOID and not self.void_reason:
            errors["void_reason"] = "Для аннулирования укажите причину."
        if errors:
            raise ValidationError(errors)

    def save(self, *args: object, **kwargs: object) -> None:
        self.full_clean()
        self._ensure_immutable_fields()
        super().save(*args, **kwargs)


class Discount(TimeStampedModel):
    child = models.ForeignKey(
        Child, verbose_name="получатель", on_delete=models.CASCADE, related_name="discounts"
    )
    service = models.ForeignKey(
        Service,
        verbose_name="услуга",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="discounts",
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
    funding_source = models.ForeignKey(
        FundingSource,
        verbose_name="источник финансирования",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="certificates",
    )
    payer_representative = models.ForeignKey(
        RecipientRepresentative,
        verbose_name="представитель-плательщик",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="payer_certificates",
    )
    payer_name = models.CharField("плательщик по сертификату", max_length=255, blank=True)
    certificate_type = models.CharField("тип", max_length=30, choices=CertificateType.choices)
    number = models.CharField("номер", max_length=100, blank=True)
    total_amount = models.DecimalField("полная сумма", max_digits=12, decimal_places=2)
    remaining_amount = models.DecimalField("остаток", max_digits=12, decimal_places=2)
    balance_account = models.OneToOneField(
        BalanceAccount,
        verbose_name="счет баланса",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="certificate",
    )
    valid_from = models.DateField("действует с", null=True, blank=True)
    valid_until = models.DateField("действует до", null=True, blank=True)
    note = models.TextField("комментарий", blank=True)

    class Meta:
        verbose_name = "сертификат"
        verbose_name_plural = "сертификаты"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["child", "certificate_type"]),
            models.Index(fields=["funding_source", "certificate_type"]),
            models.Index(fields=["payer_representative"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_certificate_type_display()} №{self.number} — {self.child}"

    @property
    def effective_remaining_amount(self) -> Decimal:
        if self.balance_account_id:
            return self.balance_account.current_balance
        return self.remaining_amount

    @property
    def is_available(self) -> bool:
        return self.effective_remaining_amount > 0

    @property
    def payer_display_name(self) -> str:
        if self.payer_name.strip():
            return self.payer_name.strip()
        if self.payer_representative_id:
            return self.payer_representative.representative.full_name
        if self.funding_source_id:
            return self.funding_source.name
        return ""

    @property
    def payer_relationship_display(self) -> str:
        if not self.payer_representative_id:
            return ""
        return self.payer_representative.get_relationship_type_display()

    def clean(self) -> None:
        errors: dict[str, str] = {}
        if (
            self.payer_representative_id
            and self.child_id
            and self.payer_representative.child_id != self.child_id
        ):
            errors["payer_representative"] = (
                "Представитель-плательщик должен относиться к выбранному получателю."
            )
        if self.total_amount is not None and self.total_amount < 0:
            errors["total_amount"] = "Полная сумма не может быть отрицательной."
        if self.remaining_amount is not None and self.remaining_amount < 0:
            errors["remaining_amount"] = "Остаток не может быть отрицательным."
        if (
            self.total_amount is not None
            and self.remaining_amount is not None
            and self.remaining_amount > self.total_amount
        ):
            errors["remaining_amount"] = "Остаток не может быть больше полной суммы."
        if self.valid_from and self.valid_until and self.valid_until < self.valid_from:
            errors["valid_until"] = "Дата окончания не может быть раньше даты начала."
        if self.balance_account_id:
            if self.child_id and self.balance_account.child_id != self.child_id:
                errors["balance_account"] = "Счет должен принадлежать получателю сертификата."
            elif self.balance_account.unit != BalanceAccount.Unit.MONEY:
                errors["balance_account"] = "Сертификат можно связать только со счетом в рублях."
            elif (
                self.funding_source_id
                and self.balance_account.funding_source_id != self.funding_source_id
            ):
                errors["balance_account"] = (
                    "Счет должен относиться к тому же источнику финансирования."
                )
        if errors:
            raise ValidationError(errors)


class ImportBatch(TimeStampedModel):
    class ImportKind(models.TextChoices):
        CERTIFICATES = "certificates", "Сертификаты"

    class Status(models.TextChoices):
        PREVIEWED = "previewed", "Проверен"
        APPLYING = "applying", "Применяется"
        APPLIED = "applied", "Применен"
        PARTIALLY_APPLIED = "partially_applied", "Применен частично"
        FAILED = "failed", "Ошибка"
        CANCELLED = "cancelled", "Отменен"

    import_kind = models.CharField("тип импорта", max_length=40, choices=ImportKind.choices)
    status = models.CharField(
        "статус",
        max_length=40,
        choices=Status.choices,
        default=Status.PREVIEWED,
    )
    original_filename = models.CharField("исходное имя файла", max_length=255)
    source_sha256 = models.CharField("SHA-256 файла", max_length=64)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="загрузил",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="import_batches",
    )
    applied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="применил",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="applied_import_batches",
    )
    applied_at = models.DateTimeField("применено", null=True, blank=True)
    total_rows = models.PositiveIntegerField("строк всего", default=0)
    valid_rows = models.PositiveIntegerField("валидных строк", default=0)
    invalid_rows = models.PositiveIntegerField("ошибочных строк", default=0)
    warning_rows = models.PositiveIntegerField("строк с предупреждениями", default=0)
    applied_rows = models.PositiveIntegerField("примененных строк", default=0)
    skipped_rows = models.PositiveIntegerField("пропущенных строк", default=0)
    header_snapshot = models.JSONField("snapshot заголовков", default=dict, blank=True)
    error_summary = models.JSONField("сводка ошибок", default=dict, blank=True)
    note = models.TextField("примечание", blank=True)

    class Meta:
        verbose_name = "пакет импорта"
        verbose_name_plural = "пакеты импорта"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["import_kind", "status", "created_at"]),
            models.Index(fields=["uploaded_by", "created_at"]),
            models.Index(fields=["source_sha256"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_import_kind_display()} #{self.pk or 'new'} — {self.original_filename}"


class ImportBatchRow(TimeStampedModel):
    class Status(models.TextChoices):
        VALID = "valid", "Готова"
        INVALID = "invalid", "Ошибка"
        APPLIED = "applied", "Применена"
        SKIPPED = "skipped", "Пропущена"
        FAILED = "failed", "Сбой"

    batch = models.ForeignKey(
        ImportBatch,
        verbose_name="пакет импорта",
        on_delete=models.CASCADE,
        related_name="rows",
    )
    row_number = models.PositiveIntegerField("номер строки")
    status = models.CharField("статус", max_length=30, choices=Status.choices)
    raw_values = models.JSONField("значения строки", default=dict, blank=True)
    normalized_values = models.JSONField("нормализованные значения", default=dict, blank=True)
    errors = models.JSONField("ошибки", default=list, blank=True)
    warnings = models.JSONField("предупреждения", default=list, blank=True)
    target_model = models.CharField("целевая модель", max_length=120, blank=True)
    target_pk = models.PositiveBigIntegerField("ID целевой записи", null=True, blank=True)
    applied_at = models.DateTimeField("применено", null=True, blank=True)

    class Meta:
        verbose_name = "строка пакета импорта"
        verbose_name_plural = "строки пакетов импорта"
        ordering = ["batch", "row_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "row_number"],
                name="unique_import_batch_row_number",
            ),
        ]
        indexes = [
            models.Index(fields=["batch", "status"]),
            models.Index(fields=["target_model", "target_pk"]),
        ]

    def __str__(self) -> str:
        return f"{self.batch} / строка {self.row_number}"
